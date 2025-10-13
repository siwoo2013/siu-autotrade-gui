# bitget_client.py
# - One-Way 모드 호환: 주문에 holdSide 절대 전송하지 않음
# - 서버(server.py)가 사용하는 최소 메서드만 안정적으로 제공
#   * place_order()
#   * get_hedge_sizes()  -> one-way에서도 long/short 형태로 리턴(내부 변환)
#   * get_avg_entry_price()

import time
import json
import hmac
import base64
import logging
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import requests

log = logging.getLogger("uvicorn.error")


class BitgetHTTPError(Exception):
    """Bitget API에서 비정상 응답(code != 00000 / HTTP 오류)시 발생"""
    def __init__(self, status_code: int, payload: Any):
        self.status_code = status_code
        self.payload = payload
        super().__init__(f"bitget-http status={status_code} body={payload}")


class BitgetClient:
    """
    Bitget USDT-M 선물(UMCBL) 전용 클라이언트 (One-Way 모드 호환)
    - 주문 시 holdSide 미전송
    - 포지션 조회로 long/short 보유량을 유추
    """

    BASE_URL = "https://api.bitget.com"
    PRODUCT_TYPE = "umcbl"   # API 파라미터는 소문자 사용 권장
    MARGIN_COIN = "USDT"

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        passphrase: str,
        mode: str = "live",
        timeout: int = 10,
        session: Optional[requests.Session] = None,
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.mode = (mode or "live").lower()
        self.timeout = timeout
        self.session = session or requests.Session()

        # 간단한 유효성 체크
        if not (self.api_key and self.api_secret and self.passphrase):
            log.warning("BitgetClient: API key/secret/passphrase not fully provided.")

    # -------------------------------
    # 내부: 서명/요청
    # -------------------------------
    def _timestamp(self) -> str:
        # Bitget는 초 단위 문자열(밀리초 허용). 여기서는 초 단위로 전송.
        return str(int(time.time()))

    def _sign(self, ts: str, method: str, request_path: str, body_str: str) -> str:
        msg = f"{ts}{method.upper()}{request_path}{body_str}"
        mac = hmac.new(self.api_secret.encode(), msg.encode(), digestmod="sha256")
        return base64.b64encode(mac.digest()).decode()

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
    ) -> Any:
        """
        Bitget REST 요청. 실패 시 BitgetHTTPError 발생.
        """
        method = method.upper()
        url = self.BASE_URL + path

        # 쿼리스트링 구성 (GET 등)
        query = ""
        if params:
            query = "?" + urlencode(params, doseq=True)
            url = url + query

        body_str = json.dumps(body or {}, separators=(",", ":"), ensure_ascii=False) if body else ""
        ts = self._timestamp()
        sign = self._sign(ts, method, path + (query if query else ""), body_str)
        headers = self._headers(ts, sign)

        try:
            if method == "GET":
                resp = self.session.get(url, headers=headers, timeout=self.timeout)
            elif method == "POST":
                resp = self.session.post(url, data=body_str, headers=headers, timeout=self.timeout)
            elif method == "DELETE":
                resp = self.session.delete(url, data=body_str, headers=headers, timeout=self.timeout)
            else:
                raise ValueError(f"Unsupported method: {method}")

        except requests.RequestException as e:
            raise BitgetHTTPError(-1, f"requests-error: {e}") from e

        # HTTP 오류
        if not (200 <= resp.status_code < 300):
            raise BitgetHTTPError(resp.status_code, f"http-error: {resp.text}")

        # Bitget 표준 응답 파싱
        try:
            data = resp.json()
        except Exception:
            raise BitgetHTTPError(resp.status_code, f"invalid-json: {resp.text}")

        code = str(data.get("code") or data.get("status") or "")
        if code not in ("00000", "0"):
            # Bitget은 정상일 때 code=00000
            raise BitgetHTTPError(resp.status_code, data)

        return data

    # -------------------------------
    # 공개 메서드
    # -------------------------------
    def place_order(
        self,
        symbol: str,
        side: str,                # 'buy' or 'sell'
        order_type: str,          # 'market' or 'limit'
        size: float,
        reduce_only: bool = False,
        client_oid: str = "",
        price: Optional[float] = None,
    ) -> str:
        """
        시장가/지정가 주문.
        - One-Way 모드 호환: holdSide 절대 전송 금지
        - reduce_only=True면 전량/부분 청산용(시장가 권장)
        """
        s = (side or "").lower().strip()
        if s not in ("buy", "sell"):
            raise ValueError("side must be 'buy' or 'sell'")

        ot = (order_type or "").lower().strip()
        if ot not in ("market", "limit"):
            raise ValueError("order_type must be 'market' or 'limit'")

        path = "/api/mix/v1/order/placeOrder"
        body: Dict[str, Any] = {
            "symbol": symbol,
            "productType": self.PRODUCT_TYPE,   # "umcbl"
            "marginCoin": self.MARGIN_COIN,     # "USDT"
            "size": str(size),
            "side": s,                          # ✅ one-way: side만 사용
            "orderType": ot,
            "reduceOnly": bool(reduce_only),
        }
        if client_oid:
            body["clientOid"] = client_oid
        if price is not None and ot == "limit":
            body["price"] = str(price)

        res = self._request("POST", path, body=body)
        data = res.get("data") or {}
        order_id = str(data.get("orderId") or data.get("order_id") or data.get("id") or "")
        return order_id

    def get_hedge_sizes(self, symbol: str) -> Dict[str, float]:
        """
        서버에서 공통으로 부르는 헬퍼.
        - 헷지 모드에선 long/short가 동시에 존재 가능
        - 원웨이 모드에선 한 방향만 존재하지만, 인터페이스를 맞추기 위해
          내부에서 long/short로 매핑해서 리턴한다.
        """
        path = "/api/mix/v1/position/queryPosition"
        params = {"symbol": symbol, "productType": self.PRODUCT_TYPE}
        res = self._request("GET", path, params=params)
        items = res.get("data") or []

        long_sz = 0.0
        short_sz = 0.0

        # Bitget은 포지션 객체에 holdSide 혹은 side 비슷한 필드 제공
        # one-way에서도 'holdSide'가 'long' 또는 'short'로 들어온다.
        for it in items:
            try:
                hold_side = str(it.get("holdSide") or it.get("side") or "").lower()
                total = float(it.get("total") or it.get("available") or it.get("openAmount") or 0.0)
                # 일부 응답 필드명은 시기마다 조금씩 다름. total/available 둘 다 시도.
            except Exception:
                continue

            if hold_side == "long":
                long_sz += total
            elif hold_side == "short":
                short_sz += total
            else:
                # 혹시 모르는 경우: size 숫자와 avgOpenPrice 등으로 방향 추정
                # openPrice/avgOpenPrice는 둘 다 존재 가능. 여기선 holdSide가 없으면 스킵.
                pass

        return {"long": float(long_sz), "short": float(short_sz)}

    def get_avg_entry_price(self, symbol: str) -> float:
        """
        평균 진입가(보유중인 포지션 기준).
        - one-way에선 현재 보유 방향의 avgOpenPrice를 반환.
        - 양방향(헷지)이더라도 평균가가 존재하는 쪽을 우선 반환(단순화).
        """
        path = "/api/mix/v1/position/queryPosition"
        params = {"symbol": symbol, "productType": self.PRODUCT_TYPE}
        res = self._request("GET", path, params=params)
        items = res.get("data") or []

        # 우선 long 우선, 없으면 short
        long_price = None
        short_price = None

        for it in items:
            try:
                hold_side = str(it.get("holdSide") or it.get("side") or "").lower()
                avg = float(it.get("avgOpenPrice") or it.get("openPrice") or 0.0)
                total = float(it.get("total") or 0.0)
            except Exception:
                continue

            if total <= 0:
                continue

            if hold_side == "long" and avg > 0:
                long_price = avg
            elif hold_side == "short" and avg > 0:
                short_price = avg

        if long_price:
            return float(long_price)
        if short_price:
            return float(short_price)
        return 0.0

    # (선택) 취소/플랜주문 등은 필요 시 확장
    # def cancel_order(self, symbol: str, order_id: str) -> bool: ...
    # def place_tp_order(...): ...
