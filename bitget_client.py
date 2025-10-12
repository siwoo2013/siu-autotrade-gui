# bitget_client.py
# -*- coding: utf-8 -*-

from __future__ import annotations

import time
import hmac
import hashlib
import base64
import json
import logging
from typing import Any, Dict, Optional

import requests


log = logging.getLogger(__name__)
log.setLevel(logging.INFO)


class BitgetClient:
    """
    Bitget Mix-Futures REST v1 간단 래퍼

    - 서명 방식: signType=2 (HMAC SHA256, Base64)
    - 기본 엔드포인트: https://api.bitget.com
    - 필수 파라미터:
        * api_key, api_secret, passphrase
        * product_type: "umcbl" (USDT-M), "dmcbl" (USDC-M) 등
    """

    BASE_URL = "https://api.bitget.com"
    TIMEOUT = 10
    MARGIN_COIN = "USDT"

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        passphrase: str,
        product_type: str = "umcbl",
        demo: bool | None = None,
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.product_type = (product_type or "umcbl").lower()

        self.session = requests.Session()
        # 고정 헤더(동적 ACCESS-* 헤더는 요청 시마다 최신값으로 넣습니다)
        self.session.headers.update(
            {
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )

        # 서버 시간-로컬 시간 차이를 저장(밀리초)
        self._delta_ms = 0
        self._sync_time_once()

    # ---------------------------------------------------------------------
    # 내부 유틸
    # ---------------------------------------------------------------------
    def _now_ms(self) -> int:
        return int(time.time() * 1000) + self._delta_ms

    def _sync_time_once(self) -> None:
        """
        Bitget 공식 "서버시간" 퍼블릭 엔드포인트가 공개 문서에 없어서,
        실패해도 치명적이지 않게 처리합니다. (요청 시각은 로컬 기준 사용)
        """
        try:
            # 문서에따라 다른 경로가 있을 수 있으므로 예외 안전 처리
            # 성공하면 delta를 조정합니다.
            url = f"{self.BASE_URL}/api/spot/v1/public/time"
            resp = self.session.get(url, timeout=self.TIMEOUT)
            if resp.ok:
                j = resp.json()
                # spot time 응답 예: {"code":"00000","msg":"success","requestTime":..., "data": 17123... }
                sv = j.get("data")
                if isinstance(sv, int):
                    self._delta_ms = sv - int(time.time() * 1000)
                    log.info("Bitget time synced. delta_ms=%s", self._delta_ms)
        except Exception as e:
            log.warning("Bitget time sync failed (use local time): %s", e)

    def _sign_v2(self, ts_ms: int, method: str, path: str, body_or_query: str) -> str:
        """
        signType=2: Base64(HMAC_SHA256(secret, prehash))
        prehash = f"{ts}{method}{path}{body_or_query}"
        """
        prehash = f"{ts_ms}{method.upper()}{path}{body_or_query}"
        mac = hmac.new(self.api_secret.encode(), prehash.encode(), hashlib.sha256).digest()
        return base64.b64encode(mac).decode()

    def _headers(self, ts_ms: int, signature: str) -> Dict[str, str]:
        """
        Bitget 사양의 ACCESS-* 헤더 세팅
        """
        return {
            "ACCESS-KEY": self.api_key,
            "ACCESS-PASSPHRASE": self.passphrase,
            "ACCESS-SIGN": signature,
            "ACCESS-TIMESTAMP": str(ts_ms),
            "ACCESS-SIGN-TYPE": "2",  # HMAC-SHA256 Base64
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _err_raise(self, resp: requests.Response, extra_body: Optional[Dict[str, Any]] = None) -> None:
        """
        에러 응답 로깅 + HTTPError 발생
        """
        body_str = "" if extra_body is None else json.dumps(extra_body, separators=(",", ":"))
        try:
            detail = resp.json()
        except Exception:
            detail = None

        log.error(
            "Bitget HTTP %s %s -> %s | url=%s | body=%s",
            resp.request.method if resp.request else "",
            resp.request.path_url if resp.request else "",
            resp.status_code,
            resp.url,
            body_str,
        )
        if detail is not None:
            log.error("Bitget response: %s", detail)

        resp.raise_for_status()

    # ---------------------------------------------------------------------
    # HTTP 요청
    # ---------------------------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        서명 포함 공통 요청 함수
        - GET: params -> query string
        - POST: body -> json
        """
        url = f"{self.BASE_URL}{path}"
        ts = self._now_ms()

        query_str = ""
        if method.upper() == "GET" and params:
            # Bitget v1은 간단하게 key=value&... 순서 중요X (실무에선 정렬 권장)
            from urllib.parse import urlencode

            query_str = "?" + urlencode(params, doseq=True)
        elif method.upper() == "POST":
            # body는 직렬화 문자열을 prehash에 그대로 포함
            query_str = json.dumps(body or {}, separators=(",", ":"))

        # prehash에 포함되는 path는 path 자체(+ GET의 경우 query string 포함 X, 문서/예제들마다 상이)
        # Bitget v1 (signType=2) 사례에 맞춰: GET은 body_or_query에 ""(빈문자열), POST는 직렬화 JSON.
        body_or_query = "" if method.upper() == "GET" else query_str

        sign = self._sign_v2(ts, method, path, body_or_query)
        headers = self._headers(ts, sign)

        if method.upper() == "GET":
            resp = self.session.get(url, headers=headers, params=(params or {}), timeout=self.TIMEOUT)
        else:
            resp = self.session.post(url, headers=headers, data=query_str.encode(), timeout=self.TIMEOUT)

        if not resp.ok:
            self._err_raise(resp, body if method.upper() == "POST" else params)

        data = resp.json()
        # Bitget 성공코드 "00000"
        if str(data.get("code")) != "00000":
            # 응답은 200이라도 내부 code가 에러일 수 있음
            log.error("Bitget biz error: %s", data)
            raise requests.HTTPError(f"Bitget business error: {data}")

        return data

    # ---------------------------------------------------------------------
    # 비즈니스 메서드
    # ---------------------------------------------------------------------
    def _body(
        self,
        symbol: str,
        side: str,
        order_type: str,
        size: float | str,
        reduce_only: bool,
        client_oid: Optional[str],
    ) -> Dict[str, Any]:
        """
        주문 바디 공통 생성
        """
        return {
            "symbol": symbol,
            "marginCoin": self.MARGIN_COIN,
            "productType": self.product_type,
            "side": side,                  # "buy" | "sell"
            "orderType": order_type,       # "market" | "limit"
            "size": str(size),
            "reduceOnly": bool(reduce_only),
            **({"clientOid": client_oid} if client_oid else {}),
        }

    def get_net_position(self, symbol: str) -> Dict[str, float]:
        """
        현재 심볼의 순포지션 크기(net)를 반환.
        Bitget 단방향(One-way)에서는 홀드 수량만 제공되는 경우가 많아
        없으면 0으로 처리.
        """
        path = "/api/mix/v1/position/singlePosition"
        params = {
            "symbol": symbol,
            "marginCoin": self.MARGIN_COIN,
            "productType": self.product_type,
        }

        try:
            j = self._request("GET", path, params=params)
        except requests.HTTPError:
            # 상위에서 처리할 수 있도록 다시 던져도 되지만, 여기선 0 반환(선호에 따라 조절)
            raise

        # 응답 예시:
        # {"code":"00000","msg":"success","requestTime":...,"data":[ ... ]} or {"data": []}
        data = j.get("data") or []

        net = 0.0
        try:
            # Bitget 단방향에서는 data가 0 또는 1개.
            # 항목 안에 "holdSide": "long"/"short", "total": "0.01" 와 같은 필드가 있을 수 있음.
            # 불확실한 스키마는 안전하게 합산.
            for pos in data:
                qty = float(pos.get("total", 0) or 0)
                side = (pos.get("holdSide") or "").lower()
                if side == "long":
                    net += qty
                elif side == "short":
                    net -= qty
        except Exception:
            # 스키마 변동 대비. 못 읽으면 0
            net = 0.0

        return {"net": net}

    def place_order(
        self,
        symbol: str | None = None,
        tv_symbol: str | None = None,
        side: str = "buy",
        order_type: str = "market",
        size: float | str = 0,
        reduce_only: bool = False,
        client_oid: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        주문 실행.
        - **호환성 추가**: `symbol` 과 `tv_symbol` 둘 다 지원합니다.
          (server.py가 symbol= 로 호출해도 정상 동작)
        """
        sym = symbol or tv_symbol
        if not sym:
            raise ValueError("symbol is required")

        norm_symbol = sym.strip().upper()
        path = "/api/mix/v1/order/placeOrder"

        body = self._body(
            symbol=norm_symbol,
            side=side,
            order_type=order_type,
            size=size,
            reduce_only=reduce_only,
            client_oid=client_oid,
        )

        return self._request("POST", path, body=body)
