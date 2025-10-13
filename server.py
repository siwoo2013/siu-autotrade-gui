import os
import json
import traceback
from typing import Dict, Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from bitget_client import BitgetClient, BitgetHTTPError

import logging

log = logging.getLogger("uvicorn.error")

app = FastAPI()

# ============================ 환경 ============================
WEBHOOK_SECRET = (os.getenv("WEBHOOK_SECRET") or "").strip()
TRADE_MODE = (os.getenv("TRADE_MODE") or "live").lower()  # live / paper(미사용)
BITGET_API_KEY = os.getenv("BITGET_API_KEY") or ""
BITGET_API_SECRET = os.getenv("BITGET_API_SECRET") or ""
BITGET_PASSPHRASE = os.getenv("BITGET_PASSPHRASE") or ""

# Bitget 클라이언트
# (오류나면 bitget_client.py의 __init__ 시그니처를 확인해서 mode/timeout 인자 조정)
bg = BitgetClient(
    api_key=BITGET_API_KEY,
    api_secret=BITGET_API_SECRET,
    passphrase=BITGET_PASSPHRASE,
    mode=TRADE_MODE,
    timeout=10,
)
from fastapi import FastAPI

@app.on_event("startup")
async def _startup():
    try:
        mode = bg.ensure_unilateral_mode()  # ← 계정을 single_hold로 강제 전환/확인
        log.info(f"[startup] ensured unilateral mode -> {mode}")
    except Exception as e:
        log.warning(f"[startup] ensure_unilateral_mode failed: {e}")

@app.get("/mode")
def get_mode():
    try:
        mode = bg.query_position_mode()
        return {"ok": True, "mode": mode}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    
# TV 심볼 → Bitget 심볼 매핑(UMCBL 선물)
SYMBOL_MAP = {
    "BTCUSDT.P": "BTCUSDT_UMCBL",
    "BTCUSDT": "BTCUSDT_UMCBL",
}


# ============================ 유틸 ============================
@app.get("/")
def health():
    return {"ok": True, "service": "siu-autotrade-gui", "mode": TRADE_MODE}


def map_symbol(raw_symbol: str) -> str:
    """예: TV에서 BTCUSDT.P -> BTCUSDT_UMCBL 로 변환"""
    if raw_symbol in SYMBOL_MAP:
        return SYMBOL_MAP[raw_symbol]
    if raw_symbol.endswith(".P"):
        base = raw_symbol[:-2]
        return f"{base}_UMCBL"
    return raw_symbol


async def ensure_close_full(symbol: str, side_to_close: str) -> Dict[str, Any]:
    """
    side_to_close: "LONG" or "SHORT" (닫고 싶은 포지션 방향)
    Bitget UMCBL 규칙: long 청산 = SELL(reduceOnly), short 청산 = BUY(reduceOnly)
    """
    try:
        sizes = bg.get_hedge_sizes(symbol)  # one-way여도 내부에서 합산 처리하도록 구현되어 있어야 함
        long_sz = float(sizes.get("long", 0.0) or 0.0)
        short_sz = float(sizes.get("short", 0.0) or 0.0)
        log.info(f"ensure_close_full | {symbol} sizes(long={long_sz}, short={short_sz})")

        if side_to_close.upper() == "LONG" and long_sz > 0:
            oid = bg.place_order(
                symbol=symbol,
                side="sell",
                order_type="market",
                size=long_sz,
                reduce_only=True,
                client_oid="tv-close-long",
            )
            return {"ok": True, "closed": {"side": "LONG", "size": long_sz, "orderId": oid}}

        if side_to_close.upper() == "SHORT" and short_sz > 0:
            oid = bg.place_order(
                symbol=symbol,
                side="buy",
                order_type="market",
                size=short_sz,
                reduce_only=True,
                client_oid="tv-close-short",
            )
            return {"ok": True, "closed": {"side": "SHORT", "size": short_sz, "orderId": oid}}

        return {"ok": True, "closed": {"skipped": True, "reason": "no position"}}

    except Exception as e:
        log.warning(f"ensure_close_full error: {e}")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ============================ 메인 웹훅 ============================
