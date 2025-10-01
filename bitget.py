
import time
import hmac
import hashlib
import base64
import json
from urllib.parse import urlencode
import requests

class BitgetClient:
    """
    Minimal Bitget USDT-M Perpetual order wrapper.
    product_type:
      - "umcbl" = USDT-M Perp (default)
      - "dmcbl" = Coin-M Perp, etc.
    """
    def __init__(self, api_key, api_secret, passphrase, product_type="umcbl", base_url="https://api.bitget.com"):
        self.api_key = api_key or ""
        self.api_secret = api_secret or ""
        self.passphrase = passphrase or ""
        self.product_type = product_type
        self.base_url = base_url.rstrip("/")

    def _ts(self):
        # Bitget expects ms timestamp string
        return str(int(time.time() * 1000))

    def _sign(self, ts, method, path, query="", body=""):
        text = ts + method.upper() + path + query + body
        h = hmac.new(self.api_secret.encode(), text.encode(), hashlib.sha256).digest()
        return base64.b64encode(h).decode()

    def _headers(self, ts, sign):
        return {
            "Content-Type": "application/json",
            "ACCESS-KEY": self.api_key,
            "ACCESS-SIGN": sign,
            "ACCESS-TIMESTAMP": ts,
            "ACCESS-PASSPHRASE": self.passphrase,
        }

    def _request(self, method, path, params=None, body=None, timeout=20):
        url = self.base_url + path
        query = ""
        if params:
            query = "?" + urlencode(params)
        payload = "" if body is None else (body if isinstance(body, str) else json.dumps(body))
        ts = self._ts()
        sign = self._sign(ts, method, path, query if params else "", payload)
        headers = self._headers(ts, sign)
        resp = requests.request(method, url + (query if params else ""), headers=headers, data=payload, timeout=timeout)
        resp.raise_for_status()
        j = resp.json()
        if str(j.get("code")) not in ("00000","0"):
            raise Exception(f"Bitget error: {j}")
        return j

    def place_market_order(self, symbol, side, size):
        path = "/api/mix/v1/order/placeOrder"
        body = {
            "symbol": symbol,
            "marginCoin": "USDT",
            "side": "open_long" if side == "BUY" else "open_short",
            "orderType": "market",
            "size": str(size),
            "productType": self.product_type
        }
        return self._request("POST", path, body=body)

    def place_limit_order(self, symbol, side, size, price):
        path = "/api/mix/v1/order/placeOrder"
        body = {
            "symbol": symbol,
            "marginCoin": "USDT",
            "side": "open_long" if side == "BUY" else "open_short",
            "orderType": "limit",
            "price": str(price),
            "size": str(size),
            "productType": self.product_type
        }
        return self._request("POST", path, body=body)

    def close_all_positions(self, symbol):
        # Placeholder: implement real close logic by fetching positions.
        return {"note":"Implement actual position close logic for your account."}
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
