# server.py
import os
import time
import json
import logging
from typing import Optional, Union

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import Response  # ← 추가 (favicon 404 방지용)
from pydantic import BaseModel, Field

# ===== Bitget 연동 함수 (bitget.py에 구현되어 있어야 함) =====
from bitget import (
    get_net_position_size,   # async def get_net_position_size(symbol) -> float (Net: 롱=+, 숏=-, 없음=0)
    place_bitget_order,      # async def place_bitget_order(symbol, side, order_type, size, price=None, reduce_only=False, client_oid=None, note=None) -> str
    close_bitget_position,   # async def close_bitget_position(symbol, side, size="ALL", client_oid=None) -> dict
)

# ===== FastAPI =====
app = FastAPI()

# ===== ENV & Logging =====
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "YOUR_WEBHOOK_SECRET")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s",
)

# ===== Utils =====
def normalize_symbol(tv_ticker: str) -> str:
    """
    TradingView {{ticker}}를 Bitget 심볼로 정규화.
    예) BINANCE:BTCUSDT.P -> BTCUSDT
    """
    t = tv_ticker.split(":")[-1]
    return t.replace(".P", "").replace(".PERP", "")

# ===== Payload Model =====
class ReversePayload(BaseModel):
    secret: str
    route: str = Field(..., description="order.reverse | order.create | order.close")
    exchange: str = "bitget"
    symbol: str
    # reverse 용
    target_side: Optional[str] = Field(None, description="BUY or SELL")
    # 공통
    type: Optional[str] = Field("MARKET", description="MARKET or LIMIT")
    size: Optional[Union[float, str]] = Field(None, description='float or "ALL"')
    price: Optional[float] = None
    reduce_only: Optional[bool] = False
    client_oid: Optional[str] = None
    note: Optional[str] = None

# ===== Health (루트 405 방지) =====
@app.api_route("/", methods=["GET", "HEAD"])
def root():
    return {"ok": True, "service": "siu-autotrade-gui"}

# ===== Favicon 404 방지 =====
@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    # 아이콘 파일을 제공하지 않으므로 204로 응답
    return Response(status_code=204)

# ===== Webhook =====
@app.post("/tv")
async def tv_webhook(request: Request):
    # 1) JSON 파싱 (+로그에 원문 일부 남기기)
    try:
        body = await request.json()
    except Exception:
        raw = (await request.body()).decode("utf-8", "ignore")
        logging.error(f"[/tv] Invalid JSON RAW: {raw[:500]}")
        raise HTTPException(400, "Invalid JSON")

    # 2) 스키마 검증
    try:
        p = ReversePayload(**body)
    except Exception as e:
        logging.error(f"[/tv] Schema error: {e}; body={json.dumps(body)[:500]}")
        raise HTTPException(400, f"Schema error: {e}")

    # 3) secret 체크
    if p.secret != WEBHOOK_SECRET:
        logging.warning("[/tv] Bad secret")
        raise HTTPException(401, "Bad secret")

    # 4) 공통 준비
    symbol = normalize_symbol(p.symbol)
    route = p.route
    cid = p.client_oid or f"tv-{int(time.time() * 1000)}"
    side_for_log = (p.target_side or body.get("side") or "-").upper()

    # === 수신 요약 로그: BUY/SELL이 딱 보이게 ===
    logging.info(f"[TV] 수신 | {symbol} | {route} | {side_for_log} | size={p.size}")

    # -------------------------------------------------
    # A) Reverse: 같은방향 스킵 / 포지션없음 신규 / 반대면 청산후 리버스
    # -------------------------------------------------
    if route == "order.reverse":
        if p.target_side not in ("BUY", "SELL"):
            raise HTTPException(400, "target_side must be BUY or SELL")
        if p.size is None:
            raise HTTPException(400, "size is required for reverse")
        if isinstance(p.size, str):
            raise HTTPException(400, 'size must be a number (e.g., 0.01), not "ALL"')

        target_side = p.target_side
        order_type = (p.type or "MARKET").upper()
        size = float(p.size)

        # 현재 포지션 조회 (Net 모드: >0 롱, <0 숏, 0 없음)
        try:
            net = await get_net_position_size(symbol)
        except Exception as e:
            logging.exception("[/tv] position query failed")
            raise HTTPException(500, f"position query failed: {e}")

        want_long = (target_side == "BUY")
        is_long = (net > 0)
        is_flat = (net == 0)

        if is_flat:
            # 포지션 없음 → 신규 진입
            order_id = await place_bitget_order(
                symbol=symbol,
                side=target_side,
                order_type=order_type,
                size=size,
                price=None,
                reduce_only=False,
                client_oid=f"{cid}-open",
                note=p.note or "reverse-open",
            )
            logging.info(f"[TV] 처리완료 | {symbol} | reverse | state=flat->open | oid={order_id}")
            return {"ok": True, "state": "flat->open", "order_id": order_id}

        if is_long == want_long:
            # 같은 방향 → 스킵
            logging.info(f"[TV] 처리완료 | {symbol} | reverse | state=same-direction-skip")
            return {"ok": True, "state": "same-direction-skip"}

        # 반대 포지션 → 전량 청산 후 신규
        try:
            await close_bitget_position(
                symbol=symbol,
                side=("SELL" if is_long else "BUY"),
                size="ALL",
                client_oid=f"{cid}-close",
            )
        except Exception as e:
            logging.exception("[/tv] close failed")
            raise HTTPException(500, f"close failed: {e}")

        try:
            order_id = await place_bitget_order(
                symbol=symbol,
                side=target_side,
                order_type=order_type,
                size=size,
                price=None,
                reduce_only=False,
                client_oid=f"{cid}-open",
                note=p.note or "reverse-open",
            )
        except Exception as e:
            logging.exception("[/tv] open failed")
            raise HTTPException(500, f"open failed: {e}")

        logging.info(f"[TV] 처리완료 | {symbol} | reverse | state=reverse | oid={order_id}")
        return {"ok": True, "state": "reverse", "order_id": order_id}

    # -------------------------------------------------
    # B) (옵션) 기존 create/close 유지하고 싶을 때
    # -------------------------------------------------
    if route == "order.create":
        side = (p.target_side or body.get("side"))
        if not (side and p.size and p.type):
            raise HTTPException(400, "missing fields for order.create (need side, size, type)")
        order_id = await place_bitget_order(
            symbol=symbol,
            side=side,
            order_type=(p.type or "MARKET").upper(),
            size=(float(p.size) if p.size != "ALL" else p.size),
            price=p.price,
            reduce_only=bool(p.reduce_only),
            client_oid=cid,
            note=p.note,
        )
        logging.info(f"[TV] 처리완료 | {symbol} | create | side={side} | oid={order_id}")
        return {"ok": True, "order_id": order_id}

    if route == "order.close":
        side = body.get("side") or "SELL"
        closed = await close_bitget_position(
            symbol=symbol,
            side=side,
            size=p.size or "ALL",
            client_oid=f"{cid}-close",
        )
        logging.info(f"[TV] 처리완료 | {symbol} | close | side={side} | closed={closed}")
        return {"ok": True, "closed": closed}

    # -------------------------------------------------
    # Unknown route
    # -------------------------------------------------
    raise HTTPException(400, f"Unknown route: {route}")
