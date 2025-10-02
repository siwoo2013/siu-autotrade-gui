# server.py
import os, uuid
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import JSONResponse
from bitget import BitgetClient

ENV = os.getenv("ENV", "prod")
app = FastAPI()

# ---- Bitget client 준비 ----
API_KEY = os.getenv("BITGET_API_KEY")
API_SECRET = os.getenv("BITGET_API_SECRET")
PASSPHRASE = os.getenv("BITGET_PASSPHRASE")
PRODUCT_TYPE = os.getenv("PRODUCT_TYPE", "umcbl")  # USDT-M Perp 기본
client = BitgetClient(API_KEY, API_SECRET, PASSPHRASE, product_type=PRODUCT_TYPE)

# ---- 라우트들 ----
@app.get("/")
def root():
    return {"ok": True, "service": "tv→bitget", "env": ENV}

@app.get("/status")
def status():
    return {"ok": True, "env": ENV}

@app.get("/positions")
def positions(symbol: str = Query(...)):
    return {"ok": True, "data": client.get_positions(symbol)}

@app.get("/orders/open")
def orders_open(symbol: str = Query(...)):
    return {"ok": True, "data": client.get_open_orders(symbol)}

@app.get("/orders/history")
def orders_history(symbol: str = Query(...), limit: int = 50):
    return {"ok": True, "data": client.get_order_history(symbol, pageSize=limit)}

@app.get("/fills")
def fills(symbol: str = Query(...), limit: int = 50):
    return {"ok": True, "data": client.get_fills(symbol, pageSize=limit)}

@app.post("/tv")
async def tv_webhook(request: Request):
    body = await request.json()
    # ... 주문 처리 로직 ...
    return {"ok": True}
