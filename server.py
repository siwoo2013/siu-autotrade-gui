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

# ========= ENV =========
BITGET_API_KEY = os.getenv("BITGET_API_KEY", "")
BITGET_API_SECRET = os.getenv("BITGET_API_SECRET", "")
BITGET_PASSPHRASE = os.getenv("BITGET_PASSPHRASE", "")
TRADE_MODE = os.getenv("TRADE_MODE", "live")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

# TP: ROE(수익률) 기준. 예: 0.07(=7%)
TP_ROE_PERCENT = float(os.getenv("TP_ROE_PERCENT", os.getenv("TP_PERCENT", "0.07")))
TP_CHECK_SEC = float(os.getenv("TP_CHECK_SEC", "2.0"))

logger = logging.getLogger("uvicorn.error")
logger.setLevel(logging.INFO)

app = FastAPI(title="siu-autotrade-gui")

bg = BitgetClient(
    api_key=BITGET_API_KEY,
    api_secret=BITGET_API_SECRET,
    passphrase=BITGET_PASSPHRASE,
    product_type="umcbl",
    margin_coin="USDT",
    logger=logger,
)

# ========= utils =========
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
_watch_symbols: Set[str] = set()

def symbol_lock(symbol: str) -> asyncio.Lock:
    if symbol not in _symbol_locks:
        _symbol_locks[symbol] = asyncio.Lock()
    return _symbol_locks[symbol]

def _fmt_qty(q: float) -> str:
    txt = f"{q:.6f}"
    return txt.rstrip("0").rstrip(".") if "." in txt else txt

async def sleep(s: float):
    await asyncio.sleep(s)

# ========= close helper =========
async def ensure_close_full(symbol: str, side_to_close: str, *, max_retry: int = 10) -> Dict[str, Any]:
    """
    hedge 기준:
      side_to_close = "LONG" | "SHORT"
      -> 해당 사이드 사이즈 전량 reduceOnly 시장가 청산
    """
    backoff = 0.25
    for _ in range(max_retry):
        try:
            d = bg.get_hedge_detail(symbol)
        except Exception as e:
            logger.info("get_hedge_detail fail: %r", e)
            await sleep(backoff); backoff = min(backoff * 1.5, 1.2)
            continue

        long_sz = float(d["long"]["size"] or 0)
        short_sz = float(d["short"]["size"] or 0)

        if side_to_close == "LONG":
            if long_sz <= 0: return {"ok": True, "closed": {"skipped": True}}
            try: bg.close_long(symbol, _fmt_qty(long_sz))
            except Exception as e: logger.info("close_long err: %r", e)
        else:
            if short_sz <= 0: return {"ok": True, "closed": {"skipped": True}}
            try: bg.close_short(symbol, _fmt_qty(short_sz))
            except Exception as e: logger.info("close_short err: %r", e)

        await sleep(backoff); backoff = min(backoff * 1.5, 1.2)
        try:
            d2 = bg.get_hedge_detail(symbol)
            if side_to_close == "LONG" and float(d2["long"]["size"] or 0) <= 0:
                return {"ok": True, "closed": {"size_before": long_sz, "size_after": 0.0}}
            if side_to_close == "SHORT" and float(d2["short"]["size"] or 0) <= 0:
                return {"ok": True, "closed": {"size_before": short_sz, "size_after": 0.0}}
        except Exception:
            pass

    return {"ok": False, "error": "close_not_flat"}

