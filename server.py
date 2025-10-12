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

# === 환경변수 ===
BITGET_API_KEY = os.environ.get("BITGET_API_KEY", "")
BITGET_API_SECRET = os.environ.get("BITGET_API_SECRET", "")
BITGET_PASSPHRASE = os.environ.get("BITGET_PASSPHRASE", "")
TRADE_MODE = os.environ.get("TRADE_MODE", "live")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "your-strong-secret")

# ROI TP 설정 (기본 7%, 40배)
ROI_TARGET = float(os.environ.get("TP_ROI", "0.07"))      # 0.07 = 7%
LEVERAGE = float(os.environ.get("TP_LEVERAGE", "40"))     # 예: 40배

# === 로거 ===
logger = logging.getLogger("uvicorn.error")
logger.setLevel(logging.INFO)

# === FastAPI ===
app = FastAPI()

bg = BitgetClient(
    api_key=BITGET_API_KEY,
    api_secret=BITGET_API_SECRET,
    passphrase=BITGET_PASSPHRASE,
    product_type="umcbl",
    margin_coin="USDT",
    logger=logger,
)

_symbol_locks: dict[str, asyncio.Lock] = {}


def symbol_lock(symbol: str) -> asyncio.Lock:
    if symbol not in _symbol_locks:
        _symbol_locks[symbol] = asyncio.Lock()
    return _symbol_locks[symbol]


async def sleep_ms(ms: int):
    await asyncio.sleep(ms / 1000.0)


def normalize_symbol(sym: str) -> str:
    if not sym:
        return "BTCUSDT_UMCBL"
    s = sym.strip().upper()
    # 트뷰에서 .P 들어오면 제거
    if s.endswith(".P"):
        s = s[:-2]
    if not s.endswith("_UMCBL"):
        if s.endswith("USDT"):
            s = s + "_UMCBL"
        else:
            s = s + "USDT_UMCBL"
    return s


def _fmt_qty(q: float) -> str:
    txt = f"{q:.6f}"
    return txt.rstrip("0").rstrip(".") if "." in txt else txt


# ---------------------------------------------------------
# 전량 청산 루프 (상대 측면만 닫는다)
# ---------------------------------------------------------
async def ensure_close_full(symbol: str, side_to_close: str, *, max_retry: int = 10) -> Dict[str, Any]:
    """
    side_to_close: 'LONG' | 'SHORT'
    """
    last_detail: Any = None
    backoff = 0.30

    for attempt in range(1, max_retry + 1):
        try:
            sizes = bg.get_hedge_sizes(symbol)
            long_sz = float(sizes["long"] or 0)
            short_sz = float(sizes["short"] or 0)
        except Exception as e:
            logger.info("get_hedge_sizes failed (retrying) #%s %s: %r", attempt, symbol, e)
            await sleep_ms(int(backoff * 1000))
            backoff = min(backoff * 1.5, 1.2)
            continue

        logger.info(f"ensure_close_full #{attempt} | {symbol} long={long_sz:.6f} short={short_sz:.6f}")

        try:
            if side_to_close == "LONG":
                if long_sz <= 0:
                    return {"ok": True, "closed": {"skipped": True}}
                bg.close_long(symbol, size=_fmt_qty(long_sz))
            else:
                if short_sz <= 0:
                    return {"ok": True, "closed": {"skipped": True}}
                bg.close_short(symbol, size=_fmt_qty(short_sz))
        except requests.RequestException as e:
            try:
                last_detail = e.response.json()
            except Exception:
                last_detail = {"raw": str(e)}
            logger.info(f"close attempt error: {last_detail}")

        await sleep_ms(int(backoff * 1000))

        try:
            sizes2 = bg.get_hedge_sizes(symbol)
            long2 = float(sizes2["long"] or 0)
            short2 = float(sizes2["short"] or 0)
        except Exception:
            long2, short2 = long_sz, short_sz

        if side_to_close == "LONG" and long2 <= 0:
            return {"ok": True}
        if side_to_close == "SHORT" and short2 <= 0:
            return {"ok": True}

        backoff = min(backoff * 1.5, 1.2)

    return {"ok": False, "error": "close_not_flat", "detail": last_detail}


