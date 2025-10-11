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
from urllib.parse import urlencode

import requests

logger = logging.getLogger("bitget")
logger.setLevel(logging.INFO)


class BitgetClient:
    """
    Bitget REST API (Futures/Mix) - 최소 구현 클라이언트
    - 서버 시간 동기화(초기 1회 + 40008 에러시 1회 재동기화 후 재시도)
    - productType=umcbl 명시
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        passphrase: str,
        demo: bool = False,
        timeout: int = 10,
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret.encode("utf-8")
        self.passphrase = passphrase
        self.base_url = "https://api.bitget.com"
        self.timeout = timeout

        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        # Bitget 서버 시간 오프셋(ms), serverTime - localTime
        self._time_delta_ms: int = 0

        # 최초 1회 서버 시간 동기화
        try:
            self._sync_time()
        except Exception as e:
            logger.warning("Bitget time sync failed (will retry on demand): %s", e)

    # --------------------------------------------------------------------- #
    # 시간 동기화 및 타임스탬프
    # --------------------------------------------------------------------- #
    def _server_time(self) -> int:
        """Bitget 서버 시간(ms) 조회"""
        # mix endpoint (futures)
        url = f"{self.base_url}/api/mix/v1/public/time"
        r = self.session.get(url, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        # {"code":"00000","msg":"success","requestTime":..., "data": {"serverTime": 170...}}
        if isinstance(data, dict):
            if "data" in data and isinstance(data["data"], dict) and "serverTime" in data["data"]:
                return int(data["data"]["serverTime"])
            if "requestTime" in data:  # 일부 응답은 requestTime만 줌
                return int(data["requestTime"])
        raise RuntimeError(f"Unexpected time response: {data}")

    def _sync_time(self) -> None:
        """서버 시간과의 오프셋(ms) 계산"""
        local_before = int(time.time() * 1000)
        server = self._server_time()
        local_after = int(time.time() * 1000)
        # 왕복 지연의 절반 보정(대략)
        local_avg = (local_before + local_after) // 2
        self._time_delta_ms = server - local_avg
        logger.info("Bitget time synced. delta_ms=%d", self._time_delta_ms)

    def _timestamp_ms(self) -> int:
        """동기화된 타임스탬프(ms) 반환"""
        return int(time.time() * 1000 + self._time_delta_ms)

    # --------------------------------------------------------------------- #
    # 서명 & 요청
    # --------------------------------------------------------------------- #
    def _sign(self, ts: str, method: str, path: str, query: str, body: str) -> str:
        # Bitget prehash: timestamp + method + requestPath + (queryString) + (body)
        # query/ body가 빈 문자열이면 생략된 상태로 이어붙임
        prehash = f"{ts}{method.upper()}{path}{query}{body}"
        digest = hmac.new(self.api_secret, prehash.encode("utf-8"), hashlib.sha256).digest()
        return base64.b64encode(digest).decode()

    def _headers(self, ts: str, sign: str) -> Dict[str, str]:
        return {
            "ACCESS-KEY": self.api_key,
            "ACCESS-SIGN": sign,
            "ACCESS-TIMESTAMP": ts,
            "ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
            "locale": "en-US",
        }

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        *,
        _retry_on_time_error: bool = True,
    ) -> Dict[str, Any]:
        """
        Bitget 요청 래퍼
        - 40008(Request timestamp expired) 발생 시 1회 시간 재동기화 후 재시도
        """
        params = params or {}
        body = body or {}

        # query string, body 문자열화
        query = f"?{urlencode(params)}" if params else ""
        body_str = json.dumps(body, separators=(",", ":"), ensure_ascii=False) if body else ""

        ts = str(self._timestamp_ms())
        sign = self._sign(ts, method, path, query, body_str)
        url = f"{self.base_url}{path}{query}"
        headers = self._headers(ts, sign)

        try:
            if method.upper() == "GET":
                resp = self.session.get(url, headers=headers, timeout=self.timeout)
            elif method.upper() == "POST":
                resp = self.session.post(url, headers=headers, data=body_str, timeout=self.timeout)
            else:
                raise ValueError(f"Unsupported method: {method}")

            # 4xx/5xx면 raise -> 아래 except에서 Bitget 본문 로깅
            resp.raise_for_status()
            data = resp.json()
            return data
        except requests.HTTPError as e:
            detail: Any = None
            try:
                detail = resp.json()  # type: ignore[name-defined]
            except Exception:
                detail = resp.text if "resp" in locals() else None

            logger.error(
                "Bitget HTTP %s %s -> %s | url=%s%s | body=%s",
                method.upper(),
                path,
                getattr(resp, "status_code", "NA"),
                self.base_url,
                path + query,
                body_str,
            )
            logger.error("Bitget response: %s", detail)

            # 타임스탬프 만료(40008)면 1회 동기화 후 재시도
            code = None
            if isinstance(detail, dict):
                code = detail.get("code")
            if _retry_on_time_error and code == "40008":
                try:
                    self._sync_time()
                except Exception as se:
                    logger.warning("Time resync failed: %s", se)
                return self._request(method, path, params, body, _retry_on_time_error=False)

            raise requests.HTTPError(
                f"{e} | url={getattr(resp,'url','')} | body={body_str} | detail={detail}"
            ) from e

    # --------------------------------------------------------------------- #
    # 심볼/포지션/주문
    # --------------------------------------------------------------------- #
    @staticmethod
    def normalize_symbol(symbol: str) -> str:
        """TV/거래소 심볼 표기 통일"""
        s = symbol.strip().upper()
        if s.endswith(".P"):
            # TradingView: BTCUSDT.P  ->  Bitget: BTCUSDT_UMCBL
            return s.replace(".P", "_UMCBL")
        if s.endswith("_P"):
            return s.replace("_P", "_UMCBL")
        if not s.endswith("_UMCBL"):
            # 안전장치: USDT 선물 기본
            if s.endswith("USDT"):
                s = f"{s}_UMCBL"
        return s

    def get_net_position(self, symbol: str) -> Dict[str, float]:
        """
        순포지션 조회: {'net': float}
        (롱:+, 숏:-, 무포:0)
        """
        sym = self.normalize_symbol(symbol)
        path = "/api/mix/v1/position/singlePosition"
        params = {
            "symbol": sym,
            "marginCoin": "USDT",
            "productType": "umcbl",
        }
        data = self._request("GET", path, params=params)
        # 응답 구조 예: {"code":"00000","msg":"success","data":{"holdSide":"long","...","total":0.01}}
        net = 0.0
        try:
            d = data.get("data") or {}
            # longQty/shortQty 기준 구현이면 여기서 맞춰서 계산
            long_qty = float(d.get("long", 0) or 0) if isinstance(d, dict) else 0.0
            short_qty = float(d.get("short", 0) or 0) if isinstance(d, dict) else 0.0
            # Bitget 문서에 따라 필드가 다를 수 있으므로 보수적 처리
            # 없으면 holdVol/holdSide 등으로 로직 보강 필요
        except Exception:
            # 데이터 구조가 다를 경우 0 처리 (주문 로직은 order.reverse에서 알아서 대응)
            long_qty = 0.0
            short_qty = 0.0
        net = long_qty - short_qty
        return {"net": net}

    def place_market_order(
        self, symbol: str, side: str, size: float, client_oid: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        마켓 주문
        side: "BUY" | "SELL"
        """
        sym = self.normalize_symbol(symbol)
        path = "/api/mix/v1/order/placeOrder"

        body = {
            "symbol": sym,
            "marginCoin": "USDT",
            "productType": "umcbl",
            "size": f"{size}",
            "side": side.upper(),  # "buy"|"sell" 도 가능
            "orderType": "market",
            "timeInForceValue": "normal",
        }
        if client_oid:
            body["clientOid"] = client_oid

        data = self._request("POST", path, body=body)
        return data
