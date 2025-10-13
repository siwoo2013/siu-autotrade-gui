
import os
import traceback
from typing import Dict, Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from bitget_client import BitgetClient, BitgetHTTPError

import logging
log = logging.getLogger("uvicorn.error")

# ============================ ENV ============================
TRADE_MODE = (os.getenv("TRADE_MODE") or "live").lower()
BITGET_API_KEY = os.getenv("BITGET_API_KEY") or ""
BITGET_API_SECRET = os.getenv("BITGET_API_SECRET") or ""
BITGET_PASSPHRASE = os.getenv("BITGET_PASSPHRASE") or ""
WEBHOOK_SECRET = (os.getenv("WEBHOOK_SECRET") or "").strip()

# ============================ Bitget Client ============================
# matches bitget_client.py signature: api_key, api_secret, passphrase, mode
bg = BitgetClient(
    api_key=BITGET_API_KEY,
    api_secret=BITGET_API_SECRET,
    passphrase=BITGET_PASSPHRASE,
    mode=TRADE_MODE,
    timeout=10,
)

app = FastAPI(title="siu-autotrade-gui")

# ============================ Utils ============================
SYMBOL_MAP = {
    "BTCUSDT.P": "BTCUSDT_UMCBL",
    "BTCUSDT": "BTCUSDT_UMCBL",
    "ETHUSDT.P": "ETHUSDT_UMCBL",
    "ETHUSDT": "ETHUSDT_UMCBL",
}
def map_symbol(raw_symbol: str) -> str:
    s = (raw_symbol or "").strip().upper()
    if s in SYMBOL_MAP:
        return SYMBOL_MAP[s]
    if s.endswith(".P"):
        return s.split(".")[0] + "_UMCBL"
    if "_" not in s:
        return s + "_UMCBL"
    return s

def ok(data: Dict[str, Any] = None, **kw):
    d = {"ok": True}
    if data:
        d.update(data)
    if kw:
        d.update(kw)
    return JSONResponse(d, status_code=200)

def err(msg: str, **kw):
    d = {"ok": False, "error": msg}
    if kw:
        d.update(kw)
    return JSONResponse(d, status_code=200)

def auth_ok(payload: Dict[str, Any]) -> bool:
    sec = (payload or {}).get("secret")
    return (WEBHOOK_SECRET and sec == WEBHOOK_SECRET)

# ============================ Diagnostics ============================
@app.get("/")
def root():
    return {"ok": True, "service": "siu-autotrade-gui", "mode": TRADE_MODE}

@app.get("/healthz")
def healthz():
    return {"ok": True, "service": "siu-autotrade-gui", "mode": TRADE_MODE}

@app.get("/__routes")
def list_routes():
    return {"ok": True, "routes": [r.path for r in app.routes]}

@app.get("/mode")
def get_mode():
    try:
        mode = bg.query_position_mode() if hasattr(bg, "query_position_mode") else "unknown"
        return {"ok": True, "mode": mode}
    except BitgetHTTPError as e:
        return {"ok": False, "error": "bitget-http", "detail": str(e)}
    except Exception as e:
        return {"ok": False, "error": type(e).__name__, "detail": str(e)}

@app.get("/positions")
def get_positions(symbol: str = "BTCUSDT.P"):
    try:
        sym = map_symbol(symbol)
        sizes = bg.get_hedge_sizes(sym)  # {"long": x, "short": y}
        return {"ok": True, "symbol": sym, "sizes": sizes}
    except BitgetHTTPError as e:
        return {"ok": False, "error": "bitget-http", "detail": str(e)}
    except Exception as e:
        return {"ok": False, "error": type(e).__name__, "detail": str(e)}

