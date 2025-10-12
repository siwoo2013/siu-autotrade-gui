# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from bitget_client import BitgetClient

# ----------------------------- Env & mode -----------------------------
APP_NAME = "siu-autotrade-gui"
BASE_DIR = Path(__file__).resolve().parent

TRADE_MODE = os.getenv("TRADE_MODE", "demo").lower()  # live | demo
if os.getenv("DEMO") is not None:
    DEMO = os.getenv("DEMO", "false").lower() in ["1", "true", "yes", "on"]
else:
    DEMO = (TRADE_MODE != "live")

MODE_TAG = "[DEMO]" if DEMO else "[LIVE]"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "your-strong-secret")

# Bitget keys
BITGET_API_KEY = os.getenv("BITGET_API_KEY", "")
BITGET_API_SECRET = os.getenv("BITGET_API_SECRET", "")
BITGET_PASSPHRASE = os.getenv("BITGET_PASSPHRASE", "")
PRODUCT_TYPE = os.getenv("PRODUCT_TYPE", "umcbl").lower()  # one-way USDT-M

# ----------------------------- FastAPI -----------------------------
log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

app = FastAPI(title=APP_NAME)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_headers=["*"],
    allow_methods=["*"],
)

# ----------------------------- Bitget client -----------------------------
_bg = BitgetClient(
    api_key=BITGET_API_KEY,
    api_secret=BITGET_API_SECRET,
    passphrase=BITGET_PASSPHRASE,
    product_type=PRODUCT_TYPE,
)

# ----------------------------- helpers -----------------------------
def tv_symbol_to_bitget(sym: str) -> str:
    """
    TV 심볼을 Bitget 심볼로 정규화:
    - 예) "BTCUSDT.P" -> "BTCUSDT_UMCBL"
    - 이미 *_UMCBL 형태면 그대로 사용
    """
    s = (sym or "").upper().strip()
    if s.endswith("_UMCBL") or s.endswith("_DMCBL"):
        return s
    base = s.replace(".P", "").replace(".UMCBL", "").replace("_UMCBL", "")
    return f"{base}_UMCBL"

def ok(data: Dict[str, Any]) -> JSONResponse:
    return JSONResponse(data)

def fail(msg: str, status: int = 500) -> JSONResponse:
    return JSONResponse({"ok": False, "error": msg}, status_code=status)

# ----------------------------- routes -----------------------------
@app.get("/")
def health() -> JSONResponse:
    return ok({"ok": True, "service": APP_NAME, "mode": "live" if not DEMO else "demo"})

@app.post("/tv")
async def tv_webhook(req: Request) -> JSONResponse:
    try:
        payload = await req.json()
    except Exception:
        return fail("invalid json", 400)

    # 1) 시크릿 체크
    if (payload or {}).get("secret") != WEBHOOK_SECRET:
        return fail("unauthorized", 401)

    route = (payload.get("route") or "").lower().strip()          # "order.reverse" 등
    exchange = (payload.get("exchange") or "").lower().strip()    # "bitget"
    tv_sym = str(payload.get("symbol") or "")
    side = (payload.get("target_side") or "buy").lower().strip()  # "buy" | "sell"
    order_type = (payload.get("type") or "market").lower().strip()
    size = float(payload.get("size") or 0.0)
    client_oid = payload.get("client_oid")

    log.info("%s [TV] 수신 | %s | %s | %s | size=%.6f",
             MODE_TAG, tv_sym, route, side.upper(), size)

    if exchange != "bitget":
        return fail("unsupported exchange", 400)

    symbol = tv_symbol_to_bitget(tv_sym)

    # 2) 단순 주문 실행 (One-way 기준: reduceOnly는 TV 메시지에 없으면 False)
    try:
        res = _bg.place_order(
            symbol=symbol,
            side=("buy" if side == "buy" else "sell"),
            order_type=("market" if order_type == "market" else "limit"),
            size=size,
            reduce_only=bool(payload.get("reduce_only", False)),
            client_oid=(client_oid or None),
        )
        return ok({"ok": True, "symbol": symbol, "result": res})
    except Exception as e:
        log.exception("Exception in /tv")
        return fail(str(e), 500)
