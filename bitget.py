# server.py
from __future__ import annotations

import os
import logging
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from bitget import BitgetClient

# ---------- 설정/모드 ----------
APP_NAME = "siu-autotrade-gui"
BASE_DIR = Path(__file__).resolve().parent

TRADE_MODE = os.getenv("TRADE_MODE", "demo").lower()
if os.getenv("DEMO") is not None:
    DEMO = os.getenv("DEMO", "false").lower() in ["1", "true", "yes", "on"]
else:
    DEMO = (TRADE_MODE != "live")

MODE_TAG = "[DEMO]" if DEMO else "[LIVE]"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "YOUR_WEBHOOK_SECRET")

BITGET_API_KEY = os.getenv("BITGET_API_KEY", "")
BITGET_API_SECRET = os.getenv("BITGET_API_SECRET", "")
BITGET_PASSPHRASE = os.getenv("BITGET_PASSPHRASE", "")

# ---------- 로거 ----------
logger = logging.getLogger(APP_NAME)
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
handler.setFormatter(formatter)
if not logger.handlers:
    logger.addHandler(handler)

# ---------- Bitget client ----------
bg_client = BitgetClient(
    api_key=BITGET_API_KEY,
    api_secret=BITGET_API_SECRET,
    passphrase=BITGET_PASSPHRASE,
    demo=DEMO,
)

# ---------- 유틸 ----------
def normalize_symbol(sym: str) -> str:
    """
    TradingView : BTCUSDT.P  → Bitget : BTCUSDT_UMCBL
    이미 *_UMCBL / *_DMCBL 이면 그대로 사용.
    """
    s = sym.upper().strip()
    if s.endswith("_UMCBL") or s.endswith("_DMCBL"):
        return s
    if s.endswith(".P"):
        return s.replace(".P", "_UMCBL")
    # 기타 케이스도 기본 UMCBL
    return f"{s}_UMCBL"

# ---------- FastAPI ----------
app = FastAPI(title=APP_NAME)

@app.get("/")
def root():
    return {"ok": True, "service": APP_NAME, "mode": "live" if not DEMO else "demo"}

@app.post("/tv")
async def tv_webhook(req: Request):
    """
    TradingView → (json) → /tv
      {
        "secret": "...",
        "route": "order.reverse",
        "exchange": "bitget",
        "symbol": "BTCUSDT.P",
        "target_side": "BUY" | "SELL",
        "type": "MARKET",
        "size": 0.001,
        "client_oid": "optional"
      }
    """
    raw = await req.body()
    try:
        data = await req.json()
    except Exception:
        logger.error(f"{MODE_TAG} [TV] Invalid JSON RAW: {raw!r}")
        return JSONResponse({"ok": False, "reason": "invalid-json"}, status_code=400)

    # 0) secret 체크
    if data.get("secret") != WEBHOOK_SECRET:
        logger.warning(f"{MODE_TAG} [TV] invalid secret")
        return JSONResponse({"ok": False, "reason": "invalid-secret"}, status_code=403)

    route = str(data.get("route", ""))
    exchange = str(data.get("exchange", "bitget")).lower()
    symbol_in = str(data.get("symbol", ""))
    side = str(data.get("target_side", "")).upper()
    typ = str(data.get("type", "MARKET")).upper()
    size = float(data.get("size", 0))
    client_oid = data.get("client_oid")

    if exchange != "bitget":
        return JSONResponse({"ok": False, "reason": "unsupported-exchange"}, status_code=400)
    if typ != "MARKET":
        return JSONResponse({"ok": False, "reason": "only-market-supported"}, status_code=400)
    if side not in ("BUY", "SELL"):
        return JSONResponse({"ok": False, "reason": "invalid-side"}, status_code=400)
    if size <= 0:
        return JSONResponse({"ok": False, "reason": "invalid-size"}, status_code=400)

    symbol = normalize_symbol(symbol_in)

    logger.info(f"{MODE_TAG} [TV] 수신 | {symbol} | {route} | {side} | size={size}")

    # 1) 포지션 조회 (★로깅 강화)
    try:
        pos = bg_client.get_net_position(symbol)
        net = float(pos.get("net", 0.0))
        logger.info(f"{MODE_TAG} 현재 순포지션 | {symbol} | net={net}")
    except Exception as e:
        # Bitget 에러 본문까지 찍히게 bitget.py에서 던지도록 변경되어 있음
        logger.exception(f"{MODE_TAG} [TV] 포지션 조회 실패 | {symbol} | err={e}")
        return JSONResponse({"ok": False, "reason": f"position-fetch-failed: {e}"}, status_code=500)

    # 2) 리버스 로직
    if route == "order.reverse":
        # (a) 같은 방향이면 무시
        if (net > 0 and side == "BUY") or (net < 0 and side == "SELL"):
            logger.info(f"{MODE_TAG} state=same-direction-skip | net={net} | side={side}")
            return {"ok": True, "state": "same-direction-skip"}

        # (b) 반대/플랫 → 먼저 청산, 그다음 신규 진입
        try:
            if abs(net) > 0:
                # 현재 보유 수량을 reduce_only로 청산
                close_side = "BUY" if net < 0 else "SELL"  # 숏보유면 BUY로 close_short, 롱보유면 SELL로 close_long
                logger.info(f"{MODE_TAG} close_position {symbol} {close_side} reduce_only size={abs(net)}")
                bg_client.place_order(
                    symbol=symbol,
                    side=close_side,
                    size=abs(net),
                    order_type="MARKET",
                    reduce_only=True,
                    client_oid=(f"tv-close-{client_oid}" if client_oid else None),
                )

            # 신규 진입
            logger.info(f"{MODE_TAG} place_order {symbol} {side} MARKET size={size}")
            bg_client.place_order(
                symbol=symbol,
                side=side,
                size=size,
                order_type="MARKET",
                reduce_only=False,
                client_oid=client_oid,
            )
            return {"ok": True, "state": "reverse"}
        except Exception as e:
            logger.exception(f"{MODE_TAG} [TV] 주문 오류 | {symbol} | err={e}")
            return JSONResponse({"ok": False, "reason": f"order-failed: {e}"}, status_code=500)

    # 기타 라우트는 아직 미지원
    return JSONResponse({"ok": False, "reason": "unsupported-route"}, status_code=400)
