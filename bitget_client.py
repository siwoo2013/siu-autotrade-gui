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


def _now_ms() -> int:
    return int(time.time() * 1000)


def _b64_hmac_sha256(secret: str, payload: str) -> str:
    sig = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(sig).decode("utf-8")


def normalize_symbol(tv_symbol: str) -> str:
    """
    TradingView 심볼을 Bitget REST 심볼로 매핑
      - BTCUSDT.P  -> BTCUSDT_UMCBL
      - ETHUSDT.P  -> ETHUSDT_UMCBL
      - 이미 _UMCBL 형태면 그대로 사용
    """
    s = tv_symbol.strip().upper()
    if s.endswith(".P"):
        base = s[:-2]  # '.P' 제거
        return f"{base}_UMCBL"
    if s.endswith("_UMCBL"):
        return s
    # 기타 케이스는 그대로
    return s


class BitgetClient:
    """
    Bitget Mix(선물) REST 간단 클라이언트 (원웨이 기본)
    - product_type: "umcbl" (USDT-M Perp)
    - order.placeOrder 사용
    - 오픈 주문엔 reduceOnly 미포함, 클로즈 주문엔 reduceOnly=True 포함
    """

    BASE_URL = "https://api.bitget.com"

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        passphrase: str,
        *,
        product_type: str = "umcbl",
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.product_type = product_type  # "umcbl"
        self.session = requests.Session()
        self.log = logger or logging.getLogger("BitgetClient")

    # -------------------------- low-level request --------------------------

    def _headers(self, method: str, path: str, query: str, body_str: str, ts: int) -> Dict[str, str]:
        """
        Bitget 서명 헤더 생성
        sign = base64( HMAC_SHA256(secret, f"{ts}{method}{path}{query}{body}") )
        """
        signing_str = f"{ts}{method.upper()}{path}{query}{body_str}"
        sign = _b64_hmac_sha256(self.api_secret, signing_str)

        return {
            "ACCESS-KEY": self.api_key,
            "ACCESS-PASSPHRASE": self.passphrase,
            "ACCESS-SIGN-TYPE": "2",       # v2
            "ACCESS-TIMESTAMP": str(ts),
            "ACCESS-SIGN": sign,
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        timeout: int = 10,
    ) -> Dict[str, Any]:
        """
        공통 요청. 에러시 상세 로그 남김.
        """
        url = self.BASE_URL + path
        ts = _now_ms()

        # Query 문자열
        query = ""
        if params:
            # Bitget는 서명 시 querystring까지 포함해야 함
            from urllib.parse import urlencode

            query = "?" + urlencode(params, doseq=True)

        body_str = json.dumps(body, separators=(",", ":"), ensure_ascii=False) if body else ""
        headers = self._headers(method, path, query, body_str, ts)

        try:
            if method.upper() == "GET":
                resp = self.session.get(url, headers=headers, params=params or {}, timeout=timeout)
            elif method.upper() == "POST":
                resp = self.session.post(url, headers=headers, params=params or {}, data=body_str, timeout=timeout)
            else:
                raise ValueError(f"Unsupported method: {method}")

            # 상태코드 체크
            try:
                resp.raise_for_status()
            except requests.HTTPError as e:
                detail = {}
                try:
                    detail = resp.json()
                except Exception:
                    pass
                # 에러 로그 (요청/응답 본문까지)
                self.log.error(
                    "Bitget HTTP %s %s -> %s | url=%s | body=%s",
                    method.upper(),
                    path,
                    resp.status_code,
                    resp.url,
                    body_str,
                )
                self.log.error("Bitget response: %s", detail)
                raise requests.HTTPError(
                    f"{e} | url={resp.url} | body={body_str} | detail={detail}"
                ) from e

            data = resp.json()
            return data

        except requests.RequestException:
            raise

    # -------------------------- helpers --------------------------

    def get_account_conf(self) -> Dict[str, Any]:
        """
        계정 설정 (holdMode 확인용)
        GET /api/mix/v1/account/account?productType=umcbl
        """
        path = "/api/mix/v1/account/account"
        params = {"productType": self.product_type}
        return self._request("GET", path, params=params)

    def get_net_position(self, tv_symbol: str) -> Dict[str, float]:
        """
        현재 심볼의 순포지션(원웨이 기준)을 근사 계산하여 반환.
        - Bitget singlePosition 응답 포맷이 환경마다 조금 달라서,
          data가 없으면 0.0으로 처리
        """
        symbol = normalize_symbol(tv_symbol)
        path = "/api/mix/v1/position/singlePosition"
        params = {
            "symbol": symbol,
            "marginCoin": "USDT",
            "productType": self.product_type,
        }
        res = self._request("GET", path, params=params)
        net = 0.0
        try:
            d = res.get("data")
            if isinstance(d, dict):
                # one-way에선 holdSide: "long"/"short"/"none", total: 수량
                side = d.get("holdSide")
                qty = float(d.get("total", 0) or 0)
                if side == "long":
                    net = qty
                elif side == "short":
                    net = -qty
            elif isinstance(d, list) and d:
                # 혹시 리스트로 올 때 첫 요소만 사용
                first = d[0]
                side = first.get("holdSide")
                qty = float(first.get("total", 0) or 0)
                if side == "long":
                    net = qty
                elif side == "short":
                    net = -qty
        except Exception:
            self.log.exception("get_net_position parse error: %s", res)

        return {"net": float(net)}

    # -------------------------- order --------------------------

    @staticmethod
    def _map_side_oneway(side_in: str) -> str:
        """
        원웨이 모드에서 허용되는 주문 side: "buy" / "sell"
        헷지 표현(open_long 등)이 들어와도 원웨이에 맞추어 평탄화
        """
        s = side_in.lower()
        if s in ("buy", "open_long", "close_short"):
            return "buy"
        if s in ("sell", "open_short", "close_long"):
            return "sell"
        # 기본은 buy로
        return "buy"

    def _body(
        self,
        symbol: str,
        side: str,
        order_type: str,
        size: float,
        reduce_only: Optional[bool],
        client_oid: Optional[str],
    ) -> Dict[str, Any]:
        """
        주문 바디 생성
        - 오픈 주문에는 reduceOnly를 '아예' 포함하지 않음
        - 청산(reduce)일 때만 reduceOnly=True 포함
        """
        body: Dict[str, Any] = {
            "symbol": symbol,
            "marginCoin": "USDT",
            "productType": self.product_type,
            "side": side,                   # one-way: buy/sell
            "orderType": order_type,        # "market"
            "size": f"{size}",
        }
        if reduce_only is True:
            body["reduceOnly"] = True
        if client_oid:
            body["clientOid"] = client_oid
        return body

    def place_order(
        self,
        tv_symbol: str,
        *,
        side: str,
        order_type: str,
        size: float,
        reduce_only: Optional[bool] = None,
        client_oid: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        주문 실행(원웨이 기본)
        - side mismatch(400172) 발생 시 상세 로그만 남기고 예외 전달
        """
        symbol = normalize_symbol(tv_symbol)
        side_norm = self._map_side_oneway(side)
        path = "/api/mix/v1/order/placeOrder"

        # 1) 일반(오픈) 주문: reduceOnly를 넣지 않는다
        if reduce_only not in (True, False):
            body_open = self._body(symbol, side_norm, order_type, size, None, client_oid)
            try:
                return self._request("POST", path, body=body_open)
            except requests.HTTPError:
                # 그대로 상위로 전달 (서버에서 정책적으로 재시도/강제청산 여부 판단)
                raise

        # 2) 청산 주문: reduceOnly=True
        else:
            if reduce_only is True:
                body_close = self._body(symbol, side_norm, order_type, size, True, client_oid)
                return self._request("POST", path, body=body_close)
            else:
                # reduce_only=False로 명시하고 싶다면 굳이 키를 넣지 않는 것이 안전
                body_open = self._body(symbol, side_norm, order_type, size, None, client_oid)
                return self._request("POST", path, body=body_open)
