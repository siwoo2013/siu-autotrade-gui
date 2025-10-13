# bitget_client.py
import time
import hmac
import hashlib
import base64
import json
from typing import Optional, Dict, Any
import requests


class BitgetHTTPError(Exception):
    def __init__(self, status: int, body: Any):
        super().__init__(f"bitget-http status={status} body={body}")
        self.status = status
        self.body = body


class BitgetClient:
    """
    Minimal Bitget mix API client tailored for SIU autotrade usage.
    - Implements proper signing for REST requests
    - place_order() ensures holdSide is present so Bitget doesn't reject direction
    - Accepts legacy 'mode' param for backward compatibility (ignored)
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        passphrase: str,
        base: str = "https://api.bitget.com",
        product_type: str = "umcbl",
        margin_coin: str = "USDT",
        timeout: int = 10,
        # ---- backward compatibility (ignored) ----
        mode: Optional[str] = None,
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.PRODUCT_TYPE = product_type  # e.g. "umcbl" or "umcb" or "p"
        self.MARGIN_COIN = margin_coin  # USDT normally
        self._compat_mode = mode  # kept only for backwards compatibility

        self.session = requests.Session()
        self.session.trust_env = False  # avoid inheriting proxies
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    # ----------------------
    # Signing & low-level
    # ----------------------
    def _sign(self, timestamp: str, method: str, request_path: str, body: str) -> str:
        """
        Bitget HMAC-SHA256 sign: base64( HMAC_SHA256(secret, timestamp + method + requestPath + body) )
        """
        method = method.upper()
        prehash = f"{timestamp}{method}{request_path}{body or ''}"
        h = hmac.new(self.api_secret.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256)
        signature = base64.b64encode(h.digest()).decode()
        return signature

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict] = None,
        body: Optional[Dict] = None,
        retries: int = 3,
    ) -> Dict[str, Any]:
        """
        Low-level request with signing, JSON parse and error mapping.
        Returns parsed JSON (dict), or raises BitgetHTTPError for HTTP / API failures.
        """
        method = method.upper()
        request_path = path if path.startswith("/") else f"/{path}"
        url = f"{self.base}{request_path}"

        body_str = ""
        if body is not None:
            # Bitget expects JSON body string for signing
            body_str = json.dumps(body, separators=(",", ":"), ensure_ascii=False)

        for attempt in range(1, retries + 1):
            timestamp = str(int(time.time() * 1000))
            sign = self._sign(timestamp, method, request_path, body_str)

            headers = {
                "ACCESS-KEY": self.api_key,
                "ACCESS-SIGN": sign,
                "ACCESS-TIMESTAMP": timestamp,
                "ACCESS-PASSPHRASE": self.passphrase,
                "Content-Type": "application/json",
            }

            try:
                resp = self.session.request(
                    method=method,
                    url=url,
                    params=params,
                    data=body_str.encode("utf-8") if body_str else None,
                    headers=headers,
                    timeout=self.timeout,
                )
            except requests.RequestException as e:
                if attempt < retries:
                    time.sleep(0.5 * attempt)
                    continue
                raise BitgetHTTPError(-1, str(e))

            status = resp.status_code
            text = resp.text or ""
            try:
                parsed = resp.json() if text else {}
            except Exception:
                parsed = {"raw_text": text}

            if status != 200:
                raise BitgetHTTPError(status, parsed)

            return parsed

        raise BitgetHTTPError(-1, "request-retries-exhausted")

    # ----------------------
    # Utility: hedge sizes
    # ----------------------
    def get_hedge_sizes(self, symbol: str) -> Dict[str, float]:
        """
        Query position/sizes for the mixed perpetual.
        Returns: {'long': float, 'short': float}
        Handles different 'data' shapes Bitget may return.
        """
        path = "/api/mix/v1/position/singlePosition"
        params = {"symbol": symbol, "marginCoin": self.MARGIN_COIN, "productType": self.PRODUCT_TYPE}

        res = self._request("GET", path, params=params)

        data = res.get("data", res) if isinstance(res, dict) else res

        long_size = 0.0
        short_size = 0.0

        if isinstance(data, list):
            for item in data:
                side = item.get("side") or item.get("holdSide") or ""
                size_val = item.get("size") or item.get("position") or 0
                try:
                    size_f = float(size_val)
                except Exception:
                    size_f = 0.0
                sl = str(side).lower()
                if sl in ("long", "open_long", "buy", "open_buy"):
                    long_size += size_f
                elif sl in ("short", "open_short", "sell", "open_sell"):
                    short_size += size_f
                else:
                    hold = (item.get("holdSide") or "").lower()
                    if "long" in hold:
                        long_size += size_f
                    elif "short" in hold:
                        short_size += size_f

        elif isinstance(data, dict):
            if "total" in data:
                total = data.get("total") or {}
                long_size = float(total.get("longSize", 0) or total.get("long", 0) or 0)
                short_size = float(total.get("shortSize", 0) or total.get("short", 0) or 0)
            elif "positions" in data and isinstance(data["positions"], list):
                for item in data["positions"]:
                    side = item.get("side") or item.get("holdSide") or ""
                    size_val = item.get("size") or item.get("position") or 0
                    try:
                        size_f = float(size_val)
                    except Exception:
                        size_f = 0.0
                    sl = str(side).lower()
                    if sl in ("long", "open_long", "buy"):
                        long_size += size_f
                    elif sl in ("short", "open_short", "sell"):
                        short_size += size_f
            else:
                side = data.get("side") or data.get("holdSide") or ""
                size_val = data.get("size") or data.get("position") or 0
                try:
                    size_f = float(size_val)
                except Exception:
                    size_f = 0.0
                sl = str(side).lower()
                if sl in ("long", "open_long", "buy"):
                    long_size += size_f
                elif sl in ("short", "open_short", "sell"):
                    short_size += size_f

        return {"long": float(long_size), "short": float(short_size)}

    # ----------------------
    # Place order (mix)
    # ----------------------
    def place_order(
        self,
        *,
        symbol: str,
        side: str,                # "buy" or "sell"
        order_type: str = "market",
        size: float,
        reduce_only: bool = False,
        client_oid: Optional[str] = None,
    ) -> str:
        """
        Place an order on mix market with explicit holdSide to avoid 'direction empty' error.

        - reduce_only=False (entry):
            side="buy"  -> holdSide = "open_long"
            side="sell" -> holdSide = "open_short"
        - reduce_only=True (close):
            side="buy"  -> holdSide = "close_short"
            side="sell" -> holdSide = "close_long"
        """
        s = side.lower().strip()
        if s not in ("buy", "sell"):
            raise ValueError("side must be 'buy' or 'sell'")

        if reduce_only:
            hold_side = "close_long" if s == "sell" else "close_short"
        else:
            hold_side = "open_long" if s == "buy" else "open_short"

        path = "/api/mix/v1/order/placeOrder"
        body = {
            "symbol": symbol,
            "productType": self.PRODUCT_TYPE,
            "marginCoin": self.MARGIN_COIN,
            "size": str(size),
            "holdSide": hold_side,          # 핵심
            "side": s,                      # 보조
            "orderType": order_type.lower(),
            "reduceOnly": True if reduce_only else False,
        }
        if client_oid:
            body["clientOid"] = client_oid

        res = self._request("POST", path, body=body)
        code = str(res.get("code") or res.get("status") or "")
        if code not in ("00000", "0"):
            raise BitgetHTTPError(400, res)

        data = res.get("data") or {}
        order_id = data.get("orderId") or data.get("order_id") or data.get("id") or ""
        return str(order_id)

    def reverse_order(self, symbol: str, target_side: str, size: float) -> str:
        """
        Convenience for 'order.reverse'.
        target_side: "BUY" or "SELL"
        """
        t = str(target_side).upper()
        if t not in ("BUY", "SELL"):
            raise ValueError("target_side must be BUY or SELL")
        side = "buy" if t == "BUY" else "sell"
        return self.place_order(symbol=symbol, side=side, order_type="market", size=size, reduce_only=False)
