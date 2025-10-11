# server.py
# -*- coding: utf-8 -*-

from __future__ import annotations

import os
import re
import json
import logging
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware

# Bitget 클라이언트 모듈 (프로젝트 내 파일)
from bitget_client import BitgetClient


# ===== 기본 설정 =============================================================

APP_NAME = "siu-autotrade-gui"
BASE_DIR = Path(__file__).resolve().parent

# 로그 포맷
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("app")


# ===== Env & mode ============================================================

# 1) TRADE_MODE 가 우선 (live | demo) — 기본 demo
TRADE_MODE = os.getenv("TRADE_MODE", "demo").lower()

# 2) DEMO 가 명시되면 호환 목적으로 반영
#    - true/1/on/yes  => 데모 모드
#    - false/0/off/no => 라이브 모드
if os.getenv("DEMO") is not None:
    DEMO = os.getenv("DEMO", "false").lower() in ["1", "true", "yes", "on"]
else:
    DEMO = (TRADE_MODE != "live")

MODE_TAG = "[DEMO]" if DEMO else "[LIVE]"

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "YOUR_WEBHOOK_SECRET")

BITGET_API_KEY = os.getenv("BITGET_API_KEY", "")
BITGET_API_SECRET = os.getenv("BITGET_API_SECRET", "")
BITGET_PASSPHRASE = os.getenv("BITGET_PASSPHRASE", "")

# Bitget 클라이언트
bg_client = BitgetClient(
    api_key=BITGET_API_KEY,
    api_secret=BITGET_API_SECRET,
    passphrase=BITGET_PASSPHRASE,
    demo=DEMO,
)


# ===== FastAPI ===============================================================

app = FastAPI(title=APP_NAME)

# 필요시 CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ===== 유틸: 심볼 정규화 =====================================================

def normalize_symbol(tv_symbol: str) -> str:
    """
    TV에서 오는 심볼(예: 'BTCUSDT.P', 'BITGET:BTCUSDT.P', 'BINANCE:BTCUSDT')
    -> Bitget U-M 선물 표준 심볼 'BTCUSDT_UMCBL' 로 변환.
    """
    s = (tv_symbol or "").strip().upper()

    # 'BITGET:BTCUSDT.P' → 'BTCUSDT.P'
    if ":" in s:
        s = s.split(":")[-1]

    # 접미어/장식 제거: '.P', '-PERP', '_PERP', 'PERP'
    s = s.replace(".P", "")
    s = s.replace("-PERP", "")
    s = s.replace("_PERP", "")
    s = s.replace("PERP", "")

    # 알파벳/숫자만 남김
    s = re.sub(r"[^A-Z0-9]", "", s)

    # Bitget U-M(USDT-Margined) 선물 접미어 보장
    if not s.endswith("_UMCBL"):
        s = f"{s}_UMCBL"

    return s


# ===== 라우트: 상태 / 파비콘 =================================================

@app.get("/")
async def index() -> Dict[str, Any]:
    return {
        "ok": True,
        "service": APP_NAME,
        "mode": "demo" if DEMO else "live",
    }


@app.get("/favicon.ico")
def favicon() -> Response:
    """
    루트 디렉터리의 favicon.ico 를 그대로 서빙
    (없으면 204)
    """
    ico_path = BASE_DIR / "favicon.ico"
    if ico_path.exists():
        return Response(
            content=ico_path.read_bytes(),
            media_type="image/x-icon",
        )
    return Response(status_code=204)


# ===== 메인: TradingView Webhook ============================================

