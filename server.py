# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, Set

import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from bitget_client import BitgetClient

# =========================
# ENV
# =========================
BITGET_API_KEY = os.environ.get("BITGET_API_KEY", "")
BITGET_API_SECRET = os.environ.get("BITGET_API_SECRET", "")
BITGET_PASSPHRASE = os.environ.get("BITGET_PASSPHRASE", "")
TRADE_MODE = os.environ.get("TRADE_MODE", "live")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

# TP(익절) 퍼센트, 체크 주기
TP_PERCENT = float(os.environ.get("TP_PERCENT", "0.07"))           # 7% 기본
TP_CHECK_SEC = float(os.environ.get("TP_CHECK_SEC", "2.0"))        # 2초마다 체크

# =========================
# APP / LOGGER
# =========================
logger = logging.getLogger("uvicorn.error")
logger.setLevel(logging.INFO)

app = FastAPI()

# =========================
# BITGET CLIENT
# =========================
bg = BitgetClient(
    api_key=BITGET_API_KEY,
    api_secret=BITGET_API_SECRET,
    passphrase=BITGET_PASSPHRASE,
    product_type="umcbl",
    margin_coin="USDT",
    logger=logger,
)

# =========================
# UTILS
# =========================
def normalize_symbol(sym: str) -> str:
    """
    TV에서 올 수 있는 심볼을 Bitget UMCBL 표기로 통일
    BTCUSDT.P -> BTCUSDT_UMCBL
    BTCUSDT -> BTCUSDT_UMCBL
    """
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
_watch_symbols: Set[str] = set()  # 익절 모니터링 대상 심볼


def symbol_lock(symbol: str) -> asyncio.Lock:
    if symbol not in _symbol_locks:
        _symbol_locks[symbol] = asyncio.Lock()
    return _symbol_locks[symbol]


async def sleep_ms(ms: int):
    await asyncio.sleep(ms / 1000.0)


def _fmt_qty(q: float) -> str:
    """
    수량 문자열화(여유 있게 6자리). 비트겟이 허용 범위에서 라운딩.
    """
    txt = f"{q:.6f}"
    return txt.rstrip("0").rstrip(".") if "." in txt else txt

# =========================
# CLOSE (확인 루프)
# =========================
async def ensure_close_full(symbol: str, side_to_close: str, *, max_retry: int = 10) -> Dict[str, Any]:
    """
    side_to_close: 'LONG' or 'SHORT'
    - 포지션 조회 -> reduceOnly 시장가로 닫기 -> 0 확인
    - 조회/주문 실패시 짧은 백오프로 재시도
    - 끝까지 0이 안 되면 신규 오픈은 중단해야 하므로 ok=False로 반환
    """
    last_detail: Any = None
    backoff = 0.30  # sec

    for attempt in range(1, max_retry + 1):
        try:
            detail = bg.get_hedge_detail(symbol)
            long_sz = float(detail["long"]["size"] or 0)
            short_sz = float(detail["short"]["size"] or 0)
        except Exception as e:
            logger.info("get_hedge_detail failed (retrying) #%s %s: %r", attempt, symbol, e)
            await sleep_ms(int(backoff * 1000))
            backoff = min(backoff * 1.5, 1.2)
            continue

        logger.info(
            "ensure_close_full #%s | %s sizes(long=%.6f, short=%.6f)",
            attempt, symbol, long_sz, short_sz
        )

        try:
            if side_to_close == "LONG":
                if long_sz <= 0:
                    return {"ok": True, "closed": {"skipped": True, "reason": "already_zero"}}
                bg.close_long(symbol, size=_fmt_qty(long_sz))
            else:
                if short_sz <= 0:
                    return {"ok": True, "closed": {"skipped": True, "reason": "already_zero"}}
                bg.close_short(symbol, size=_fmt_qty(short_sz))
        except requests.RequestException as e:
            try:
                last_detail = e.response.json()  # type: ignore
            except Exception:
                last_detail = {
                    "raw": getattr(e, "response", None)
                    and getattr(e.response, "text", "")
                    or str(e)
                }
            logger.info("close attempt error (retrying): %s", last_detail)

        # 체결 반영 대기 + 재확인
        await sleep_ms(int(backoff * 1000))
        try:
            d2 = bg.get_hedge_detail(symbol)
            long2 = float(d2["long"]["size"] or 0)
            short2 = float(d2["short"]["size"] or 0)
        except Exception:
            long2 = long_sz
            short2 = short_sz

        if side_to_close == "LONG" and long2 <= 0:
            return {"ok": True, "closed": {"size_before": long_sz, "size_after": long2}}
        if side_to_close == "SHORT" and short2 <= 0:
            return {"ok": True, "closed": {"size_before": short_sz, "size_after": short2}}

        backoff = min(backoff * 1.5, 1.2)

    return {"ok": False, "error": "close_not_flat", "detail": last_detail}

