# -*- coding: utf-8 -*-
"""
Bitget REST client (UMCBL / One-Way 전용)
- 안정화 포인트:
  * 모든 요청에 'Connection: close' 강제 (keep-alive 재사용 끊기)
  * ConnectionResetError / requests.ConnectionError 재시도(지수 백오프)
  * 5xx/429 자동 재시도
- 서버(server.py) 기대 시그니처에 맞춤:
    BitgetClient(api_key, api_secret, passphrase, mode="live", timeout=10)
    .get_hedge_sizes(symbol)
    .place_order(symbol, side, order_type, size, reduce_only, client_oid)
    .place_market_order(symbol, side, size, reduce_only=False)
    .query_position_mode()         (가능하면; 실패 시 예외)
    .ensure_unilateral_mode()      (가능하면; 실패 시 예외)
- 심볼은 server.py에서 BTCUSDT.P -> BTCUSDT_UMCBL 로 변환되어 들어온다고 가정.
"""

import time
import json
import hmac
import hashlib
import logging
from typing import Dict, Any, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("uvicorn.error")


class BitgetHTTPError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"bitget-http status={status} body={body}")
        self.status = status
        self.body = body


class BitgetClient:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        passphrase: str,
        mode: str = "live",
        timeout: int = 10,
        base_url: Optional[str] = None,
    ):
        self.api_key = api_key
        self.api_secret = api_secret.encode()
        self.passphrase = passphrase
        self.timeout = timeout

        # Bitget U本位 선물(UMCBL) 표준 도메인
        self.base_url = base_url or "https://api.bitget.com"

        # requests.Session with robust retry
        self.session = self._new_session()

        # 제품 계열 (umcbl) 고정 사용. marginCoin은 USDT로.
        self.margin_coin = "USDT"
        self.product_type = "umcbl"

    # ---------- Session / Retry ----------
    def _new_session(self) -> requests.Session:
        s = requests.Session()
        retry = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=frozenset(["GET", "POST"]),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=2, pool_maxsize=2)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        # 헤더 기본값
        s.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-CHANNEL-API-CODE": "siu-autotrade",  # 임의 식별자(옵션)
            # 핵심: keep-alive 재사용 끊기 (연결 재설정 방지)
            "Connection": "close",
        })
        return s

    # ---------- Sign ----------
    @staticmethod
    def _ts_ms() -> str:
        return str(int(time.time() * 1000))

    def _sign(self, ts: str, method: str, path_with_qs: str, body: str) -> str:
        # bitget: sign = HMAC_SHA256(timestamp + method + requestPath + body)
        message = f"{ts}{method.upper()}{path_with_qs}{body}".encode()
        return hmac.new(self.api_secret, message, hashlib.sha256).hexdigest()

    # ---------- Low-level request with retry for ConnectionReset ----------
    def _request(self, method: str, path: str, params: dict = None, body: dict = None) -> Any:
        url = self.base_url + path
        params = params or {}
        body = body or {}

        qs = ""
        if params:
            # Bitget는 쿼리 문자열도 시그니처에 포함되도록 requestPath 그대로 사용해야 함
            items = [f"{k}={params[k]}" for k in sorted(params.keys())]
            qs = "?" + "&".join(items)

        request_path_for_sign = path + qs
        payload = json.dumps(body, separators=(",", ":"), ensure_ascii=False)

        ts = self._ts_ms()
        sign = self._sign(ts, method, request_path_for_sign, payload if method.upper() != "GET" else "")

        headers = {
            "ACCESS-KEY": self.api_key,
            "ACCESS-SIGN": sign,
            "ACCESS-PASSPHRASE": self.passphrase,
            "ACCESS-TIMESTAMP": ts,
            "Locale": "en-US",
            # keep-alive로 인한 재사용을 끊기
            "Connection": "close",
        }

        # 실제 호출 (Connection reset 시도 시 세션 재생성하여 재시도)
        attempts = 0
        last_exc: Optional[Exception] = None
        while attempts < 3:
            try:
                if method.upper() == "GET":
                    resp = self.session.get(url, headers=headers, params=params, timeout=(4, self.timeout), verify=True)
                else:
                    resp = self.session.post(url, headers=headers, params=params, data=payload, timeout=(4, self.timeout), verify=True)

                # Bitget 표준 응답 처리
                if resp.status_code >= 400:
                    raise BitgetHTTPError(resp.status_code, f"http-error: {resp.text}")

                data = resp.json()
                # 일부 v1 API는 {"code":"00000","msg":"success","data":...} 형태
                # 실패일 때 code != "00000"
                if isinstance(data, dict) and "code" in data and data["code"] not in ("00000", 0, "0"):
                    raise BitgetHTTPError(resp.status_code, f"api-error: {resp.text}")

                return data.get("data") if isinstance(data, dict) and "data" in data else data

            except (requests.exceptions.ConnectionError, ConnectionResetError) as e:
                last_exc = e
                log.warning(f"[bitget] connection error, retrying... attempts={attempts+1} err={e}")
                # 세션 완전 재생성
                try:
                    self.session.close()
                except Exception:
                    pass
                self.session = self._new_session()
                time.sleep(0.6 * (attempts + 1))
                attempts += 1
                continue
            except requests.exceptions.ReadTimeout as e:
                last_exc = e
                log.warning(f"[bitget] read timeout, retrying... attempts={attempts+1}")
                time.sleep(0.6 * (attempts + 1))
                attempts += 1
                continue

        # 모든 재시도 실패
        if last_exc:
            raise BitgetHTTPError(-1, f"body=requests-error: {repr(last_exc)}")
        raise BitgetHTTPError(-1, "unknown-error")

    # ---------- Public-ish helpers ----------
    def server_time(self) -> Any:
        # v1 마켓 타임은 지역별로 404가 날 수 있어; 실패해도 무시 권장
        try:
            return self._request("GET", "/api/mix/v1/market/time")
        except BitgetHTTPError as e:
            log.warning(f"BitgetClient: initial time sync failed: {e}")
            return None

    # ---------- Position / Accounts ----------
    def _positions(self, symbol: str) -> Any:
        # GET /api/mix/v1/position/singlePosition?symbol=BTCUSDT_UMCBL&marginCoin=USDT
        params = {"symbol": symbol, "marginCoin": self.margin_coin}
        return self._request("GET", "/api/mix/v1/position/singlePosition", params=params)

    def get_hedge_sizes(self, symbol: str) -> Dict[str, float]:
        """
        원웨이 기준이라도, API 응답에는 long/short 구성이 들어온다.
        없으면 0.0으로 처리.
        """
        data = self._positions(symbol)
        long_sz = 0.0
        short_sz = 0.0
        if isinstance(data, dict):
            long_pos = data.get("long")
            short_pos = data.get("short")
            if isinstance(long_pos, dict):
                try:
                    long_sz = float(long_pos.get("total", 0))  # 포지션 수량 키는 total or available depending on API
                except Exception:
                    pass
            if isinstance(short_pos, dict):
                try:
                    short_sz = float(short_pos.get("total", 0))
                except Exception:
                    pass
        return {"long": long_sz, "short": short_sz}

    # ---------- Orders ----------
    def place_order(
        self,
        *,
        symbol: str,
        side: str,                 # "buy" | "sell"
        order_type: str,           # "market" | "limit"
        size: float,
        reduce_only: bool = False,
        client_oid: Optional[str] = None,
        price: Optional[float] = None,
    ) -> str:
        """
        POST /api/mix/v1/order/placeOrder
        returns clientOid (또는 orderId 계열) 문자열
        """
        body = {
            "symbol": symbol,
            "marginCoin": self.margin_coin,
            "size": f"{size}",
            "side": side.lower(),                   # buy / sell
            "orderType": order_type.lower(),        # market / limit
            "reduceOnly": reduce_only,
            "timeInForceValue": "normal",
        }
        if client_oid:
            body["clientOid"] = client_oid
        if price is not None:
            body["price"] = f"{price}"

        data = self._request("POST", "/api/mix/v1/order/placeOrder", body=body)
        # 성공 시 {"orderId":"...","clientOid":"..."}류가 오는데, 통일해서 clientOid 우선 반환
        if isinstance(data, dict):
            return str(data.get("clientOid") or data.get("orderId") or "")
        return str(data)

    def place_market_order(self, *, symbol: str, side: str, size: float, reduce_only: bool = False) -> Dict[str, Any]:
        oid = self.place_order(
            symbol=symbol,
            side=side,
            order_type="market",
            size=size,
            reduce_only=reduce_only,
            client_oid=f"siu-{int(time.time()*1000)}",
        )
        return {"clientOid": oid, "symbol": symbol, "side": side, "size": size, "reduceOnly": reduce_only}

    # ---------- Mode Ops (best-effort) ----------
    def query_position_mode(self) -> str:
        """
        Bitget에 모드 조회 API가 리전/버전에 따라 다르므로, 실패 시 BitgetHTTPError 발생할 수 있음.
        """
        # 일부 문서 기준: GET /api/mix/v1/account/positionMode?productType=umcbl
        params = {"productType": self.product_type}
        data = self._request("GET", "/api/mix/v1/account/positionMode", params=params)
        # {"holdMode":"single_hold"} 기대
        if isinstance(data, dict):
            return str(data.get("holdMode") or data.get("posMode") or "unknown")
        return "unknown"

    def ensure_unilateral_mode(self) -> str:
        """
        단방향 모드로 전환. 실패하면 예외(404 등) 던질 수 있음.
        """
        # POST /api/mix/v1/account/setPositionMode  body: {"productType":"umcbl","holdMode":"single_hold"}
        body = {"productType": self.product_type, "holdMode": "single_hold"}
        self._request("POST", "/api/mix/v1/account/setPositionMode", body=body)
        return "single_hold"
