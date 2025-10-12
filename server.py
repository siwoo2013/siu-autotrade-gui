# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict

import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from bitget_client import BitgetClient

# ---------- env ----------
BITGET_API_KEY = os.environ.get("BITGET_API_KEY", "")
BITGET_API_SECRET = os.environ.get("BITGET_API_SECRET", "")
BITGET_PASSPHRASE = os.environ.get("BITGET_PASSPHRASE", "")
TRADE_MODE = os.environ.get("TRADE_MODE", "live")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

# ---------- app & logger ----------
logger = logging.getLogger("uvicorn.error")
logger.setLevel(logging.INFO)

app = FastAPI()

# ---------- client ----------
bg = BitgetClient(
    api_key=BITGET_API_KEY,
    api_secret=BITGET_API_SECRET,
    passphrase=BITGET_PASSPHRASE,
    product_type="umcbl",
    margin_coin="USDT",
    logger=logger,
)

# ---------- symbol normalize / lock ----------
def normalize_symbol(sym: str) -> str:
    if not sym:
        return "BTCUSDT_UMCBL"
    s = sym.strip().upper()
    if s.endswith("_UMCBL"):
        return s
    if s.endswith(".P"):
        s = s[:-2]
    if s.endswith("USDT"):
        return s + "_UMCBL"
    return s

_symbol_locks: dict[str, asyncio.Lock] = {}

def symbol_lock(symbol: str) -> asyncio.Lock:
    if symbol not in _symbol_locks:
        _symbol_locks[symbol] = asyncio.Lock()
    return _symbol_locks[symbol]

# ---------- helpers ----------
async def sleep_ms(ms: int):  # 짧은 대기
    await asyncio.sleep(ms / 1000.0)

async def ensure_close_full(symbol: str, side_to_close: str, *, max_retry: int = 3) -> Dict[str, Any]:
    """
    side_to_close: "LONG" or "SHORT"
    - 현재 반대 레그의 '보유 수량'을 조회 후 '전량 청산'을 재시도하며 확인한다.
    - 네트워크/일시 에러에 대해 재시도.
    - 성공적으로 0이 되면 {"ok": True, "closed": ...} 반환, 아니면 {"ok": False, ...}
    """
    last_detail: Any = None
    for attempt in range(1, max_retry + 1):
        sizes = bg.get_hedge_sizes(symbol)
        long_sz = float(sizes["long"])
        short_sz = float(sizes["short"])
        logger.info("ensure_close_full #%s | %s sizes(long=%.6f, short=%.6f)", attempt, symbol, long_sz, short_sz)

        try:
            if side_to_close == "LONG" and long_sz > 0:
                bg.close_long(symbol, size=str(long_sz))
            elif side_to_close == "SHORT" and short_sz > 0:
                bg.close_short(symbol, size=str(short_sz))
            else:
                # 이미 닫혀 있음
                return {"ok": True, "closed": {"skipped": True, "reason": "already_zero"}}
        except requests.RequestException as e:
            # 네트워크/HTTP 에러: detail 저장 후 재시도
            try:
                last_detail = e.response.json()  # type: ignore[assignment]
            except Exception:
                last_detail = {"raw": getattr(e.response, "text", "") if getattr(e, "response", None) else str(e)}
            logger.info("close attempt error (will retry): %s", last_detail)

        # 체결 반영 대기 후 재확인
        await sleep_ms(300)
        sizes2 = bg.get_hedge_sizes(symbol)
        long2 = float(sizes2["long"])
        short2 = float(sizes2["short"])
        if side_to_close == "LONG" and long2 <= 0:
            return {"ok": True, "closed": {"size_before": long_sz, "size_after": long2}}
        if side_to_close == "SHORT" and short2 <= 0:
            return {"ok": True, "closed": {"size_before": short_sz, "size_after": short2}}

    return {"ok": False, "error": "close_not_flat", "detail": last_detail}

# ---------- routes ----------

@app.get("/")
def health() -> Dict[str, Any]:
    return {"ok": True, "service": "siu-autotrade-gui", "mode": TRADE_MODE}

@app.post("/tv")
async def tv(request: Request):
    # 0) 입력 파싱
    try:
        payload = await request.json()
    except Exception:
        body = await request.body()
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            return JSONResponse({"ok": False, "error": "bad-json"}, status_code=400)

    # 1) 인증
    if str(payload.get("secret")) != str(WEBHOOK_SECRET):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    # 2) 필드
    route = str(payload.get("route", "")).strip()
    exchange = str(payload.get("exchange", "bitget")).lower()
    raw_symbol = str(payload.get("symbol", "BTCUSDT_UMCBL")).strip()
    symbol = normalize_symbol(raw_symbol)
    target_side = str(payload.get("target_side", "")).upper()  # BUY / SELL
    order_type = str(payload.get("type", "MARKET")).lower()
    size = str(payload.get("size", "0.001"))

    if exchange != "bitget":
        return JSONResponse({"ok": False, "error": "unsupported-exchange"}, status_code=400)

    logger.info("[LIVE] [TV] 수신 | raw_symbol=%s -> %s | %s | %s | size=%s",
                raw_symbol, symbol, route, target_side, size)

    # 심볼별 직렬화(동시 신호로 인한 레이스 방지)
    async with symbol_lock(symbol):

        # 단순 오픈
        if route == "order.open":
            res = bg.open_long(symbol, size, order_type) if target_side == "BUY" \
                else bg.open_short(symbol, size, order_type)
            return {"ok": True, "result": res}

        # 안전한 리버스
        if route == "order.reverse":
            try:
                # 1) 반대 레그 전량 청산 확인
                if target_side == "BUY":
                    close_res = await ensure_close_full(symbol, "SHORT")  # 숏 전량 닫기
                    if not close_res.get("ok"):
                        logger.error("reverse abort: could not close SHORT: %s", close_res)
                        return JSONResponse({"ok": False, "error": "close-failed", "detail": close_res}, status_code=500)
                    # 2) 신규 진입
                    open_res = bg.open_long(symbol, size, order_type)
                elif target_side == "SELL":
                    close_res = await ensure_close_full(symbol, "LONG")   # 롱 전량 닫기
                    if not close_res.get("ok"):
                        logger.error("reverse abort: could not close LONG: %s", close_res)
                        return JSONResponse({"ok": False, "error": "close-failed", "detail": close_res}, status_code=500)
                    open_res = bg.open_short(symbol, size, order_type)
                else:
                    return JSONResponse({"ok": False, "error": "bad-target-side"}, status_code=400)

                return {"ok": True, "closed": close_res, "opened": open_res}

            except requests.HTTPError as e:
                try:
                    detail = e.response.json()
                except Exception:
                    detail = {"raw": getattr(e.response, "text", "")}
                logger.error("HTTPError during reverse: %s", detail)
                return JSONResponse(
                    {"ok": False, "error": "bitget-http", "status": getattr(e.response, "status_code", None), "detail": detail},
                    status_code=500,
                )
            except Exception as e:  # noqa: BLE001
                logger.exception("Exception in /tv reverse: %r", e)
                return JSONResponse({"ok": False, "error": "exception", "detail": str(e)}, status_code=500)

        return JSONResponse({"ok": False, "error": "unsupported-route"}, status_code=400)