# ============================ Trading ============================
@app.post("/tv")
async def tv(request: Request):
    try:
        try:
            payload = await request.json()
        except Exception:
            body = await request.body()
            log.error(f"Invalid JSON body: {body[:200]}")
            return err("invalid-json")

        if not auth_ok(payload):
            return err("unauthorized")

        route = (payload.get("route") or "").strip()
        target_side = (payload.get("target") or payload.get("target_side") or "").upper().strip()
        raw_symbol = payload.get("symbol") or payload.get("ticker") or ""
        symbol = map_symbol(raw_symbol)
        size = float(payload.get("size") or payload.get("qty") or 0)
        otype = (payload.get("type") or "MARKET").upper()

        log.info(f"[TV] route={route} symbol={symbol} target={target_side} size={size} type={otype}")
        if otype != "MARKET":
            return err("only-market-supported")

        # 신규 오픈
        if route == "order.open":
            if size <= 0:
                return err("invalid-size")
            open_side = "buy" if target_side == "BUY" else "sell"
            oid = bg.place_order(symbol=symbol, side=open_side,
                                 order_type="market", size=size, reduce_only=False,
                                 client_oid=f"tv-open-{target_side.lower()}")
            return ok(route=route, opened={"orderId": oid, "side": target_side, "size": size})

        # 리버스
        elif route == "order.reverse":
            if size <= 0 or target_side not in ("BUY", "SELL"):
                return err("invalid-params")

            sizes = bg.get_hedge_sizes(symbol)
            long_sz = float(sizes.get("long") or 0.0)
            short_sz = float(sizes.get("short") or 0.0)

            if long_sz == 0.0 and short_sz == 0.0:
                open_side = "buy" if target_side == "BUY" else "sell"
                oid = bg.place_order(symbol=symbol, side=open_side,
                                     order_type="market", size=size, reduce_only=False,
                                     client_oid=f"tv-rev-open-{target_side.lower()}")
                return ok(route=route, action="reverse-flat->open",
                          opened={"orderId": oid, "side": target_side, "size": size})

            if target_side == "SELL":  # 롱 청산 → 잔량 숏 오픈
                close_qty = min(size, long_sz)
                closed = None
                if long_sz > 0:
                    cid = bg.place_order(symbol=symbol, side="sell",
                                         order_type="market", size=close_qty, reduce_only=True,
                                         client_oid="tv-rev-close-long")
                    closed = {"closed_long": close_qty, "orderId": cid}
                remain = size - close_qty
                opened = None
                if remain > 0:
                    oid = bg.place_order(symbol=symbol, side="sell",
                                         order_type="market", size=remain, reduce_only=False,
                                         client_oid="tv-rev-open-sell-remain")
                    opened = {"orderId": oid, "side": "SELL", "size": remain}
                return ok(route=route, closed=closed, opened=opened)

            elif target_side == "BUY":  # 숏 청산 → 잔량 롱 오픈
                close_qty = min(size, short_sz)
                closed = None
                if short_sz > 0:
                    cid = bg.place_order(symbol=symbol, side="buy",
                                         order_type="market", size=close_qty, reduce_only=True,
                                         client_oid="tv-rev-close-short")
                    closed = {"closed_short": close_qty, "orderId": cid}
                remain = size - close_qty
                opened = None
                if remain > 0:
                    oid = bg.place_order(symbol=symbol, side="buy",
                                         order_type="market", size=remain, reduce_only=False,
                                         client_oid="tv-rev-open-buy-remain")
                    opened = {"orderId": oid, "side": "BUY", "size": remain}
                return ok(route=route, closed=closed, opened=opened)

            return err("unhandled-reverse-branch")

        else:
            return err(f"unknown-route: {route}")

    except BitgetHTTPError as e:
        log.error(f"BitgetHTTPError: {e}")
        return err("bitget-http", detail=str(e))
    except Exception as e:
        log.error(f"Exception in /tv: {e}\n{traceback.format_exc()}")
        return err(type(e).__name__, detail=str(e))

# ============================ Startup ============================
@app.on_event("startup")
async def _startup():
    try:
        if hasattr(bg, "ensure_unilateral_mode"):
            mode = bg.ensure_unilateral_mode()
            log.info(f"[startup] ensured unilateral mode -> {mode}")
    except Exception as e:
        log.warning(f"[startup] ensure_unilateral_mode skipped: {e}")
