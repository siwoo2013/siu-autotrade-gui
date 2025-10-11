# bitget_client.py
from __future__ import annotations

import time
import hmac
import hashlib
import json
from typing import Any, Dict
import requests


API_BASE = "https://api.bitget.com"


class BitgetClient:
    """
    Bitget Mix(선물) 간단 클라이언트
      - 포지션 조회: singlePosition (productType 필수)
      - 마켓 주문: placeOrder (open/close)
    """

    def __init__(self, api_key: str, api_secret: str, passphrase: str, demo: bool = False) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.session = requests.Session()
        self.demo = demo  # 로깅용 플래그

    # ---------- 내부 헬퍼 ----------

    def _product_type(self, symbol: str) -> str:
        """
        심볼에서 productType 자동 유추.
        * *_UMCBL -> umcbl
        * *_DMCBL -> dmcbl
        * 그 외는 기본 umcbl
        """
        s = symbol.upper()
        if s.endswith("_UMCBL"):
            return "umcbl"
        if s.endswith("_DMCBL"):
            return "dmcbl"
        return "umcbl"

    def _ts(self) -> str:
        return str(int(time.time() * 1000))

    def _sign(self, ts: str, method: str, path_with_qs: str, body: str = "") -> str:
        msg = ts + method + path_with_qs + body
        return hmac.new(self.api_secret.encode(), msg.encode(), hashlib.sha256).hexdigest()

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
        params: Dict[str, Any] | None = None,
        body: Dict[str, Any] | None = None,
    ) -> Any:
        """
        Bitget 요청 공통부. 4xx/5xx 시 응답 본문까지 포함시켜 예외로 던진다.
        """
        url = API_BASE + path
        ts = self._ts()

        q = params or {}
        b = body or {}

        # 고정 정렬 쿼리스트링(서명 일관성)
        if q:
            from urllib.parse import urlencode
            qs = "?" + urlencode(sorted(q.items()))
        else:
            qs = ""

        body_str = json.dumps(b, separators=(",", ":")) if b else ""

        sign = self._sign(ts, method, path + qs, body_str)
        headers = self._headers(ts, sign)

        if method == "GET":
            resp = self.session.get(url, headers=headers, params=q, timeout=10)
        else:
            resp = self.session.post(url, headers=headers, params=q, data=body_str, timeout=10)

        if resp.status_code >= 400:
            # ★ 상태코드 에러 → 본문까지 예외 메시지에 포함
            raise requests.HTTPError(
                f"{resp.status_code} {resp.reason} | url={resp.url} | body={resp.text}",
                response=resp,
            )

        data = resp.json()
        # Bitget은 200이어도 내부 code가 실패일 수 있음
        if isinstance(data, dict) and data.get("code") not in (None, "00000", "0"):
            raise requests.HTTPError(
                f"Bitget error code={data.get('code')} msg={data.get('msg')} | url={resp.url} | body={resp.text}"
            )

        return data.get("data", data)

    # ---------- 공개 메서드 ----------

    def get_net_position(self, symbol: str, margin_coin: str = "USDT") -> Dict[str, float]:
        """
        현재 심볼의 순포지션 수량(net)을 반환.
        """
        product_type = self._product_type(symbol)

        path = "/api/mix/v1/position/singlePosition"
        params = {
            "symbol": symbol,
            "marginCoin": margin_coin,
            "productType": product_type,   # ★ 필수
        }
        data = self._request("GET", path, params=params)

        long_total = float(data.get("long", {}).get("total", 0) or 0)
        short_total = float(data.get("short", {}).get("total", 0) or 0)
        net = long_total - short_total
        return {"net": net}

    def place_order(
        self,
        symbol: str,
        side: str,                 # "BUY" | "SELL"
        size: float,
        order_type: str = "MARKET",
        reduce_only: bool = False,
        client_oid: str | None = None,
        margin_coin: str = "USDT",
    ) -> Any:
        """
        마켓 주문. (open/close 를 side + reduce_only 로 결정)
        """
        product_type = self._product_type(symbol)

        side = side.upper()
        if reduce_only:
            api_side = "close_short" if side == "BUY" else "close_long"
        else:
            api_side = "open_long" if side == "BUY" else "open_short"

        path = "/api/mix/v1/order/placeOrder"
        body = {
            "symbol": symbol,
            "productType": product_type,
            "marginCoin": margin_coin,
            "size": str(size),
            "side": api_side,
            "orderType": "market" if order_type.upper() == "MARKET" else "limit",
        }
        if client_oid:
            body["clientOid"] = client_oid

        return self._request("POST", path, body=body)
