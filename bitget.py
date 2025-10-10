import os
import time
import hmac
import json
import hashlib
import base64
from enum import Enum
from typing import Optional, Dict, Any

import requests


class PositionState(str, Enum):
    FLAT = "flat"
    LONG = "long"
    SHORT = "short"


class BitgetClient:
    """
    - demo=True  : 실호출 대신 로그만 남기고 성공 시뮬레이션
    - demo=False : Bitget REST LIVE 호출
    """
    def __init__(self, api_key: str, api_secret: str, passphrase: str,
                 base_url: str = "https://api.bitget.com", demo: bool = True):
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.base_url = base_url.rstrip("/")
        self.demo = demo
        self.session = requests.Session()
        # 기본 마진코인(USDT-M Perp)
        self.margin_coin = "USDT"

    # ===== Helpers ============================================================
    def _ts(self) -> str:
        # Bitget는 밀리초 타임스탬프 문자열 요구
        return str(int(time.time() * 1000))

    def _sign(self, ts: str, method: str, path: str, body: str) -> str:
        msg = ts + method.upper() + path + body
        mac = hmac.new(self.api_secret.encode(), msg.encode(), hashlib.sha256).digest()
        return base64.b64encode(mac).decode()

    def _headers(self, ts: str, sign: str) -> Dict[str, str]:
        return {
            "ACCESS-KEY": self.api_key,
            "ACCESS-SIGN": sign,
            "ACCESS-TIMESTAMP": ts,
            "ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = self.base_url + path
        payload = json.dumps(body or {}, separators=(",", ":"))
        ts = self._ts()
        sign = self._sign(ts, method, path, payload if method != "GET" else "")
        headers = self._headers(ts, sign)
        if method == "GET":
            resp = self.session.get(url, headers=headers, params=body or {}, timeout=10)
        else:
            resp = self.session.request(method, url, headers=headers, data=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") not in ("00000", 0, "0", None):
            raise RuntimeError(f"Bitget error: {data}")
        return data

    def _log(self, msg: str):
        tag = "[DEMO]" if self.demo else "[LIVE]"
        print(f"{tag} {msg}", flush=True)

    # ===== Public =============================================================
    def get_net_position(self, symbol: str) -> PositionState:
        """
        LIVE: 단일 심볼 포지션 조회
        DEMO: 항상 FLAT로 시작, 내부 상태 없이 '거래 기록'에 의존하지 않고 서버 로직에서 reverse/skip 판단 가능.
        """
        if self.demo:
            # 데모에서는 서버 로직에서 same-direction-skip / reverse 등 판단 로그만 찍고
            # 체결 영향은 없으니, 여기서는 거래소 조회 생략
            return PositionState.FLAT

        # Bitget 단일 포지션 조회 (USDT-M)
        path = "/api/mix/v1/position/singlePosition"
        params = {"symbol": symbol, "marginCoin": self.margin_coin}
        data = self._request("GET", path, params)

        pos_data = (data.get("data") or {})
        if not pos_data:
            return PositionState.FLAT

        holdSide = (pos_data.get("holdSide") or "").lower()  # long|short|none
        if holdSide == "long":
            return PositionState.LONG
        if holdSide == "short":
            return PositionState.SHORT
        return PositionState.FLAT

    def place_market_order(self, symbol: str, side: str, size: float,
                           reduce_only: bool = False, client_oid: Optional[str] = None) -> str:
        """
        LIVE: 시장가 주문
        DEMO: 로깅만
        """
        side = side.upper()  # BUY|SELL

        if self.demo:
            self._log(f"place_order {symbol} {side} MARKET size={size} reduce_only={reduce_only} -> net=~0.0")
            return f"tv-{int(time.time()*1000)}-open"

        path = "/api/mix/v1/order/placeOrder"
        body = {
            "symbol": symbol,
            "marginCoin": self.margin_coin,
            "size": str(size),
            "side": "open_long" if side == "BUY" else "open_short",
            "orderType": "market",
            "reduceOnly": reduce_only,
        }
        if client_oid:
            body["clientOid"] = client_oid

        data = self._request("POST", path, body)
        oid = (data.get("data") or {}).get("orderId") or f"tv-{int(time.time()*1000)}-open"
        self._log(f"place_order {symbol} {side} MARKET size={size} reduce_only={reduce_only} -> oid={oid}")
        return oid

    def close_position(self, symbol: str, side: str, client_oid: Optional[str] = None) -> None:
        """
        LIVE: 전량 청산 (side 기준은 '무엇으로 청산하나' BUY/SELL)
        DEMO: 로깅만
        """
        side = side.upper()

        if self.demo:
            self._log(f"close_position {symbol} side={side} size=ALL -> net=0.0")
            return

        path = "/api/mix/v1/order/closeAllPositions"
        # Bitget은 심볼/마진코인만 주면 전량 청산 가능(포지션 방향은 거래소가 판단)
        body = {
            "symbol": symbol,
            "marginCoin": self.margin_coin,
        }
        data = self._request("POST", path, body)
        self._log(f"close_position {symbol} side={side} size=ALL -> done")
