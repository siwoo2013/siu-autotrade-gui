# bitget_client.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Any, Dict, Optional

import requests


class BitgetClient:
    """
    Minimal Bitget Mix (UMCBL) REST client (sign-type=2).

    - product_type: "umcbl" (USDT-M futures)
    - margin_coin : "USDT"
    - one-way position mode: use side = "buy" / "sell"
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
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.product_type = product_type
        self.margin_coin = margin_coin
        self.timeout = timeout

        self.session = requests.Session()
        self.log = logger or logging.getLogger("bitget")

        # sanity
        if not all([self.api_key, self.api_secret, self.passphrase]):
            raise ValueError("Bitget keys missing")

    # ---------------- internal helpers ---------------- #

    def _timestamp_ms(self) -> str:
        return str(int(time.time() * 1000))

    def _sign(self, ts: str, method: str, path: str, body: str) -> str:
        """sign-type=2 signature (base64(hmac_sha256(secret, ts+method+path+body)))"""
        message = (ts + method.upper() + path + body).encode("utf-8")
        digest = hmac.new(self.api_secret.encode("utf-8"), message, hashlib.sha256).digest()
        return base64.b64encode(digest).decode("utf-8")

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = self.BASE_URL + path
        ts = self._timestamp_ms()

        # for signature: params serialized into path query (GET)
        query = ""
        if method.upper() == "GET" and params:
            # Bitget signs only the requestPath (with query string)
            items = "&".join([f"{k}={params[k]}" for k in sorted(params.keys())])
            query = "?" + items

        raw_body = json.dumps(body, separators=(",", ":"), ensure_ascii=False) if body else ""
        sign = self._sign(ts, method, path + query, raw_body)

        headers = {
            "ACCESS-KEY": self.api_key,
            "ACCESS-PASSPHRASE": self.passphrase,
            "ACCESS-SIGN-TYPE": self.SIGN_TYPE,
            "ACCESS-TIMESTAMP": ts,
            "ACCESS-SIGN": sign,
            "Content-Type": "application/json",
        }

        resp = self.session.request(
            method=method.upper(),
            url=url,
            headers=headers,
            params=params if method.upper() == "GET" else None,
            data=raw_body if method.upper() != "GET" else None,
            timeout=self.timeout,
        )

        # log non-2xx
        if not (200 <= resp.status_code < 300):
            try:
                detail = resp.json()
            except Exception:
                detail = {"raw": resp.text}
            self.log.error(
                "Bitget HTTP %s %s -> %s | url=%s | body=%s",
                method.upper(),
                path,
                resp.status_code,
                resp.url,
                raw_body if raw_body else "",
            )
            self.log.error("Bitget response: %s", detail)
            resp.raise_for_status()

        data = resp.json()
        return data

    # ---------------- public APIs ---------------- #

    def get_net_position(self, symbol: str) -> Dict[str, float]:
        """
        Return {'net': float}  (one-way 기준: longQty - shortQty)
        """
        path = "/api/mix/v1/position/singlePosition"
        params = {
            "symbol": symbol,
            "marginCoin": self.margin_coin,
            "productType": self.product_type,
        }
        res = self._request("GET", path, params=params)
        # Bitget success: {"code":"00000","msg":"success","data":{...}} or {"data":[]}
        net = 0.0
        try:
            d = res.get("data") or {}
            # one-way mode에서는 holdSide가 "long"/"short"가 아니라 "net"만 올 수 있음
            if isinstance(d, dict):
                long_qty = float(d.get("total", {}).get("longTotalSize", "0") or "0")
                short_qty = float(d.get("total", {}).get("shortTotalSize", "0") or "0")
                net = long_qty - short_qty
        except Exception:
            pass
        return {"net": net}

    def place_order(
        self,
        *,
        tv_symbol: str,
        side: str,               # "buy" | "sell" (one-way)
        order_type: str,         # "market" | "limit"
        size: str,
        reduce_only: bool = False,
        client_oid: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        One-way 모드: side = "buy" / "sell"
        reduce_only=True 는 강제 청산용
        """
        path = "/api/mix/v1/order/placeOrder"
        body = {
            "symbol": tv_symbol,
            "marginCoin": self.margin_coin,
            "productType": self.product_type,
            "side": side,                   # one-way
            "orderType": order_type,
            "size": str(size),
            "reduceOnly": bool(reduce_only),
        }
        if client_oid:
            body["clientOid"] = client_oid

        return self._request("POST", path, body=body)
