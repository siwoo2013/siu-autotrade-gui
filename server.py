# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
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

# TP: ROE(= unrealizedPnL / margin)
TP_ROE_PERCENT = float(os.getenv("TP_ROE_PERCENT", os.getenv("TP_PERCENT", "0.07")))
TP_CHECK_SEC = float(os.getenv("TP_CHECK_SEC", "2.0"))

# Re-entry after TP
REENTRY_ENABLED = str(os.getenv("REENTRY_ENABLED", "false")).lower() in ("1", "true", "yes", "y", "on")
REENTRY_DELAY_SEC = float(os.getenv("REENTRY_DELAY_SEC", "3.0"))
REENTRY_SIZE_MULT = float(os.getenv("REENTRY_SIZE_MULT", "1.0"))
REENTRY_COOLDOWN_SEC = float(os.getenv("REENTRY_COOLDOWN_SEC", "30"))
REENTRY_MAX_TRIES = int(os.getenv("REENTRY_MAX_TRIES", "1"))

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

# ========= utils / state =========
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
_watch_symbols: Set[str] = set()                 # TP 감시 대상
_last_reentry_at: dict[str, float] = {}          # 쿨다운 관리
_reentry_tries_since_tp: dict[str, int] = {}     # TP 이벤트당 재진입 횟수

def symbol_lock(symbol: str) -> asyncio.Lock:
    if symbol not in _symbol_locks:
        _symbol_locks[symbol] = asyncio.Lock()
    return _symbol_locks[symbol]

def _fmt_qty(q: float) -> str:
    txt = f"{q:.6f}"
    return txt.rstrip("0").rstrip(".") if "." in txt else txt

async def sleep(s: float):  # small helper
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

# ========= re-entry =========
async def schedule_reentry(symbol: str, direction: str, closed_size: float):
    """
    direction: "LONG" or "SHORT" (익절된 방향과 동일하게 재진입)
    """
    if not REENTRY_ENABLED:
        return

    now = time.time()
    last = _last_reentry_at.get(symbol, 0.0)
    tries = _reentry_tries_since_tp.get(symbol, 0)

    # 쿨다운 / 횟수 제한
    if (now - last) < REENTRY_COOLDOWN_SEC:
        logger.info("[reentry] cooldown active for %s (%.1fs left)", symbol, REENTRY_COOLDOWN_SEC - (now - last))
        return
    if tries >= REENTRY_MAX_TRIES:
        logger.info("[reentry] max tries reached for %s (tries=%d)", symbol, tries)
        return

    qty = max(0.0, closed_size * REENTRY_SIZE_MULT)
    if qty <= 0:
        return

    async def _task():
        await sleep(REENTRY_DELAY_SEC)
        async with symbol_lock(symbol):
            try:
                if direction == "LONG":
                    res = bg.open_long(symbol, _fmt_qty(qty), "market")
                else:
                    res = bg.open_short(symbol, _fmt_qty(qty), "market")
                _watch_symbols.add(symbol)
                _last_reentry_at[symbol] = time.time()
                _reentry_tries_since_tp[symbol] = _reentry_tries_since_tp.get(symbol, 0) + 1
                logger.info("[reentry] opened %s %s size=%s -> %s", symbol, direction, qty, res)
            except Exception as e:
                logger.info("[reentry] open error %s %s: %r", symbol, direction, e)

    asyncio.create_task(_task())

# ========= TP monitor (ROE%) =========
async def tp_monitor_loop():
    """
    ROE(= unrealizedPnL / margin) 기준으로 TP_ROE_PERCENT 이상이면 reduceOnly 전량 청산
    - 롱: pnl>0 이고 pnl/margin >= TP_ROE_PERCENT -> close_long
    - 숏: pnl>0 이고 pnl/margin >= TP_ROE_PERCENT -> close_short
    이후 REENTRY_* 설정에 따라 동일 방향 재진입 스케줄
    """
    logger.info("[tp] monitor started: ROE=%.4f, interval=%.2fs, reentry=%s",
                TP_ROE_PERCENT, TP_CHECK_SEC, REENTRY_ENABLED)
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
                            # 동일 방향 재진입
                            await schedule_reentry(sym, "LONG", ls)

                    # SHORT
                    ss = float(d["short"]["size"] or 0)
                    sm = float(d["short"]["margin"] or 0)
                    sp = float(d["short"]["pnl"] or 0)
                    if ss > 0 and sm > 0:
                        roe = sp / sm
                        if roe >= TP_ROE_PERCENT:
                            logger.info("[tp] SHORT ROE %.4f >= %.4f | %s", roe, TP_ROE_PERCENT, sym)
                            bg.close_short(sym, _fmt_qty(ss))
                            # 동일 방향 재진입
                            await schedule_reentry(sym, "SHORT", ss)

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
        "reentry_enabled": REENTRY_ENABLED,
        "reentry_delay": REENTRY_DELAY_SEC,
        "reentry_size_mult": REENTRY_SIZE_MULT,
        "reentry_cooldown": REENTRY_COOLDOWN_SEC,
        "reentry_max_tries": REENTRY_MAX_TRIES,
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
                res = bg.open_long(symbol, _fmt_qty(size), otype)
            elif target == "SELL":
                res = bg.open_short(symbol, _fmt_qty(size), otype)
            else:
                return JSONResponse({"ok": False, "error": "bad-target-side"}, 400)
            _watch_symbols.add(symbol)
            # TP 이벤트가 새로 시작되므로 재진입 카운터 리셋
            _reentry_tries_since_tp[symbol] = 0
            return {"ok": True, "opened": res}

        elif route == "order.reverse":
            if size <= 0:
                return JSONResponse({"ok": False, "error": "invalid-size"}, 400)
            if target == "BUY":
                closed = await ensure_close_full(symbol, "SHORT")
                if not closed.get("ok"):
                    return JSONResponse({"ok": False, "error": "close-failed", "detail": closed}, 500)
                res = bg.open_long(symbol, _fmt_qty(size), otype)
            elif target == "SELL":
                closed = await ensure_close_full(symbol, "LONG")
                if not closed.get("ok"):
                    return JSONResponse({"ok": False, "error": "close-failed", "detail": closed}, 500)
                res = bg.open_short(symbol, _fmt_qty(size), otype)
            else:
                return JSONResponse({"ok": False, "error": "bad-target-side"}, 400)
            _watch_symbols.add(symbol)
            _reentry_tries_since_tp[symbol] = 0
            return {"ok": True, "closed": closed, "opened": res}

        return JSONResponse({"ok": False, "error": "unsupported-route"}, 400)
