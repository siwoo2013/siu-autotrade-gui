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

# Bitget 클라이언트 (현재 버전: __init__(api_key, api_secret, passphrase, product_type) 만 지원)
from bitget_client import BitgetClient

# -----------------------------------------------------------------------------
# 환경
# -----------------------------------------------------------------------------
APP_NAME = "siu-autotrade-gui"
BASE_DIR = Path(__file__).resolve().parent

TRADE_MODE = os.getenv("TRADE_MODE", "demo").lower()  # live | demo
DEMO = (TRADE_MODE != "live")                         # 헬스체크 표시에만 사용
MODE_TAG = "[DEMO]" if DEMO else "[LIVE]"

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "YOUR_WEBHOOK_SECRET")

BITGET_API_KEY = os.getenv("BITGET_API_KEY", "").strip()
BITGET_API_SECRET = os.getenv("BITGET_API_SECRET", "").strip()
BITGET_PASSPHRASE = os.getenv("BITGET_PASSPHRASE", "").strip()

PRODUCT_TYPE = "umcbl"  # U-margined perpetual

# -----------------------------------------------------------------------------
# FastAPI
# -----------------------------------------------------------------------------
app = FastAPI(title=APP_NAME)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------------------------------------------------------
# Bitget Client (주의: demo/logger 같은 인자는 넘기지 않음)
# -----------------------------------------------------------------------------
_bg = BitgetClient(
    api_key=BITGET_API_KEY,
    api_secret=BITGET_API_SECRET,
    passphrase=BITGET_PASSPHRASE,
    product_type=PRODUCT_TYPE,
)

# -----------------------------------------------------------------------------
# 유틸
# -----------------------------------------------------------------------------
def normalize_symbol(tv_symbol: str) -> str:
    """
    TradingView 심볼(BTCUSDT.P, BTCUSDTPERP 등)을 Bitget U-Perp 표기(BTCUSDT_UMCBL)로 변환
    """
    s = tv_symbol.upper().strip()
    s = re.sub(r"\.P$", "", s)                 # .P 제거
    s = re.sub(r"PERP(ETUAL)?$", "", s)        # PERP/ PERPETUAL 제거
    s = s.replace(":", "")
    s = re.sub(r"[^A-Z0-9]", "", s)            # 기호 제거
    if not s.endswith("_UMCBL"):
        s = f"{s}_UMCBL"
    return s


def side_map_for_oneway(target_side: str) -> Dict[str, str]:
    """
    BUY/SELL → (open, close) 매핑 (원웨이 모드)
    open:  BUY→buy, SELL→sell
    close: BUY→sell, SELL→buy (reduceOnly=True)
    """
    t = target_side.upper()
    if t not in ("BUY", "SELL"):
        raise ValueError("target_side must be BUY or SELL")
    return {
        "open": "buy" if t == "BUY" else "sell",
        "close": "sell" if t == "BUY" else "buy",
    }


# -----------------------------------------------------------------------------
# 라우트
# -----------------------------------------------------------------------------
@app.get("/")
def health() -> Dict[str, Any]:
    return {"ok": True, "service": APP_NAME, "mode": "demo" if DEMO else "live"}


@app.post("/tv")
async def tv_webhook(req: Request):
    """
    TradingView Webhook 엔드포인트
    요청 예)
    {
      "secret": "your-strong-secret",
      "route": "order.reverse",
      "exchange": "bitget",
      "symbol": "BTCUSDT.P",
      "target_side": "BUY",
      "type": "MARKET",
      "size": 0.001
    }
    """
    raw = await req.body()
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid-json"}, status_code=400)

    # 보안
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

    # open/close 매핑
    try:
        m = side_map_for_oneway(target_side)
    except ValueError as ve:
        return JSONResponse({"ok": False, "error": str(ve)}, status_code=400)

    side_open = m["open"]     # buy/sell
    side_close = m["close"]   # sell/buy (reduceOnly=True)

    # 고유 oid
    client_oid = f"tv-{int(os.times().elapsed * 1000)}"

    # 1) 오픈 시도
    try:
        _bg.place_order(
            tv_symbol=symbol,                 # BitgetClient는 tv_symbol 파라미터를 받음
            side=side_open,                   # "buy"/"sell"
            order_type=ord_type.lower(),      # "market"
            size=size,
            reduce_only=False,
            client_oid=client_oid,
        )
        return JSONResponse({"ok": True, "action": "open", "side": side_open})

    except requests.HTTPError as e1:
        # 400/side mismatch (원웨이에서 반대포지션 보유) 처리
        status = getattr(getattr(e1, "response", None), "status_code", None)
        body_text = ""
        try:
            body_text = e1.response.text or ""
        except Exception:
            body_text = str(e1)

        if status != 400 and "side mismatch" not in body_text:
            print(f"Exception in /tv (open): {e1}")
            return JSONResponse({"ok": False, "error": "open-failed", "detail": str(e1)}, status_code=500)

        # 2) 강제 reduceOnly-close 후 재오픈
        try:
            print(f"side mismatch on OPEN -> force CLOSE first: {side_close} (size={size})")
            _bg.place_order(
                tv_symbol=symbol,
                side=side_close,
                order_type=ord_type.lower(),
                size=size,
                reduce_only=True,
                client_oid=f"{client_oid}-close",
            )
        except Exception as e_close:
            print(f"force close failed (continue to OPEN): {e_close}")

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
                "close_side": side_close,
                "open_side": side_open
            })
        except Exception as e2:
            print(f"Exception in /tv (re-open): {e2}")
            return JSONResponse({"ok": False, "error": "reopen-failed", "detail": str(e2)}, status_code=500)

    except Exception as e:
        print(f"Exception in /tv (open/unknown): {e}")
        return JSONResponse({"ok": False, "error": "open-failed", "detail": str(e)}, status_code=500)


# -----------------------------------------------------------------------------
# Entrypoint
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "10000"))
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=False)
