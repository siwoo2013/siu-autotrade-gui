import hashlib
import hmac
import json
import time
import base64
from typing import Any, Dict, Optional, Tuple, List
from urllib.parse import urlencode

import requests


class BitgetHTTPError(Exception):
    pass


class BitgetClient:
    """
    Bitget U 本位(UMCBL) 최소 래퍼
    - 서명(Base64)로 수정 → 40009(sign signature error) 해결
    - singlePosition 호출에 productType=umcbl 명시
    - 응답이 dict/list 모두 방어적으로 파싱
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        passphrase: str,
        base_url: str = "https://api.bitget.com",
        timeout: float = 8.0,
        user_agent: str = "siu-autotrade/1.0",
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.base = base_url.rstrip("/")
        self.timeout = timeout

        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent})

    # -------------------------------------------------------------------------
    # 내부 공용
    # -------------------------------------------------------------------------

    def _sign(
        self,
        ts_ms: str,
        method: str,
        path: str,
        params: Dict[str, Any],
        body: Dict[str, Any],
    ) -> str:
        """
        Bitget spec:
          sign = base64( HMAC_SHA256( secret, timestamp + method + path + query + body ) )
          - query 는 '?' + urlencode(params) (없으면 빈 문자열)
          - body 는 공백 없는 JSON 문자열
        """
        query = ""
        if params:
            query = "?" + urlencode(params, doseq=True)

        payload = ""
        if body:
            payload = json.dumps(body, separators=(",", ":"), ensure_ascii=False)

        prehash = ts_ms + method.upper() + path + query + payload
        digest = hmac.new(
            self.api_secret.encode("utf-8"),
            prehash.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        return base64.b64encode(digest).decode()

    def _request(
        self,
        m: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
    ):
        url = self.base + path
        ts = str(int(time.time() * 1000))
        q = params or {}
        b = body or {}

        sign = self._sign(ts, m, path, q, b)
        headers = {
            "Content-Type": "application/json",
            "ACCESS-KEY": self.api_key,
            "ACCESS-SIGN": sign,
            "ACCESS-TIMESTAMP": ts,
            "ACCESS-PASSPHRASE": self.passphrase,
        }

        resp = self.session.request(
            method=m,
            url=url,
            params=q,
            json=b if b else None,
            headers=headers,
            timeout=self.timeout,
        )

        if 200 <= resp.status_code < 300:
            try:
                data = resp.json()
            except Exception:
                raise BitgetHTTPError(
                    f"bitget-http invalid-json status={resp.status_code} body={resp.text[:300]}"
                )
            if isinstance(data, dict) and data.get("code") not in (None, "00000", 0):
                raise BitgetHTTPError(
                    f"bitget-http code={data.get('code')} msg={data.get('msg')}"
                )
            return data.get("data", data)

        try:
            j = resp.json()
            code = j.get("code")
            msg = j.get("msg")
            raise BitgetHTTPError(
                f"bitget-http status={resp.status_code} code={code} msg={msg}"
            )
        except Exception:
            raise BitgetHTTPError(
                f"bitget-http status={resp.status_code} body={resp.text[:300]}"
            )

    # -------------------------------------------------------------------------
    # 조회/포지션
    # -------------------------------------------------------------------------

    def get_hedge_sizes(self, symbol: str) -> Tuple[float, float]:
        path = "/api/mix/v1/position/singlePosition"
        params = {
            "symbol": symbol,
            "marginCoin": "USDT",
            "productType": "umcbl",  # ★ 400 방지
        }
        data = self._request("GET", path, params=params)

        if isinstance(data, dict):
            items: List[Dict[str, Any]] = data.get("data") or data.get("positions") or []
        else:
            items = data or []

        long_sz = 0.0
        short_sz = 0.0

        def _to_f(x) -> float:
            try:
                return float(x)
            except Exception:
                return 0.0

        for it in items:
            hold = (it.get("holdSide") or it.get("side") or "").lower()
            size = _to_f(it.get("total") or it.get("totalSize") or it.get("available") or 0)
            if hold == "long":
                long_sz = size
            elif hold == "short":
                short_sz = size

        return long_sz, short_sz

    # -------------------------------------------------------------------------
    # 주문
    # -------------------------------------------------------------------------

    def place_order(
        self,
        symbol: str,
        side: str,
        size: float,
        order_type: str = "market",
        reduce_only: bool = False,
        client_oid: Optional[str] = None,
    ) -> Dict[str, Any]:
        path = "/api/mix/v1/order/placeOrder"
        body = {
            "symbol": symbol,
            "marginCoin": "USDT",
            "productType": "umcbl",
            "side": side,
            "orderType": order_type,
            "size": f"{size:.6f}",
            "reduceOnly": reduce_only,
        }
        if client_oid:
            body["clientOid"] = client_oid
        return self._request("POST", path, body=body)

    def close_all(self, symbol: str, hold_side: str) -> Dict[str, Any]:
        if hold_side.lower() not in ("long", "short"):
            raise ValueError("hold_side must be 'long' or 'short'")
        path = "/api/mix/v1/position/closePosition"
        body = {
            "symbol": symbol,
            "marginCoin": "USDT",
            "productType": "umcbl",
            "holdSide": hold_side.lower(),
        }
        return self._request("POST", path, body=body)

    # -------------------------------------------------------------------------
    # TP 설정
    # -------------------------------------------------------------------------

    def set_tp_percent(
        self,
        symbol: str,
        hold_side: str,
        percent: float,
        entry_price: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        if entry_price is None:
            path = "/api/mix/v1/position/singlePosition"
            params = {
                "symbol": symbol,
                "marginCoin": "USDT",
                "productType": "umcbl",
            }
            data = self._request("GET", path, params=params)
            avg = None
            items = data.get("data") if isinstance(data, dict) else (data or [])
            for it in items or []:
                hs = (it.get("holdSide") or it.get("side") or "").lower()
                if hs == hold_side.lower():
                    avg = it.get("averageOpenPrice") or it.get("avgPrice") or it.get("openPrice")
                    break
            try:
                entry_price = float(avg) if avg is not None else None
            except Exception:
                entry_price = None

        if not entry_price or entry_price <= 0:
            return None

        if hold_side.lower() == "long":
            trigger_price = entry_price * (1.0 + percent)
        else:
            trigger_price = entry_price * (1.0 - percent)

        path = "/api/mix/v1/plan/placeTPSL"
        body = {
            "symbol": symbol,
            "marginCoin": "USDT",
            "productType": "umcbl",
            "planType": "tp",
            "triggerType": "market_price",
            "triggerPrice": f"{trigger_price:.2f}",
            "holdSide": hold_side.lower(),
        }
        return self._request("POST", path, body=body)
