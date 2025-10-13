# bitget_client.py
# - Mix 선물(UMCBL) 원웨이 호환
# - 주문 side: open_long/open_short/close_long/close_short (buy/sell 사용 안 함)
# - 타임스탬프 ms + 서버시간 동기화 + 40008 자동 재시도
# - 포지션 조회: /position/allPosition 사용

import time
import json
import hmac
import base64
import logging
from typing import Any, Dict, Optional, List
from urllib.parse import urlencode

import requests

log = logging.getLogger("uvicorn.error")


class BitgetHTTPError(Exception):
    def __init__(self, status_code: int, payload: Any):
        self.status_code = status_code
        self.payload = payload
        super().__init__(f"bitget-http status={status_code} body={payload}")


class BitgetClient:
    BASE_URL = "https://api.bitget.com"
    PRODUCT_TYPE = "umcbl"   # USDT-M Perp
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
        self._time_offset_ms = 0

        if not (self.api_key and self.api_secret and self.passphrase):
            log.warning("BitgetClient: API key/secret/passphrase not fully provided.")

        try:
            self.sync_time()
        except Exception as e:
            log.warning(f"BitgetClient: initial time sync failed: {e}")

    # ------------ 시간/서명 ------------
    def _now_ms(self) -> int:
        return int(time.time() * 1000)

    def _timestamp(self) -> str:
        return str(self._now_ms() + self._time_offset_ms)

    def sync_time(self):
        path = "/api/mix/v1/market/time"
        url = self.BASE_URL + path
        resp = self.session.get(url, timeout=self.timeout)
        resp.raise_for_status()
        js = resp.json()
        code = str(js.get("code") or js.get("status") or "")
        if code not in ("00000", "0"):
            raise RuntimeError(js)
        server_ms = int(js.get("data"))
        local_ms = self._now_ms()
        self._time_offset_ms = server_ms - local_ms
        log.info(f"Bitget time sync: offset={self._time_offset_ms}ms")

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
        }

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        _retry_on_40008: bool = True,
    ) -> Any:
        method = method.upper()
        url = self.BASE_URL + path

        query = ""
        if params:
            query = "?" + urlencode(params, doseq=True)
            url += query

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

        if not (200 <= resp.status_code < 300):
            raise BitgetHTTPError(resp.status_code, f"http-error: {resp.text}")

        try:
            data = resp.json()
        except Exception:
            raise BitgetHTTPError(resp.status_code, f"invalid-json: {resp.text}")

        code = str(data.get("code") or data.get("status") or "")
        if code not in ("00000", "0"):
            if _retry_on_40008 and ("40008" in json.dumps(data)):
                log.warning("Bitget 40008 detected → resync time & retry once")
                try:
                    self.sync_time()
                except Exception as e:
                    log.warning(f"time resync failed: {e}")
                return self._request(method, path, params, body, _retry_on_40008=False)
            raise BitgetHTTPError(resp.status_code, data)

        return data

    # ------------ 주문/포지션 ------------
    def _mix_side(self, side: str, reduce_only: bool) -> str:
        """
        입력: side='buy'|'sell', reduce_only(bool)
        출력: Bitget mix side(open_long/open_short/close_long/close_short)
        """
        s = (side or "").lower().strip()
        if s not in ("buy", "sell"):
            raise ValueError("side must be 'buy' or 'sell'")
        if reduce_only:
            # long 청산은 sell, short 청산은 buy
            return "close_long" if s == "sell" else "close_short"
        else:
            # 신규 오픈: buy→open_long, sell→open_short
            return "open_long" if s == "buy" else "open_short"

    def place_order(
        self,
        symbol: str,
        side: str,                 # 'buy' or 'sell'
        order_type: str,           # 'market' or 'limit'
        size: float,
        reduce_only: bool = False,
        client_oid: str = "",
        price: Optional[float] = None,
    ) -> str:
        ot = (order_type or "").lower().strip()
        if ot not in ("market", "limit"):
            raise ValueError("order_type must be 'market' or 'limit'")

        mix_side = self._mix_side(side, reduce_only)

        path = "/api/mix/v1/order/placeOrder"
        body: Dict[str, Any] = {
            "symbol": symbol,
            "productType": self.PRODUCT_TYPE,
            "marginCoin": self.MARGIN_COIN,
            "size": str(size),
            "side": mix_side,              # ✅ open_/close_ 형태로 전송
            "orderType": ot,
        }
        if client_oid:
            body["clientOid"] = client_oid
        if price is not None and ot == "limit":
            body["price"] = str(price)

        res = self._request("POST", path, body=body)
        data = res.get("data") or {}
        return str(data.get("orderId") or data.get("order_id") or data.get("id") or "")

    def _all_positions(self) -> List[Dict[str, Any]]:
        """모든 포지션(해당 productType)을 가져온다."""
        path = "/api/mix/v1/position/allPosition"
        params = {"productType": self.PRODUCT_TYPE}
        res = self._request("GET", path, params=params)
        return res.get("data") or []

    def get_hedge_sizes(self, symbol: str) -> Dict[str, float]:
        """
        long/short 보유 수량을 합산해 리턴(원웨이도 동일 인터페이스).
        """
        items = self._all_positions()
        long_sz = 0.0
        short_sz = 0.0
        for it in items:
            if it.get("symbol") != symbol:
                continue
            hold_side = str(it.get("holdSide") or it.get("side") or "").lower()
            # Bitget 응답: total | available | openAmount 등 케이스가 다양 → 우선 total
            total = float(it.get("total") or it.get("available") or it.get("openAmount") or 0.0)
            if hold_side == "long":
                long_sz += total
            elif hold_side == "short":
                short_sz += total
        return {"long": float(long_sz), "short": float(short_sz)}

    def get_avg_entry_price(self, symbol: str) -> float:
        items = self._all_positions()
        long_price = None
        short_price = None
        for it in items:
            if it.get("symbol") != symbol:
                continue
            hold_side = str(it.get("holdSide") or it.get("side") or "").lower()
            avg = float(it.get("avgOpenPrice") or it.get("openPrice") or 0.0)
            total = float(it.get("total") or 0.0)
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
