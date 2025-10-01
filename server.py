
import os
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from bitget import BitgetClient

app = FastAPI(title="TV Webhook → Bitget Trader")

WEBHOOK_SECRET    = os.getenv("WEBHOOK_SECRET", "")
BITGET_API_KEY    = os.getenv("BITGET_API_KEY", "")
BITGET_API_SECRET = os.getenv("BITGET_API_SECRET", "")
BITGET_PASSPHRASE = os.getenv("BITGET_PASSPHRASE", "")
BITGET_BASE_URL   = os.getenv("BITGET_BASE_URL", "https://api.bitget.com")
PRODUCT_TYPE      = os.getenv("PRODUCT_TYPE", "umcbl")
ENV               = os.getenv("ENV", "prod")

client = BitgetClient(
    api_key=BITGET_API_KEY,
    api_secret=BITGET_API_SECRET,
    passphrase=BITGET_PASSPHRASE,
    product_type=PRODUCT_TYPE,
    base_url=BITGET_BASE_URL
)

@app.get("/")
def root():
    return {"ok": True, "service": "tv→bitget", "env": ENV}

@app.post("/tv")
async def tv_webhook(request: Request):
    """
    TradingView Webhook JSON example:
    {
      "secret":"MYSECRET",
      "symbol":"BTCUSDT",
      "side":"BUY",          # BUY | SELL | FLAT
      "qty": 0.001,          # order size
      "type": "MARKET",      # MARKET | LIMIT
      "price": 62000.0       # LIMIT only
    }
    """
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    if WEBHOOK_SECRET and data.get("secret") != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Bad secret")

    symbol = str(data.get("symbol", "BTCUSDT")).upper()
    side   = str(data.get("side", "BUY")).upper()
    otype  = str(data.get("type", "MARKET")).upper()
    qty    = float(data.get("qty", 0) or 0)
    price  = data.get("price", None)

    if side not in ("BUY","SELL","FLAT"):
        raise HTTPException(status_code=400, detail="side must be BUY|SELL|FLAT")

    if side in ("BUY","SELL") and qty <= 0:
        raise HTTPException(status_code=400, detail="qty must be > 0")

    try:
        if side == "FLAT":
            result = client.close_all_positions(symbol=symbol)
            return JSONResponse({"ok": True, "action":"FLAT", "result": result})

        if otype == "MARKET":
            result = client.place_market_order(symbol=symbol, side=side, size=qty)
        elif otype == "LIMIT":
            if price is None:
                raise HTTPException(status_code=400, detail="price is required for LIMIT")
            result = client.place_limit_order(symbol=symbol, side=side, size=qty, price=float(price))
        else:
            raise HTTPException(status_code=400, detail="Unsupported order type")

        return JSONResponse({"ok": True, "symbol": symbol, "side": side, "type": otype, "result": result})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
from fastapi import Query

@app.get("/status")
def status():
    return {"ok": True, "env": ENV}

@app.get("/positions")
def positions(symbol: str = Query(..., description="e.g. BTCUSDT")):
    try:
        res = client.get_positions(symbol)
        return {"ok": True, "data": res}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/orders/open")
def orders_open(symbol: str = Query(...)):
    try:
        res = client.get_open_orders(symbol)
        return {"ok": True, "data": res}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/orders/history")
def orders_history(symbol: str = Query(...), limit: int = 50):
    try:
        res = client.get_order_history(symbol, pageSize=limit)
        return {"ok": True, "data": res}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/fills")
def fills(symbol: str = Query(...), limit: int = 50):
    try:
        res = client.get_fills(symbol, pageSize=limit)
        return {"ok": True, "data": res}
    except Exception as e:
        return {"ok": False, "error": str(e)}
