# bitget_client.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import time
import hmac
import json
import hashlib
import logging
from typing import Any, Dict, Optional

import requests


log = logging.getLogger("bitget")
log.setLevel(logging.INFO)


class BitgetClient:
    """
    Bitget Mix(USDT-M) 선물 전용 경량 클라이언트.

    - 기본 One-way 모드('buy'/'sell' + reduceOnly 플래그)로 주문
    - 400172(side mismatch) 발생 시:
        * 원웨이 전제에서 반대 방향 + reduceOnly=True 로 강제 청산 실행
        * 그 후 원래 주문(신규 진입) 재시도
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

        self._delta_ms = 0
        try:
            self.sync_time()
        except Exception as e:
            log.warning("Bitget time sync failed (will retry on demand): %s", e)

    # ---- 시간 & 서명 -------------------------------------------------------
    def _ts_ms(self) -> int:
        return int(time.time() * 1000) + self._delta_ms

    def sync_time(self) -> None:
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

    # ---- 요청 공통 ---------------------------------------------------------
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

        # request_path (서명용)
        query = ""
        if params:
            from urllib.parse import urlencode
            query = "?" + urlencode(params, doseq=True)

        ts = str(self._ts_ms())
        sign = self._sign(ts, method, f"{path}{query}", body_str)
        headers = self._headers(ts, sign)

        try:
            if method.upper() == "GET":
                resp = self.session.get(url, headers=headers, params=params, timeout=timeout)
            elif method.upper() == "POST":
                resp = self.session.post(url, headers=headers, params=params, data=body_str, timeout=timeout)
            else:
                raise ValueError(f"Unsupported method: {method}")
        except requests.RequestException:
            raise

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

    # ---- 포지션 ------------------------------------------------------------
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

    # ---- 주문 --------------------------------------------------------------
    @staticmethod
    def _opp(side: str) -> str:
        return "sell" if side.lower() == "buy" else "buy"

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
        원웨이 모드 기본 주문 + side mismatch(400172) 시 강제청산 후 재시도.
        """
        path = "/api/mix/v1/order/placeOrder"
        order_type = "market" if type.upper() == "MARKET" else "limit"

        def _body(side_value: str, ro: bool) -> Dict[str, Any]:
            b: Dict[str, Any] = {
                "symbol": symbol,
                "marginCoin": self.margin_coin,
                "productType": self.product_type,
                "side": side_value,            # one-way: buy/sell
                "orderType": order_type,
                "size": str(size),
                "reduceOnly": bool(ro),
            }
            if client_oid:
                b["clientOid"] = client_oid
            return b

        body_open = _body(side.lower(), reduce_only)

        try:
            return self._request("POST", path, body=body_open)

        except requests.HTTPError as e:
            msg = str(e).lower()

            # 400172: side mismatch → 반대방향 reduceOnly 청산 후 재시도
            if ("400172" in msg) or ("side mismatch" in msg):
                if not reduce_only:
                    close_side = self._opp(side)
                    body_close = _body(close_side, True)
                    log.warning("side mismatch -> force close first: %s (size=%s)", close_side, size)
                    try:
                        self._request("POST", path, body=body_close)
                    except Exception as ce:
                        log.warning("force close failed (will still try open): %s", ce)

                    # close 이후 다시 원래 주문
                    return self._request("POST", path, body=body_open)

            # 그 외는 그대로 전달
            raise
