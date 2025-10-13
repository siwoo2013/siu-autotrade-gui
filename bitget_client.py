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


class BitgetClient:
    """
    Bitget Mix (UMCBL) REST client (sign-type=2, HMAC base64).
    - _request: 일시 오류 재시도(지수 백오프)
    - get_hedge_sizes / get_hedge_detail / get_last_price 제공
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
        *,
        max_retry: int = 4,
    ) -> Dict[str, Any]:
        """
        네트워크/일시 오류에 대해 재시도(백오프)한다.
        성공 2xx면 JSON 반환, 그렇지 않으면 raise_for_status.
        """
        m = method.upper()
        query = ""
        if m == "GET" and params:
            ordered: List[Tuple[str, Any]] = [(k, params[k]) for k in sorted(params.keys())]
            query = "?" + urlencode(ordered)

        url = self.BASE_URL + path + query

        # body
        raw_body = ""
        if m != "GET" and body:
            raw_body = json.dumps(body, separators=(",", ":"), ensure_ascii=False)

        backoff = 0.25  # sec
        last_exc: Optional[Exception] = None

        for attempt in range(1, max_retry + 1):
            ts = self._timestamp_ms()
            sign = self._sign(ts, m, path + query, raw_body)
            headers = {
                "ACCESS-KEY": self.api_key,
                "ACCESS-PASSPHRASE": self.passphrase,
                "ACCESS-SIGN-TYPE": self.SIGN_TYPE,
                "ACCESS-TIMESTAMP": ts,
                "ACCESS-SIGN": sign,
                "Content-Type": "application/json",
            }

            try:
                resp = self.session.request(
                    method=m,
                    url=url,
                    headers=headers,
                    data=raw_body if m != "GET" else None,
                    timeout=self.timeout,
                )
                # 정상
                if 200 <= resp.status_code < 300:
                    return resp.json()

                # 오류 바디 로깅
                level = "error"
                try:
                    payload = resp.json()
                    code = str(payload.get("code"))
                    if code in {"22002", "400172"}:  # not position / side mismatch 등은 info
                        level = "info"
                except Exception:
                    payload = {"raw": resp.text}

                log_fn = self.log.info if level == "info" else self.log.error
                log_fn("Bitget HTTP %s %s -> %s | url=%s | body=%s",
                       m, path, resp.status_code, resp.url, raw_body or "")
                log_fn("Bitget response: %s", payload)

                resp.raise_for_status()

            except (ConnectionError, Timeout, ProtocolError) as e:
                last_exc = e
                self.log.warning("Bitget request retry %s/%s (%s) %s %s",
                                 attempt, max_retry, type(e).__name__, m, path)
                time.sleep(backoff)
                backoff = min(backoff * 2.0, 1.5)
                continue

        # 재시도 실패
        if last_exc:
            raise last_exc
        raise RuntimeError("Bitget request failed without response")

    # ---------------- market ---------------- #

    def get_last_price(self, symbol: str) -> float:
        """현재가(틱커)"""
        res = self._request("GET", "/api/mix/v1/market/ticker", params={"symbol": symbol})
        data = res.get("data", {}) or {}
        # price 키가 region/버전에 따라 다를 수 있어 가능한 후보를 사용
        for k in ("last", "lastPrice", "close", "closePrice"):
            v = data.get(k)
            if v is not None:
                try:
                    return float(v)
                except Exception:
                    pass
        raise RuntimeError(f"ticker parse failed: {data}")

    # ---------------- positions ---------------- #

    def get_hedge_detail(self, symbol: str) -> Dict[str, Dict[str, float]]:
        """
        현재 심볼의 롱/숏 {size, avgPrice} 조회 (hedge 모드 기준).
        return {"long": {"size": float, "avg": float}, "short": {...}}
        """
        path = "/api/mix/v1/position/singlePosition"
        params = {"symbol": symbol, "marginCoin": self.margin_coin}
        res = self._request("GET", path, params=params)

        data = res.get("data", {})
        long_size = short_size = 0.0
        long_avg = short_avg = 0.0

        # dict(total/long/short) 형식
        if isinstance(data, dict):
            long_node = data.get("long") or {}
            short_node = data.get("short") or {}
            try:
                long_size = float(long_node.get("total", 0) or long_node.get("available", 0) or 0)
            except Exception:
                pass
            try:
                short_size = float(short_node.get("total", 0) or short_node.get("available", 0) or 0)
            except Exception:
                pass
            try:
                long_avg = float(long_node.get("averageOpenPrice", 0) or long_node.get("avgOpenPrice", 0) or 0)
            except Exception:
                pass
            try:
                short_avg = float(short_node.get("averageOpenPrice", 0) or short_node.get("avgOpenPrice", 0) or 0)
            except Exception:
                pass

        # list(레그별) 형식
        elif isinstance(data, list):
            for p in data:
                if not isinstance(p, dict):
                    continue
                side = (p.get("holdSide") or p.get("side") or "").lower()
                size = p.get("total") or p.get("totalSize") or p.get("available") or p.get("availableSize") or 0
                avg = p.get("averageOpenPrice") or p.get("avgOpenPrice") or 0
                try:
                    fsize = float(size or 0)
                except Exception:
                    fsize = 0.0
                try:
                    favg = float(avg or 0)
                except Exception:
                    favg = 0.0

                if side.startswith("long"):
                    long_size += fsize
                    long_avg = favg or long_avg
                elif side.startswith("short"):
                    short_size += fsize
                    short_avg = favg or short_avg

        return {"long": {"size": long_size, "avg": long_avg}, "short": {"size": short_size, "avg": short_avg}}

    def get_hedge_sizes(self, symbol: str) -> Dict[str, float]:
        d = self.get_hedge_detail(symbol)
        return {"long": d["long"]["size"], "short": d["short"]["size"]}

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
