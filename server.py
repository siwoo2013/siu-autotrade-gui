import os
import json
import math
import time
import traceback
from typing import Dict, Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn

from bitget_client import BitgetClient, BitgetHTTPError

SERVICE_NAME = "siu-autotrade-gui"

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "your-strong-secret").strip()
TRADE_MODE = os.getenv("TRADE_MODE", "live").strip().lower()  # "live" | "paper"
TP_PERCENT = float(os.getenv("TP_PERCENT", "7"))  # 체결 후 TP % (수익률 기준)

app = FastAPI()

# -------------------------------
# Bitget Client
# -------------------------------
bg = BitgetClient(
    api_key=os.getenv("BITGET_API_KEY", ""),
    api_secret=os.getenv("BITGET_API_SECRET", ""),
    passphrase=os.getenv("BITGET_PASSPHRASE", ""),
    mode=TRADE_MODE,
)

# -------------------------------
# Symbol mapping
# -------------------------------
TV_SYMBOL_MAP = {
    # 필요한 심볼은 계속 추가
    "BTCUSDT.P": "BTCUSDT_UMCBL",
    "ETHUSDT.P": "ETHUSDT_UMCBL",
    "BTCUSD.P": "BTCUSD_UMCBL",
    "ETHUSD.P": "ETHUSD_UMCBL",
}

def normalize_tv_symbol(raw_symbol: str) -> str:
    """
    TradingView에서 오는 심볼을 Bitget REST 심볼로 변환.
    - 공백/프리픽스 제거 (예: BINANCE:BTCUSDT.P → BTCUSDT.P)
    - 사전에 있으면 매핑 사용
    - .P 로 끝나면 _UMCBL 로 강제 변환(안전장치)
    """
    s = (raw_symbol or "").strip().upper()
    # 프리픽스 제거 (BINANCE:BTCUSDT.P -> BTCUSDT.P)
    if ":" in s:
        s = s.split(":", 1)[1]

    if s in TV_SYMBOL_MAP:
        return TV_SYMBOL_MAP[s]

    if s.endswith(".P"):
        base = s[:-2]  # ".P" 제거
        return f"{base}_UMCBL"

    # 이미 UMCBL로 온다면 그대로 사용
    if s.endswith("_UMCBL") or s.endswith("_DMCBL") or s.endswith("_CMCBL"):
        return s

    # fallback: 그대로(로그에서 확인용)
    return s


# -------------------------------
# Helpers
# -------------------------------
def ok(data: Dict[str, Any]) -> JSONResponse:
    return JSONResponse({"ok": True, **data})

def err(msg: str, **extra) -> JSONResponse:
    return JSONResponse({"ok": False, "error": msg, **extra})

def side_to_hedge_flag(side: str) -> str:
    """
    내부 표준 방향 → Bitget hedge 방향 문자열
    - "LONG"  → "long"
    - "SHORT" → "short"
    """
    s = side.strip().upper()
    if s == "LONG":
        return "long"
    if s == "SHORT":
        return "short"
    return s.lower()


# -------------------------------
# Ensure close-all for a hedge side
# -------------------------------
async def ensure_close_full(symbol: str, hedge_side: str) -> Dict[str, Any]:
    """
    헷지모드 기준 전량청산:
      - 현재 포지션 크기 조회
      - 남은 수량이 있으면 reduceOnly Market으로 한 번에 닫기
    """
    hflag = side_to_hedge_flag(hedge_side)
    # 재시도(네트워크/서명에러 방어)
    last_exc = None
    for i in range(1, 5):
        try:
            sizes = bg.get_hedge_sizes(symbol)  # {"long": 0.0, "short": 0.0}
            break
        except Exception as e:
            last_exc = e
            app.logger.info(f"ensure_close_full retry #{i} | {symbol} sizes fetch failed: {e}")
            time.sleep(0.25)
    else:
        raise last_exc or RuntimeError("sizes fetch failed")

    long_sz = float(sizes.get("long") or 0.0)
    short_sz = float(sizes.get("short") or 0.0)
    app.logger.info(f"ensure_close_full #1 | {symbol} sizes(long={long_sz:.6f}, short={short_sz:.6f})")

    remain = long_sz if hflag == "long" else short_sz
    if remain <= 0:
        return {"closed": {"skipped": True, "size": 0}}

    # reduceOnly 로 전량 청산
    side_for_close = "sell" if hflag == "long" else "buy"
    try:
        order_id = bg.place_order(
            symbol=symbol,
            side=side_for_close,          # 반대 사이드로 체결
            order_type="market",
            size=remain,
            reduce_only=True,             # 핵심: 청산
            client_oid=f"tv-{int(time.time()*1000)}-close"
        )
        return {"closed": {"skipped": False, "size": remain, "orderId": order_id}}
    except Exception as e:
        app.logger.error(f"EA close failed {symbol} {hflag}: {e}")
        raise


