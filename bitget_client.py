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
    Bitget Mix (UMCBL) REST client (sign-type=2, HMAC).
    - _request: 일시 오류 재시도(지수 백오프)
    - get_hedge_sizes: data가 dict/list 모두 올바르게 파싱
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

    # ---------------- positions ---------------- #

    def get_hedge_sizes(self, symbol: str) -> Dict[str, float]:
        """
        현재 심볼의 롱/숏 총 수량 조회 (hedge 모드 기준).
        Bitget가 data를 dict 또는 list 형태로 줄 수 있어 둘 다 파싱한다.

        return {"long": float, "short": float}
        """
        path = "/api/mix/v1/position/singlePosition"
        params = {"symbol": symbol, "marginCoin": self.margin_coin}
        res = self._request("GET", path, params=params)

        data = res.get("data", {})
        long_qty = 0.0
        short_qty = 0.0

        # ① data가 dict이고 data.total 안에 합계가 들어오는 경우
        if isinstance(data, dict):
            total = data.get("total", {}) or {}
            if isinstance(total, dict):
                long_qty = float(total.get("longTotalSize", 0) or 0)
                short_qty = float(total.get("shortTotalSize", 0) or 0)
            else:
                # dict인데 total이 없으면 포지션 0으로 간주
                long_qty = 0.0
                short_qty = 0.0

        # ② data가 list(레그별 객체)로 오는 경우
        elif isinstance(data, list):
            for p in data:
                if not isinstance(p, dict):
                    continue
                side = (p.get("holdSide") or p.get("side") or "").lower()
                # 가능한 수량 키들 중 하나를 사용
                size = (
                    p.get("total", None)
                    or p.get("totalSize", None)
                    or p.get("available", None)
                    or p.get("availableSize", None)
                    or 0
                )
                try:
                    fsize = float(size or 0)
                except Exception:
                    fsize = 0.0

                if side.startswith("long"):
                    long_qty += fsize
                elif side.startswith("short"):
                    short_qty += fsize

        return {"long": float(long_qty), "short": float(short_qty)}

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