# ---------------------------------------------------------
# Health
# ---------------------------------------------------------
@app.get("/")
def health():
    return {"ok": True, "service": "siu-autotrade-gui", "mode": TRADE_MODE}


# ---------------------------------------------------------
# TV Webhook
# ---------------------------------------------------------
@app.post("/tv")
async def tv(request: Request):
    try:
        payload = await request.json()
    except Exception:
        body = await request.body()
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            return JSONResponse({"ok": False, "error": "bad-json"}, 400)

    # auth
    if str(payload.get("secret")) != str(WEBHOOK_SECRET):
        return JSONResponse({"ok": False, "error": "unauthorized"}, 401)

    route = str(payload.get("route", "")).strip()
    raw_symbol = str(payload.get("symbol", "BTCUSDT_UMCBL")).strip()
    symbol = normalize_symbol(raw_symbol)
    target_side = str(payload.get("target_side", "")).upper()
    order_type = str(payload.get("type", "MARKET")).lower()
    size = str(payload.get("size", "0.001"))

    logger.info(f"[LIVE] [TV] 수신 | raw={raw_symbol} -> symbol={symbol} | {route} | {target_side} | size={size}")

    async with symbol_lock(symbol):
        if route == "order.reverse":
            try:
                if target_side == "BUY":
                    # 1) 숏 전량 청산
                    close_res = await ensure_close_full(symbol, "SHORT")
                    if not close_res.get("ok"):
                        return JSONResponse({"ok": False, "error": "close-failed", "detail": close_res}, 500)

                    # 2) 롱 오픈
                    opened = bg.open_long(symbol, size, order_type)

                    # 3) 체결가 기준 TP(ROI) 등록
                    try:
                        entry_price = float(opened.get("data", {}).get("priceAvg", 0) or 0)
                        if entry_price <= 0:
                            entry_price = bg.get_ticker_last(symbol)
                        if entry_price > 0:
                            bg.place_take_profit_by_roi(symbol, "long", entry_price, leverage=LEVERAGE, roi_target=ROI_TARGET)
                        else:
                            logger.warning("entry price unavailable; skip TP")
                    except Exception as e:
                        logger.warning(f"TP 설정 실패: {e}")

                elif target_side == "SELL":
                    # 1) 롱 전량 청산
                    close_res = await ensure_close_full(symbol, "LONG")
                    if not close_res.get("ok"):
                        return JSONResponse({"ok": False, "error": "close-failed", "detail": close_res}, 500)

                    # 2) 숏 오픈
                    opened = bg.open_short(symbol, size, order_type)

                    # 3) 체결가 기준 TP(ROI) 등록
                    try:
                        entry_price = float(opened.get("data", {}).get("priceAvg", 0) or 0)
                        if entry_price <= 0:
                            entry_price = bg.get_ticker_last(symbol)
                        if entry_price > 0:
                            bg.place_take_profit_by_roi(symbol, "short", entry_price, leverage=LEVERAGE, roi_target=ROI_TARGET)
                        else:
                            logger.warning("entry price unavailable; skip TP")
                    except Exception as e:
                        logger.warning(f"TP 설정 실패: {e}")

                else:
                    return JSONResponse({"ok": False, "error": "bad-target-side"}, 400)

                return {"ok": True, "opened": opened}

            except Exception as e:
                logger.exception("Exception in /tv reverse")
                return JSONResponse({"ok": False, "error": str(e)}, 500)

        elif route == "order.open":
            # 단순 오픈(테스트/수동)
            if target_side == "BUY":
                res = bg.open_long(symbol, size, order_type)
            elif target_side == "SELL":
                res = bg.open_short(symbol, size, order_type)
            else:
                return JSONResponse({"ok": False, "error": "bad-target-side"}, 400)
            return {"ok": True, "result": res}

        return JSONResponse({"ok": False, "error": "unsupported-route"}, 400)
