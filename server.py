# server.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import re
import json
import logging
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware

from bitget_client import BitgetClient


# ===== 기본 설정 =============================================================
APP_NAME = "siu-autotrade-gui"
BASE_DIR = Path(__file__).resolve().parent

# 모드: live/demo (환경변수 TRADE_MODE 우선)
TRADE_MODE = os.getenv("TRADE_MODE", "demo").lower()
if os.getenv("DEMO") is not None:  # 하위호환
    DEMO = os.getenv("DEMO", "false").lower() in ["1", "true", "yes", "on"]
else:
    DEMO = (TRADE_MODE != "live")

MODE_TAG = "[DEMO]" if DEMO else "[LIVE]"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "your-strong-secret")

# Bitget 키
BITGET_API_KEY = os.getenv("BITGET_API_KEY", "")
BITGET_API_SECRET = os.getenv("BITGET_API_SECRET", "")
BITGET_PASSPHRASE = os.getenv("BITGET_PASSPHRASE", "")
MARGIN_COIN = os.getenv("MARGIN_COIN", "USDT")
PRODUCT_TYPE = os.getenv("PRODUCT_TYPE", "umcbl")  # USDT-M perpetual

# ===== 로거 ================================================================
log = logging.getLogger("app")
log.setLevel(logging.INFO)
h = logging.StreamHandler()
h.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
log.addHandler(h)

# ===== FastAPI ==============================================================
app = FastAPI(title=APP_NAME)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ===== Bitget 클라이언트 ====================================================
_bg = BitgetClient(
    api_key=BITGET_API_KEY,
    api_secret=BITGET_API_SECRET,
    passphrase=BITGET_PASSPHRASE,
    demo=DEMO,
    margin_coin=MARGIN_COIN,
    product_type=PRODUCT_TYPE,
)


# ===== 유틸 ==============================================================

def normalize_symbol(sym: str) -> str:
    """
    TradingView 심볼을 Bitget API 심볼로 변환.
    - 예) BTCUSDT.P  → BTCUSDT_UMCBL
    - 이미 _UMCBL 등이 붙어 있으면 그대로 둔다.
    """
    s = sym.strip().upper()
    if "_UMCBL" in s or "_DMCBL" in s or "_CMCBL" in s:
        return s
    # .P, PERP, PERPETUAL 등에서 _UMCBL로 표준화
    s = re.sub(r"\.P$", "_UMCBL", s)
    s = re.sub(r"PERP(ETUAL)?$", "_UMCBL", s)
    if not s.endswith("_UMCBL"):
        # Bitget USDT-M perpetual 기본 접미사
        s = f"{s}_UMCBL"
    return s


def json_ok() -> Dict[str, Any]:
    return {"ok": True, "service": APP_NAME, "mode": "live" if not DEMO else "demo"}


# ===== 라우트 ==============================================================

@app.get("/")
async def health() -> JSONResponse:
    return JSONResponse(json_ok())


@app.get("/favicon.ico")
async def fav() -> Response:
    # 정적 파비콘 있으면 서빙(없어도 200)
    ico = BASE_DIR / "favicon.ico"
    if ico.exists():
        return Response(content=ico.read_bytes(), media_type="image/x-icon")
    return Response(status_code=204)


@app.post("/tv")
async def tv_webhook(req: Request) -> JSONResponse:
    """
    TradingView → Render 웹훅 엔드포인트
    Body 예:
    {
      "secret":"your-strong-secret",
      "route":"order.reverse",
      "exchange":"bitget",
      "symbol":"{{ticker}}",
      "target_side":"BUY" | "SELL",
      "type":"MARKET",
      "size":0.01
    }
    """
    raw = await req.body()
    try:
        body = json.loads(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw)
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)

    # 1) 인증
    secret = str(body.get("secret", ""))
    if secret != WEBHOOK_SECRET:
        return JSONResponse({"ok": False, "error": "Forbidden"}, status_code=403)

    route = str(body.get("route", ""))
    ex = str(body.get("exchange", "bitget")).lower()
    tv_symbol = str(body.get("symbol", ""))
    target_side = str(body.get("target_side", "")).upper()  # BUY/SELL
    ord_type = str(body.get("type", "MARKET")).upper()
    size = float(body.get("size", 0.0) or 0.0)
    client_oid = body.get("client_oid")

    symbol = normalize_symbol(tv_symbol)

    log.info(
        "%s [TV] 수신 | %s | %s | %s | size=%.6f",
        MODE_TAG, symbol, route, target_side, size
    )

    if ex != "bitget":
        return JSONResponse({"ok": False, "error": "only bitget supported"}, status_code=400)
    if route != "order.reverse":
        return JSONResponse({"ok": False, "error": "unknown route"}, status_code=400)
    if target_side not in ("BUY", "SELL"):
        return JSONResponse({"ok": False, "error": "invalid target_side"}, status_code=400)
    if size <= 0:
        return JSONResponse({"ok": False, "error": "invalid size"}, status_code=400)

    # 2) 순포지션 확인
    try:
        pos = _bg.get_net_position(symbol)  # {'net': float}
        net = float(pos.get("net", 0.0))
    except Exception as e:
        log.error("get_net_position failed: %s", e)
        return JSONResponse({"ok": False, "error": "position fetch failed"}, status_code=500)

    # same-direction skip 규칙
    same_dir = (net > 0 and target_side == "BUY") or (net < 0 and target_side == "SELL")
    if same_dir:
        log.info("%s 처리완료 | %s | reverse | state=same-direction-skip", MODE_TAG, symbol)
        return JSONResponse({"ok": True, "state": "same-direction-skip"})

    # 3) 반대 방향이면 (1) 청산(reduceOnly) → (2) 신규 진입 순서로 진행
    try:
        # 3-1) 기존 포지션이 있으면 우선 청산
        if net != 0:
            close_side = "SELL" if net > 0 else "BUY"
            _bg.place_order(
                symbol,
                side=close_side,
                type="MARKET",
                size=size,
                reduce_only=True,
                client_oid=(client_oid or None),
            )
            log.info("%s close_position %s side=%s size=%.6f", MODE_TAG, symbol, close_side, size)

        # 3-2) 신규 진입
        _bg.place_order(
            symbol,
            side=target_side,
            type=ord_type,
            size=size,
            reduce_only=False,
            client_oid=(client_oid or None),
        )
        log.info("%s place_order %s %s %s size=%.6f", MODE_TAG, symbol, target_side, ord_type, size)

    except Exception as e:
        log.exception("Exception in /tv")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    state = "flat->open" if net == 0 else "reverse"
    return JSONResponse({"ok": True, "state": state})