# ========= TP monitor (ROE%) =========
async def tp_monitor_loop():
    """
    ROE(= unrealizedPnL / margin) 기준으로 TP_ROE_PERCENT 이상이면 reduceOnly 전량 청산
    - 롱: pnl>0 이고 pnl/margin >= TP_ROE_PERCENT -> close_long
    - 숏: pnl>0 이고 pnl/margin >= TP_ROE_PERCENT -> close_short
    """
    logger.info("[tp] monitor started: ROE=%.4f, interval=%.2fs", TP_ROE_PERCENT, TP_CHECK_SEC)
    while True:
        try:
            for sym in list(_watch_symbols):
                try:
                    d = bg.get_hedge_detail(sym)
                    # LONG
                    ls = float(d["long"]["size"] or 0)
                    lm = float(d["long"]["margin"] or 0)
                    lp = float(d["long"]["pnl"] or 0)
                    if ls > 0 and lm > 0:
                        roe = lp / lm
                        if roe >= TP_ROE_PERCENT:
                            logger.info("[tp] LONG ROE %.4f >= %.4f | %s", roe, TP_ROE_PERCENT, sym)
                            bg.close_long(sym, _fmt_qty(ls))
                    # SHORT
                    ss = float(d["short"]["size"] or 0)
                    sm = float(d["short"]["margin"] or 0)
                    sp = float(d["short"]["pnl"] or 0)
                    if ss > 0 and sm > 0:
                        roe = sp / sm
                        if roe >= TP_ROE_PERCENT:
                            logger.info("[tp] SHORT ROE %.4f >= %.4f | %s", roe, TP_ROE_PERCENT, sym)
                            bg.close_short(sym, _fmt_qty(ss))
                except Exception as e:
                    logger.info("[tp] monitor error %s: %r", sym, e)
            await asyncio.sleep(TP_CHECK_SEC)
        except Exception as e:
            logger.info("[tp] loop err: %r", e)
            await asyncio.sleep(TP_CHECK_SEC)

@app.on_event("startup")
async def _startup():
    asyncio.create_task(tp_monitor_loop())

# ========= routes =========
@app.get("/")
def root():
    return {
        "ok": True,
        "service": "siu-autotrade-gui",
        "mode": TRADE_MODE,
        "tp_roe_percent": TP_ROE_PERCENT,
        "tp_interval": TP_CHECK_SEC,
        "watch": list(_watch_symbols),
    }

@app.post("/tv")
async def tv(request: Request):
    try:
        payload = await request.json()
    except Exception:
        raw = await request.body()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            return JSONResponse({"ok": False, "error": "bad-json"}, 400)

    if str(payload.get("secret")) != str(WEBHOOK_SECRET):
        return JSONResponse({"ok": False, "error": "unauthorized"}, 401)

    route = str(payload.get("route", "")).strip()
    raw_symbol = str(payload.get("symbol", "BTCUSDT.P"))
    symbol = normalize_symbol(raw_symbol)
    target = str(payload.get("target_side", "")).upper()
    otype = str(payload.get("type", "MARKET")).lower()
    size = float(payload.get("size", 0.0))

    logger.info("[TV] route=%s symbol=%s target=%s size=%s", route, symbol, target, size)

    async with symbol_lock(symbol):
        if route == "order.open":
            if size <= 0:
                return JSONResponse({"ok": False, "error": "invalid-size"}, 400)
            if target == "BUY":
                res = bg.open_long(symbol, str(size), otype)
            elif target == "SELL":
                res = bg.open_short(symbol, str(size), otype)
            else:
                return JSONResponse({"ok": False, "error": "bad-target-side"}, 400)
            _watch_symbols.add(symbol)
            return {"ok": True, "opened": res}

        elif route == "order.reverse":
            if size <= 0:
                return JSONResponse({"ok": False, "error": "invalid-size"}, 400)
            if target == "BUY":
                closed = await ensure_close_full(symbol, "SHORT")
                if not closed.get("ok"):
                    return JSONResponse({"ok": False, "error": "close-failed", "detail": closed}, 500)
                res = bg.open_long(symbol, str(size), otype)
            elif target == "SELL":
                closed = await ensure_close_full(symbol, "LONG")
                if not closed.get("ok"):
                    return JSONResponse({"ok": False, "error": "close-failed", "detail": closed}, 500)
                res = bg.open_short(symbol, str(size), otype)
            else:
                return JSONResponse({"ok": False, "error": "bad-target-side"}, 400)
            _watch_symbols.add(symbol)
            return {"ok": True, "closed": closed, "opened": res}

        return JSONResponse({"ok": False, "error": "unsupported-route"}, 400)
