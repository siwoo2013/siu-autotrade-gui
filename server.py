# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict

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

# ---------- routes ----------

@app.get("/")
def health() -> Dict[str, Any]:
    return {"ok": True, "service": "siu-autotrade-gui", "mode": TRADE_MODE}


@app.post("/tv")
async def tv(request: Request):
    try:
        payload = await request.json()
    except Exception:
        body = await request.body()
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            return JSONResponse({"ok": False, "error": "bad-json"}, status_code=400)

    # auth
    if str(payload.get("secret")) != str(WEBHOOK_SECRET):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    route = str(payload.get("route", "")).strip()
    exchange = str(payload.get("exchange", "bitget")).lower()
    symbol = str(payload.get("symbol", "BTCUSDT_UMCBL")).strip()
    target_side = str(payload.get("target_side", "")).upper()  # BUY / SELL
    order_type = str(payload.get("type", "MARKET")).lower()    # market / limit
    size = str(payload.get("size", "0.001"))

    if exchange != "bitget":
        return JSONResponse({"ok": False, "error": "unsupported-exchange"}, status_code=400)

    logger.info("[LIVE] [TV] 수신 | %s | %s | %s | size=%s", symbol, route, target_side, size)

    # --- helper: open only ---
    def open_by_side(side: str):
        if side == "BUY":
            return bg.open_long(symbol, size, order_type)
        else:
            return bg.open_short(symbol, size, order_type)

    # --- helper: close only ---
    def close_by_side(side: str):
        if side == "BUY":
            # BUY 청산 = close_long
            return bg.close_long(symbol, size, order_type)
        else:
            # SELL 청산 = close_short
            return bg.close_short(symbol, size, order_type)

    try:
        # 1) 단순 오픈
        if route == "order.open":
            res = open_by_side(target_side)
            return {"ok": True, "result": res}

        # 2) EA 스타일 리버스:
        #    - target_side=BUY  -> (1) close_short  (2) open_long
        #    - target_side=SELL -> (1) close_long   (2) open_short
        elif route == "order.reverse":
            if target_side == "BUY":
                logger.info("[LIVE] reverse: EA | close_short -> open_long | size=%s", size)
                close_res = bg.close_short(symbol, size, order_type)
                open_res = bg.open_long(symbol, size, order_type)
                return {"ok": True, "closed": close_res, "opened": open_res}
            elif target_side == "SELL":
                logger.info("[LIVE] reverse: EA | close_long -> open_short | size=%s", size)
                close_res = bg.close_long(symbol, size, order_type)
                open_res = bg.open_short(symbol, size, order_type)
                return {"ok": True, "closed": close_res, "opened": open_res}
            else:
                return JSONResponse({"ok": False, "error": "bad-target-side"}, status_code=400)

        else:
            return JSONResponse({"ok": False, "error": "unsupported-route"}, status_code=400)

    except requests.HTTPError as e:
        try:
            detail = e.response.json()
        except Exception:
            detail = {"raw": getattr(e.response, "text", "")}
        logger.error("HTTPError during %s: %s", route, detail)
        return JSONResponse({"ok": False, "error": "bitget-http", "status": getattr(e.response, "status_code", None), "detail": detail}, status_code=500)
    except Exception as e:  # noqa: BLE001
        logger.exception("Exception in /tv (EA): %r", e)
        return JSONResponse({"ok": False, "error": "exception", "detail": str(e)}, status_code=500)
