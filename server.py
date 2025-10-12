# server.py
# -*- coding: utf-8 -*-

from __future__ import annotations

import os
import re
import json
from pathlib import Path
from typing import Any, Dict

import requests
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from bitget_client import BitgetClient

APP_NAME = "siu-autotrade-gui"
BASE_DIR = Path(__file__).resolve().parent

TRADE_MODE = os.getenv("TRADE_MODE", "demo").lower()   # live | demo
DEMO = (TRADE_MODE != "live")
MODE_TAG = "[DEMO]" if DEMO else "[LIVE]"

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "YOUR_WEBHOOK_SECRET")

BITGET_API_KEY = os.getenv("BITGET_API_KEY", "").strip()
BITGET_API_SECRET = os.getenv("BITGET_API_SECRET", "").strip()
BITGET_PASSPHRASE = os.getenv("BITGET_PASSPHRASE", "").strip()

PRODUCT_TYPE = "umcbl"  # U-margined perpetual (Bitget expects lowercase here)

app = FastAPI(title=APP_NAME)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

_bg = BitgetClient(
    api_key=BITGET_API_KEY,
    api_secret=BITGET_API_SECRET,
    passphrase=BITGET_PASSPHRASE,
    product_type=PRODUCT_TYPE,
)

# ---------------- helpers ---------------- #

def normalize_symbol(tv_symbol: str) -> str:
    s = tv_symbol.upper().strip()
    s = re.sub(r"\.P$", "", s)                  # .P 제거
    s = re.sub(r"PERP(ETUAL)?$", "", s)         # PERP/ PERPETUAL 제거
    s = s.replace(":", "")
    # 밑줄(_)은 유지해야 Bitget 심볼 형식 보존됨
    s = re.sub(r"[^A-Z0-9_]", "", s)

    # 중복 방지: 이미 _UMCBL로 끝나면 그대로, 아니면 한 번만 붙이기
    if not s.endswith("_UMCBL"):
        # 혹시 '...UMCBL'로 끝나는데 밑줄만 빠진 경우 대비
        s = re.sub(r"UMCBL$", "_UMCBL", s)
        if not s.endswith("_UMCBL"):
            s = f"{s}_UMCBL"
    return s

def side_open_for_oneway(target_side: str) -> str:
    t = target_side.upper()
    if t == "BUY":
        return "buy"
    if t == "SELL":
        return "sell"
    raise ValueError("target_side must be BUY or SELL")

def opposite(side_buy_sell: str) -> str:
    return "sell" if side_buy_sell == "buy" else "buy"