# =========================
# TP MONITOR (익절 7%)
# =========================
async def tp_monitor_loop():
    """
    _watch_symbols 에 등록된 심볼을 주기적으로 검사해서
    - 롱: (last - avg) / avg >= TP_PERCENT → 롱 전량 reduceOnly 청산
    - 숏: (avg - last) / avg >= TP_PERCENT → 숏 전량 reduceOnly 청산
    """
    logger.info("[tp] monitor started: TP_PERCENT=%.4f, interval=%.2fs", TP_PERCENT, TP_CHECK_SEC)
    while True:
        try:
            for sym in list(_watch_symbols):
                try:
                    d = bg.get_hedge_detail(sym)
                    long_sz = float(d["long"]["size"] or 0)
                    short_sz = float(d["short"]["size"] or 0)
                    long_avg = float(d["long"]["avg"] or 0)
                    short_avg = float(d["short"]["avg"] or 0)

                    # 포지션 없으면 스킵
                    if long_sz <= 0 and short_sz <= 0:
                        continue

                    last = bg.get_last_price(sym)

                    # 롱 TP
                    if long_sz > 0 and long_avg > 0:
                        up = (last - long_avg) / long_avg
                        if up >= TP_PERCENT:
                            logger.info("[tp] LONG take-profit %.4f >= %.4f | %s last=%.4f avg=%.4f size=%.6f",
                                        up, TP_PERCENT, sym, last, long_avg, long_sz)
                            bg.close_long(sym, size=_fmt_qty(long_sz))

                    # 숏 TP
                    if short_sz > 0 and short_avg > 0:
                        up = (short_avg - last) / short_avg
                        if up >= TP_PERCENT:
                            logger.info("[tp] SHORT take-profit %.4f >= %.4f | %s last=%.4f avg=%.4f size=%.6f",
                                        up, TP_PERCENT, sym, last, short_avg, short_sz)
                            bg.close_short(sym, size=_fmt_qty(short_sz))

                except Exception as e:
                    logger.info("[tp] monitor error on %s: %r", sym, e)

            await asyncio.sleep(TP_CHECK_SEC)
        except Exception as e:
            logger.info("[tp] loop exception: %r", e)
            await asyncio.sleep(TP_CHECK_SEC)


@app.on_event("startup")
async def _startup():
    # TP 모니터 백그라운드 태스크 시작
    asyncio.create_task(tp_monitor_loop())

# =========================
# ROUTES
# =========================
@app.get("/")
def health() -> Dict[str, Any]:
    return {
        "ok": True,
        "service": "siu-autotrade-gui",
        "mode": TRADE_MODE,
        "tp_percent": TP_PERCENT,
        "tp_interval": TP_CHECK_SEC,
        "watch": list(_watch_symbols),
    }


@app.post("/tv")
async def tv(request: Request):
    # 0) JSON 파싱
    try:
        payload = await request.json()
    except Exception:
        raw = await request.body()
        try:
            payload = json.loads(raw.decode("utf-8"))
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

    logger.info(
        "[LIVE] [TV] 수신 | raw=%s -> %s | %s | %s | size=%s",
        raw_symbol, symbol, route, target_side, size
    )

    # 심볼별 직렬화
    async with symbol_lock(symbol):

        # ✅ 신규 진입 전 현재 포지션 확인 가능 (필요 시 조회)
        if route == "order.open":
            try:
                d = bg.get_hedge_detail(symbol)
                # 신규 진입 (양방향: 반대 보유와 무관)
                if target_side == "BUY":
                    res = bg.open_long(symbol, size, order_type)
                elif target_side == "SELL":
                    res = bg.open_short(symbol, size, order_type)
                else:
                    return JSONResponse({"ok": False, "error": "bad-target-side"}, status_code=400)

                # TP 모니터에 등록
                _watch_symbols.add(symbol)
                return {"ok": True, "before": d, "result": res}
            except requests.HTTPError as e:
                try:
                    detail = e.response.json()
                except Exception:
                    detail = {"raw": getattr(e.response, "text", "")}
                return JSONResponse(
                    {"ok": False, "error": "bitget-http", "status": getattr(e.response, "status_code", None), "detail": detail},
                    status_code=500,
                )

        if route == "order.reverse":
            try:
                if target_side == "BUY":
                    close_res = await ensure_close_full(symbol, "SHORT")  # 숏 전량 청산
                    if not close_res.get("ok"):
                        logger.error("reverse abort: SHORT close failed: %s", close_res)
                        return JSONResponse({"ok": False, "error": "close-failed", "detail": close_res}, status_code=500)
                    open_res = bg.open_long(symbol, size, order_type)

                elif target_side == "SELL":
                    close_res = await ensure_close_full(symbol, "LONG")   # 롱 전량 청산
                    if not close_res.get("ok"):
                        logger.error("reverse abort: LONG close failed: %s", close_res)
                        return JSONResponse({"ok": False, "error": "close-failed", "detail": close_res}, status_code=500)
                    open_res = bg.open_short(symbol, size, order_type)

                else:
                    return JSONResponse({"ok": False, "error": "bad-target-side"}, status_code=400)

                # TP 모니터에 등록
                _watch_symbols.add(symbol)
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
            except Exception as e:  # 네트워크/파싱 예외 등
                logger.exception("Exception in /tv reverse: %r", e)
                return JSONResponse({"ok": False, "error": "exception", "detail": str(e)}, status_code=500)

        return JSONResponse({"ok": False, "error": "unsupported-route"}, status_code=400)
