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

log = logging.getLogger("bitget")
if not log.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )


class BitgetClient:
    """
    Bitget U-M (USDT-Margined) 선물 전용 클라이언트.
    - product_type: 'umcbl' 고정 (BTCUSDT_UMCBL 같은 심볼과 매칭)
    - marginCoin: 'USDT' 고정
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        passphrase: str,
        demo: bool = True,
        timeout: int = 10,
    ) -> None:
        self.api_key = api_key or ""
        self.api_secret = api_secret or ""
        self.passphrase = passphrase or ""
        self.timeout = timeout
        self.demo = demo

        # 고정 파라미터
        self.product_type = "umcbl"  # ← 오류 로그에 나오던 필드
        self.margin_coin = "USDT"

        # 공용 세션
        self.session = requests.Session()
        self.base_url = "https://api.bitget.com"

        # 서버 시간 보정(밀리초). 초기에 동기화 시도하되 실패해도 동작은 계속.
        self._delta_ms = 0
        try:
            self._sync_time()
        except Exception as e:
            log.warning("Bitget time sync failed (will retry on demand): %s", e)

    # --------------------------------------------------------------------- #
    # 시간 동기화
    # --------------------------------------------------------------------- #
    def _server_ts_ms(self) -> int:
        """
        Bitget 서버 시간(ms). v2 공개 엔드포인트 사용.
        """
        url = f"{self.base_url}/api/v2/public/time"
        r = self.session.get(url, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        # {'code':'00000','msg':'success','requestTime':..., 'data':{'time': 171xxx}}
        return int(data.get("data", {}).get("time"))

    def _sync_time(self) -> None:
        local_ms_1 = int(time.time() * 1000)
        server_ms = self._server_ts_ms()
        local_ms_2 = int(time.time() * 1000)
        rtt = (local_ms_2 - local_ms_1) // 2
        self._delta_ms = server_ms - (local_ms_1 + rtt)
        log.info("Bitget time synced. delta_ms=%s", self._delta_ms)

    def _now_ms(self) -> int:
        """
        서버 시간 보정값을 반영한 현재시각(ms).
        """
        return int(time.time() * 1000) + self._delta_ms

    # --------------------------------------------------------------------- #
    # 서명/요청
    # --------------------------------------------------------------------- #
    def _sign(self, ts_ms: int, method: str, path_qs: str, body: str = "") -> str:
        """
        Bitget 시그니처:  sign = base64( HMAC_SHA256(secret, f"{ts}{method}{path}{body}") )
        * ts는 문자열 (밀리초)
        * path는 '/api/...' + 쿼리스트링 포함
        """
        msg = f"{ts_ms}{method.upper()}{path_qs}{body}"
        mac = hmac.new(
            self.api_secret.encode("utf-8"),
            msg=msg.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        return base64.b64encode(mac).decode()

    def _headers(self, ts_ms: int, sign: str) -> Dict[str, str]:
        return {
            "ACCESS-KEY": self.api_key,
            "ACCESS-SIGN": sign,
            "ACCESS-TIMESTAMP": str(ts_ms),
            "ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
            "X-CHANNEL-API-CODE": "bitget",  # 없어도 무방
        }

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        auth: bool = True,
    ) -> Dict[str, Any]:
        """
        Bitget REST 요청 래퍼.
        - 실패 시 상세 본문 로깅
        """
        url = f"{self.base_url}{path}"
        qs = ""
        if params:
            # params 순서는 서명에 영향 → requests가 처리하는 쿼리 문자열보다
            # 서명 생성 시에는 path에 미리 붙이는 방식이 안전
            from urllib.parse import urlencode

            qs = "?" + urlencode(params, doseq=False, safe=",:")
            url = f"{url}{qs}"

        body_str = json.dumps(body) if body else ""
        headers = {}

        if auth:
            ts = self._now_ms()
            # 간헐적 timestamp 오류(40008) 재시도: 시간 재동기화 후 1회 더 시도
            def sign_headers(ts_ms: int) -> Dict[str, str]:
                sign = self._sign(ts_ms, method, f"{path}{qs}", body_str)
                return self._headers(ts_ms, sign)

            headers = sign_headers(ts)
        else:
            headers = {"Content-Type": "application/json"}

        try:
            if method.upper() == "GET":
                resp = self.session.get(url, headers=headers, timeout=self.timeout)
            else:
                resp = self.session.post(url, headers=headers, data=body_str, timeout=self.timeout)

            try:
                resp.raise_for_status()
            except requests.HTTPError as e:
                # 세부 본문 로깅
                detail = {}
                try:
                    detail = resp.json()
                except Exception:
                    pass
                log.error("Bitget HTTP %s %s -> %s | url=%s | body=%s",
                          method.upper(), path, resp.status_code, url, body_str)
                if detail:
                    log.error("Bitget response: %s", detail)

                # 타임스탬프 만료면 한 번만 다시 동기화 후 재시도
                if isinstance(detail, dict) and detail.get("code") == "40008":
                    try:
                        self._sync_time()
                        ts2 = self._now_ms()
                        hdr2 = self._headers(ts2, self._sign(ts2, method, f"{path}{qs}", body_str))
                        if method.upper() == "GET":
                            resp2 = self.session.get(url, headers=hdr2, timeout=self.timeout)
                        else:
                            resp2 = self.session.post(url, headers=hdr2, data=body_str, timeout=self.timeout)
                        resp2.raise_for_status()
                        return resp2.json()
                    except Exception:
                        pass

                raise requests.HTTPError(
                    f"{e} | url={resp.url} | body={body_str} | detail={detail}"
                ) from e

            return resp.json()

        except requests.RequestException:
            raise  # 상위에서 처리/로그

    # --------------------------------------------------------------------- #
    # 공개/계정/거래
    # --------------------------------------------------------------------- #
    def get_net_position(self, symbol: str) -> Dict[str, Any]:
        """
        단일 심볼의 순포지션 수량 조회.
        Bitget: GET /api/mix/v1/position/singlePosition
        """
        path = "/api/mix/v1/position/singlePosition"
        params = {
            "symbol": symbol,
            "marginCoin": self.margin_coin,
            "productType": self.product_type,
        }
        data = self._request("GET", path, params=params)
        # data 예시: {'code':'00000','msg':'success','data':{'holdSide':'long','total': 0.01, ...}}
        info = data.get("data") or {}
        long_avail = float(info.get("longHoldAvail", 0) or info.get("long", 0) or 0)
        short_avail = float(info.get("shortHoldAvail", 0) or info.get("short", 0) or 0)
        # 순포지션 (롱: +, 숏: -) — 없으면 0
        net = long_avail - short_avail
        return {"net": net, "raw": info}

    def close_position(self, symbol: str, side: str = "ALL") -> Dict[str, Any]:
        """
        전량 청산(가능하면 거래소의 close-position 엔드포인트 사용).
        Bitget: POST /api/mix/v1/position/close-position
        - side: 'long' | 'short' | 'ALL'
        """
        path = "/api/mix/v1/position/close-position"
        body = {
            "symbol": symbol,
            "marginCoin": self.margin_coin,
            "productType": self.product_type,
        }
        if side.upper() in ("LONG", "SHORT"):
            body["holdSide"] = side.lower()
        # 'ALL'이면 holdSide 생략 → 전체 청산
        return self._request("POST", path, body=body)

    def place_order(
        self,
        symbol: str,
        side: str,
        type: str,
        size: float,
        reduce_only: bool = False,
        client_oid: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        시장가 주문:
        Bitget: POST /api/mix/v1/order/placeOrder
        """
        path = "/api/mix/v1/order/placeOrder"
        # Bitget의 'orderType' 에는 'market' / 'limit' 등. 모두 소문자로.
        order_type = "market" if type.upper() == "MARKET" else "limit"

        body = {
            "symbol": symbol,
            "marginCoin": self.margin_coin,
            "productType": self.product_type,
            "side": side.lower(),                # buy | sell
            "orderType": order_type,             # market | limit
            "size": str(size),                   # 문자열 허용
            "reduceOnly": bool(reduce_only),
        }
        if client_oid:
            body["clientOid"] = client_oid

        return self._request("POST", path, body=body)
