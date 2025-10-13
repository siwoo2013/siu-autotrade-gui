import os
import json
import traceback
from typing import Dict, Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from bitget_client import BitgetClient, BitgetHTTPError

import logging

log = logging.getLogger("uvicorn.error")

app = FastAPI()

# 환경
WEBHOOK_SECRET = (os.getenv("WEBHOOK_SECRET") or "").strip()
TRADE_MODE = (os.getenv("TRADE_MODE") or "live").lower()  # live / paper(미사용)
BITGET_API_KEY = os.getenv("BITGET_API_KEY") or ""
BITGET_API_SECRET = os.getenv("BITGET_API_SECRET") or ""
BITGET_PASSPHRASE = os.getenv("BITGET_PASSPHRASE") or ""

# Bitget 클라이언트
bg = BitgetClient(
    api_key=BITGET_API_KEY,
    api_secret=BITGET_API_SECRET,
    passphrase=BITGET_PASSPHRASE,
    mode=TRADE_MODE,
    timeout=10,
)

SYMBOL_MAP = {
    # TV 심볼(예: BTCUSDT.P) -> Bitget 심볼(UMCBL)
    "BTCUSDT.P": "BTCUSDT_UMCBL",
    "BTCUSDT": "BTCUSDT_UMCBL",
}


@app.get("/")
def health():
    return {"ok": True, "service": "siu-autotrade-gui", "mode": TRADE_MODE}


def map_symbol(raw_symbol: str) -> str:
    # 예: TV에서 BTCUSDT.P 로 오면 BTCUSDT_UMCBL 로 변환
    if raw_symbol in SYMBOL_MAP:
        return SYMBOL_MAP[raw_symbol]
    # 간단 변환: *.P -> _UMCBL
    if raw_symbol.endswith(".P"):
        base = raw_symbol[:-2]
        return f"{base}_UMCBL"
    return raw_symbol


async def ensure_close_full(symbol: str, side_to_close: str) -> Dict[str, Any]:
    """
    side_to_close: "LONG" or "SHORT"  (닫고 싶은 포지션 방향)
    Bitget UMCBL: long 청산=SELL(reduceOnly), short 청산=BUY(reduceOnly)
    """
    try:
        sizes = bg.get_hedge_sizes(symbol)
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


def compute_tp_price(entry: float, side: str, profit_pct: float) -> float:
    """
    side: "BUY"(롱) / "SELL"(숏) 신규 진입 기준
    profit_pct=0.07 -> 7%
    """
    if entry <= 0:
        return 0.0
    if side.upper() == "BUY":     # 롱이면 위로 7%
        return round(entry * (1 + profit_pct), 2)
    else:                         # 숏이면 아래로 7%
        return round(entry * (1 - profit_pct), 2)


@app.post("/tv")
async def tv(request: Request) -> JSONResponse:
    """
    TradingView Webhook 엔드포인트.
    요청 형태 예)
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
        payload = await request.json()
    except Exception:
        body = await request.body()
        log.error(f"Invalid JSON body: {body[:200]}")
        return JSONResponse({"ok": False, "error": "invalid-json"}, status_code=200)

    try:
        secret = (payload.get("secret") or "").strip()
        if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=200)

        route = (payload.get("route") or "").strip()
        raw_symbol = (payload.get("symbol") or "").strip()
        symbol = map_symbol(raw_symbol)
        target_side = (payload.get("target_side") or "").upper()
        order_type = (payload.get("type") or "MARKET").upper()
        size = float(payload.get("size") or 0)

        log.info(f"[LIVE] [TV] 수신 | raw={raw_symbol} -> symbol={symbol} | {route} | {target_side} | size={size}")

        if (payload.get("exchange") or "").lower() != "bitget":
            return JSONResponse({"ok": False, "error": "exchange-not-supported"}, status_code=200)

        if order_type != "MARKET":
            return JSONResponse({"ok": False, "error": "only-market-supported"}, status_code=200)

        # ---------- 라우트 처리 ----------
if route == "order.open":
    # --- 포지션 오픈 ---
    opened = bg.place_market_order(symbol, target_side, size)
    
    # --- TP 임시 OFF ---
    entry = 0.0
    tp_price = 0.0
    tp_id = None
    # if tp_price > 0:
    #     tp_side = "sell" if target_side == "BUY" else "buy"
    #     tp_id = bg.place_tp_order(symbol=symbol, side=tp_side, trigger_price=tp_price, size=size)
    
    return jsonify({
        "ok": True,
        "opened": opened,
        "tp_price": tp_price
    })


        elif route == "order.reverse":
            # Reverse: 현재 포지션을 반대방향으로 뒤집음
            # 예) BUY reverse -> long으로 가려 함: 먼저 숏 전량청산 -> 롱 오픈
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

            entry = bg.get_avg_entry_price(symbol)
            tp_price = compute_tp_price(entry, target_side, 0.07)
            tp_id = None
            if tp_price > 0:
                tp_side = "sell" if target_side == "BUY" else "buy"
                tp_id = bg.place_tp_order(symbol=symbol, side=tp_side, trigger_price=tp_price, size=size)

            return JSONResponse(
                {
                    "ok": True,
                    "route": route,
                    "closed": close_res,
                    "opened": {"orderId": oid, "side": target_side, "size": size},
                    "tp": {"trigger_price": tp_price, "tp_id": tp_id},
                },
                status_code=200,
            )

        else:
            return JSONResponse({"ok": False, "error": f"unknown-route: {route}"}, status_code=200)

    except BitgetHTTPError as e:
        # Bitget가 4xx/5xx, sign error 등 돌려준 케이스
        log.error(f"BitgetHTTPError: {e}")
        return JSONResponse({"ok": False, "error": "bitget-http", "detail": str(e)}, status_code=200)

    except Exception as e:
        # 어떤 예외라도 200으로 돌려서 500이 안 나가게
        tb = traceback.format_exc()
        log.error(f"Exception in /tv: {e}\n{tb}")
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}", "detail": str(e)}, status_code=200)
