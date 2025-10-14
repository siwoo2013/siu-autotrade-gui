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
from requests.exceptions import ConnectionError, Timeout
from urllib3.exceptions import ProtocolError


class BitgetHTTPError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"bitget-http status={status} body={body}")
        self.status = status
        self.body = body


class BitgetClient:
    """
    Bitget Mix (UMCBL) REST client
    - Sign: Base64(HMAC-SHA256(timestamp + method + path + body))
    - Robust retry for transient network issues
    - Helpers: ticker, positions(hedge detail), orders
    """

    BASE_URL = "https://api.bitget.com"
    SIGN_TYPE = "2"  # HMAC-SHA256 base64

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

    # --------- internal --------- #
    def _ts(self) -> str:
        return str(int(time.time() * 1000))

    def _sign(self, ts: str, method: str, path_with_qs: str, body: str) -> str:
        msg = (ts + method.upper() + path_with_qs + body).encode("utf-8")
        dig = hmac.new(self.api_secret.encode("utf-8"), msg, hashlib.sha256).digest()
        return base64.b64encode(dig).decode("utf-8")

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        *,
        max_retry: int = 4,
    ) -> Dict[str, Any]:
        m = method.upper()
        params = params or {}
        body = body or {}
        qs = ""
        if m == "GET" and params:
            parts = [(k, params[k]) for k in sorted(params.keys())]
            qs = "?" + urlencode(parts)

        url = self.BASE_URL + path + qs
        body_str = "" if m == "GET" else json.dumps(body, separators=(",", ":"), ensure_ascii=False)

        backoff = 0.25
        last_exc: Optional[Exception] = None
        for _try in range(1, max_retry + 1):
            ts = self._ts()
            sign = self._sign(ts, m, path + qs, body_str)
            headers = {
                "ACCESS-KEY": self.api_key,
                "ACCESS-PASSPHRASE": self.passphrase,
                "ACCESS-SIGN-TYPE": self.SIGN_TYPE,
                "ACCESS-TIMESTAMP": ts,
                "ACCESS-SIGN": sign,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Connection": "close",
            }
            try:
                resp = self.session.request(
                    m,
                    url,
                    headers=headers,
                    data=body_str if m != "GET" else None,
                    timeout=self.timeout,
                )
                if 200 <= resp.status_code < 300:
                    return resp.json()
                try:
                    payload = resp.json()
                except Exception:
                    payload = {"raw": resp.text}
                self.log.error("Bitget HTTP %s %s -> %s | %s", m, path, resp.status_code, payload)
                resp.raise_for_status()
            except (ConnectionError, Timeout, ProtocolError) as e:
                last_exc = e
                self.log.warning("retry %s %s %s: %s", _try, m, path, e)
                time.sleep(backoff)
                backoff = min(backoff * 1.5, 1.2)
                continue

        if last_exc:
            raise last_exc
        raise RuntimeError("Bitget request failed")

    # --------- market --------- #
    def get_last_price(self, symbol: str) -> float:
        res = self._request("GET", "/api/mix/v1/market/ticker", params={"symbol": symbol})
        data = res.get("data", {}) or {}
        for k in ("last", "lastPrice", "close", "closePrice", "markPrice"):
            v = data.get(k)
            if v is not None:
                try:
                    return float(v)
                except Exception:
                    pass
        raise RuntimeError(f"ticker parse failed: {data}")

    # --------- positions (hedge) --------- #
    def get_hedge_detail(self, symbol: str) -> Dict[str, Dict[str, float]]:
        """
        return:
        {
          "long": {"size": float, "avg": float, "margin": float, "pnl": float, "lev": float},
          "short":{"size": float, "avg": float, "margin": float, "pnl": float, "lev": float}
        }
        """
        path = "/api/mix/v1/position/singlePosition"
        params = {"symbol": symbol, "marginCoin": self.margin_coin}
        res = self._request("GET", path, params=params)
        data = res.get("data", {})
        out = {
            "long": {"size": 0.0, "avg": 0.0, "margin": 0.0, "pnl": 0.0, "lev": 0.0},
            "short": {"size": 0.0, "avg": 0.0, "margin": 0.0, "pnl": 0.0, "lev": 0.0},
        }

        def fill(dst: Dict[str, float], node: Dict[str, Any]):
            def fget(keys: List[str], cast=float, default=0.0):
                for k in keys:
                    if k in node and node[k] is not None:
                        try:
                            return cast(node[k])
                        except Exception:
                            pass
                return default

            dst["size"] = fget(["total", "totalSize", "available", "availableSize"])
            dst["avg"] = fget(["averageOpenPrice", "avgOpenPrice"])
            dst["margin"] = fget(["margin", "marginAmount"])
            dst["pnl"] = fget(["unrealizedPL", "unrealizedPnl", "profit", "upl"])
            dst["lev"] = fget(["leverage"], cast=float) or fget(["leverage"], cast=int)

        if isinstance(data, dict):
            l = data.get("long") or {}
            s = data.get("short") or {}
            fill(out["long"], l)
            fill(out["short"], s)
        elif isinstance(data, list):  # some regions return list
            for p in data:
                if not isinstance(p, dict):
                    continue
                side = (p.get("holdSide") or p.get("side") or "").lower()
                node = {}
                # normalize keys
                for k in p.keys():
                    node[k] = p[k]
                if side.startswith("long"):
                    fill(out["long"], node)
                elif side.startswith("short"):
                    fill(out["short"], node)

        return out

    def get_hedge_sizes(self, symbol: str) -> Dict[str, float]:
        d = self.get_hedge_detail(symbol)
        return {"long": d["long"]["size"], "short": d["short"]["size"]}

    # --------- order helpers (hedge-aware sides) --------- #
    @staticmethod
    def _map_side_for_hedge(logical_side: str, reduce_only: bool) -> str:
        s = logical_side.lower()
        if not reduce_only:
            return "open_long" if s == "buy" else "open_short"
        return "close_short" if s == "buy" else "close_long"

    def _place(
        self,
        *,
        tv_symbol: str,
        side: str,
        order_type: str,
        size: str,
        reduce_only: bool,
        client_oid: Optional[str] = None,
        price: Optional[str] = None,
        tif: Optional[str] = None,
    ) -> Dict[str, Any]:
        body = {
            "symbol": tv_symbol,
            "marginCoin": self.margin_coin,
            "productType": self.product_type,
            "side": self._map_side_for_hedge(side, reduce_only),
            "orderType": order_type.lower(),
            "size": str(size),
            "reduceOnly": bool(reduce_only),
        }
        if client_oid:
            body["clientOid"] = client_oid
        if price and body["orderType"] == "limit":
            body["price"] = str(price)
        if tif:
            body["timeInForceValue"] = tif
        return self._request("POST", "/api/mix/v1/order/placeOrder", body=body)

    def place_market_order(self, *, symbol: str, side: str, size: float, reduce_only: bool = False) -> Dict[str, Any]:
        return self._place(
            tv_symbol=symbol,
            side=side,
            order_type="market",
            size=str(size),
            reduce_only=reduce_only,
            client_oid=f"siu-{int(time.time()*1000)}",
        )

    # convenience
    def open_long(self, symbol: str, size: str, order_type: str = "market") -> Dict[str, Any]:
        return self._place(tv_symbol=symbol, side="buy", order_type=order_type, size=size, reduce_only=False)

    def open_short(self, symbol: str, size: str, order_type: str = "market") -> Dict[str, Any]:
        return self._place(tv_symbol=symbol, side="sell", order_type=order_type, size=size, reduce_only=False)

    def close_long(self, symbol: str, size: str, order_type: str = "market") -> Dict[str, Any]:
        return self._place(tv_symbol=symbol, side="sell", order_type=order_type, size=size, reduce_only=True)

    def close_short(self, symbol: str, size: str, order_type: str = "market") -> Dict[str, Any]:
        return self._place(tv_symbol=symbol, side="buy", order_type=order_type, size=size, reduce_only=True)
