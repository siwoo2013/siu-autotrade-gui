# bitget_client.py
# -*- coding: utf-8 -*-

import base64
import hashlib
import hmac
import json
import time
from typing import Any, Dict, Optional, Tuple

import requests


class BitgetHTTPError(Exception):
    """Bitget HTTP Error wrapper"""

    def __init__(self, status: int, detail: str = ""):
        self.status = status
        self.detail = detail or ""
        super().__init__(f"bitget-http status={status} detail={detail}")


class BitgetClient:
    """
    Minimal Bitget REST client (UMCBL / Hedge mode)

    - 신규/청산 주문: /api/mix/v1/order/placeOrder (holdSide 사용)
    - 포지션 조회:     /api/mix/v1/position/singlePosition
    """

    BASE_URL = "https://api.bitget.com"
    PRODUCT_TYPE = "umcbl"
    MARGIN_COIN = "USDT"

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        passphrase: str,
        base_url: Optional[str] = None,
        timeout: float = 10.0,
        **_ignored,  # 예전 코드에서 넘기던 mode 등 불필요 키워드를 무시하기 위해
    ) -> None:
        if not api_key or not api_secret or not passphrase:
            raise ValueError("Bitget credentials are required")

        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.base_url = base_url or self.BASE_URL
        self.timeout = timeout

        self.session = requests.Session()
        self.session.headers.update(
            {
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )

    # -----------------------------
    # low-level helpers
    # -----------------------------
    @staticmethod
    def _ts_ms() -> str:
        return str(int(time.time() * 1000))

    def _sign(self, ts: str, method: str, path: str, query: str, body: str) -> str:
        """
        Bitget sign rule:
          sign = base64( HMAC_SHA256( secret, ts + method + path + query + body ) )
        - path 는 '/api/...'
        - query 가 있으면 '?k=v&...' 포함, 없으면 빈 문자열
        - body 는 JSON 문자열 (POST/DELETE 시)
        """
        prehash = ts + method.upper() + path + (query or "") + (body or "")
        mac = hmac.new(
            self.api_secret.encode("utf-8"),
            prehash.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return base64.b64encode(mac).decode()

    def _auth_headers(self, ts: str, sign: str) -> Dict[str, str]:
        return {
            "ACCESS-KEY": self.api_key,
            "ACCESS-SIGN": sign,
            "ACCESS-TIMESTAMP": ts,
            "ACCESS-PASSPHRASE": self.passphrase,
            "X-CHANNEL-API-CODE": "bitget-python",
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        auth: bool = True,
        max_retry: int = 1,
    ) -> Dict[str, Any]:
        """
        Make a signed HTTP request to Bitget.
        Robust against transient network errors and schema variations.
        """
        url = self.base_url + path
        query_str = ""
        if params:
            # Bitget은 서명 시 query string을 포함해야 함 (선행 '?' 포함)
            from urllib.parse import urlencode

            query_str = "?" + urlencode(params, doseq=True)

        payload = json.dumps(body, separators=(",", ":")) if body else ""
        ts = self._ts_ms()
        headers = self.session.headers.copy()

        if auth:
            sign = self._sign(ts, method, path, query_str, payload)
            headers.update(self._auth_headers(ts, sign))

        tries = 0
        while True:
            tries += 1
            try:
                resp = self.session.request(
                    method=method.upper(),
                    url=url + query_str,
                    data=payload if payload else None,
                    timeout=self.timeout,
                    headers=headers,
                )
            except requests.RequestException as e:
                if tries <= max_retry:
                    time.sleep(0.3)
                    continue
                raise BitgetHTTPError(0, f"requests-error: {e!r}")

            # Bitget는 200이어도 내부 code가 실패일 수 있음.
            if resp.status_code >= 400:
                raise BitgetHTTPError(resp.status_code, resp.text)

            try:
                data = resp.json()
            except ValueError:
                # JSON 파싱 실패
                raise BitgetHTTPError(resp.status_code, resp.text)

            return data

    # -----------------------------
    # public helpers
    # -----------------------------
    def get_hedge_sizes(self, symbol: str) -> Dict[str, float]:
        """
        현재 심볼의 long/short 사이즈(절대 수량)를 반환
        - Bitget 응답이 dict 또는 list로 올 때 모두 대응
        """
        path = "/api/mix/v1/position/singlePosition"
        params = {
            "symbol": symbol,
            "marginCoin": self.MARGIN_COIN,
            "productType": self.PRODUCT_TYPE,
        }

        res = self._request("GET", path, params=params)
        # 정상: {"code":"00000","msg":"success","requestTime":...,"data":{"total":{...},"list":[...]}}
        # 때로는 data가 list만 올 때가 있어, 모두 대비
        if res.get("code") != "00000":
            raise BitgetHTTPError(400, json.dumps(res, ensure_ascii=False))

        data = res.get("data")
        total = {}
        if isinstance(data, dict):
            total = data.get("total") or {}
            # 혹시 list만 채워져있으면 list에서 요약 계산
            if not total and isinstance(data.get("list"), list):
                total = self._sum_from_list(data["list"])
        elif isinstance(data, list):
            total = self._sum_from_list(data)
        else:
            total = {}

        long_sz = float(total.get("long", 0) or 0)
        short_sz = float(total.get("short", 0) or 0)
        return {"long": long_sz, "short": short_sz}

    @staticmethod
    def _sum_from_list(items: list) -> Dict[str, float]:
        long_sz = 0.0
        short_sz = 0.0
        for it in items:
            try:
                hs = (it.get("holdSide") or "").lower()
                sz = float(it.get("total", it.get("size", 0)) or 0)
                if hs == "long":
                    long_sz += sz
                elif hs == "short":
                    short_sz += sz
            except Exception:
                continue
        return {"long": long_sz, "short": short_sz}

    def place_order(
        self,
        *,
        symbol: str,
        side: str,                # "buy" | "sell" (진입 기준 방향)
        order_type: str = "market",
        size: float,
        reduce_only: bool = False,
        client_oid: Optional[str] = None,
    ) -> str:
        """
        Hedge 모드 대응 신규/청산 주문
        - 신규(진입): side=buy -> open_long, side=sell -> open_short
        - 청산(감소): side=buy -> close_short, side=sell -> close_long
        """
        side = side.lower().strip()
        if side not in ("buy", "sell"):
            raise ValueError("side must be 'buy' or 'sell'")

        if reduce_only:
            # 진입 반대 방향 포지션 청산
            hold_side = "close_long" if side == "sell" else "close_short"
        else:
            # 신규 진입
            hold_side = "open_long" if side == "buy" else "open_short"

        body = {
            "symbol": symbol,
            "productType": self.PRODUCT_TYPE,
            "marginCoin": self.MARGIN_COIN,
            "size": str(size),
            "holdSide": hold_side,
            "orderType": order_type.lower(),   # "market" / "limit"
            "reduceOnly": True if reduce_only else False,
        }
        if client_oid:
            body["clientOid"] = client_oid

        res = self._request("POST", "/api/mix/v1/order/placeOrder", body=body)
        # 성공 예: {"code":"00000","msg":"success","requestTime":..., "data":{"orderId":"..."}}
        if res.get("code") != "00000":
            # Bitget의 실패 응답 전체를 detail로 올려서 로그에서 바로 원인 파악 가능
            raise BitgetHTTPError(400, json.dumps(res, ensure_ascii=False))

        data = res.get("data") or {}
        return str(data.get("orderId") or data.get("order_id") or "")

    # 선택: TP/SL(계획 주문) 등록이 필요할 때 사용할 수 있는 헬퍼
    def place_take_profit_percent(
        self,
        *,
        symbol: str,
        entry_price: float,
        percent: float,
        direction: str,  # "long" | "short"
        size: float,
    ) -> Optional[str]:
        """
        Bitget 계획주문(Plan) API 예시 (필수는 아님)
        - 단순 참고용. 실제 거래소 정책/파라미터는 계정/상품 설정에 따라 달라질 수 있음.
        """
        # 방향에 따라 익절 트리거 계산 (진입가 기준)
        if direction == "long":
            trigger = entry_price * (1 + percent / 100.0)
            side = "sell"      # 롱의 익절은 매도
            hold_side = "close_long"
        else:
            trigger = entry_price * (1 - percent / 100.0)
            side = "buy"       # 숏의 익절은 매수
            hold_side = "close_short"

        body = {
            "symbol": symbol,
            "productType": self.PRODUCT_TYPE,
            "marginCoin": self.MARGIN_COIN,
            "planType": "profit_plan",
            "triggerType": "market_price",
            "triggerPrice": str(trigger),
            "size": str(size),
            "executePrice": "",             # 시장가 실행 시 빈 문자열
            "holdSide": hold_side,          # 중요
        }

        # Bitget 계획 주문 엔드포인트(계정 권한/상품별로 상이할 수 있음)
        path = "/api/mix/v1/plan/placePlan"
        try:
            res = self._request("POST", path, body=body)
        except BitgetHTTPError:
            return None

        if res.get("code") != "00000":
            return None
        data = res.get("data") or {}
        return str(data.get("planId") or "")

