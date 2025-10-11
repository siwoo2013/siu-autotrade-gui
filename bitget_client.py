# bitget_client.py
# -*- coding: utf-8 -*-

from __future__ import annotations
import time
import json
import hmac
import base64
import hashlib
import logging
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import requests

log = logging.getLogger(__name__)


class BitgetClient:
    def __init__(self, api_key: str, api_secret: str, passphrase: str):
        self.base = "https://api.bitget.com"
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.session = requests.Session()

    # ──────────────────────────────────────────────────────────────────────
    # Bitget 시그니처: ts + method + requestPath(+query) + body(JSON)
    # ts : 밀리초 문자열
    # method : 대문자
    # requestPath : 예) /api/mix/v1/position/singlePosition?symbol=...&...
    # body : POST/PUT면 JSON 문자열(없으면 "")
    # ──────────────────────────────────────────────────────────────────────
    def _sign(self, ts: str, method: str, request_path_with_query: str, body_str: str = "") -> str:
        prehash = f"{ts}{method.upper()}{request_path_with_query}{body_str}"
        digest = hmac.new(self.api_secret.encode(), prehash.encode(), hashlib.sha256).digest()
        return base64.b64encode(digest).decode()

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        timeout: int = 10,
    ) -> Dict[str, Any]:
        # 쿼리스트링 구성 (서명과 URL 둘 다 동일하게 사용)
        query = urlencode(params or {}, doseq=True)
        request_path = f"{path}?{query}" if query else path
        url = f"{self.base}{request_path}"

        # 바디 직렬화 (POST/PUT 때만)
        body_str = json.dumps(body, separators=(",", ":")) if body else ""

        # 밀리초 타임스탬프
        ts = str(int(time.time() * 1000))

        # 사인
        sign = self._sign(ts, method, request_path, body_str)

        headers = {
            "ACCESS-KEY": self.api_key,
            "ACCESS-PASSPHRASE": self.passphrase,
            "ACCESS-TIMESTAMP": ts,
            "ACCESS-SIGN": sign,
            # Bitget 최신 스펙에서 사인 타입 2 사용 권장/요구
            "ACCESS-SIGN-TYPE": "2",
            "Content-Type": "application/json",
        }

        resp = self.session.request(
            method=method.upper(),
            url=url,
            headers=headers,
            data=body_str if body else None,
            timeout=timeout,
        )

        # 로깅(디버그)
        if resp.status_code >= 400:
            try:
                err_json = resp.json()
            except Exception:
                err_json = resp.text
            log.error(
                "Bitget %s %s => %s | headers=%s | body=%s | resp=%s",
                method, url, resp.status_code, {k: headers[k] for k in headers if k != "ACCESS-SIGN"},
                body_str, err_json,
            )
            resp.raise_for_status()

        return resp.json()

    # ──────────────────────────────────────────────────────────────────────
    # 공개/프라이빗 API 래퍼들
    # ──────────────────────────────────────────────────────────────────────
    def get_net_position(self, symbol_umcbl: str, margin_coin: str = "USDT") -> Dict[str, Any]:
        # Bitget 문서 기준 singlePosition 는 productType 필요
        path = "/api/mix/v1/position/singlePosition"
        params = {
            "symbol": symbol_umcbl,
            "marginCoin": margin_coin,
            "productType": "umcbl",
        }
        return self._request("GET", path, params=params)

    def place_market_order(
        self,
        symbol_umcbl: str,
        side: str,           # "buy" or "sell"
        size: float,
        margin_coin: str = "USDT",
        reduce_only: bool = False,
        client_oid: Optional[str] = None,
    ) -> Dict[str, Any]:
        path = "/api/mix/v1/order/placeOrder"
        body = {
            "symbol": symbol_umcbl,
            "marginCoin": margin_coin,
            "size": str(size),
            "side": side.lower(),       # buy / sell
            "orderType": "market",
            "reduceOnly": reduce_only,
            "clientOid": client_oid or f"tv-{int(time.time()*1000)}",
            "productType": "umcbl",
        }
        return self._request("POST", path, body=body)

    def close_all(self, symbol_umcbl: str, side: str, margin_coin: str = "USDT") -> Dict[str, Any]:
        # 필요 시 구현 (예: 전체 청산용)
        path = "/api/mix/v1/position/closePosition"
        body = {
            "symbol": symbol_umcbl,
            "marginCoin": margin_coin,
            "productType": "umcbl",
            "holdSide": side.lower(),   # long / short
        }
        return self._request("POST", path, body=body)