def float_safe(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default

# ---------------- routes ---------------- #

@app.get("/")
def health() -> Dict[str, Any]:
    return {"ok": True, "service": APP_NAME, "mode": "demo" if DEMO else "live"}

@app.post("/tv")
async def tv_webhook(req: Request):
    raw = await req.body()
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid-json"}, status_code=400)

    if data.get("secret") != WEBHOOK_SECRET:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    route = (data.get("route") or "").strip()
    exchange = (data.get("exchange") or "").lower()
    tv_symbol = (data.get("symbol") or "").strip()
    target_side_raw = (data.get("target_side") or "").upper().strip()   # "BUY"/"SELL"
    ord_type = (data.get("type") or "MARKET").upper().strip()           # "MARKET"|"LIMIT"
    size = float_safe(data.get("size"), 0.0)

    symbol = normalize_symbol(tv_symbol)
    print(f"{MODE_TAG} [TV] 수신 | {symbol} | {route} | {target_side_raw} | size={size:.6f}")

    if exchange != "bitget":
        return JSONResponse({"ok": False, "error": "unsupported-exchange"}, status_code=400)

    if route not in ("order.open", "order.reverse"):
        return JSONResponse({"ok": False, "error": "unsupported-route"}, status_code=400)

    if ord_type not in ("MARKET", "LIMIT"):
        return JSONResponse({"ok": False, "error": "unsupported-ordertype"}, status_code=400)

    try:
        side_target = side_open_for_oneway(target_side_raw)  # "buy"|"sell"
    except ValueError as ve:
        return JSONResponse({"ok": False, "error": str(ve)}, status_code=400)

    if size <= 0:
        return JSONResponse({"ok": False, "error": "invalid-size"}, status_code=400)

    client_oid_base = f"tv-{int(os.times().elapsed * 1000)}"

    # 0) 현재 순포지션(net) 확인
    try:
        pos = _bg.get_net_position(symbol)
        net = float_safe(pos.get("net"), 0.0)
    except Exception as e:
        return JSONResponse({"ok": False, "error": "position-fetch-failed", "detail": str(e)}, status_code=500)

    # net > 0: 순롱 / net < 0: 순숏 / net == 0: 무포지션
    print(f"{MODE_TAG} net={net} ( {('LONG' if net>0 else 'SHORT' if net<0 else 'FLAT')} )")

    try:
        # ---------------- order.reverse: 전량 청산 후 반전 진입 ----------------
        if route == "order.reverse":
            if net != 0:
                # 1) 전량 청산
                close_side = "sell" if net > 0 else "buy"  # 롱이면 sell로 감산, 숏이면 buy로 감산
                close_qty = abs(net)
                print(f"{MODE_TAG} reverse: close ALL first | side={close_side} reduceOnly size={close_qty}")
                if close_qty > 0:
                    _bg.place_order(
                        tv_symbol=symbol,
                        side=close_side,
                        order_type=ord_type.lower(),
                        size=str(close_qty),
                        reduce_only=True,
                        client_oid=f"{client_oid_base}-rev-close",
                    )

            # 2) 목표 방향으로 오픈
            print(f"{MODE_TAG} reverse: open | side={side_target} size={size}")
            _bg.place_order(
                tv_symbol=symbol,
                side=side_target,
                order_type=ord_type.lower(),
                size=str(size),
                reduce_only=False,
                client_oid=f"{client_oid_base}-rev-open",
            )
            return JSONResponse({"ok": True, "action": "reverse", "open_side": side_target})

        # ---------------- order.open: 필요 시 부분감산 후 남는 수량만 오픈 ----------------
        else:  # order.open
            if (net > 0 and side_target == "buy") or (net < 0 and side_target == "sell") or (net == 0):
                # 같은 방향이거나 무포 → 그대로 오픈
                print(f"{MODE_TAG} open: direct open | side={side_target} size={size}")
                _bg.place_order(
                    tv_symbol=symbol,
                    side=side_target,
                    order_type=ord_type.lower(),
                    size=str(size),
                    reduce_only=False,
                    client_oid=f"{client_oid_base}-open",
                )
                return JSONResponse({"ok": True, "action": "open", "side": side_target})

            # 반대 방향이면 먼저 감산
            need_reduce = min(abs(net), size)
            remain_open = max(0.0, size - need_reduce)
            reduce_side = side_target  # 원웨이: 반대 포지션 감산은 '목표쪽 + reduceOnly=True' 로 보냄

            if need_reduce > 0:
                print(f"{MODE_TAG} open: reduce first | side={reduce_side} reduceOnly size={need_reduce}")
                _bg.place_order(
                    tv_symbol=symbol,
                    side=reduce_side,
                    order_type=ord_type.lower(),
                    size=str(need_reduce),
                    reduce_only=True,
                    client_oid=f"{client_oid_base}-open-reduce",
                )

            if remain_open > 0:
                print(f"{MODE_TAG} open: then open remain | side={side_target} size={remain_open}")
                _bg.place_order(
                    tv_symbol=symbol,
                    side=side_target,
                    order_type=ord_type.lower(),
                    size=str(remain_open),
                    reduce_only=False,
                    client_oid=f"{client_oid_base}-open-remain",
                )

            return JSONResponse({
                "ok": True,
                "action": "open-after-reduce",
                "reduced": need_reduce,
                "opened": remain_open,
                "side": side_target
            })

    except requests.HTTPError as http_err:
        # Bitget REST 오류 자세히 노출
        status = getattr(getattr(http_err, "response", None), "status_code", None)
        body_text = ""
        try:
            body_text = http_err.response.text or ""
        except Exception:
            body_text = str(http_err)
        print(f"{MODE_TAG} HTTPError: {status} | {body_text}")
        return JSONResponse({"ok": False, "error": "bitget-http", "status": status, "detail": body_text}, status_code=500)
    except Exception as e:
        print(f"{MODE_TAG} Exception: {e}")
        return JSONResponse({"ok": False, "error": "exception", "detail": str(e)}, status_code=500)

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "10000"))
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=False)
