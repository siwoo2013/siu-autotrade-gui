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
    Minimal Bitget Mix (UMCBL) REST client (sign-type=2).

    - product_type: "umcbl" (USDT-M perpetual)
    - margin_coin : "USDT"
    - One-way logic input: side = "buy" | "sell"
      * If Bitget account is actually HEDGE mode, we auto-map:
        - reduce_only=False: buy -> open_long,  sell -> open_short
        - reduce_only=True : buy -> close_short, sell -> close_long
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
        sign-type=2 signature:
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
        Keep the exact same query-string (order & encoding) for both:
        - signature input (path + '?' + query)
        - real HTTP request URL
        """
        method_u = method.upper()
        url = self.BASE_URL + path
        ts = self._timestamp_ms()

        # ---- Build deterministic query string (sorted by key) ----
        query = ""
        if method_u == "GET" and params:
            ordered: List[Tuple[str, Any]] = [(k, params[k]) for k in sorted(params.keys())]
            query = "?" + urlencode(ordered)
            url = url + query  # use the same exact string in the real request URL

        raw_body = ""
        if method_u != "GET" and body:
            raw_body = json.dumps(body, separators=(",", ":"), ensure_ascii=False)

        sign = self._sign(ts, method_u, path + query, raw_body)

        headers = {
            "ACCESS-KEY": self.api_key,
            "ACCESS-PASSPHRASE": self.passphrase,
            "ACCESS-SIGN-TYPE": self.SIGN_TYPE,
            "ACCESS-TIMESTAMP": ts,
            "ACCESS-SIGN": sign,
            "Content-Type": "application/json",
        }

        resp = self.session.request(
            method=method_u,
            url=url,                 # params는 이미 URL에 포함 (순서 보존)
            headers=headers,
            params=None,
            data=raw_body if method_u != "GET" else None,
            timeout=self.timeout,
        )

        if not (200 <= resp.status_code < 300):
            try:
                detail = resp.json()
            except Exception:
                detail = {"raw": resp.text}
            self.log.error(
                "Bitget HTTP %s %s -> %s | url=%s | body=%s",
                method_u, path, resp.status_code, resp.url, raw_body if raw_body else ""
            )
            self.log.error("Bitget response: %s", detail)
            resp.raise_for_status()

        return resp.json()

    # ---------------- public APIs ---------------- #

    def get_net_position(self, symbol: str) -> Dict[str, float]:
        """
        Return {'net': float}  (one-way 기준: longQty - shortQty)

        Safe strategy:
        1) Try singlePosition with (symbol, marginCoin) ONLY
        2) If 4xx, fallback to allPosition(productType) and filter by symbol
        """
        # 1) Primary: singlePosition(symbol, marginCoin)
        path = "/api/mix/v1/position/singlePosition"
        params = {
            "symbol": symbol,
            "marginCoin": self.margin_coin,
            # NOTE: productType intentionally omitted here (causes 400 in some regions)
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
            # 2) Fallback: allPosition(productType), then filter the symbol
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

    # ---- hedge helpers ----
    @staticmethod
    def _map_side_for_hedge(logical_side: str, reduce_only: bool) -> str:
        """
        Convert logical side (buy/sell + reduce_only) to hedge side string.
        """
        s = logical_side.lower()
        if not reduce_only:
            return "open_long" if s == "buy" else "open_short"
        # reduce_only=True: reduce opposite leg
        return "close_short" if s == "buy" else "close_long"

    # core sender used by place_order (with optional override side)
    def _send_place_order(
        self,
        *,
        tv_symbol: str,
        side: str,
        order_type: str,
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
            "side": side,
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
    One-way 전용: buy/sell만 사용.
    (이 버전은 hedge 재시도를 하지 않습니다.)
    """
    try:
        return self._send_place_order(
            tv_symbol=tv_symbol,
            side=side,                     # ← buy / sell 그대로
            order_type=order_type,
            size=size,
            reduce_only=reduce_only,
            client_oid=client_oid,
            price=price,
            time_in_force=time_in_force,
        )
    except requests.HTTPError as e:
        # 디버깅 보조용 로그만 남기고 그대로 올림
        try:
            j = e.response.json()
            self.log.error("place_order failed(one-way): code=%s msg=%s", j.get("code"), j.get("msg"))
        except Exception:
            pass
        raise