# ========================= server.py (전체) =========================
import os
import json
import traceback
from typing import Dict, Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import logging

# --- Bitget 클라이언트 로드 ---
try:
    from bitget_client import BitgetClient, BitgetHTTPError  # 오빠가 올린 파일 기준
except Exception:  # 비상용(모듈이 비어있거나 바뀌었을 때)
    BitgetClient = None
    class BitgetHTTPError(Exception):
        pass

log = logging.getLogger("uvicorn.error")
app = FastAPI(title="siu-autotrade-gui")

# ========================= 환경설정 =========================
ENV = {
    "TRADE_MODE": os.getenv("TRADE_MODE", "live").lower(),  # live / paper / test
    "API_KEY": os.getenv("BITGET_API_KEY", ""),
    "API_SECRET": os.getenv("BITGET_API_SECRET", ""),
    "API_PASSPHRASE": os.getenv("BITGET_API_PASSPHRASE", ""),
    "WEBHOOK_SECRET": os.getenv("WEBHOOK_SECRET", ""),
}
if not ENV["API_KEY"] or not ENV["API_SECRET"] or not ENV["API_PASSPHRASE"]:
    log.warning("[ENV] Bitget API 키/시크릿/패스프레이즈가 비어 있습니다.")

# ========================= 심볼 매핑 =========================
# TV/외부에서 오는 심볼을 Bitget REST 심볼로 정규화
def normalize_symbol(sym: str) -> str:
    s = (sym or "").upper().strip()
    # BTCUSDT.P, BTCUSDT.PS 등 → BTCUSDT_UMCBL
    if s.endswith(".P") or s.endswith(".PS") or s.endswith(".UMCBL"):
        base = s.split(".")[0]
        return f"{base}_UMCBL"
    # 이미 표준이면 그대로
    if s.endswith("_UMCBL"):
        return s
    # 안전 기본값(무리하지 않음)
    return s + "_UMCBL" if "_" not in s else s

# ========================= Bitget 어댑터 =========================
class BG:
    def __init__(self):
        if BitgetClient is None:
            raise RuntimeError("bitget_client.py 를 로드할 수 없습니다.")
        self.cli = BitgetClient(
            api_key=ENV["API_KEY"],
            secret_key=ENV["API_SECRET"],
            passphrase=ENV["API_PASSPHRASE"],
            trade_mode=ENV["TRADE_MODE"],  # 오빠 파일의 생성자 시그니처에 맞춰 전달
        )

    # 모드 조회(있으면 사용)
    def query_position_mode(self) -> str:
        fn = getattr(self.cli, "query_position_mode", None)
        if callable(fn):
            return fn()  # 기대값: "single_hold" | "double_hold"
        return "unknown"

    # 원웨이 보정(있으면 사용)
    def ensure_unilateral_mode(self) -> str:
        fn = getattr(self.cli, "ensure_unilateral_mode", None)
        if callable(fn):
            return fn()  # 기대값: "single_hold"
        return "unknown"

    # 현재 롱/숏 보유 수량(양수 float)
    def get_hedge_sizes(self, symbol: str) -> Dict[str, float]:
        fn = getattr(self.cli, "get_hedge_sizes", None)
        if not callable(fn):
            raise RuntimeError("bitget_client.get_hedge_sizes 가 없습니다.")
        return fn(symbol)

    # 마켓 주문 (reduceOnly 지원)
    def place_market_order(self, *, symbol: str, side: str, size: float, reduce_only: bool = False) -> Dict[str, Any]:
        fn = getattr(self.cli, "place_market_order", None)
        if not callable(fn):
            raise RuntimeError("bitget_client.place_market_order 가 없습니다.")
        return fn(symbol=symbol, side=side, size=size, reduce_only=reduce_only)

bg = BG()

# ========================= 유틸 =========================
def ok(data: Dict[str, Any] = None, **kw):
    d = {"ok": True}
    if data:
        d.update(data)
    if kw:
        d.update(kw)
    return JSONResponse(d, status_code=200)

def err(msg: str, **kw):
    d = {"ok": False, "error": msg}
    if kw:
        d.update(kw)
    return JSONResponse(d, status_code=200)

def auth_ok(payload: Dict[str, Any]) -> bool:
    """웹훅 shared secret 검증"""
    sec = (payload or {}).get("secret")
    return (ENV["WEBHOOK_SECRET"] and sec == ENV["WEBHOOK_SECRET"])

# ========================= 진단/보조 엔드포인트 =========================
@app.get("/healthz")
def healthz():
    return ok()

@app.get("/mode")
def get_mode():
    try:
        mode = bg.query_position_mode()
        return ok(mode=mode)
    except BitgetHTTPError as e:
        log.error(f"/mode bitget err: {e}")
        return err("bitget-http", detail=str(e))
    except Exception as e:
        return err(type(e).__name__, detail=str(e))

