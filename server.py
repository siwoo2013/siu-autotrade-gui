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
DEMO = (os.getenv("DEMO", "true").lower() in ["1", "true", "yes", "on"]) or (
    os.getenv("TRADE_MODE", "demo").lower() != "live"
)
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
TV_TICKER_RE = re.compile(r"^[A-Z0-9]+:([A-Z0-9]+)(?:\.[A-Z0-9]+)?$")

def normalize_symbol(tv_symbol: str) -> str:
    """
    TV: 'BINANCE:BTCUSDT.P' -> 'BTCUSDT'
    """
    m = TV_TICKER_RE.match(tv_symbol)
    return m.group(1) if m else tv_symbol

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
    # 1) Parse JSON
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    try:
        data = TvWebhook(**body)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))

    # 2) Secret check
    if WEBHOOK_SECRET and (data.secret or "") != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Bad secret")

    # 3) Normalize inputs
    tv_symbol = data.symbol
    symbol = normalize_symbol(tv_symbol)
    route = data.route.lower().strip()
    order_type = (data.type or "MARKET").upper()
    size = float(data.size) if data.size is not None else None
    target_side = (data.target_side or "").upper()
    client_oid = data.client_oid

    log(f"[TV] 수신 | {symbol} | {route} | {target_side or data.side} | size={size}")

    # 4) Routing
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

        # 반대 포지션 리버스
        if (pos == PositionState.LONG and target_side == "SELL") or \
           (pos == PositionState.SHORT and target_side == "BUY"):
            # 전량 청산
            close_side = "SELL" if pos == PositionState.LONG else "BUY"
            bg_client.close_position(symbol, close_side, client_oid=client_oid)

        # 신규 진입
        oid = bg_client.place_market_order(symbol, target_side, size, reduce_only=False, client_oid=client_oid)
        state = "flat->open" if pos == PositionState.FLAT else "reverse"
        log(f"[TV] 처리완료 | {symbol} | reverse | state={state} | oid={oid}")
        return JSONResponse({"ok": True, "state": state, "order_id": oid})

    elif route == "order.close":
        # 전량 청산
        side = (data.side or "").upper()
        if side not in ["BUY", "SELL"]:
            raise HTTPException(status_code=400, detail="side must be BUY or SELL")
        bg_client.close_position(symbol, side, client_oid=client_oid)
        log(f"[TV] 처리완료 | {symbol} | close | side={side}")
        return JSONResponse({"ok": True, "state": "close"})

    else:
        raise HTTPException(status_code=400, detail=f"Unknown route: {route}")