@app.post("/tv")
async def tv_webhook(req: Request):
    """
    TradingView 웹훅 수신:
      {
        "secret":"<WEBHOOK_SECRET>",
        "route":"order.reverse",
        "exchange":"bitget",
        "symbol":"{{ticker}}",
        "target_side":"BUY" | "SELL",
        "type":"MARKET",
        "size":0.01
      }
    """
    try:
        raw = await req.body()
        # TV가 메시지 앞에 텍스트를 붙이는 경우 대비 (예: "SuperTrend Buy!{json}")
        try:
            payload = json.loads(raw)
        except Exception:
            # 마지막 '{' 이후만 추출
            text = raw.decode("utf-8", errors="ignore")
            idx = text.find("{")
            if idx >= 0:
                payload = json.loads(text[idx:])
            else:
                raise ValueError(f"Invalid JSON RAW: {text!r}")

        secret = str(payload.get("secret", "")).strip()
        route = str(payload.get("route", "")).strip()
        exchange = str(payload.get("exchange", "bitget")).strip().lower()
        tv_symbol = str(payload.get("symbol", "")).strip()
        target_side = str(payload.get("target_side", "")).strip().upper()
        ord_type = str(payload.get("type", "MARKET")).strip().upper()
        size = float(payload.get("size", 0))

        # 보안검증
        if secret != WEBHOOK_SECRET:
            log.warning("%s [TV] secret mismatch", MODE_TAG)
            return JSONResponse(status_code=401, content={"ok": False, "error": "unauthorized"})

        # 라우트/거래소/기초검증
        if route != "order.reverse":
            return JSONResponse(status_code=400, content={"ok": False, "error": "unsupported route"})
        if exchange != "bitget":
            return JSONResponse(status_code=400, content={"ok": False, "error": "unsupported exchange"})
        if target_side not in ("BUY", "SELL"):
            return JSONResponse(status_code=400, content={"ok": False, "error": "invalid target_side"})
        if ord_type != "MARKET":
            return JSONResponse(status_code=400, content={"ok": False, "error": "only MARKET supported"})
        if size <= 0:
            return JSONResponse(status_code=400, content={"ok": False, "error": "invalid size"})

        symbol = normalize_symbol(tv_symbol)

        log.info(
            "%s [TV] 수신 | %s | %s | %s | size=%.6f",
            MODE_TAG, symbol, route, target_side, size,
        )

        # === 포지션 조회
        pos = bg_client.get_net_position(symbol)  # {'net': float} (롱:+, 숏:-, 0:무포)
        net = float(pos.get("net", 0.0))

        # === 로직: reverse
        # 1) 무포 → 신규 진입
        # 2) 같은 방향 → 무시
        # 3) 반대 방향 → 전량 청산 후 반대 방향 신규 진입
        if abs(net) < 1e-12:
            # flat -> open
            bg_client.place_order(symbol, side=target_side, type=ord_type, size=size, reduce_only=False)
            state = "state-flat->open"
        else:
            curr_dir = "BUY" if net > 0 else "SELL"
            if curr_dir == target_side:
                # 같은 방향 → 무시
                state = "state=same-direction-skip"
            else:
                # 반대 방향 → 전량 청산 후 신규
                try:
                    # 우선 전량 청산
                    bg_client.close_position(symbol, side="ALL")
                except Exception as e:
                    # close_position 실패하면 reduce_only 로 청산 시도
                    log.warning("%s close_position 실패, reduce_only로 청산 시도: %s", MODE_TAG, e)
                    anti = "SELL" if net > 0 else "BUY"
                    bg_client.place_order(symbol, side=anti, type="MARKET", size=abs(net), reduce_only=True)
                # 신규 진입
                bg_client.place_order(symbol, side=target_side, type=ord_type, size=size, reduce_only=False)
                state = "state=reverse->open"

        log.info("%s [TV] 처리완료 | %s | reverse | %s", MODE_TAG, symbol, state)
        return {"ok": True, "state": state, "symbol": symbol}

    except Exception as e:
        log.exception("Exception in /tv")
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


# ===== 로컬 실행용 (render는 Procfile/uvicorn 사용) ==========================

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "10000"))
    log.info("Starting %s on port %d", APP_NAME, port)
    uvicorn.run("server:app", host="0.0.0.0", port=port, log_level="info")
