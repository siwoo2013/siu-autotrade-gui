# server.py
# -*- coding: utf-8 -*-

from __future__ import annotations

import os
import re
import json
import logging
from pathlib import Path
from typing import Any, Dict

import requests
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from bitget_client import BitgetClient

# ===== Env & mode ============================================================
APP_NAME = "siu-autotrade-gui"
BASE_DIR = Path(__file__).resolve().parent

# 1) TRADE_MODE (live|demo)이 우선
TRADE_MODE = os.getenv("TRADE_MODE", "demo").lower()

# 2) DEMO 환경변수(하위호환)
if os.getenv("DEMO") is not None:
    DEMO = os.getenv("DEMO", "false").lower() in ["1", "true", "yes", "on"]
else:
    DEMO = (TRADE_MODE != "live")

MODE_TAG = "[DEMO]" if DEMO else "[LIVE]"

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "YOUR_WEBHOOK_SECRET")

BITGET_API_KEY = os.getenv("BITGET_API_KEY", "").strip()
BITGET_API_SECRET = os.getenv("BITGET_API_SECRET", "").strip()
BITGET_PASSPHRASE = os.getenv("BITGET_PASSPHRASE", "").strip()

PRODUCT_TYPE = "umcbl"  # U-margined perpetual

# ===== Logger ================================================================
logger = logging.getLogger(APP_NAME)
logger.setLevel(logging.INFO)
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
logger.addHandler(_handler)

# ===== FastAPI ===============================================================
app = FastAPI(title=APP_NAME)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ===== Bitget Client 인스턴스 ================================================
_bg = BitgetClient(
    api_key=BITGET_API_KEY,
    api_secret=BITGET_API_SECRET,
    passphrase=BITGET_PASSPHRASE,
    product_type=PRODUCT_TYPE,
    demo=DEMO,
    logger=logger,
)

# ===== 유틸 ==================================================================
def normalize_symbol(tv_symbol: str) -> str:
    """
    TradingView에서 오는 심볼(BTCUSDT.P, BTCUSDTPERP 등)을
    Bitget U-Perp 표기(BTCUSDT_UMCBL)로 정규화.
    """
    s = tv_symbol.upper().strip()

    # 끝의 .P / PERP / U / U:PERP / PERPETUAL 등 제거
    s = re.sub(r"\.P$", "", s)
    s = re.sub(r"PERP(ETUAL)?$", "", s)
    s = s.replace(":", "")

    # 가장 단순형 BTCUSDT 로 통일
    # (예: BTCUSDT.P, BTCUSDT, BTCUSDT_PERP, BTCUSDTU -> BTCUSDT)
    s = re.sub(r"[^A-Z0-9]", "", s)

    if not s.endswith("_UMCBL"):
        s = f"{s}_UMCBL"
    return s


def side_map_for_oneway(target_side: str) -> Dict[str, str]:
    """
    TradingView target_side(BUY/SELL)를 오픈/클로즈에 쓸 실제 비트겟 side로 맵핑.
    - 오픈: BUY -> buy / SELL -> sell
    - 클로즈: BUY의 반대는 sell, SELL의 반대는 buy (reduceOnly=True)
    """
    t = target_side.upper()
    if t not in ("BUY", "SELL"):
        raise ValueError("target_side must be BUY or SELL")

    side_open = "buy" if t == "BUY" else "sell"
    side_close = "sell" if t == "BUY" else "buy"
    return {"open": side_open, "close": side_close}


# ===== Routes ================================================================
@app.get("/")
def health() -> Dict[str, Any]:
    return {
        "ok": True,
        "service": APP_NAME,
        "mode": "demo" if DEMO else "live",
    }


@app.post("/tv")
async def tv_webhook(req: Request):
    """
    TradingView → Render Webhook 엔드포인트
    body 예:
    {
      "secret": "...",
      "route": "order.reverse",
      "exchange": "bitget",
      "symbol": "BTCUSDT.P",
      "target_side": "BUY",  # or SELL
      "type": "MARKET",      # MARKET만 지원
      "size": 0.001
    }
    """
    body = await req.body()
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid-json"}, status_code=400)

    # 1) 보안
    if payload.get("secret") != WEBHOOK_SECRET:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    route = payload.get("route", "")
    exchange = payload.get("exchange", "").lower()
    tv_symbol = payload.get("symbol", "")
    target_side = payload.get("target_side", "").upper()
    ord_type = payload.get("type", "MARKET").upper()
    size = payload.get("size", 0)

    symbol = normalize_symbol(tv_symbol)

    logger.info(
        "%s [TV] 수신 | %s | %s | %s | size=%.6f",
        MODE_TAG,
        symbol,
        route,
        target_side,
        float(size or 0),
    )

    if exchange != "bitget":
        return JSONResponse({"ok": False, "error": "unsupported-exchange"}, status_code=400)

    if route not in ("order.reverse", "order.open"):
        return JSONResponse({"ok": False, "error": "unsupported-route"}, status_code=400)

    # 오픈/클로즈 맵
    try:
        sides = side_map_for_oneway(target_side)
    except ValueError as ve:
        return JSONResponse({"ok": False, "error": str(ve)}, status_code=400)

    side_open = sides["open"]
    side_close = sides["close"]

    client_oid = f"tv-{int(os.times().elapsed*1000)}"

    # === 1) 우선 '오픈' 시도 =================================================
    try:
        _bg.place_order(
            tv_symbol=symbol,                # BitgetClient는 tv_symbol로 받음(내부정규화)
            side=side_open,                  # buy/sell
            order_type=ord_type.lower(),     # market/limit (여기선 market)
            size=size,
            reduce_only=False,
            client_oid=client_oid,
        )
        return JSONResponse({"ok": True, "action": "open", "side": side_open})

    except requests.HTTPError as e1:
        # 원웨이에서 반대 포지션 보유시 발생하는 400172(side mismatch) 등 400번
        status = getattr(e1, "response", None).status_code if getattr(e1, "response", None) else None
        msg = ""
        try:
            msg = e1.response.text  # 서버가 내려준 본문
        except Exception:
            msg = str(e1)

        # 400이 아니고, 본문에도 side mismatch 없으면 그냥 에러 리턴
        if status != 400 and "side mismatch" not in msg:
            logger.error("Exception in /tv (open): %s", str(e1))
            return JSONResponse({"ok": False, "error": "open-failed", "detail": str(e1)}, status_code=500)

        # === 2) 강제 reduceOnly CLOSE 후 재오픈 ==============================
        try:
            logger.info("side mismatch on OPEN -> force CLOSE first: %s (size=%s)", side_close, size)
            _bg.place_order(
                tv_symbol=symbol,
                side=side_close,              # 반대 방향
                order_type=ord_type.lower(),
                size=size,
                reduce_only=True,             # 강제 청산
                client_oid=f"{client_oid}-close",
            )
        except Exception as e_close:
            # 강제청산 실패해도 이후 오픈 재시도는 해 본다
            logger.error("force close failed (continue to OPEN): %s", str(e_close))

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
            logger.error("Exception in /tv (re-open): %s", str(e2))
            return JSONResponse({"ok": False, "error": "reopen-failed", "detail": str(e2)}, status_code=500)

    except Exception as e1:
        logger.error("Exception in /tv (open/unknown): %s", str(e1))
        return JSONResponse({"ok": False, "error": "open-failed", "detail": str(e1)}, status_code=500)


# ===== Uvicorn Entrypoint ====================================================
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "10000"))
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=False)
