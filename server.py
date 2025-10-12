# server.py
# -*- coding: utf-8 -*-

from __future__ import annotations

import os
import re
import json
from pathlib import Path
from typing import Any, Dict

import requests
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from bitget_client import BitgetClient

APP_NAME = "siu-autotrade-gui"
BASE_DIR = Path(__file__).resolve().parent

TRADE_MODE = os.getenv("TRADE_MODE", "demo").lower()   # live | demo
DEMO = (TRADE_MODE != "live")
MODE_TAG = "[DEMO]" if DEMO else "[LIVE]"

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "YOUR_WEBHOOK_SECRET")

BITGET_API_KEY = os.getenv("BITGET_API_KEY", "").strip()
BITGET_API_SECRET = os.getenv("BITGET_API_SECRET", "").strip()
BITGET_PASSPHRASE = os.getenv("BITGET_PASSPHRASE", "").strip()

PRODUCT_TYPE = "umcbl"  # U-margined perpetual

app = FastAPI(title=APP_NAME)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

_bg = BitgetClient(
    api_key=BITGET_API_KEY,
    api_secret=BITGET_API_SECRET,
    passphrase=BITGET_PASSPHRASE,
    product_type=PRODUCT_TYPE,
)

def normalize_symbol(tv_symbol: str) -> str:
    s = tv_symbol.upper().strip()
    s = re.sub(r"\.P$", "", s)                 # .P 제거
    s = re.sub(r"PERP(ETUAL)?$", "", s)        # PERP/ PERPETUAL 제거
    s = s.replace(":", "")
    s = re.sub(r"[^A-Z0-9]", "", s)
    if not s.endswith("_UMCBL"):
        s = f"{s}_UMCBL"
    return s

def side_open_for_oneway(target_side: str) -> str:
    """BUY/SELL -> buy/sell (오픈용)"""
    t = target_side.upper()
    if t == "BUY":
        return "buy"
    if t == "SELL":
        return "sell"
    raise ValueError("target_side must be BUY or SELL")

@app.get("/")
def health() -> Dict[str, Any]:
    return {"ok": True, "service": APP_NAME, "mode": "demo" if DEMO else "live"}

@app.post("/tv")
async def tv_webhook(req: Request):
    raw = await req.body()
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid-json"}, status_code=400)

    if data.get("secret") != WEBHOOK_SECRET:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    route = data.get("route", "")
    exchange = (data.get("exchange") or "").lower()
    tv_symbol = data.get("symbol", "")
    target_side = (data.get("target_side") or "").upper()
    ord_type = (data.get("type") or "MARKET").upper()
    size = float(data.get("size") or 0)

    symbol = normalize_symbol(tv_symbol)
    print(f"{MODE_TAG} [TV] 수신 | {symbol} | {route} | {target_side} | size={size:.6f}")

    if exchange != "bitget":
        return JSONResponse({"ok": False, "error": "unsupported-exchange"}, status_code=400)
    if route not in ("order.reverse", "order.open"):
        return JSONResponse({"ok": False, "error": "unsupported-route"}, status_code=400)

    try:
        side_open = side_open_for_oneway(target_side)  # buy/sell
    except ValueError as ve:
        return JSONResponse({"ok": False, "error": str(ve)}, status_code=400)

    client_oid = f"tv-{int(os.times().elapsed * 1000)}"

    # 1) OPEN 시도
    try:
        _bg.place_order(
            tv_symbol=symbol,
            side=side_open,                 # "buy" or "sell"
            order_type=ord_type.lower(),   # "market"
            size=size,
            reduce_only=False,
            client_oid=client_oid,
        )
        return JSONResponse({"ok": True, "action": "open", "side": side_open})

    except requests.HTTPError as e1:
        status = getattr(getattr(e1, "response", None), "status_code", None)
        body_text = ""
        try:
            body_text = e1.response.text or ""
        except Exception:
            body_text = str(e1)

        # Bitget 원웨이: 강제 close 는 open 과 "같은 방향 + reduceOnly=True" 로 보내야 함
        if status == 400 and "side mismatch" in body_text:
            try:
                print(f"side mismatch on OPEN -> force CLOSE first: {side_open} (reduceOnly, size={size})")
                _bg.place_order(
                    tv_symbol=symbol,
                    side=side_open,                 # ★ open 과 같은 방향
                    order_type=ord_type.lower(),
                    size=size,
                    reduce_only=True,               # ★ reduceOnly 로 강제 청산
                    client_oid=f"{client_oid}-close",
                )
            except Exception as e_close:
                print(f"force close failed (continue to OPEN): {e_close}")

            # 다시 OPEN
            try:
                _bg.place_order(
                    tv_symbol=symbol,
                    side=side_open,
                    order_type=ord_type.lower(),
                    size=size,
                    reduce_only=False,
                    client_oid=f"{client_oid}-open",
                )
                return JSONResponse({
                    "ok": True,
                    "action": "force-close-then-open",
                    "open_side": side_open
                })
            except Exception as e2:
                print(f"Exception in /tv (re-open): {e2}")
                return JSONResponse({"ok": False, "error": "reopen-failed", "detail": str(e2)}, status_code=500)

        # 그 외 에러
        print(f"Exception in /tv (open): {e1}")
        return JSONResponse({"ok": False, "error": "open-failed", "detail": str(e1)}, status_code=500)

    except Exception as e:
        print(f"Exception in /tv (open/unknown): {e}")
        return JSONResponse({"ok": False, "error": "open-failed", "detail": str(e)}, status_code=500)

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "10000"))
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=False)
