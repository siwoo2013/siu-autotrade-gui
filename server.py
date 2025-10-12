# server.py
# -*- coding: utf-8 -*-

from __future__ import annotations

import os
import re
import json
import time
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

# Render 환경변수
BITGET_API_KEY = os.getenv("BITGET_API_KEY", "")
BITGET_API_SECRET = os.getenv("BITGET_API_SECRET", "")
BITGET_PASSPHRASE = os.getenv("BITGET_PASSPHRASE", "")

# 운영 모드(문자열) – live / demo (서버 표시용)
TRADE_MODE = os.getenv("TRADE_MODE", "demo").lower()
# 예전 호환: DEMO=true/false
if os.getenv("DEMO") is not None:
    DEMO = os.getenv("DEMO", "false").lower() in ["1", "true", "yes", "on"]
else:
    DEMO = (TRADE_MODE != "live")

MODE_TAG = "[DEMO]" if DEMO else "[LIVE]"

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "your-strong-secret").strip()


# ===== 로깅 ==================================================================

logger = logging.getLogger(APP_NAME)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
logger.setLevel(logging.INFO)
logger.addHandler(handler)


# ===== FastAPI ==============================================================

app = FastAPI(title=APP_NAME)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


# ===== 유틸 함수 =============================================================

def safe_json_loads(raw: str) -> Dict[str, Any]:
    """
    TV 메시지 앞에 안내 문구가 붙는 경우가 있어
    첫 '{'부터 끝 '}'까지를 잘라 JSON만 파싱.
    """
    i = raw.find("{")
    j = raw.rfind("}")
    if i >= 0 and j > i:
        raw = raw[i:j + 1]
    return json.loads(raw)


def normalize_symbol(sym: str) -> str:
    """
    TradingView에서 넘어오는 심볼을 Bitget REST 심볼로 변환.
    - BTCUSDT.P  -> BTCUSDT_UMCBL (USDT-M Perp)
    - BTCUSDT.P_UMCBL -> BTCUSDT_UMCBL (이미 형식일 수도 있음)
    기타 .P, PERP, _UMCBL 변형을 넉넉히 수용.
    """
    s = sym.strip().upper()
    # 이미 _UMCBL 붙은 경우 정규화만
    if s.endswith("_UMCBL"):
        base = s.replace(".P", "").replace("_UMCBL", "")
        return f"{base}_UMCBL"

    # .P, .PERP 등 제거 후 UMCBL 부착
    base = re.sub(r"(\.P(ERP)?)$", "", s)
    base = base.replace("_UMCBL", "")
    return f"{base}_UMCBL"


def now_ms() -> int:
    return int(time.time() * 1000)


# ===== Bitget 클라이언트 =====================================================

# 생성자에 demo/trade_mode 인자 전달하지 않습니다(최근 버전 기준).
_bg = BitgetClient(
    api_key=BITGET_API_KEY,
    api_secret=BITGET_API_SECRET,
    passphrase=BITGET_PASSPHRASE,
    product_type="umcbl",      # USDT-M perpetual
    logger=logger,
)


# ===== 헬스 체크 =============================================================

@app.get("/")
async def root():
    return {"ok": True, "service": APP_NAME, "mode": "demo" if DEMO else "live"}


# ===== TV Webhook ============================================================

@app.post("/tv")
async def tv_webhook(request: Request) -> Response:
    raw = await request.body()
    body_text = raw.decode("utf-8", errors="ignore").strip()

    try:
        data = safe_json_loads(body_text)
    except Exception:
        logger.error("Invalid JSON RAW: %s", body_text)
        return JSONResponse({"ok": False, "error": "invalid-json"}, status_code=400)

    # 시크릿 검사
    if data.get("secret", "").strip() != WEBHOOK_SECRET:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    route = str(data.get("route", "")).strip()          # "order.reverse"
    exchange = str(data.get("exchange", "")).strip()     # "bitget"
    symbol_in = str(data.get("symbol", "")).strip()      # e.g. "BTCUSDT.P", "BTCUSDT.P_UMCBL"
    target_side = str(data.get("target_side", "")).upper().strip()  # "BUY"/"SELL"
    ord_type = str(data.get("type", "MARKET")).upper().strip()
    size = data.get("size", 0)

    # 숫자 size 보정
    try:
        size = float(size)
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid-size"}, status_code=400)

    symbol = normalize_symbol(symbol_in)

    logger.info(
        "%s [TV] 수신 | %s | %s | %s | size=%.6f",
        MODE_TAG, symbol, route, target_side, size
    )

    if exchange.lower() != "bitget":
        return JSONResponse({"ok": False, "error": "unsupported-exchange"}, status_code=400)

    if route != "order.reverse":
        return JSONResponse({"ok": False, "error": "unsupported-route"}, status_code=400)

    # client_oid (선택)
    client_oid = f"tv-{now_ms()}"

    # 원웨이 모드 전제:
    #  - reverse: 들어온 side(예: BUY)면 기존 반대 포지션(SELL)을 닫고 -> 요청 side 방향으로 포지션 잡기
    #  - 구현: 'side mismatch(400172)' 가 날 때 reduceOnly close 후 다시 open

    side_open = "buy" if target_side == "BUY" else "sell"
    opposite_side = "sell" if side_open == "buy" else "buy"

    try:
        # 1) 우선 정방향으로 오픈 시도
        _bg.place_order(
            tv_symbol=symbol,            # <- 파라미터명 tv_symbol 로 통일
            side=side_open,              # "buy"/"sell"
            order_type=ord_type.lower(), # "market"
            size=size,
            reduce_only=False,
            client_oid=client_oid,
        )
        return JSONResponse({"ok": True, "action": "open", "side": side_open})

    except Exception as e1:
        # place_order 가 HTTPError(detail 포함)를 올려줌
        msg = str(e1)
        if "400172" not in msg and "side mismatch" not in msg:
            # 다른 오류면 그대로 리턴
            logger.error("Exception in /tv (open): %s", msg)
            return JSONResponse({"ok": False, "error": "open-failed", "detail": msg}, status_code=500)

        # 2) side mismatch → 강제 reduceOnly close 먼저
        try:
            logger.info("side mismatch on OPEN -> force CLOSE first: %s (size=%s)", opposite_side, size)
            _bg.place_order(
                tv_symbol=symbol,
                side=opposite_side,
                order_type=ord_type.lower(),
                size=size,
                reduce_only=True,
                client_oid=f"{client_oid}-close",
            )
        except Exception as e_close:
            logger.error("force close failed (continue to OPEN): %s", str(e_close))

        # 3) close 이후 다시 오픈 시도
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
                "ok": True, "action": "force-close-then-open",
                "close_side": opposite_side, "open_side": side_open
            })
        except Exception as e2:
            logger.error("Exception in /tv (re-open): %s", str(e2))
            return JSONResponse({
                "ok": False, "error": "reopen-failed", "detail": str(e2)
            }, status_code=500)