@app.get("/positions")
def get_positions(symbol: str = "BTCUSDT.P"):
    try:
        sym = normalize_symbol(symbol)
        sizes = bg.get_hedge_sizes(sym)
        return ok(symbol=sym, sizes=sizes)
    except BitgetHTTPError as e:
        return err("bitget-http", detail=str(e))
    except Exception as e:
        return err(type(e).__name__, detail=str(e))

# ========================= TV / Webhook =========================
@app.post("/tv")
async def tv(request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    try:
        # --- 인증 ---
        if not auth_ok(payload):
            log.warning(f"[TV] secret mismatch: {payload}")
            return err("unauthorized")

        raw_symbol = payload.get("symbol") or payload.get("ticker") or ""
        symbol = normalize_symbol(raw_symbol)
        route = (payload.get("route") or "").strip()
        target = (payload.get("target") or payload.get("target_side") or "").upper().strip()
        qty = float(payload.get("size") or payload.get("qty") or 0)
        otype = (payload.get("type") or "MARKET").upper()

        log.info(f"[LIVE] [TV] 수신 | raw={raw_symbol} -> symbol={symbol} | route={route} | target={target} | size={qty}")

        if otype != "MARKET":
            return err("only-market-supported")

        # ---------- 공통 도우미 ----------
        def open_by_target(tg: str, q: float):
            if tg == "BUY":
                return bg.place_market_order(symbol=symbol, side="buy", size=q, reduce_only=False)
            elif tg == "SELL":
                return bg.place_market_order(symbol=symbol, side="sell", size=q, reduce_only=False)
            else:
                raise ValueError(f"invalid target: {tg}")

        # ---------- 라우팅 ----------
        if route == "order.open":
            if qty <= 0:
                return err("invalid-size")
            order = open_by_target(target, qty)
            return ok(action="open", order=order)

        elif route == "order.reverse":
            if target not in ("BUY", "SELL"):
                return err("invalid-target")

            sizes = bg.get_hedge_sizes(symbol)  # {"long": x, "short": y}
            long_sz = float(sizes.get("long") or 0.0)
            short_sz = float(sizes.get("short") or 0.0)
            log.info(f"ensure_close_full | {symbol} sizes(long={long_sz}, short={short_sz})")

            if qty <= 0:
                return err("invalid-size")

            # 플랫이면 'reverse' = 신규 오픈
            if long_sz == 0.0 and short_sz == 0.0:
                order = open_by_target(target, qty)
                return ok(action="reverse-flat->open", order=order)

            # SELL 타겟
            if target == "SELL":
                # 롱 보유 시: 롱 먼저 reduceOnly로 청산
                if long_sz > 0:
                    close_qty = min(qty, long_sz)
                    bg.place_market_order(symbol=symbol, side="sell", size=close_qty, reduce_only=True)
                    remain = qty - close_qty
                    if remain > 0:
                        # 잔량은 숏 오픈
                        order = bg.place_market_order(symbol=symbol, side="sell", size=remain, reduce_only=False)
                        return ok(action="reverse-long->sell(open-remaining)", order=order)
                    return ok(action="reverse-long->sell(close-only)")
                # 롱이 없고 숏 보유면: 단순 증액
                order = bg.place_market_order(symbol=symbol, side="sell", size=qty, reduce_only=False)
                return ok(action="increase-short", order=order)

            # BUY 타겟
            elif target == "BUY":
                if short_sz > 0:
                    close_qty = min(qty, short_sz)
                    bg.place_market_order(symbol=symbol, side="buy", size=close_qty, reduce_only=True)
                    remain = qty - close_qty
                    if remain > 0:
                        order = bg.place_market_order(symbol=symbol, side="buy", size=remain, reduce_only=False)
                        return ok(action="reverse-short->buy(open-remaining)", order=order)
                    return ok(action="reverse-short->buy(close-only)")
                order = bg.place_market_order(symbol=symbol, side="buy", size=qty, reduce_only=False)
                return ok(action="increase-long", order=order)

        else:
            return err(f"unknown-route: {route}")

    except BitgetHTTPError as e:
        log.error(f"BitgetHTTPError: {e}")
        return err("bitget-http", detail=str(e))

    except Exception as e:
        tb = traceback.format_exc()
        log.error(f"Exception in /tv: {e}\n{tb}")
        return err(type(e).__name__, detail=str(e))

# ========================= 앱 시작 시 원웨이 보정(가능하면) =========================
@app.on_event("startup")
async def _startup():
    try:
        mode = bg.ensure_unilateral_mode()
        log.info(f"[startup] ensured unilateral mode -> {mode}")
    except Exception as e:
        log.warning(f"[startup] ensure_unilateral_mode skipped: {e}")

# ========================= 끝 =========================
