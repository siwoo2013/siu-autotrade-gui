import hashlib
import hmac
import json
import time
from typing import Any, Dict, Optional, Tuple, List

import requests


class BitgetHTTPError(Exception):
    pass


class BitgetClient:
    """
    최소한의 Bitget U本位(UMCBL) 선물 연동 래퍼.
    - 네트워크/Bitget 오류 메시지를 풍부하게 남기도록 _request 강화
    - singlePosition 조회 시 productType=umcbl 명시 (400 방지)
    - 응답이 dict/list 어느 쪽이어도 파싱되게 방어코드
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        passphrase: str,
        base_url: str = "https://api.bitget.com",
        timeout: float = 8.0,
        user_agent: str = "siu-autotrade/1.0",
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.base = base_url.rstrip("/")
        self.timeout = timeout

        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent})

    # -------------------------------------------------------------------------
    # 내부 공용
    # -------------------------------------------------------------------------

    def _sign(
        self,
        ts_ms: str,
        method: str,
        path: str,
        params: Dict[str, Any],
        body: Dict[str, Any],
    ) -> str:
        query = ""
        if params:
            # Bitget는 Query string을 key=value&key2=value2 순으로 붙여 넣어야 함
            query = "?" + "&".join(
                f"{k}={params[k]}" for k in sorted(params.keys())
            )

        payload = ""
        if body:
            payload = json.dumps(body, separators=(",", ":"), ensure_ascii=False)

        prehash = ts_ms + method.upper() + path + query + payload
        digest = hmac.new(
            self.api_secret.encode("utf-8"),
            prehash.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).hexdigest()
        return digest

    def _request(
        self,
        m: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
    ):
        """
        Bitget REST 요청. 2xx가 아니면 Bitget code/msg와 함께 예외 발생.
        """
        url = self.base + path
        ts = str(int(time.time() * 1000))
        q = params or {}
        b = body or {}

        sign = self._sign(ts, m, path, q, b)
        headers = {
            "Content-Type": "application/json",
            "ACCESS-KEY": self.api_key,
            "ACCESS-SIGN": sign,
            "ACCESS-TIMESTAMP": ts,
            "ACCESS-PASSPHRASE": self.passphrase,
        }

        resp = self.session.request(
            method=m,
            url=url,
            params=q,
            json=b if b else None,
            headers=headers,
            timeout=self.timeout,
        )

        # 2xx면 정상
        if 200 <= resp.status_code < 300:
            try:
                data = resp.json()
            except Exception:
                raise BitgetHTTPError(
                    f"bitget-http invalid-json status={resp.status_code} body={resp.text[:300]}"
                )
            # Bitget 표준 성공 코드는 '00000'
            if isinstance(data, dict) and data.get("code") not in (None, "00000", 0):
                raise BitgetHTTPError(
                    f"bitget-http code={data.get('code')} msg={data.get('msg')}"
                )
            # 보통 'data' 필드에 실제 페이로드가 들어옴
            return data.get("data", data)

        # 비정상: Bitget의 code/msg를 최대한 추출해 로그 풍부화
        try:
            j = resp.json()
            code = j.get("code")
            msg = j.get("msg")
            raise BitgetHTTPError(
                f"bitget-http status={resp.status_code} code={code} msg={msg}"
            )
        except Exception:
            raise BitgetHTTPError(
                f"bitget-http status={resp.status_code} body={resp.text[:300]}"
            )

    # -------------------------------------------------------------------------
    # 조회/포지션
    # -------------------------------------------------------------------------

    def get_hedge_sizes(self, symbol: str) -> Tuple[float, float]:
        """
        현재 심볼의 헤지 모드 포지션 수량(long/short)을 반환.
        Bitget가 상황에 따라 data를 dict/list 어느 쪽으로 주는지 몰라서 전부 대비.
        또한 일부 환경에서 productType=umcbl을 요구하므로 명시한다.
        """
        path = "/api/mix/v1/position/singlePosition"
        params = {
            "symbol": symbol,
            "marginCoin": "USDT",
            "productType": "umcbl",  # ★ 400 방지
        }
        data = self._request("GET", path, params=params)

        # data가 dict일 수도, list일 수도 있음
        if isinstance(data, dict):
            items: List[Dict[str, Any]] = data.get("data") or data.get("positions") or []
        else:
            items = data or []

        long_sz = 0.0
        short_sz = 0.0

        def _to_f(x) -> float:
            try:
                return float(x)
            except Exception:
                return 0.0

        for it in items:
            hold = (it.get("holdSide") or it.get("side") or "").lower()
            # 크기 키 이름이 환경에 따라 다를 수 있어 방어적 파싱
            size = _to_f(it.get("total") or it.get("totalSize") or it.get("available") or 0)
            if hold == "long":
                long_sz = size
            elif hold == "short":
                short_sz = size

        return long_sz, short_sz

    # -------------------------------------------------------------------------
    # 주문
    # -------------------------------------------------------------------------

    def place_order(
        self,
        symbol: str,
        side: str,  # "buy" | "sell"
        size: float,
        order_type: str = "market",  # "market" | "limit"
        reduce_only: bool = False,
        client_oid: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        일반 주문(시장가/지정가). Hedge 모드 사용.
        side: "buy"(=LONG 오픈 or SHORT 청산), "sell"(=SHORT 오픈 or LONG 청산)
        """
        path = "/api/mix/v1/order/placeOrder"
        body = {
            "symbol": symbol,
            "marginCoin": "USDT",
            "productType": "umcbl",
            "side": side,
            "orderType": order_type,
            "size": f"{size:.6f}",
            "reduceOnly": reduce_only,
        }
        if client_oid:
            body["clientOid"] = client_oid

        return self._request("POST", path, body=body)

    # 전량 청산(holdSide 기준)
    def close_all(
        self,
        symbol: str,
        hold_side: str,  # "long" | "short"
    ) -> Dict[str, Any]:
        """
        해당 holdSide 전량 청산 (시장가)
        """
        if hold_side.lower() not in ("long", "short"):
            raise ValueError("hold_side must be 'long' or 'short'")

        path = "/api/mix/v1/position/closePosition"
        body = {
            "symbol": symbol,
            "marginCoin": "USDT",
            "productType": "umcbl",
            "holdSide": hold_side.lower(),
        }
        return self._request("POST", path, body=body)

    # -------------------------------------------------------------------------
    # TP 설정 (포지션 기준으로 TP만)
    # -------------------------------------------------------------------------

    def set_tp_percent(
        self,
        symbol: str,
        hold_side: str,  # "long" | "short"
        percent: float,  # 0.07 = +7%
        entry_price: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        포지션 기준 TP(익절) 플랜 등록. percent는 +7% == 0.07 처럼 전달.
        - entry_price가 없으면 singlePosition 정보에서 평균가를 추정.
        - Bitget POST /api/mix/v1/plan/placeTPSL 사용.
        """
        # 평균가가 없으면 조회
        if entry_price is None:
            path = "/api/mix/v1/position/singlePosition"
            params = {
                "symbol": symbol,
                "marginCoin": "USDT",
                "productType": "umcbl",
            }
            data = self._request("GET", path, params=params)
            avg = None
            items = data.get("data") if isinstance(data, dict) else (data or [])
            for it in items or []:
                hs = (it.get("holdSide") or it.get("side") or "").lower()
                if hs == hold_side.lower():
                    avg = it.get("averageOpenPrice") or it.get("avgPrice") or it.get("openPrice")
                    break
            try:
                entry_price = float(avg) if avg is not None else None
            except Exception:
                entry_price = None

        if not entry_price or entry_price <= 0:
            # 평균가를 못 찾으면 TP 설정은 스킵 (주문 자체는 이미 체결됐으므로 동작엔 지장 없음)
            return None

        if hold_side.lower() == "long":
            trigger_price = entry_price * (1.0 + percent)
        else:  # short
            trigger_price = entry_price * (1.0 - percent)

        path = "/api/mix/v1/plan/placeTPSL"
        body = {
            "symbol": symbol,
            "marginCoin": "USDT",
            "productType": "umcbl",
            "planType": "tp",            # take-profit
            "triggerType": "market_price",
            "triggerPrice": f"{trigger_price:.2f}",
            "holdSide": hold_side.lower(),
        }
        return self._request("POST", path, body=body)
