# server.py
import os
import re
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ValidationError
from typing import Optional

from bitget import BitgetClient, PositionState

APP_NAME = "siu-autotrade-gui"
BASE_DIR = Path(__file__).resolve().parent

# ===== Env & mode ============================================================
from pathlib import Path
import os

APP_NAME = "siu-autotrade-gui"
BASE_DIR = Path(__file__).resolve().parent

# 1) TRADE_MODE가 우선권 (live | demo)
TRADE_MODE = os.getenv("TRADE_MODE", "demo").lower()  # 기본 demo

# 2) DEMO 환경변수가 명시되면 그 값으로 덮어쓰기 (호환 목적)
#    - true/1/on/yes => 데모 모드
#    - false/0/off/no => 라이브 모드
if os.getenv("DEMO") is not None:
    DEMO = os.getenv("DEMO", "false").lower() in ["1", "true", "yes", "on"]
else:
    DEMO = (TRADE_MODE != "live")

MODE_TAG = "[DEMO]" if DEMO else "[LIVE]"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "YOUR_WEBHOOK_SECRET")

# ===== FastAPI ===============================================================
app = FastAPI(title=APP_NAME)

# ===== Static: favicon =======================================================
@app.get("/favicon.ico", include_in_schema=False)
def get_favicon():
    ico = BASE_DIR / "favicon.ico"
    if ico.exists():
        resp = FileResponse(ico, media_type="image/x-icon")
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp
    return Response(status_code=204)

@app.head("/favicon.ico", include_in_schema=False)
def head_favicon():
    ico = BASE_DIR / "favicon.ico"
    if ico.exists():
        headers = {
            "Cache-Control": "public, max-age=31536000, immutable",
            "Content-Type": "image/x-icon",
            "Content-Length": str(ico.stat().st_size),
        }
        return Response(status_code=200, headers=headers)
    return Response(status_code=204)

# ===== Helpers ===============================================================
# TV 예: BINANCE:BTCUSDT.P, BYBIT:ETHUSDT.P 등
TV_TICKER_RE = re.compile(r"^[A-Z0-9]+:([A-Z0-9]+)(?:\.[A-Z0-9]+)?$")

def normalize_symbol(tv_symbol: str) -> str:
    """TV 심볼에서 거래소 접두사/접미사를 제거: BINANCE:BTCUSDT.P -> BTCUSDT"""
    m = TV_TICKER_RE.match(tv_symbol)
    return m.group(1) if m else tv_symbol

def normalize_symbol_for_bitget(tv_symbol: str) -> str:
    """
    Bitget USDT-M Perp 심볼로 변환.
    TV: BINANCE:BTCUSDT.P -> BTCUSDT -> BTCUSDT_UMCBL
    (이미 _UMCBL 이 붙어있으면 그대로 둠)
    """
    base = normalize_symbol(tv_symbol)
    if not base.endswith("_UMCBL"):
        base = f"{base}_UMCBL"
    return base

def log(msg: str):
    print(f"{MODE_TAG} {msg}", flush=True)

# ===== Bitget Client =========================================================
bg_client = BitgetClient(
    api_key=os.getenv("BITGET_API_KEY", ""),
    api_secret=os.getenv("BITGET_API_SECRET", ""),
    passphrase=os.getenv("BITGET_API_PASSPHRASE", ""),
    base_url=os.getenv("BITGET_BASE_URL", "https://api.bitget.com"),
    demo=DEMO,
)

# ===== Request Model =========================================================
class TvWebhook(BaseModel):
    secret: Optional[str] = None
    route: str
    exchange: str
    symbol: str
    target_side: Optional[str] = None  # BUY | SELL (for order.reverse)
    type: Optional[str] = "MARKET"
    size: Optional[float] = None       # e.g., 0.01
    side: Optional[str] = None         # for order.close
    client_oid: Optional[str] = None

# ===== Root ==================================================================
@app.get("/", include_in_schema=False)
def root():
    return JSONResponse({"ok": True, "service": APP_NAME, "mode": "demo" if DEMO else "live"})

# ===== TradingView Webhook ====================================================
@app.post("/tv")
async def tv_webhook(request: Request):
    # 1) Parse JSON (원본이 JSON 한 줄이어야 함)
    try:
        body = await request.json()
    except Exception:
        # 디버깅용 원문을 남겨 원인 파악 쉽게
        raw = await request.body()
        log(f"[tv] Invalid JSON RAW: {raw.decode(errors='ignore')[:300]}")
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # 2) 모델 검증
    try:
        data = TvWebhook(**body)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))

    # 3) Secret 검증
    if WEBHOOK_SECRET and (data.secret or "") != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Bad secret")

    # 4) 입력 정규화
    tv_symbol = data.symbol
    ex = (data.exchange or "").lower().strip()
    # ★ Bitget이면 자동으로 UMCBL 심볼 붙이기
    if ex == "bitget":
        symbol = normalize_symbol_for_bitget(tv_symbol)
    else:
        symbol = normalize_symbol(tv_symbol)

    route = data.route.lower().strip()
    order_type = (data.type or "MARKET").upper()
    size = float(data.size) if data.size is not None else None
    target_side = (data.target_side or "").upper()
    client_oid = data.client_oid

    log(f"[TV] 수신 | {symbol} | {route} | {target_side or data.side} | size={size}")

    # 5) 라우팅
    if route == "order.reverse":
        if order_type != "MARKET":
            raise HTTPException(status_code=400, detail="Only MARKET supported")
        if target_side not in ["BUY", "SELL"]:
            raise HTTPException(status_code=400, detail="target_side must be BUY or SELL")
        if size is None or size <= 0:
            raise HTTPException(status_code=400, detail="size must be > 0")

        # 현재 포지션 조회
        pos = bg_client.get_net_position(symbol)

        # 같은 방향 스킵
        if (pos == PositionState.LONG and target_side == "BUY") or \
           (pos == PositionState.SHORT and target_side == "SELL"):
            log(f"[TV] 처리완료 | {symbol} | reverse | state=same-direction-skip")
            return JSONResponse({"ok": True, "state": "same-direction-skip"})

        # 반대 포지션이면 전량 청산
        if (pos == PositionState.LONG and target_side == "SELL") or \
           (pos == PositionState.SHORT and target_side == "BUY"):
            close_side = "SELL" if pos == PositionState.LONG else "BUY"
            bg_client.close_position(symbol, close_side, client_oid=client_oid)

        # 신규 진입
        oid = bg_client.place_market_order(symbol, target_side, size, reduce_only=False, client_oid=client_oid)
        state = "flat->open" if pos == PositionState.FLAT else "reverse"
        log(f"[TV] 처리완료 | {symbol} | reverse | state={state} | oid={oid}")
        return JSONResponse({"ok": True, "state": state, "order_id": oid})

    elif route == "order.close":
        # 전량 청산 (side=BUY/SELL 로 전달)
        side = (data.side or "").upper()
        if side not in ["BUY", "SELL"]:
            raise HTTPException(status_code=400, detail="side must be BUY or SELL")
        bg_client.close_position(symbol, side, client_oid=client_oid)
        log(f"[TV] 처리완료 | {symbol} | close | side={side}")
        return JSONResponse({"ok": True, "state": "close"})

    else:
        raise HTTPException(status_code=400, detail=f"Unknown route: {route}")
