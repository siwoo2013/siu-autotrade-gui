# bitget_client.py
import time
import hmac
import hashlib
import base64
import json
from typing import Optional, Dict, Any, Tuple
import requests
from urllib.parse import urlencode


class BitgetHTTPError(Exception):
    def __init__(self, status: int, body: Any):
        super().__init__(f"bitget-http status={status} body={body}")
        self.status = status
        self.body = body


class BitgetClient:
    """
    Bitget mix API client for SIU autotrade.
    - FIX: GET/DELETE 서명 시 querystring 포함
    - place_order: holdSide 명시 (open_long/short, close_long/short)
    - __init__ mode 파라미터 호환 (무시)
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
        mode: Optional[str] = None,   # backward-compat only (ignored)
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.PRODUCT_TYPE = product_type
        self.MARGIN_COIN = margin_coin
        self._compat_mode = mode

        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    # -------------- signing --------------

    @staticmethod
    def _build_signed_path(path: str, params: Optional[Dict[str, Any]]) -> str:
        """
        Bitget 서명은 requestPath(+querystring) 이어야 함.
        params가 있으면 키 정렬 후 urlencode 해서 '?a=1&b=2' 붙임.
        """
        request_path = path if path.startswith("/") else f"/{path}"
        if params:
            # Bitget는 key 정렬된 쿼리를 권장
            items = sorted((k, "" if v is None else str(v)) for k, v in params.items())
            query = urlencode(items, doseq=False)
            if query:
                request_path = f"{request_path}?{query}"
        return request_path

    def _sign(self, timestamp: str, method: str, request_path: str, body: str) -> str:
        payload = f"{timestamp}{method.upper()}{request_path}{body or ''}"
        mac = hmac.new(self.api_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256)
        return base64.b64encode(mac.digest()).decode()

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict] = None,
        body: Optional[Dict] = None,
        retries: int = 3,
    ) -> Dict[str, Any]:
        method = method.upper()
        signed_path = self._build_signed_path(path, params)
        url = f"{self.base}{signed_path}"

        body_str = ""
        if body is not None:
            body_str = json.dumps(body, separators=(",", ":"), ensure_ascii=False)

        for attempt in range(1, retries + 1):
            ts = str(int(time.time() * 1000))
            sign = self._sign(ts, method, signed_path, body_str)

            headers = {
                "ACCESS-KEY": self.api_key,
                "ACCESS-SIGN": sign,
                "ACCESS-TIMESTAMP": ts,
                "ACCESS-PASSPHRASE": self.passphrase,
                "Content-Type": "application/json",
            }

            try:
                resp = self.session.request(
                    method=method,
                    url=url,  # 이미 signed_path에 query 포함됨
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

    # -------------- positions --------------

    def get_hedge_sizes(self, symbol: str) -> Dict[str, float]:
        """
        /api/mix/v1/position/singlePosition
        다양한 응답 포맷을 안전하게 파싱 → {'long': float, 'short': float}
        """
        path = "/api/mix/v1/position/singlePosition"
        params = {"symbol": symbol, "marginCoin": self.MARGIN_COIN, "productType": self.PRODUCT_TYPE}
        res = self._request("GET", path, params=params)

        data = res.get("data", res) if isinstance(res, dict) else res
        long_size = 0.0
        short_size = 0.0

        def add(side_val, size_val):
            nonlocal long_size, short_size
            try:
                sz = float(size_val or 0)
            except Exception:
                sz = 0.0
            side = (side_val or "").lower()
            if side in ("long", "open_long", "buy", "open_buy"):
                long_size += sz
            elif side in ("short", "open_short", "sell", "open_sell"):
                short_size += sz

        if isinstance(data, list):
            for it in data:
                add(it.get("side") or it.get("holdSide"), it.get("size") or it.get("position"))
        elif isinstance(data, dict):
            if "total" in data:
                total = data.get("total") or {}
                long_size = float(total.get("longSize", 0) or total.get("long", 0) or 0)
                short_size = float(total.get("shortSize", 0) or total.get("short", 0) or 0)
            elif isinstance(data.get("positions"), list):
                for it in data["positions"]:
                    add(it.get("side") or it.get("holdSide"), it.get("size") or it.get("position"))
            else:
                add(data.get("side") or data.get("holdSide"), data.get("size") or data.get("position"))

        return {"long": float(long_size), "short": float(short_size)}

    # -------------- orders --------------

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
        mix placeOrder
        reduce_only=False: buy->open_long, sell->open_short
        reduce_only=True : buy->close_short, sell->close_long
        """
        s = side.lower().strip()
        if s not in ("buy", "sell"):
            raise ValueError("side must be 'buy' or 'sell'")

        hold_side = (
            "close_long" if (reduce_only and s == "sell") else
            "close_short" if (reduce_only and s == "buy") else
            "open_long" if s == "buy" else
            "open_short"
        )

        path = "/api/mix/v1/order/placeOrder"
        body = {
            "symbol": symbol,
            "productType": self.PRODUCT_TYPE,
            "marginCoin": self.MARGIN_COIN,
            "size": str(size),
            "holdSide": hold_side,
            "side": s,
            "orderType": order_type.lower(),
            "reduceOnly": bool(reduce_only),
        }
        if client_oid:
            body["clientOid"] = client_oid

        res = self._request("POST", path, body=body)
        code = str(res.get("code") or res.get("status") or "")
        if code not in ("00000", "0"):
            raise BitgetHTTPError(400, res)

        data = res.get("data") or {}
        return str(data.get("orderId") or data.get("order_id") or data.get("id") or "")

    def reverse_order(self, symbol: str, target_side: str, size: float) -> str:
        t = target_side.upper()
        if t not in ("BUY", "SELL"):
            raise ValueError("target_side must be BUY or SELL")
        s = "buy" if t == "BUY" else "sell"
        return self.place_order(symbol=symbol, side=s, order_type="market", size=size, reduce_only=False)
