# server.py
import os
import time
import json
import logging
from typing import Optional, Union
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

# ===== Bitget 연동 함수 (bitget.py에 구현) =====
from bitget import (
    get_net_position_size,   # async def get_net_position_size(symbol) -> float
    place_bitget_order,      # async def place_bitget_order(...)
    close_bitget_position,   # async def close_bitget_position(...)
)

# ===== FastAPI =====
app = FastAPI()

# ===== 경로 & 로깅/환경 =====
BASE_DIR = Path(__file__).resolve().parent

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "YOUR_WEBHOOK_SECRET")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s",
)

# ===== 유틸 =====
def normalize_symbol(tv_ticker: str) -> str:
    """
    TradingView {{ticker}} -> Bitget 심볼 정규화
    예) BINANCE:BTCUSDT.P -> BTCUSDT
    """
    t = tv_ticker.split(":")[-1]
    return t.replace(".P", "").replace(".PERP", "")

# ===== 페이로드 모델 =====
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

# ===== 헬스체크 =====
@app.api_route("/", methods=["GET", "HEAD"])
def root():
    return {"ok": True, "service": "siu-autotrade-gui"}

# ===== Favicon 서빙 (루트/ 또는 static/ 에서 읽기) =====
@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    # 우선 루트 경로 favicon.ico
    ico_path = BASE_DIR / "favicon.ico"
    # 없으면 static/favicon.ico도 시도
    if not ico_path.exists():
        ico_path = BASE_DIR / "static" / "favicon.ico"
    if ico_path.exists():
        resp = FileResponse(ico_path, media_type="image/x-icon")
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp
    # 파일이 없으면 204 No Content로
    return Response(status_code=204)

# ===== Webhook 엔드포인트 =====
@app.post("/tv")
async def tv_webhook(request: Request):
    # 1) JSON 파싱
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

    # 3) 시크릿 체크
    if p.secret != WEBHOOK_SECRET:
        logging.warning("[/tv] Bad secret")
        raise HTTPException(401, "Bad secret")

    # 4) 공통 준비
    symbol = normalize_symbol(p.symbol)
    route = p.route
    cid = p.client_oid or f"tv-{int(time.time() * 1000)}"
    side_for_log = (p.target_side or body.get("side") or "-").upper()

    # 수신 로그 요약
    logging.info(f"[TV] 수신 | {symbol} | {route} | {side_for_log} | size={p.size}")

    # -------------------------------------------------
    # A) Reverse: 같은방향 스킵 / 포지션없음 신규 / 반대면 청산 후 리버스
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

        # 현재 순포지션 조회 (Net: >0 롱, <0 숏, 0 없음)
        try:
            net = await get_net_position_size(symbol)
        except Exception as e:
            logging.exception("[/tv] position query failed")
            raise HTTPException(500, f"position query failed: {e}")

        want_long = (target_side == "BUY")
        is_long = (net > 0)
        is_flat = (net == 0)

        if is_flat:
            # 신규 진입
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
    # B) (옵션) order.create / order.close
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

    # 알 수 없는 route
    raise HTTPException(400, f"Unknown route: {route}")
