# bitget_client.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import base64, hashlib, hmac, json, logging, time
from typing import Any, Dict, Optional, List, Tuple
from urllib.parse import urlencode
import requests


class BitgetClient:
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

    # ---------- internals ----------

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

        # deterministic query string (order matters for signature)
        query = ""
        if m == "GET" and params:
            ordered: List[Tuple[str, Any]] = [(k, params[k]) for k in sorted(params.keys())]
            query = "?" + urlencode(ordered)
            url = url + query  # use exactly same query in real request

        raw_body = ""
        if m != "GET" and body:
            raw_body = json.dumps(body, separators=(",", ":"), ensure_ascii=False)

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
            method=m, url=url, headers=headers,
            params=None, data=raw_body if m != "GET" else None,
            timeout=self.timeout,
        )
        if not (200 <= resp.status_code < 300):
            try:
                detail = resp.json()
            except Exception:
                detail = {"raw": resp.text}
            self.log.error("Bitget HTTP %s %s -> %s | url=%s | body=%s",
                           m, path, resp.status_code, resp.url, raw_body if raw_body else "")
            self.log.error("Bitget response: %s", detail)
            resp.raise_for_status()
        return resp.json()

    # ---------- public ----------

    def get_net_position(self, symbol: str) -> Dict[str, float]:
        # primary
        path = "/api/mix/v1/position/singlePosition"
        params = {"symbol": symbol, "marginCoin": self.margin_coin}
        try:
            res = self._request("GET", path, params=params)
            data = (res.get("data") or {})
            total = (data.get("total") or {})
            long_qty = float(total.get("longTotalSize", 0) or 0)
            short_qty = float(total.get("shortTotalSize", 0) or 0)
            return {"net": long_qty - short_qty}
        except requests.HTTPError:
            # fallback
            res2 = self._request("GET", "/api/mix/v1/position/allPosition",
                                 params={"productType": self.product_type})
            net = 0.0
            for item in (res2.get("data") or []):
                if item.get("symbol") == symbol:
                    long_qty = float(item.get("longTotalSize", 0) or 0)
                    short_qty = float(item.get("shortTotalSize", 0) or 0)
                    net = long_qty - short_qty
                    break
            return {"net": net}

    # -- hedge mapping for fallback --
    @staticmethod
    def _map_side_for_hedge(logical_side: str, reduce_only: bool) -> str:
        s = logical_side.lower()
        if not reduce_only:
            return "open_long" if s == "buy" else "open_short"
        return "close_short" if s == "buy" else "close_long"

    def _send_place_order(
        self, *, tv_symbol: str, side: str, order_type: str, size: str,
        reduce_only: bool, client_oid: Optional[str], price: Optional[str],
        time_in_force: Optional[str],
    ) -> Dict[str, Any]:
        path = "/api/mix/v1/order/placeOrder"
        body: Dict[str, Any] = {
            "symbol": tv_symbol,
            "marginCoin": self.margin_coin,
            "productType": self.product_type,
            "side": side,  # buy/sell or mapped hedge keyword
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
        self, *, tv_symbol: str, side: str, order_type: str, size: str,
        reduce_only: bool = False, client_oid: Optional[str] = None,
        price: Optional[str] = None, time_in_force: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Try one-way 'buy'/'sell' first. On 400172(side mismatch) or 40774(unilateral/hedge mismatch),
        retry once with hedge keywords (open_long/open_short/close_long/close_short).
        """
        try:
            return self._send_place_order(
                tv_symbol=tv_symbol, side=side, order_type=order_type, size=size,
                reduce_only=reduce_only, client_oid=client_oid,
                price=price, time_in_force=time_in_force,
            )
        except requests.HTTPError as e:
            try:
                j = e.response.json()
            except Exception:
                j = {}
            code = str(j.get("code", ""))
            msg = str(j.get("msg", "")).lower()
            should_retry = (code in {"400172", "40774"}) or \
                           ("side mismatch" in msg) or \
                           ("unilateral position" in msg and "must also" in msg)
            if not should_retry:
                raise
            hedge_side = self._map_side_for_hedge(side, reduce_only)
            self.log.warning("Retrying with hedge side: %s -> %s (reduceOnly=%s, code=%s)",
                             side, hedge_side, reduce_only, code)
            return self._send_place_order(
                tv_symbol=tv_symbol, side=hedge_side, order_type=order_type, size=size,
                reduce_only=reduce_only, client_oid=(client_oid + "-h") if client_oid else None,
                price=price, time_in_force=time_in_force,
            )
