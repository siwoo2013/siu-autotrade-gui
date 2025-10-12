# bitget_client.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Any, Dict, Optional, List, Tuple
from urllib.parse import urlencode

import requests


class BitgetClient:
    """
    Bitget Mix (UMCBL) REST client (sign-type=2), One-way 전용 (side: buy/sell).
    - product_type: "umcbl" (USDT-M perpetual)
    - margin_coin : "USDT"
    """

    BASE_URL = "https://api.bitget.com"
    SIGN_TYPE = "2"  # HMAC-SHA256 + base64

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        passphrase: str,
        *,
        product_type: str = "umcbl",
        margin_coin: str = "USDT",
        timeout: int = 10,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        if not api_key or not api_secret or not passphrase:
            raise ValueError("Bitget keys missing")

        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.product_type = product_type
        self.margin_coin = margin_coin
        self.timeout = timeout

        self.session = requests.Session()
        self.log = logger or logging.getLogger("bitget")

    # ---------------- internal helpers ---------------- #

    def _timestamp_ms(self) -> str:
        return str(int(time.time() * 1000))

    def _sign(self, ts: str, method: str, path_with_query: str, body: str) -> str:
        """
        sign-type=2:
        base64( HMAC_SHA256(secret, ts + UPPER(method) + path_with_query + body) )
        """
        msg = (ts + method.upper() + path_with_query + body).encode("utf-8")
        digest = hmac.new(self.api_secret.encode("utf-8"), msg, hashlib.sha256).digest()
        return base64.b64encode(digest).decode("utf-8")

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        쿼리 문자열의 '순서'까지 서명에 포함되므로,
        서명에 사용한 query 를 실제 요청 URL에도 '그대로' 사용한다.
        """
        m = method.upper()
        url = self.BASE_URL + path
        ts = self._timestamp_ms()

        # 1) 정렬된 쿼리 문자열 생성 (order 고정)
        query = ""
        if m == "GET" and params:
            ordered: List[Tuple[str, Any]] = [(k, params[k]) for k in sorted(params.keys())]
            query = "?" + urlencode(ordered)
            url = url + query  # 실제 요청 URL에도 동일 문자열 사용

        # 2) body 직렬화
        raw_body = ""
        if m != "GET" and body:
            raw_body = json.dumps(body, separators=(",", ":"), ensure_ascii=False)

        # 3) 서명
        sign = self._sign(ts, m, path + query, raw_body)

        headers = {
            "ACCESS-KEY": self.api_key,
            "ACCESS-PASSPHRASE": self.passphrase,
            "ACCESS-SIGN-TYPE": self.SIGN_TYPE,
            "ACCESS-TIMESTAMP": ts,
            "ACCESS-SIGN": sign,
            "Content-Type": "application/json",
        }

        # 4) 요청 (params 사용하지 않음: 순서가 깨질 수 있음)
        resp = self.session.request(
            method=m,
            url=url,
            headers=headers,
            params=None,
            data=raw_body if m != "GET" else None,
            timeout=self.timeout,
        )

        if not (200 <= resp.status_code < 300):
            try:
                detail = resp.json()
            except Exception:
                detail = {"raw": resp.text}
            self.log.error(
                "Bitget HTTP %s %s -> %s | url=%s | body=%s",
                m, path, resp.status_code, resp.url, raw_body if raw_body else ""
            )
            self.log.error("Bitget response: %s", detail)
            resp.raise_for_status()

        return resp.json()

    # ---------------- public APIs ---------------- #

    def get_net_position(self, symbol: str) -> Dict[str, float]:
        """
        {'net': float} 반환 (one-way 기준: longQty - shortQty)
        1) singlePosition(symbol, marginCoin) 시도
        2) 4xx면 allPosition(productType)로 폴백 후 심볼 필터
        """
        # primary
        path = "/api/mix/v1/position/singlePosition"
        params = {
            "symbol": symbol,
            "marginCoin": self.margin_coin,  # productType은 일부 리전에서 400 유발
        }
        try:
            res = self._request("GET", path, params=params)
            data = res.get("data") or {}
            total = data.get("total", {}) or {}
            long_qty = float(total.get("longTotalSize", 0) or 0)
            short_qty = float(total.get("shortTotalSize", 0) or 0)
            net = long_qty - short_qty
            return {"net": net}
        except requests.HTTPError:
            # fallback
            path2 = "/api/mix/v1/position/allPosition"
            params2 = {"productType": self.product_type}
            res2 = self._request("GET", path2, params=params2)
            net = 0.0
            for item in (res2.get("data") or []):
                if item.get("symbol") == symbol:
                    long_qty = float(item.get("longTotalSize", 0) or 0)
                    short_qty = float(item.get("shortTotalSize", 0) or 0)
                    net = long_qty - short_qty
                    break
            return {"net": net}

    def _send_place_order(
        self,
        *,
        tv_symbol: str,
        side: str,            # "buy" | "sell"
        order_type: str,      # "market" | "limit"
        size: str,
        reduce_only: bool,
        client_oid: Optional[str],
        price: Optional[str],
        time_in_force: Optional[str],
    ) -> Dict[str, Any]:
        path = "/api/mix/v1/order/placeOrder"
        body: Dict[str, Any] = {
            "symbol": tv_symbol,
            "marginCoin": self.margin_coin,
            "productType": self.product_type,
            "side": side,                            # 원웨이: buy/sell
            "orderType": order_type.lower(),
            "size": str(size),
            "reduceOnly": bool(reduce_only),
        }
        if client_oid:
            body["clientOid"] = client_oid
        if price and order_type.lower() == "limit":
            body["price"] = str(price)
        if time_in_force:
            body["timeInForceValue"] = time_in_force
        return self._request("POST", path, body=body)

    def place_order(
        self,
        *,
        tv_symbol: str,
        side: str,               # "buy" | "sell" (one-way)
        order_type: str,         # "market" | "limit"
        size: str,
        reduce_only: bool = False,
        client_oid: Optional[str] = None,
        price: Optional[str] = None,
        time_in_force: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        One-way 전용 주문. hedge 재시도 없음.
        """
        return self._send_place_order(
            tv_symbol=tv_symbol,
            side=side,
            order_type=order_type,
            size=size,
            reduce_only=reduce_only,
            client_oid=client_oid,
            price=price,
            time_in_force=time_in_force,
        )
