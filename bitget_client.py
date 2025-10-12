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
    Bitget Mix (UMCBL) REST client (sign-type=2, HMAC).

    - hedge-first 전송(서버에서 사용)
    - 22002/400172는 INFO 레벨 로깅
    - get_hedge_sizes(): 현재 롱/숏 수량 동시 조회
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
        m = method.upper()
        url = self.BASE_URL + path
        ts = self._timestamp_ms()

        # query (ordered)
        query = ""
        if m == "GET" and params:
            ordered: List[Tuple[str, Any]] = [(k, params[k]) for k in sorted(params.keys())]
            query = "?" + urlencode(ordered)
            url = url + query

        # body
        raw_body = ""
        if m != "GET" and body:
            raw_body = json.dumps(body, separators=(",", ":"), ensure_ascii=False)

        # sign
        sign = self._sign(ts, m, path + query, raw_body)

        headers = {
            "ACCESS-KEY": self.api_key,
            "ACCESS-PASSPHRASE": self.passphrase,
            "ACCESS-SIGN-TYPE": self.SIGN_TYPE,
            "ACCESS-TIMESTAMP": ts,
            "ACCESS-SIGN": sign,
            "Content-Type": "application/json",
        }

        resp = self.session.request(
            method=m,
            url=url,
            headers=headers,
            params=None,
            data=raw_body if m != "GET" else None,
            timeout=self.timeout,
        )

        if 200 <= resp.status_code < 300:
            return resp.json()

        # known soft errors -> info
        level = "error"
        payload: Any
        try:
            payload = resp.json()
            code = str(payload.get("code"))
            if code in {"22002", "400172"}:
                level = "info"
        except Exception:
            payload = {"raw": resp.text}

        log_fn = self.log.info if level == "info" else self.log.error
        log_fn("Bitget HTTP %s %s -> %s | url=%s | body=%s", m, path, resp.status_code, resp.url, raw_body or "")
        log_fn("Bitget response: %s", payload)

        resp.raise_for_status()
        return {}

    # ---------------- positions ---------------- #

    def get_hedge_sizes(self, symbol: str) -> Dict[str, float]:
        """
        현재 심볼의 롱/숏 총 수량 조회 (hedge 모드 기준).
        return {"long": float, "short": float}
        """
        path = "/api/mix/v1/position/singlePosition"
        params = {"symbol": symbol, "marginCoin": self.margin_coin}
        res = self._request("GET", path, params=params)
        data = res.get("data") or {}
        total = data.get("total", {}) or {}
        long_qty = float(total.get("longTotalSize", 0) or 0)
        short_qty = float(total.get("shortTotalSize", 0) or 0)
        return {"long": long_qty, "short": short_qty}

    # ---- hedge helpers ----
    @staticmethod
    def _map_side_for_hedge(logical_side: str, reduce_only: bool) -> str:
        s = logical_side.lower()
        if not reduce_only:
            return "open_long" if s == "buy" else "open_short"
        return "close_short" if s == "buy" else "close_long"

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
        side: str,               # "buy" | "sell"
        order_type: str,         # "market" | "limit"
        size: str,
        reduce_only: bool = False,
        client_oid: Optional[str] = None,
        price: Optional[str] = None,
        time_in_force: Optional[str] = None,
    ) -> Dict[str, Any]:
        side_first = self._map_side_for_hedge(side, reduce_only)
        return self._send_place_order(
            tv_symbol=tv_symbol,
            side=side_first,
            order_type=order_type,
            size=size,
            reduce_only=reduce_only,
            client_oid=client_oid,
            price=price,
            time_in_force=time_in_force,
        )

    # -------- EA helpers -------- #

    def open_long(self, symbol: str, size: str, order_type: str = "market") -> Dict[str, Any]:
        return self.place_order(tv_symbol=symbol, side="buy",  order_type=order_type, size=size,
                                reduce_only=False, client_oid=f"tv-{int(time.time()*1000)}-open-l")

    def open_short(self, symbol: str, size: str, order_type: str = "market") -> Dict[str, Any]:
        return self.place_order(tv_symbol=symbol, side="sell", order_type=order_type, size=size,
                                reduce_only=False, client_oid=f"tv-{int(time.time()*1000)}-open-s")

    def close_long(self, symbol: str, size: str, order_type: str = "market") -> Dict[str, Any]:
        return self.place_order(tv_symbol=symbol, side="sell", order_type=order_type, size=size,
                                reduce_only=True, client_oid=f"tv-{int(time.time()*1000)}-close-l")

    def close_short(self, symbol: str, size: str, order_type: str = "market") -> Dict[str, Any]:
        return self.place_order(tv_symbol=symbol, side="buy",  order_type=order_type, size=size,
                                reduce_only=True, client_oid=f"tv-{int(time.time()*1000)}-close-s")
