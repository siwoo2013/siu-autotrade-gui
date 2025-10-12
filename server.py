import os
import time
import uuid
import logging
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from bitget_client import BitgetClient, BitgetHTTPError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("siu")

app = FastAPI()

# ---------------------------------------------------------------------
# 환경변수
# ---------------------------------------------------------------------
API_KEY = os.getenv("BITGET_API_KEY", "")
API_SECRET = os.getenv("BITGET_API_SECRET", "")
API_PASS = os.getenv("BITGET_PASSPHRASE", "")
TRADE_MODE = os.getenv("TRADE_MODE", "live")  # "live" | "paper"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "your-strong-secret")

# TP 설정 (퍼센트), 예: 0.07 => +7%
TP_PERCENT = float(os.getenv("TP_PERCENT", "0.07"))

bg = BitgetClient(API_KEY, API_SECRET, API_PASS)


# ---------------------------------------------------------------------
# 유틸
# ---------------------------------------------------------------------
def ok(**kw):
    return JSONResponse({"ok": True, **kw})


def fail(error: str, **kw):
    return JSONResponse({"ok": False, "error": error, **kw})


async def ensure_close_full(symbol: str, close_side: str) -> Dict[str, Any]:
    """
    close_side: "LONG" 또는 "SHORT" (해당 보유 방향을 전량 청산)
    - 포지션 사이즈 조회 실패시 400이 날 수 있어 재시도 백오프 적용
    """
    close_side = close_side.upper()
    assert close_side in ("LONG", "SHORT")

    # 사이즈 조회
    retry = 0
    while True:
        try:
            long_sz, short_sz = bg.get_hedge_sizes(symbol)
            log.info(f"ensure_close_full #{retry+1} | {symbol} sizes(long={long_sz:.6f}, short={short_sz:.6f})")
            break
        except BitgetHTTPError as e:
            retry += 1
            if retry >= 10:
                log.error(f"get_hedge_sizes failed 10/10 {symbol}: {e}")
                raise
            log.info(f"get_hedge_sizes failed (retrying) #{retry} {symbol}: {e}")
            time.sleep(0.2 + 0.1 * retry)

    if close_side == "LONG" and long_sz > 0:
        return bg.close_all(symbol, "long")
    if close_side == "SHORT" and short_sz > 0:
        return bg.close_all(symbol, "short")
    # 닫을 게 없으면 스킵
    return {"skipped": True, "detail": "No position to close"}


# ---------------------------------------------------------------------
# 라우트
# ---------------------------------------------------------------------
@app.get("/")
async def health():
    return {"ok": True, "service": "siu-autotrade-gui", "mode": TRADE_MODE}


@app.post("/tv")
async def tv(request: Request):
    """
    TradingView → webhook 진입점
    payload 예:
      {
        "secret": "your-strong-secret",
        "route": "order.reverse",    // 또는 "order.open"
        "exchange": "bitget",
        "symbol": "BTCUSDT_UMCBL",
        "target_side": "BUY" | "SELL",
        "type": "MARKET",
        "size": "0.001"
      }
    """
    payload = await request.json()
    if payload.get("secret") != WEBHOOK_SECRET:
        return fail("unauthorized")

    # 공통 파라미터
    route = payload.get("route") or ""
    symbol = payload.get("symbol") or "BTCUSDT_UMCBL"
    tgt_side = (payload.get("target_side") or "").upper()  # BUY/SELL
    otype = (payload.get("type") or "MARKET").lower()
    try:
        size = float(str(payload.get("size", "0.001")))
    except Exception:
        size = 0.001

    log.info(f"[LIVE] [TV] 수신 | raw={payload.get('symbol')} -> symbol={symbol} | {route} | {tgt_side} | size={size:.6f}")

    # ---------------------------------------------
    # order.open : 단순 신규 (헤지모드 유지)
    # ---------------------------------------------
    if route == "order.open":
        try:
            # BUY는 롱 오픈, SELL은 숏 오픈
            side = "buy" if tgt_side == "BUY" else "sell"
            cid = f"tv-{uuid.uuid4().hex[:8]}-open"
            res = bg.place_order(symbol, side=side, size=size, order_type=otype, reduce_only=False, client_oid=cid)
            log.info(f"open: {side} size={size} -> {res}")

            # TP(익절) 7% 등록(주문 체결 직후 포지션 기준)
            hold = "long" if tgt_side == "BUY" else "short"
            try:
                tp_res = bg.set_tp_percent(symbol, hold_side=hold, percent=TP_PERCENT)
                log.info(f"place TP {TP_PERCENT*100:.1f}% {hold}: {tp_res}")
            except BitgetHTTPError as e:
                log.warning(f"TP place skipped: {e}")

            return ok(service="siu-autotrade-gui", mode=TRADE_MODE, opened=res)
        except BitgetHTTPError as e:
            return fail("bitget-http", detail=str(e))

    # ---------------------------------------------
    # order.reverse : 반대 방향으로 전환
    #  - 현재 보유 반대편 전량 청산 → 신규 반대 오픈 → TP등록
    # ---------------------------------------------
    if route == "order.reverse":
        if tgt_side not in ("BUY", "SELL"):
            return fail("bad-request", detail="target_side must be BUY or SELL")
        try:
            # 1) 반대편 청산
            if tgt_side == "BUY":
                close_res = await ensure_close_full(symbol, "SHORT")
            else:
                close_res = await ensure_close_full(symbol, "LONG")
        except BitgetHTTPError as e:
            log.error(f"Exception in /tv reverse: {e}")
            return fail("bitget-http", detail=str(e))

        # 2) 신규 오픈
        try:
            side = "buy" if tgt_side == "BUY" else "sell"
            cid = f"tv-{uuid.uuid4().hex[:8]}-rev-open"
            open_res = bg.place_order(symbol, side=side, size=size, order_type=otype, reduce_only=False, client_oid=cid)
            log.info(f"reverse: open | {side} size={size} -> {open_res}")
        except BitgetHTTPError as e:
            return fail("bitget-http", detail=str(e), closed=close_res)

        # 3) TP 등록 (포지션 기준 7%)
        hold = "long" if tgt_side == "BUY" else "short"
        try:
            tp_res = bg.set_tp_percent(symbol, hold_side=hold, percent=TP_PERCENT)
            log.info(f"place TP {TP_PERCENT*100:.1f}% {hold}: {tp_res}")
        except BitgetHTTPError as e:
            log.warning(f"TP place skipped: {e}")
            tp_res = None

        return ok(service="siu-autotrade-gui", mode=TRADE_MODE, closed=close_res, opened=open_res, tp=tp_res)

    return fail("unknown-route", detail=route)
