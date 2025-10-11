# bitget_client.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import time
import hmac
import json
import hashlib
import logging
from typing import Any, Dict, Optional, Tuple, List

import requests


log = logging.getLogger("bitget")
log.setLevel(logging.INFO)


class BitgetClient:
    """
    Bitget Mix(USDT-M) 선물 전용 경량 클라이언트.
    - 기본 One-way 모드('buy'/'sell')로 주문.
    - 만약 계정/심볼이 Hedge라서 400172(side mismatch)가 오면
      hedge 형식(open_long/close_long/open_short/close_short)으로 즉시 재시도.
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        passphrase: str,
        *,
        demo: bool = False,
        margin_coin: str = "USDT",
        product_type: str = "umcbl",  # USDT-M perpetual
        base_url: str = "https://api.bitget.com",
        session: Optional[requests.Session] = None,
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret.encode()
        self.passphrase = passphrase
        self.margin_coin = margin_coin
        self.product_type = product_type
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()

        # 서버 시간과 로컬 시간 drift 보정 (ms)
        self._delta_ms = 0
        try:
            self.sync_time()
        except Exception as e:
            log.warning("Bitget time sync failed (will retry on demand): %s", e)

    # --------------------------------------------------------------------- #
    # 시간 & 서명
    # --------------------------------------------------------------------- #
    def _ts_ms(self) -> int:
        return int(time.time() * 1000) + self._delta_ms

    def sync_time(self) -> None:
        """
        Bitget 서버 시간 동기화.
        /api/v2/public/time 가 표준 공개 엔드포인트.
        """
        url = f"{self.base_url}/api/v2/public/time"
        r = self.session.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        # {"code":"00000","msg":"success","requestTime":...,"data":{"serverTime":...}}
        srv_ms = int(data.get("data", {}).get("serverTime"))
        now_ms = int(time.time() * 1000)
        self._delta_ms = srv_ms - now_ms
        log.info("Bitget time synced. delta_ms=%s", self._delta_ms)

    def _sign(self, ts: str, method: str, request_path: str, body: str = "") -> str:
        """
        sign = HMAC_SHA256(secret, ts + method + request_path + body) -> base64
        Bitget v2 사인 타입 '2' 사용.
        """
        import base64

        pre = f"{ts}{method.upper()}{request_path}{body}"
        h = hmac.new(self.api_secret, pre.encode(), hashlib.sha256).digest()
        return base64.b64encode(h).decode()

    def _headers(self, ts: str, sign: str) -> Dict[str, str]:
        return {
            "ACCESS-KEY": self.api_key,
            "ACCESS-SIGN": sign,
            "ACCESS-TIMESTAMP": ts,
            "ACCESS-PASSPHRASE": self.passphrase,
            "ACCESS-SIGN-TYPE": "2",
            "Content-Type": "application/json",
            "X-CHANNEL-API-CODE": "PY",
        }

    # --------------------------------------------------------------------- #
    # 요청 래퍼
    # --------------------------------------------------------------------- #
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        timeout: int = 15,
    ) -> Dict[str, Any]:
        """
        Bitget 호출 공통부. 실패 시 HTTPError 발생.
        Bitget 에러는 로그에 code/msg와 함께 남긴다.
        """
        url = f"{self.base_url}{path}"
        params = params or {}
        body = body or {}
        body_str = json.dumps(body, separators=(",", ":"), ensure_ascii=False) if body else ""
        # 서명용 path(+query)는 request_path 기준
        query = ""
        if params:
            # Bitget 서명은 request_path?queryString 형태
            from urllib.parse import urlencode

            query = "?" + urlencode(params, doseq=True)

        # 시간 스탬프
        ts = str(self._ts_ms())

        sign = self._sign(ts, method, f"{path}{query}", body_str)
        headers = self._headers(ts, sign)

        # 전송
        try:
            if method.upper() == "GET":
                resp = self.session.get(url, headers=headers, params=params, timeout=timeout)
            elif method.upper() == "POST":
                resp = self.session.post(url, headers=headers, params=params, data=body_str, timeout=timeout)
            else:
                raise ValueError(f"Unsupported method: {method}")
        except requests.RequestException as e:
            # 네트워크 오류 등
            raise

        # 2xx 아닌 경우 Bitget 본문을 읽어서 같이 올림
        try:
            resp.raise_for_status()
        except requests.HTTPError as e:
            detail: Any = {}
            try:
                detail = resp.json()
            except Exception:
                pass
            log.error(
                "Bitget HTTP %s %s -> %s | url=%s | body=%s",
                method, path, resp.status_code, resp.url, body_str,
            )
            log.error("Bitget response: %s", detail)
            raise requests.HTTPError(
                f"{e} | url={resp.url} | body={body_str} | detail={detail}"
            ) from e

        # 정상 응답
        data = resp.json()
        # {"code":"00000","msg":"success", ...}
        if data.get("code") != "00000":
            log.error("Bitget logical error: %s", data)
            raise requests.HTTPError(f"Bitget logical error: {data}")
        return data

    # --------------------------------------------------------------------- #
    # 정보/포지션
    # --------------------------------------------------------------------- #
    def get_net_position(self, symbol: str) -> Dict[str, float]:
        """
        현재 심볼의 순포지션 수량(롱:+, 숏:-)을 대략 계산.
        Bitget 응답 포맷이 상황에 따라 달라질 수 있어 최대한 안전하게 파싱.
        """
        path = "/api/mix/v1/position/singlePosition"
        params = {
            "symbol": symbol,
            "marginCoin": self.margin_coin,
            "productType": self.product_type,
        }
        data = self._request("GET", path, params=params)
        d = data.get("data")
        net = 0.0

        # data가 리스트(포지션들)일 수도 있고, 단일 딕셔너리일 수도 있음
        if isinstance(d, list):
            for p in d:
                try:
                    hold_side = (p.get("holdSide") or "").lower()
                    sz = float(p.get("total", p.get("available", p.get("openAmount", 0))) or 0)
                    if hold_side == "long":
                        net += sz
                    elif hold_side == "short":
                        net -= sz
                except Exception:
                    continue
        elif isinstance(d, dict):
            try:
                hold_side = (d.get("holdSide") or "").lower()
                sz = float(d.get("total", d.get("available", d.get("openAmount", 0))) or 0)
                if hold_side == "long":
                    net += sz
                elif hold_side == "short":
                    net -= sz
            except Exception:
                pass

        return {"net": net}

    # --------------------------------------------------------------------- #
    # 주문
    # --------------------------------------------------------------------- #
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
        시장가 주문(표준 one-way → side mismatch면 hedge로 재시도).
        """
        path = "/api/mix/v1/order/placeOrder"
        order_type = "market" if type.upper() == "MARKET" else "limit"

        def _body(side_value: str) -> Dict[str, Any]:
            b: Dict[str, Any] = {
                "symbol": symbol,
                "marginCoin": self.margin_coin,
                "productType": self.product_type,
                "side": side_value,            # one-way: buy/sell, hedge: open_long/...
                "orderType": order_type,
                "size": str(size),
                "reduceOnly": bool(reduce_only),
            }
            if client_oid:
                b["clientOid"] = client_oid
            return b

        # 1) one-way로 우선 시도 (원웨이 계정이라면 여기서 성공)
        body1 = _body(side.lower())
        try:
            return self._request("POST", path, body=body1)
        except requests.HTTPError as e:
            msg = str(e)
            # 400172(side mismatch)일 때만 hedge 형식으로 재시도
            if ("400172" not in msg) and ("side mismatch" not in msg.lower()):
                raise

            if reduce_only:
                mapped = "close_short" if side.lower() == "buy" else "close_long"
            else:
                mapped = "open_long" if side.lower() == "buy" else "open_short"

            body2 = _body(mapped)
            log.warning(
                "Bitget side mismatch -> retry with hedge side: %s (reduceOnly=%s)", mapped, reduce_only
            )
            return self._request("POST", path, body=body2)
