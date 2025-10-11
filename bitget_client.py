# bitget_client.py
# -*- coding: utf-8 -*-

from __future__ import annotations

import time
import hmac
import json
import base64
import hashlib
import logging
from typing import Any, Dict, Optional

import requests


log = logging.getLogger("bitget")
BASE_URL = "https://api.bitget.com"


class BitgetClient:
    """
    Bitget U-M(USDT-M) 선물 전용 아주 얇은 래퍼.
    - 서명(ACCESS-SIGN) 생성
    - 단일 포지션 조회(singlePosition)
    - 시장가 주문(placeOrder) / reduce_only 지원
    - (옵션) 전량청산 보조(close_position) — 실패하면 서버에서 reduce_only로 대체
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        passphrase: str,
        *,
        demo: bool = False,
        timeout: int = 10,
    ) -> None:
        self.api_key = api_key.strip()
        self.api_secret = api_secret.strip()
        self.passphrase = passphrase.strip()
        self.demo = bool(demo)
        self.timeout = int(timeout)

        self.session = requests.Session()
        self.session.headers.update(
            {
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )

    # ----- 내부 유틸 ---------------------------------------------------------

    @staticmethod
    def _ts() -> str:
        # Bitget은 초 단위 문자열 타임스탬프 사용
        return str(int(time.time()))

    def _sign(self, ts: str, method: str, path: str, body: str) -> str:
        """
        sign = base64( HMAC_SHA256(secret, ts + method + path + body) )
        """
        msg = f"{ts}{method.upper()}{path}{body}".encode("utf-8")
        secret = self.api_secret.encode("utf-8")
        digest = hmac.new(secret, msg, hashlib.sha256).digest()
        return base64.b64encode(digest).decode()

    def _headers(self, ts: str, sign: str) -> Dict[str, str]:
        return {
            "ACCESS-KEY": self.api_key,
            "ACCESS-SIGN": sign,
            "ACCESS-TIMESTAMP": ts,
            "ACCESS-PASSPHRASE": self.passphrase,
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = f"{BASE_URL}{path}"

        # Bitget 서명은 requestPath(쿼리 포함 X) + body(JSON)
        body_str = json.dumps(body, separators=(",", ":")) if body else ""
        ts = self._ts()
        sign = self._sign(ts, method, path, body_str)

        headers = self._headers(ts, sign)

        try:
            if method.upper() == "GET":
                resp = self.session.get(url, headers=headers, params=params or {}, timeout=self.timeout)
            else:
                # Bitget은 POST 때 body(JSON), 쿼리스트링은 보통 없음
                resp = self.session.post(url, headers=headers, data=body_str or "{}", timeout=self.timeout)

            # 디버그 도움: 실패시 본문 로그
            if not resp.ok:
                try:
                    j = resp.json()
                except Exception:
                    j = {"raw": resp.text}
                log.error(
                    "Bitget HTTP %s %s -> %s | url=%s | body=%s",
                    method.upper(),
                    path,
                    resp.status_code,
                    resp.url,
                    body_str or "",
                )
                log.error("Bitget response: %s", j)
                resp.raise_for_status()

            return resp.json()

        except requests.HTTPError as e:
            # Bitget 에러코드/메시지 노출
            try:
                detail = resp.json()
            except Exception:
                detail = {"raw": getattr(resp, "text", "")}
            raise requests.HTTPError(f"{e} | url={resp.url} | body={body_str} | detail={detail}") from e

    # ----- 외부 API ----------------------------------------------------------

    def get_net_position(self, symbol: str) -> Dict[str, float]:
        """
        단일 심볼 순포지션(롱 +, 숏 -)을 float로 반환.
        Bitget: GET /api/mix/v1/position/singlePosition
        required: symbol, marginCoin, productType
        """
        path = "/api/mix/v1/position/singlePosition"
        params = {
            "symbol": symbol,
            "marginCoin": "USDT",
            "productType": "umcbl",  # **중요**: 누락 시 400 "sign signature error" 유발 사례 있음
        }
        data = self._request("GET", path, params=params)

        # 응답 케이스 방어적으로 처리
        # 보통 {"code":"00000","msg":"success","data":{"openDelegateSize":...,"holdSide":"long/short/none", ...}}
        d = data.get("data") or {}
        hold_side = str(d.get("holdSide", "")).lower()

        # 수량 필드가 케이스마다 다를 수 있어 보수적으로 합산
        size_fields = (
            "total", "totalSize", "available", "availableSize", "holdVolume", "openDelegateSize"
        )
        qty = 0.0
        for k in size_fields:
            try:
                qty = float(d.get(k, 0)) or qty
            except Exception:
                pass

        if qty == 0.0 or hold_side in ("", "none"):
            return {"net": 0.0}

        if hold_side.startswith("long"):
            return {"net": abs(qty)}

        if hold_side.startswith("short"):
            return {"net": -abs(qty)}

        # 혹시 모르는 케이스
        return {"net": 0.0}

    def place_order(
        self,
        symbol: str,
        *,
        side: str,           # "BUY" | "SELL"
        type: str = "MARKET",  # 시장가만 사용
        size: float,
        reduce_only: bool = False,
    ) -> Dict[str, Any]:
        """
        POST /api/mix/v1/order/placeOrder
        """
        path = "/api/mix/v1/order/placeOrder"
        body = {
            "symbol": symbol,
            "marginCoin": "USDT",
            "size": str(size),
            "side": side.upper(),                # BUY / SELL
            "orderType": type.lower(),           # market
            "timeInForceValue": "normal",
            "reduceOnly": bool(reduce_only),
            "productType": "umcbl",
        }
        return self._request("POST", path, body=body)

    def close_position(self, symbol: str, *, side: str = "ALL") -> Dict[str, Any]:
        """
        (보조) 전량청산 시도.
        Bitget에는 여러 청산 엔드포인트가 있고 계정 설정에 따라 다르게 동작함.
        - 실패 시 서버(server.py)에서 reduce_only 시장가 반대 주문으로 fallback 하도록 설계.
        """
        path = "/api/mix/v1/position/closePosition"
        # holdSide: long/short/all
        hold_side = {
            "BUY": "long",
            "SELL": "short",
            "ALL": "all",
        }.get(side.upper(), "all")

        body = {
            "symbol": symbol,
            "marginCoin": "USDT",
            "holdSide": hold_side,
            "productType": "umcbl",
        }
        return self._request("POST", path, body=body)
