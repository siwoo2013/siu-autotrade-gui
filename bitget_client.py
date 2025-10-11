# bitget_client.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import time
import hmac
import json
import hashlib
import logging
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import requests


log = logging.getLogger("bitget")
log.setLevel(logging.INFO)


class BitgetClient:
    """
    Bitget USDT-M(UMCBL) 원웨이 모드 전용 경량 클라이언트.

    - 신규 진입: side = "BUY"/"SELL", reduceOnly=False
    - 청산    : side = "BUY"/"SELL", reduceOnly=True
    - 400172(side mismatch) 발생 시:
        * 신규(open, reduceOnly=False)라면 반대방향 reduceOnly=True로 강제 청산 후, 다시 신규 시도
        * 청산(reduceOnly=True)에서 난 경우엔 포지션이 없다고 보고 같은 방향 신규(open)로 전환
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        passphrase: str,
        *,
        demo: bool = False,                 # 현재는 실제/데모 URL 동일, 키만 다르게 사용
        margin_coin: str = "USDT",
        product_type: str = "umcbl",        # USDT-M perpetual
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

        self._delta_ms = 0
        try:
            self.sync_time()
        except Exception as e:
            log.warning("Bitget time sync failed (will retry on demand): %s", e)

    # ---- 시간/서명 --------------------------------------------------------
    def _ts_ms(self) -> int:
        return int(time.time() * 1000) + self._delta_ms

    def sync_time(self) -> None:
        # v2 time 엔드포인트 (권장)
        url = f"{self.base_url}/api/v2/public/time"
        r = self.session.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        srv_ms = int(data.get("data", {}).get("serverTime"))
        now_ms = int(time.time() * 1000)
        self._delta_ms = srv_ms - now_ms
        log.info("Bitget time synced. delta_ms=%s", self._delta_ms)

    def _sign(self, ts: str, method: str, request_path: str, body: str = "") -> str:
        import base64
        pre = f"{ts}{method.upper()}{request_path}{body}"
        sig = hmac.new(self.api_secret, pre.encode(), hashlib.sha256).digest()
        return base64.b64encode(sig).decode()

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

    # ---- 공통 요청 --------------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        timeout: int = 15,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        params = params or {}
        body = body or {}
        body_str = json.dumps(body, separators=(",", ":"), ensure_ascii=False) if body else ""

        query = ""
        if params:
            query = "?" + urlencode(params, doseq=True)

        ts = str(self._ts_ms())
        sign = self._sign(ts, method, f"{path}{query}", body_str)
        headers = self._headers(ts, sign)

        if method.upper() == "GET":
            resp = self.session.get(url, headers=headers, params=params, timeout=timeout)
        elif method.upper() == "POST":
            resp = self.session.post(url, headers=headers, params=params, data=body_str, timeout=timeout)
        else:
            raise ValueError(f"Unsupported method: {method}")

        try:
            resp.raise_for_status()
        except requests.HTTPError as e:
            detail = {}
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

        data = resp.json()
        if data.get("code") != "00000":
            log.error("Bitget logical error: %s", data)
            raise requests.HTTPError(f"Bitget logical error: {data}")
        return data

    # ---- 포지션 -----------------------------------------------------------
    def get_net_position(self, symbol: str) -> Dict[str, float]:
        path = "/api/mix/v1/position/singlePosition"
        params = {
            "symbol": symbol,
            "marginCoin": self.margin_coin,
            "productType": self.product_type,
        }
        data = self._request("GET", path, params=params)
        d = data.get("data")
        net = 0.0

        if isinstance(d, list):
            for p in d:
                try:
                    side = (p.get("holdSide") or "").lower()
                    sz = float(p.get("total", p.get("available", p.get("openAmount", 0)) or 0))
                    if side == "long":
                        net += sz
                    elif side == "short":
                        net -= sz
                except Exception:
                    continue
        elif isinstance(d, dict):
            try:
                side = (d.get("holdSide") or "").lower()
                sz = float(d.get("total", d.get("available", d.get("openAmount", 0)) or 0))
                if side == "long":
                    net += sz
                elif side == "short":
                    net -= sz
            except Exception:
                pass

        return {"net": net}

    # ---- 주문(원웨이) ------------------------------------------------------
    @staticmethod
    def _norm_side(side: str) -> str:
        s = side.strip().lower()
        # 혹시 헤지 표현이 들어오면 보정
        m = {"open_long": "buy", "close_long": "sell", "open_short": "sell", "close_short": "buy"}
        return m.get(s, s)

    def _body(self, symbol: str, side: str, order_type: str, size, reduce_only: bool, client_oid: str | None):
        return {
            "symbol": symbol,
            "marginCoin": self.margin_coin,
            "productType": self.product_type,
            "side": side,                         # "buy" | "sell"
            "orderType": order_type,              # "market"
            "size": str(size),
            "reduceOnly": bool(reduce_only),
            **({"clientOid": client_oid} if client_oid else {}),
        }

    def place_order(
        self,
        symbol: str,
        side: str,               # "BUY" | "SELL" (대소문자 무관)
        type: str,               # "MARKET" | "LIMIT" -> 현재 MARKET 사용
        size,
        reduce_only: bool = False,
        client_oid: str | None = None,
    ) -> Dict[str, Any]:
        """
        원웨이 모드용 주문. 기본은 'buy'/'sell' + reduceOnly 플래그만 사용.
        side mismatch(400172) 대응 포함.
        """
        side_norm = self._norm_side(side)
        order_type = type.strip().lower()
        path = "/api/mix/v1/order/placeOrder"

        # 1차 시도
        body_open = self._body(symbol, side_norm, order_type, size, reduce_only, client_oid)
        try:
            return self._request("POST", path, body=body_open)
        except requests.HTTPError as e:
            # Bitget 응답 본문에서 코드/메시지 뽑기
            msg = str(e)
            if ("400172" in msg) or ("side mismatch" in msg):
                if not reduce_only:
                    # 신규인데 방향이 안 맞으면 -> 반대방향 강제청산 후 다시 신규
                    close_side = "sell" if side_norm == "buy" else "buy"
                    body_close = self._body(symbol, close_side, order_type, size, True, client_oid)
                    log.warning("side mismatch on OPEN -> force CLOSE first: %s (size=%s)", close_side, size)
                    try:
                        self._request("POST", path, body=body_close)
                    except Exception as ce:
                        log.warning("force close failed (continue to OPEN): %s", ce)
                    return self._request("POST", path, body=self._body(symbol, side_norm, order_type, size, False, client_oid))
                else:
                    # 청산인데 포지션이 없으면 -> 같은 방향 신규로 전환
                    log.warning("side mismatch on CLOSE -> fallback to OPEN(side=%s, size=%s)", side_norm, size)
                    return self._request("POST", path, body=self._body(symbol, side_norm, order_type, size, False, client_oid))
            raise