@app.post("/tv")
async def tv(request: Request) -> JSONResponse:
    """
    TradingView Webhook 엔드포인트.

    요청 예:
    {
      "secret": "your-strong-secret",
      "route": "order.reverse" | "order.open",
      "exchange": "bitget",
      "symbol": "BTCUSDT.P",
      "target_side": "BUY" | "SELL",
      "type": "MARKET",
      "size": 0.001
    }
    """
    try:
        try:
            payload = await request.json()
        except Exception:
            body = await request.body()
            log.error(f"Invalid JSON body: {body[:200]}")
            return JSONResponse({"ok": False, "error": "invalid-json"}, status_code=200)

        # -------- 인증/기본 파라미터 --------
        secret = (payload.get("secret") or "").strip()
        if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=200)

        route = (payload.get("route") or "").strip()
        raw_symbol = (payload.get("symbol") or "").strip()
        symbol = map_symbol(raw_symbol)
        target_side = (payload.get("target_side") or "").upper()
        order_type = (payload.get("type") or "MARKET").upper()
        size = float(payload.get("size") or 0)

        log.info(f"[{TRADE_MODE.upper()}] [TV] 수신 | raw={raw_symbol} -> symbol={symbol} | route={route} | target={target_side} | size={size}")

        if (payload.get("exchange") or "").lower() != "bitget":
            return JSONResponse({"ok": False, "error": "exchange-not-supported"}, status_code=200)

        if order_type != "MARKET":
            return JSONResponse({"ok": False, "error": "only-market-supported"}, status_code=200)

        # ======================= 라우트 처리 =======================
        if route == "order.open":
            # 신규 방향으로 시장가 진입 (원웨이 모드에서는 동일 방향 추가 진입=증액)
            open_side = "buy" if target_side == "BUY" else "sell"
            oid = bg.place_order(
                symbol=symbol,
                side=open_side,
                order_type="market",
                size=size,
                reduce_only=False,
                client_oid=f"tv-open-{target_side.lower()}",
            )

            # ---- TP 임시 OFF: avg/TP 계산 및 예약주문 생략 ----
            # entry = bg.get_avg_entry_price(symbol)
            # tp_price = compute_tp_price(entry, target_side, 0.07)
            # if tp_price > 0:
            #     tp_side = "sell" if target_side == "BUY" else "buy"
            #     tp_id = bg.place_tp_order(symbol=symbol, side=tp_side, trigger_price=tp_price, size=size)

            return JSONResponse(
                {
                    "ok": True,
                    "route": route,
                    "opened": {"orderId": oid, "side": target_side, "size": size},
                    "tp": {"disabled": True},  # 명시적으로 TP 비활성화 표기
                },
                status_code=200,
            )

        elif route == "order.reverse":
            # 현재 반대 포지션 전량청산 후, target_side 방향으로 시장가 오픈
            close_dir = "SHORT" if target_side == "BUY" else "LONG"
            close_res = await ensure_close_full(symbol, close_dir)

            open_side = "buy" if target_side == "BUY" else "sell"
            oid = bg.place_order(
                symbol=symbol,
                side=open_side,
                order_type="market",
                size=size,
                reduce_only=False,
                client_oid=f"tv-rev-open-{target_side.lower()}",
            )

            # ---- TP 임시 OFF: avg/TP 계산 및 예약주문 생략 ----
            # entry = bg.get_avg_entry_price(symbol)
            # tp_price = compute_tp_price(entry, target_side, 0.07)
            # tp_id = None
            # if tp_price > 0:
            #     tp_side = "sell" if target_side == "BUY" else "buy"
            #     tp_id = bg.place_tp_order(symbol=symbol, side=tp_side, trigger_price=tp_price, size=size)

            return JSONResponse(
                {
                    "ok": True,
                    "route": route,
                    "closed": close_res,
                    "opened": {"orderId": oid, "side": target_side, "size": size},
                    "tp": {"disabled": True},
                },
                status_code=200,
            )

        else:
            return JSONResponse({"ok": False, "error": f"unknown-route: {route}"}, status_code=200)

    except BitgetHTTPError as e:
        # Bitget 4xx/5xx, 서명오류 등
        log.error(f"BitgetHTTPError: {e}")
        return JSONResponse({"ok": False, "error": "bitget-http", "detail": str(e)}, status_code=200)

    except Exception as e:
        # 어떤 예외라도 200으로 (TV가 500을 에러로 간주하지 않게)
        tb = traceback.format_exc()
        log.error(f"Exception in /tv: {e}\n{tb}")
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}", "detail": str(e)}, status_code=200)
