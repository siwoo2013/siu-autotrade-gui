import time
import hmac
import json
import base64
import hashlib
from typing import Any, Dict, Optional

import requests
from urllib.parse import urlencode


class BitgetHTTPError(Exception):
    """HTTP 레벨 에러(비정상 status)"""
    def __init__(self, status: int, body: str):
        super().__init__(f"bitget-http status={status} body={body}")
        self.status = status
        self.body = body


class BitgetClient:
    """
    Bitget REST v1 간단 클라이언트 (UMCBL 선물 전용)
    - live/paper 모드 선택 가능 (기본 live)
    - 필요한 최소 API만 구현
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        passphrase: str,
        mode: str = "live",
        timeout: int = 10,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.api_secret = (api_secret or "").strip()
        self.passphrase = (passphrase or "").strip()
        self.mode = (mode or "live").lower()
        self.timeout = timeout

        # Bitget 기본 REST 엔드포인트
        self.base_url = "https://api.bitget.com"

        self.session = requests.Session()
        self.session.headers.update(
            {
                "Content-Type": "application/json",
                "locale": "en-US",
            }
        )

    # --------------------------
    # 내부 유틸
    # --------------------------
    def _timestamp(self) -> str:
        # 밀리초 타임스탬프(문자열)
        return str(int(time.time() * 1000))

    def _sign(self, ts: str, method: str, request_path: str, body: str = "") -> str:
        """
        Bitget 서명 규칙: sign = base64(hmac_sha256(secret, ts + method + request_path + body))
        - request_path 는 반드시 path(+query) 형태여야 함 (호스트 제외)
        - body는 POST/PUT 일 때 json 문자열, GET은 빈 문자열
        """
        payload = f"{ts}{method.upper()}{request_path}{body}"
        mac = hmac.new(
            self.api_secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return base64.b64encode(mac).decode("utf-8")

    def _auth_headers(self, ts: str, sign: str) -> Dict[str, str]:
        return {
            "ACCESS-KEY": self.api_key,
            "ACCESS-SIGN": sign,
            "ACCESS-TIMESTAMP": ts,
            "ACCESS-PASSPHRASE": self.passphrase,
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        auth: bool = True,
        retry: int = 1,
    ) -> Dict[str, Any]:
        """
        공용 요청 함수.
        - Bitget은 requestPath(=path+query)에 대해 서명해야 함.
        - 비정상 status → BitgetHTTPError
        - JSON 파싱 실패 대비
        """
        url = self.base_url + path

        # 쿼리스트링
        q = ""
        if params:
            q = "?" + urlencode(params)

        # 서명 바디
        body_str = ""
        if body is not None:
            body_str = json.dumps(body, separators=(",", ":"), ensure_ascii=False)

        # 서명
        headers = {}
        if auth:
            ts = self._timestamp()
            sign = self._sign(ts, method, path + q, body_str if method.upper() != "GET" else "")
            headers.update(self._auth_headers(ts, sign))

        # 실제 요청
        for attempt in range(retry + 1):
            resp = self.session.request(
                method=method.upper(),
                url=url,
                params=params,
                data=(body_str if method.upper() != "GET" else None),
                headers=headers,
                timeout=self.timeout,
            )
            text = resp.text
            if resp.status_code // 100 != 2:
                # 4xx/5xx
                if attempt < retry:
                    time.sleep(0.25)
                    continue
                raise BitgetHTTPError(resp.status_code, text)

            try:
                data = resp.json()
            except Exception:
                data = {"raw": text}

            return data

        # 여기는 보통 오지 않음
        raise RuntimeError("unreachable _request")

    # --------------------------
    # 공개 메서드
    # --------------------------
    def get_hedge_sizes(self, symbol: str) -> Dict[str, float]:
        """
        현재 심볼의 hedge 포지션 사이즈(long/short) 조회.
        Bitget: GET /api/mix/v1/position/singlePosition
          - params: symbol, productType=umcbl, marginCoin=USDT
        반환 예시(라퍼):
          {"long": 0.001, "short": 0.0}
        """
        params = {
            "symbol": symbol,
            "productType": "umcbl",
            "marginCoin": "USDT",
        }

        # 400 일시적 오류가 나올 수 있으므로 짧은 재시도
        last_exc = None
        for i in range(10):
            try:
                res = self._request("GET", "/api/mix/v1/position/singlePosition", params=params, retry=0)
                # Bitget 성공 구조: {"code":"00000","data":{...}} 또는 {"code":"00000","data":[...]}
                if not isinstance(res, dict):
                    return {"long": 0.0, "short": 0.0}

                if res.get("code") != "00000":
                    raise BitgetHTTPError(400, json.dumps(res, ensure_ascii=False))

                data = res.get("data")
                # data 가 dict 또는 list 로 온다. 모두 처리
                long_sz = 0.0
                short_sz = 0.0

                def _upd(d: Dict[str, Any]):
                    nonlocal long_sz, short_sz
                    # side: "long"/"short", total: "0.001" 형태 가능
                    side = (d.get("holdSide") or d.get("side") or "").lower()
                    sz = float(d.get("total", 0) or d.get("size", 0) or 0)
                    if side == "long":
                        long_sz = max(long_sz, sz)
                    elif side == "short":
                        short_sz = max(short_sz, sz)

                if isinstance(data, dict):
                    _upd(data)
                elif isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict):
                            _upd(item)

                return {"long": float(long_sz), "short": float(short_sz)}

            except BitgetHTTPError as e:
                last_exc = e
                time.sleep(0.2)
            except Exception as e:
                last_exc = e
                time.sleep(0.2)

        # 최종 실패
        raise last_exc or RuntimeError("get_hedge_sizes failed")

   def place_order(
        self,
        *,
        symbol: str,
        side: str,                # "buy" | "sell" 그대로 유지
        order_type: str = "market",
        size: float,
        reduce_only: bool = False,
        client_oid: Optional[str] = None,
    ) -> str:
        """
        Hedge 모드 대응:
        - 롱 진입: holdSide="open_long"
        - 숏 진입: holdSide="open_short"
        - 롱 청산: holdSide="close_long"
        - 숏 청산: holdSide="close_short"
        """
        side = side.lower()
        hold_side = None
        if reduce_only:
            # 청산 주문
            hold_side = "close_long" if side == "sell" else "close_short"
        else:
            # 신규 진입
            hold_side = "open_long" if side == "buy" else "open_short"

        body = {
            "symbol": symbol,
            "productType": "umcbl",
            "marginCoin": "USDT",
            "size": str(size),
            "holdSide": hold_side,
            "orderType": order_type.lower(),
            "reduceOnly": True if reduce_only else False,
        }
        if client_oid:
            body["clientOid"] = client_oid

        res = self._request("POST", "/api/mix/v1/order/placeOrder", body=body)
        if res.get("code") != "00000":
            raise BitgetHTTPError(400, json.dumps(res, ensure_ascii=False))

        data = res.get("data") or {}
        return str(data.get("orderId") or data.get("order_id") or "")

    def get_avg_entry_price(self, symbol: str) -> float:
        """
        평균 진입가(대략)를 구한다.
        - singlePosition 데이터의 평균가 필드를 사용(필드명이 상황에 따라 다를 수 있으므로 가능한 값들 시도)
        - 롱/숏 둘 중 포지션이 있는 쪽 우선, 둘 다 있으면 가중평균
        """
        params = {
            "symbol": symbol,
            "productType": "umcbl",
            "marginCoin": "USDT",
        }
        res = self._request("GET", "/api/mix/v1/position/singlePosition", params=params)

        if res.get("code") != "00000":
            raise BitgetHTTPError(400, json.dumps(res, ensure_ascii=False))

        data = res.get("data")
        positions = []
        if isinstance(data, dict):
            positions = [data]
        elif isinstance(data, list):
            positions = [d for d in data if isinstance(d, dict)]

        total_cost = 0.0
        total_size = 0.0

        def _get_price(d: Dict[str, Any]) -> float:
            for key in ("averageOpenPrice", "avgPrice", "openAvgPrice", "openPrice"):
                v = d.get(key)
                if v is not None:
                    try:
                        return float(v)
                    except Exception:
                        pass
            return 0.0

        for p in positions:
            sz = float(p.get("total", 0) or p.get("size", 0) or 0)
            if sz <= 0:
                continue
            price = _get_price(p)
            if price > 0:
                total_cost += price * sz
                total_size += sz

        if total_size > 0:
            return total_cost / total_size
        # fallback: 현재가가 필요하면 시세 API를 붙여야 하지만, 여기선 0 반환
        return 0.0

    def place_tp_order(
        self,
        *,
        symbol: str,
        side: str,              # TP 실행 시 실제 체결 side ("buy"/"sell") - 포지션의 반대 방향
        trigger_price: float,
        size: float,
    ) -> Optional[str]:
        """
        TP(익절) 계획 주문. Bitget '플랜' 주문 엔드포인트 시도.
        - 호환성 이슈가 있을 수 있으므로 실패 시 None 반환(서버에서 경고만 남김)
        """
        # Bitget 문서 기준: /api/mix/v1/plan/placePlan 또는 /placeTPSL 중 택일
        # 여기서는 placePlan 을 우선 시도 (시장가 트리거)
        body = {
            "symbol": symbol,
            "marginCoin": "USDT",
            "productType": "umcbl",
            "planType": "profit_plan",
            "triggerType": "market_price",
            "triggerPrice": str(trigger_price),
            "size": str(size),
            "side": side.lower(),            # 계획 주문 체결 방향(포지션 반대)
            "orderType": "market",           # 트리거 시 시장가 청산
            # "reduceOnly": True  # 일부 엔드포인트는 지원 X
        }
        try:
            res = self._request("POST", "/api/mix/v1/plan/placePlan", body=body)
            if res.get("code") != "00000":
                # 호환 안 되면 조용히 스킵
                return None
            data = res.get("data") or {}
            return str(data.get("planId") or data.get("orderId") or "")
        except Exception:
            return None
