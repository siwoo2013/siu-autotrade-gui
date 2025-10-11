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
        """
        Bitget 서버 시간(ms) 조회.
        Mix 공개 시세 엔드포인트의 requestTime을 사용 (항상 ms epoch).
        """
        url = f"{self.base_url}/api/mix/v1/market/ticker"
        params = {"symbol": "BTCUSDT_UMCBL"}  # 아무 액티브한 심볼이면 OK
        r = self.session.get(url, params=params, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        # 예: {"code":"00000","msg":"success","requestTime":1760155297530,"data":{...}}
        if isinstance(data, dict) and "requestTime" in data:
            return int(data["requestTime"])

        # 혹시 requestTime이 없다면 헤더 Date를 fallback으로 사용
        # (아래는 거의 안 타지만 안전빵)
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(r.headers.get("Date"))
            return int(dt.timestamp() * 1000)
        except Exception:
            pass

        raise RuntimeError(f"Unexpected ticker response for time sync: {data}")
   

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

    # --- [ADD] 서버에서 호출하는 시그니처 그대로 받는 래퍼 -----------------
    def place_order(
        self,
        symbol: str,
        side: str,             # "BUY" | "SELL"
        type: str,             # "MARKET" 등 - 여기서는 MARKET만 사용
        size: float,
        reduce_only: bool = False,
        client_oid: str | None = None,
    ) -> dict:
        """
        server.py가 기대하는 시그니처. type은 현재 MARKET만 지원.
        내부적으로 place_order_market을 호출한다.
        """
        # type은 현재 무시 (MARKET만 처리)
        return self.place_order_market(
            symbol=symbol,
            side=side,
            size=size,
            reduce_only=reduce_only,
            client_oid=client_oid,
        )

    # --- [ADD/KEEP] 실제 발주 로직 (마켓 주문) ------------------------------
    def place_order_market(
        self,
        symbol: str,
        side: str,             # "BUY" | "SELL"
        size: float,
        reduce_only: bool = False,
        client_oid: str | None = None,
    ) -> dict:
        """
        Bitget USDT-M 선물 마켓주문 실행
        """
        path = "/api/mix/v1/order/placeOrder"
        body = {
            "symbol": symbol,
            "productType": self.product_type,   # "umcbl"
            "marginCoin": self.margin_coin,     # "USDT"
            "size": str(size),
            "side": side.lower(),               # buy / sell
            "orderType": "market",              # 마켓 주문
            "timeInForceValue": "normal",
        }
        if reduce_only:
            body["reduceOnly"] = True
        if client_oid:
            body["clientOid"] = client_oid

        return self._request("POST", path, body=body)