def _tp_price_after_fill(avg_price: float, side: str, tp_percent: float) -> float:
    """
    체결가 기준 TP 목표가 계산 (수익률 % 기준)
     - LONG: avg * (1 + p/100)
     - SHORT: avg * (1 - p/100)
    """
    p = abs(tp_percent) / 100.0
    if side.upper() == "BUY":   # LONG 오픈
        return avg_price * (1.0 + p)
    else:                       # SELL 오픈 → SHORT
        return avg_price * (1.0 - p)


def set_tp_percent(symbol: str, side: str, tp_percent: float, position_size: float) -> Optional[Dict[str, Any]]:
    """
    포지션 진입 후, 현재 평균 진입가 조회 → TP 주문(감시/시장) 등록.
    Bitget는 바로 %지정 불가라 가격 계산해서 넣어야 함.
    """
    try:
        avg = bg.get_avg_entry_price(symbol)
        tp_price = _tp_price_after_fill(avg, side, tp_percent)

        # take-profit은 reduceOnly=True 로, 반대 방향으로 트리거/시장(TP) 등록
        tp_side = "sell" if side.upper() == "BUY" else "buy"
        tp_oid = bg.place_tp_order(
            symbol=symbol,
            side=tp_side,
            trigger_price=tp_price,
            size=position_size
        )
        return {"avg": avg, "tp_price": tp_price, "tp_order_id": tp_oid}
    except Exception as e:
        app.logger.warning(f"set_tp_percent failed: {e}")
        return None


# -------------------------------
# Routes
# -------------------------------
@app.get("/")
def health():
    return {"ok": True, "service": SERVICE_NAME, "mode": TRADE_MODE}

@app.post("/tv")
async def tv(request: Request):
    """
    Webhook 엔드포인트
    JSON payload 예:
    {
      "secret": "your-strong-secret",
      "route": "order.reverse",
      "exchange": "bitget",
      "symbol": "{{ticker}}",               // ex) BTCUSDT.P
      "target_side": "BUY" | "SELL",        // 반대 신호
      "type": "MARKET",
      "size": 0.001
    }
    """
    try:
        body = await request.body()
        data = json.loads(body.decode("utf-8"))
    except Exception:
        return err("invalid-json")

    if str(data.get("secret", "")).strip() != WEBHOOK_SECRET:
        return err("unauthorized")

    route = str(data.get("route", "")).strip()
    exchange = str(data.get("exchange", "bitget")).strip().lower()
    raw_symbol = str(data.get("symbol", "")).strip()

    # ★★ 핵심: TV심볼 → Bitget 심볼 매핑
    symbol = normalize_tv_symbol(raw_symbol)

    target_side = str(data.get("target_side", "")).strip().upper()  # BUY/SELL
    order_type = str(data.get("type", "MARKET")).strip().upper()
    size = float(data.get("size", 0) or 0)

    app.logger.info(f"[LIVE] [TV] 수신 | raw={raw_symbol} -> symbol={symbol} | {route} | {target_side} | size={size:.6f}")

    if exchange != "bitget":
        return err("unsupported-exchange")

    if route != "order.reverse":
        return err("unsupported-route", route=route)

    # ===== EA 스타일: 먼저 반대 포지션 전량 청산 후, 신규 오픈 =====
    # 1) 청산
    close_hedge = "SHORT" if target_side == "BUY" else "LONG"
    try:
        close_res = await ensure_close_full(symbol, close_hedge)
    except Exception as e:
        app.logger.error(f"Exception in /tv reverse(close): {e}")
        return err("reverse-close-failed", detail=str(e))

    # 2) 신규 오픈
    side_for_open = "buy" if target_side == "BUY" else "sell"
    try:
        oid = bg.place_order(
            symbol=symbol,
            side=side_for_open,
            order_type=order_type.lower(),
            size=size,
            reduce_only=False,
            client_oid=f"tv-{int(time.time()*1000)}-open"
        )
    except BitgetHTTPError as e:
        # 종종 400/시그니처/네트워크 오류 → 아주 간단 재시도
        app.logger.warning(f"open order first failed({e}); retrying once…")
        try:
            time.sleep(0.2)
            oid = bg.place_order(
                symbol=symbol,
                side=side_for_open,
                order_type=order_type.lower(),
                size=size,
                reduce_only=False,
                client_oid=f"tv-{int(time.time()*1000)}-open-h"
            )
        except Exception as e2:
            app.logger.error(f"Exception in /tv reverse(open): {e2}")
            return err("reverse-open-failed", detail=str(e2))

    # 3) TP(수익률 %) 등록 (실패해도 주문은 성공이므로 경고만)
    tp_info = set_tp_percent(symbol, target_side, TP_PERCENT, position_size=size)

    return ok({
        "route": route,
        "symbol": symbol,
        "target_side": target_side,
        "size": size,
        "closed": close_res.get("closed", {}),
        "opened": {"orderId": oid},
        "tp": tp_info or {"skipped": True},
    })


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
