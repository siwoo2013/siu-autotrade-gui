# server.py
import os
import time
from typing import Optional, Union

from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel, Field

# === Bitget 연동 함수 ===
# bitget.py에 아래 3개 함수를 이 이름으로 제공한다고 가정
from bitget import (
    get_net_position_size,   # async def get_net_position_size(symbol) -> float (Net 모드: 롱=+, 숏=-, 없음=0)
    place_bitget_order,      # async def place_bitget_order(symbol, side, order_type, size, price=None, reduce_only=False, client_oid=None, note=None) -> str
    close_bitget_position,   # async def close_bitget_position(symbol, side, size="ALL", client_oid=None) -> dict
)

app = FastAPI()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "YOUR_WEBHOOK_SECRET")

# ---------------------------
# 유틸
# ---------------------------
def normalize_symbol(tv_ticker: str) -> str:
    """
    TradingView {{ticker}}를 Bitget 심볼로 정규화.
    예) BINANCE:BTCUSDT.P -> BTCUSDT
    """
    t = tv_ticker.split(":")[-1]
    return t.replace(".P", "").replace(".PERP", "")

# ---------------------------
# 요청 스키마
# ---------------------------
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

# ---------------------------
# 헬스체크(루트 405 방지)
# ---------------------------
@app.api_route("/", methods=["GET", "HEAD"])
def root():
    return {"ok": True, "service": "siu-autotrade-gui"}

# ---------------------------
# 웹훅 엔드포인트
# ---------------------------
@app.post("/tv")
async def tv_webhook(request: Request):
    # 1) JSON 파싱
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    # 2) 스키마 검증
    try:
        p = ReversePayload(**body)
    except Exception as e:
        raise HTTPException(400, f"Schema error: {e}")

    # 3) secret 체크
    if p.secret != WEBHOOK_SECRET:
        raise HTTPException(401, "Bad secret")

    # 4) 심볼 정규화
    symbol = normalize_symbol(p.symbol)
    route = p.route
    cid = p.client_oid or f"tv-{int(time.time()*1000)}"

    # ---------------------------
    # A) 리버스(요청한 핵심 로직)
    # ---------------------------
    if route == "order.reverse":
        if p.target_side not in ("BUY", "SELL"):
            raise HTTPException(400, "target_side must be BUY or SELL")
        if p.size is None:
            raise HTTPException(400, "size is required for reverse")
        if isinstance(p.size, str):
            # reverse는 고정 수량이 원칙이라 "ALL"은 허용하지 않음
            raise HTTPException(400, 'size must be a number (e.g., 0.01), not "ALL"')

        target_side = p.target_side
        order_type = (p.type or "MARKET").upper()
        size = float(p.size)

        # 1) 현재 포지션 조회 (Net 모드 기준: >0 롱, <0 숏, 0 없음)
        try:
            net = await get_net_position_size(symbol)
        except Exception as e:
            raise HTTPException(500, f"position query failed: {e}")

        want_long = (target_side == "BUY")
        is_long = (net > 0)
        is_flat = (net == 0)

        if is_flat:
            # 포지션 없음 → 신규 시장가 진입
            order_id = await place_bitget_order(
                symbol=symbol,
                side=target_side,
                order_type=order_type,
                size=size,
                price=None,
                reduce_only=False,
                client_oid=f"{cid}-open",
                note=p.note or "reverse-open"
            )
            return {"ok": True, "state": "flat->open", "order_id": order_id}

        if is_long == want_long:
            # 같은 방향 신호 → 스킵
            return {"ok": True, "state": "same-direction-skip"}

        # 반대 포지션 → 전량 청산 후, 목표 방향 신규 진입
        try:
            await close_bitget_position(
                symbol=symbol,
                side=("SELL" if is_long else "BUY"),  # 기존 포지션 반대 사이드로 reduce-only 시장가 청산하도록 구현되어 있어야 함
                size="ALL",
                client_oid=f"{cid}-close"
            )
        except Exception as e:
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
                note=p.note or "reverse-open"
            )
        except Exception as e:
            raise HTTPException(500, f"open failed: {e}")

        return {"ok": True, "state": "reverse", "order_id": order_id}

    # ---------------------------
    # B) 선택: 기존 create/close 유지 (원하면 사용)
    # ---------------------------
    if route == "order.create":
        if not (p.size and p.type and (p.note is not None or True)):
            raise HTTPException(400, "missing fields for order.create")
        order_id = await place_bitget_order(
            symbol=symbol,
            side=p.target_side or body.get("side"),  # 호환성: 과거 메시지에 side가 있을 수 있음
            order_type=(p.type or "MARKET").upper(),
            size=(float(p.size) if isinstance(p.size, (int, float, str)) and p.size != "ALL" else p.size),
            price=p.price,
            reduce_only=bool(p.reduce_only),
            client_oid=cid,
            note=p.note
        )
        return {"ok": True, "order_id": order_id}

    if route == "order.close":
        closed = await close_bitget_position(
            symbol=symbol,
            side=body.get("side") or "SELL",
            size=p.size or "ALL",
            client_oid=f"{cid}-close"
        )
        return {"ok": True, "closed": closed}

    # ---------------------------
    # 알 수 없는 라우트
    # ---------------------------
    raise HTTPException(400, f"Unknown route: {route}")
