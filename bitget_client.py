# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Any, Dict, Optional

import requests


class BitgetHTTPError(requests.HTTPError):
    def __init__(self, message: str, detail: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.detail = detail or {}


class BitgetClient:
    """
    Bitget USDT-M Perp (umcbl) 전용 간단 클라이언트.
    - 포지션 조회(헤지), 마켓 주문, TP 등록(Plan) 지원
    - 오류/형태 다양성에 최대한 내성 있게 파싱
    """

    BASE = "https://api.bitget.com"

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        passphrase: str,
        product_type: str = "umcbl",
        margin_coin: str = "USDT",
        timeout: int = 10,
        logger: Optional[logging.Logger] = None,
    ):
        self.api_key = api_key
        self.api_secret = api_secret.encode("utf-8")
        self.passphrase = passphrase
        self.product_type = product_type
        self.margin_coin = margin_coin
        self.timeout = timeout
        self.log = logger or logging.getLogger(__name__)

        self.session = requests.Session()
        self.session.headers.update(
            {
                "Content-Type": "application/json",
                "X-ACCESS-KEY": self.api_key,
                "X-ACCESS-PASSPHRASE": self.passphrase,
            }
        )

    # ------------------------
    # Low-level helpers
    # ------------------------
    @staticmethod
    def _ts_ms() -> str:
        return str(int(time.time() * 1000))

    def _sign(self, ts: str, method: str, path: str, body: str) -> str:
        raw = f"{ts}{method}{path}{body}".encode("utf-8")
        sig = hmac.new(self.api_secret, raw, hashlib.sha256).hexdigest()
        return sig

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """서명/전송/오류 내성 포함 공통 요청"""
        url = self.BASE + path
        m = method.upper()
        data_txt = json.dumps(body) if body else ""
        ts = self._ts_ms()
        sign = self._sign(ts, m, path, data_txt)

        headers = {
            "X-ACCESS-TIMESTAMP": ts,
            "X-ACCESS-SIGN": sign,
        }
        try:
            resp = self.session.request(
                m,
                url,
                params=params,
                data=data_txt if body else None,
                headers=headers,
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise BitgetHTTPError(f"network-error: {e}") from e

        # HTTP 오류 -> Bitget 에러에 맞춰 detail 뽑기
        if not resp.ok:
            detail = {}
            try:
                detail = resp.json()
            except Exception:
                detail = {"raw": resp.text}
            raise BitgetHTTPError(
                f"bitget-http status={resp.status_code}", detail=detail
            )

        try:
            payload = resp.json()
        except Exception:
            raise BitgetHTTPError("invalid-json", {"raw": resp.text})

        # Bitget 표준 포맷 검사
        code = str(payload.get("code", ""))
        if code not in ("00000", "0", "success", "Success"):  # '0' 대응
            raise BitgetHTTPError("bitget-error", payload)

        return payload

    # ------------------------
    # Public helpers
    # ------------------------
    def _client_oid(self, tag: str) -> str:
        return f"tv-{int(time.time()*1000)}-{tag}"

    # 가격 조회 (체결가 없을 때 보정용)
    def get_ticker_last(self, symbol: str) -> float:
        try:
            res = self._request(
                "GET",
                "/api/mix/v1/market/ticker",
                params={"symbol": symbol, "productType": self.product_type},
            )
            data = res.get("data", {}) or {}
            last = data.get("last") or data.get("close")
            return float(last or 0)
        except Exception:
            return 0.0

    # 포지션 사이즈(Long/Short) 조회 - 다양한 응답형태 방어
    def get_hedge_sizes(self, symbol: str) -> Dict[str, float]:
        path = "/api/mix/v1/position/singlePosition"
        params = {
            "symbol": symbol,
            "productType": self.product_type,
            "marginCoin": self.margin_coin,
        }
        res = self._request("GET", path, params=params)
        data = res.get("data", {})

        items: list[dict] = []
        if isinstance(data, dict):
            # 흔한 형태: {"list":[...]} | {"positions":[...]} | 단일 dict 등
            items = (
                data.get("list")
                or data.get("positions")
                or ([data] if data else [])
            )
            if isinstance(items, dict):
                items = [items]
        elif isinstance(data, list):
            items = data

        long_sz = 0.0
        short_sz = 0.0

        for it in items:
            side = (
                it.get("holdSide")
                or it.get("side")
                or it.get("positionSide")
                or it.get("posSide")
                or ""
            ).lower()
            # 수량 후보키 다양성 고려
            qty = (
                it.get("available")
                or it.get("total")
                or it.get("size")
                or it.get("holdVolume")
                or 0
            )
            try:
                fqty = float(qty or 0)
            except Exception:
                fqty = 0.0

            if side.startswith("long"):
                long_sz += fqty
            elif side.startswith("short"):
                short_sz += fqty

        return {"long": float(long_sz), "short": float(short_sz)}

    # ------------------------
    # Orders (market)
    # ------------------------
    def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        size: str,
        reduce_only: bool = False,
    ) -> Dict[str, Any]:
        path = "/api/mix/v1/order/placeOrder"
        body = {
            "symbol": symbol,
            "marginCoin": self.margin_coin,
            "productType": self.product_type,
            "side": side,             # open_long/open_short/close_long/close_short/buy/sell
            "orderType": order_type,  # market/limit
            "size": size,
            "reduceOnly": reduce_only,
            "clientOid": self._client_oid(side),
        }
        return self._request("POST", path, body=body)

    def open_long(self, symbol: str, size: str, order_type: str = "market"):
        return self.place_order(symbol, "open_long", order_type, size, reduce_only=False)

    def open_short(self, symbol: str, size: str, order_type: str = "market"):
        return self.place_order(symbol, "open_short", order_type, size, reduce_only=False)

    def close_long(self, symbol: str, size: str, order_type: str = "market"):
        return self.place_order(symbol, "close_long", order_type, size, reduce_only=True)

    def close_short(self, symbol: str, size: str, order_type: str = "market"):
        return self.place_order(symbol, "close_short", order_type, size, reduce_only=True)

    # ------------------------
    # TP(Plan) – 주문 이후 등록 (ROI 기준)
    # ------------------------
    def place_take_profit_by_roi(
        self,
        symbol: str,
        side: str,            # 'long' | 'short'
        entry_price: float,
        leverage: float,
        roi_target: float = 0.07,  # 7% 기본
    ):
        """
        ROI 기준으로 TP가격 계산 후 Plan 주문 생성.
        ROI% = 가격변동률% × 레버리지  =>  가격변동률 = ROI / 레버리지
        """
        path = "/api/mix/v1/plan/placePlan"

        if side.lower() == "long":
            tp_price = entry_price * (1 + roi_target / leverage)
            side_type = "long"
        else:
            tp_price = entry_price * (1 - roi_target / leverage)
            side_type = "short"

        body = {
            "symbol": symbol,
            "marginCoin": self.margin_coin,
            "planType": "profit_plan",
            "triggerPrice": f"{tp_price:.2f}",
            "executePrice": f"{tp_price:.2f}",
            "triggerType": "market_price",
            "side": side_type,
            "size": "0",  # 전량
        }
        self.log.info(f"[TP] ROI {roi_target*100:.2f}% ({side_type}) @ {tp_price:.2f}")
        return self._request("POST", path, body=body)
