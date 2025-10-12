# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict

import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from bitget_client import BitgetClient

# ---------- env ----------
BITGET_API_KEY = os.environ.get("BITGET_API_KEY", "")
BITGET_API_SECRET = os.environ.get("BITGET_API_SECRET", "")
BITGET_PASSPHRASE = os.environ.get("BITGET_PASSPHRASE", "")
TRADE_MODE = os.environ.get("TRADE_MODE", "live")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

# ---------- app & logger ----------
logger = logging.getLogger("uvicorn.error")
logger.setLevel(logging.INFO)

app = FastAPI()

# ---------- client ----------
bg = BitgetClient(
    api_key=BITGET_API_KEY,
    api_secret=BITGET_API_SECRET,
    passphrase=BITGET_PASSPHRASE,
    product_type="umcbl",
    margin_coin="USDT",
    logger=logger,
)

# ---------- routes ----------

@app.get("/")
def health() -> Dict[str, Any]:
    return {"ok": True, "service": "siu-autotrade-gui", "mode": TRADE_MODE}


@app.post("/tv")
async def tv(request: Request):
    # 0) 입력 파싱
    try:
        payload = await request.json()
    except Exception:
        body = await request.body()
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            return JSONResponse({"ok": False, "error": "bad-json"}, status_code=400)

    # 1) 인증
    if str(payload.get("secret")) != str(WEBHOOK_SECRET):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    # 2) 필드
    route = str(payload.get("route", "")).strip()
    exchange = str(payload.get("exchange", "bitget")).lower()
    symbol = str(payload.get("symbol", "BTCUSDT_UMCBL")).strip()
    target_side = str(payload.get("target_side", "")).upper()  # BUY / SELL
    order_type = str(payload.get("type", "MARKET")).lower()    # market / limit
    size = str(payload.get("size", "0.001"))

    if exchange != "bitget":
        return JSONResponse({"ok": False, "error": "unsupported-exchange"}, status_code=400)

    logger.info("[LIVE] [TV] 수신 | %s | %s | %s | size=%s", symbol, route, target_side, size)

    # --- open/close helpers ---
    def open_by_side(side: str):
        if side == "BUY":
            return bg.open_long(symbol, size, order_type)
        else:
            return bg.open_short(symbol, size, order_type)

    def close_by_side(side: str):
        if side == "BUY":
            # BUY 청산 = close_long
            return bg.close_long(symbol, size, order_type)
        else:
            # SELL 청산 = close_short
            return bg.close_short(symbol, size, order_type)

    try:
        # 3) 단순 오픈
        if route == "order.open":
            res = open_by_side(target_side)
            return {"ok": True, "result": res}

        # 4) EA 스타일 리버스 (포지션 없으면 청산 건너뛰고 신규 오픈)
        elif route == "order.reverse":
            def try_close_then_open(close_func, open_func, log_label: str):
                logger.info("[LIVE] reverse: EA | %s | size=%s", log_label, size)

                close_res = None
                # (1) 청산은 best-effort: 실패해도 계속 진행
                try:
                    close_res = close_func(symbol, size, order_type)
                except requests.HTTPError as e:
                    try:
                        detail = e.response.json()
                    except Exception:
                        detail = {"raw": getattr(e.response, "text", "")}
                    logger.info("EA close skipped (no position or rejected): %s", detail)
                    close_res = {"skipped": True, "detail": detail}

                # (2) 신규 진입은 반드시 실행
                open_res = open_func(symbol, size, order_type)
                return {"ok": True, "closed": close_res, "opened": open_res}

            if target_side == "BUY":
                return try_close_then_open(bg.close_short, bg.open_long, "close_short -> open_long")
            elif target_side == "SELL":
                return try_close_then_open(bg.close_long, bg.open_short, "close_long -> open_short")
            else:
                return JSONResponse({"ok": False, "error": "bad-target-side"}, status_code=400)

        else:
            return JSONResponse({"ok": False, "error": "unsupported-route"}, status_code=400)

    except requests.HTTPError as e:
        try:
            detail = e.response.json()
        except Exception:
            detail = {"raw": getattr(e.response, "text", "")}
        logger.error("HTTPError during %s: %s", route, detail)
        return JSONResponse(
            {"ok": False, "error": "bitget-http", "status": getattr(e.response, "status_code", None), "detail": detail},
            status_code=500,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("Exception in /tv (EA): %r", e)
        return JSONResponse({"ok": False, "error": "exception", "detail": str(e)}, status_code=500)
