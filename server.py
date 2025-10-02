import os
import uuid
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import JSONResponse

# 우리 Bitget 래퍼
from bitget import BitgetClient

# ───────────────────────────────────────────────────────────
# 환경 변수
ENV = os.getenv("ENV", "prod")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
API_KEY = os.getenv("BITGET_API_KEY", "")
API_SECRET = os.getenv("BITGET_API_SECRET", "")
PASSPHRASE = os.getenv("BITGET_PASSPHRASE", "")
PRODUCT_TYPE = os.getenv("PRODUCT_TYPE", "umcbl")  # USDT-M Perp 기본
MARGIN_COIN = os.getenv("MARGIN_COIN", "USDT")     # 필요 시 사용

# 필수 키 체크(웹훅만 테스트할 때는 없어도 되지만, 조회/주문 시 필요)
if not all([API_KEY, API_SECRET, PASSPHRASE]):
    print("[WARN] BITGET API KEY/SECRET/PASSPHRASE가 비어 있습니다. 주문/조회 시 오류가 날 수 있습니다.")

# FastAPI 인스턴스
app = FastAPI(title="TV → Bitget Trader", version="1.0.0")

# Bitget 클라이언트
client = BitgetClient(
    api_key=API_KEY,
    api_secret=API_SECRET,
    passphrase=PASSPHRASE,
    product_type=PRODUCT_TYPE,
)

# ───────────────────────────────────────────────────────────
# 기본/헬스체크
@app.get("/")
def root():
    return {"ok": True, "service": "tv→bitget", "env": ENV}

@app.get("/status")
def status():
    return {"ok": True, "env": ENV, "productType": PRODUCT_TYPE}

# ───────────────────────────────────────────────────────────
# 조회 엔드포인트 (주문 들어갔는지/리버스 되었는지 확인용)
@app.get("/positions")
def positions(symbol: str = Query(..., description="예: BTCUSDT")):
    try:
        data = client.get_positions(symbol)
        return {"ok": True, "data": data}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/orders/open")
def orders_open(symbol: str = Query(...)):
    try:
        data = client.get_open_orders(symbol)
        return {"ok": True, "data": data}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/orders/history")
def orders_history(symbol: str = Query(...), limit: int = 50):
    try:
        data = client.get_order_history(symbol, pageSize=limit)
        return {"ok": True, "data": data}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/fills")
def fills(symbol: str = Query(...), limit: int = 50):
    try:
        data = client.get_fills(symbol, pageSize=limit)
        return {"ok": True, "data": data}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ───────────────────────────────────────────────────────────
# 트레이딩뷰 웹훅 수신
# 예시 JSON:
# {
#   "secret":"MYSECRET",
#   "symbol":"BTCUSDT",
#   "side":"BUY" | "SELL" | "FLAT",
#   "qty": 0.001,
#   "type":"MARKET" | "LIMIT",
#   "price": 63000   # LIMIT일 때만
# }
@app.post("/tv")
async def tv_webhook(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    secret = body.get("secret", "")
    if not WEBHOOK_SECRET or secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Bad secret")

    symbol: str = body.get("symbol")
    side: str = (body.get("side") or "").upper()   # BUY / SELL / FLAT
    otype: str = (body.get("type") or "MARKET").upper()  # MARKET / LIMIT
    qty: Optional[float] = body.get("qty")
    price: Optional[float] = body.get("price")

    if not symbol or not side:
        raise HTTPException(status_code=400, detail="symbol/side required")

    # 고유 ID(선택)
    client_oid = f"tv-{uuid.uuid4().hex[:12]}"

    try:
        # 청산(모두 닫기)
        if side == "FLAT":
            result = client.close_all_positions(symbol=symbol)
            return JSONResponse({"ok": True, "action": "FLAT", "symbol": symbol, "result": result})

        # 주문
        if otype == "MARKET":
            if not qty:
                raise HTTPException(status_code=400, detail="qty required for MARKET")
            result = client.place_market_order(symbol=symbol, side=side, size=qty)

        elif otype == "LIMIT":
            if not (qty and price):
                raise HTTPException(status_code=400, detail="qty & price required for LIMIT")
            result = client.place_limit_order(symbol=symbol, side=side, size=qty, price=price)

        else:
            raise HTTPException(status_code=400, detail="type must be MARKET or LIMIT")

        # 응답 반환
        return JSONResponse({
            "ok": True,
            "symbol": symbol,
            "side": side,
            "type": otype,
            "qty": qty,
            "price": price,
            "clientOid": client_oid,
            "result": result
        })

    except HTTPException:
        raise
    except Exception as e:
        # Bitget 오류 등
        raise HTTPException(status_code=500, detail=str(e))
