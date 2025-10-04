# bitget.py
from __future__ import annotations
import time
import uuid
from dataclasses import dataclass, asdict
from typing import List, Dict, Optional


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class Order:
    id: str
    client_oid: Optional[str]
    ts: int
    symbol: str
    side: str           # BUY / SELL
    type: str           # MARKET / LIMIT
    size: float
    price: Optional[float] = None
    reduce_only: bool = False
    status: str = "open"  # open / filled / canceled
    note: Optional[str] = None


@dataclass
class Fill:
    id: str
    order_id: str
    ts: int
    symbol: str
    side: str
    size: float
    price: float


class DemoExchange:
    """
    매우 간단한 모의 거래 엔진 (메모리 기반)
    - 마크가격(DEMO_MARK_PRICE)을 기준으로 마켓주문은 즉시 체결
    - 리밋주문은 등록만 하고 체결은 하지 않음(조회/취소만 가능)
    - 포지션: size만 관리(+롱/-숏)
    """
    def __init__(self, mark_price: float = 100.0):
        self.mark_price = mark_price
        self.positions: Dict[str, float] = {}      # symbol -> size(+롱 / -숏)
        self.orders: Dict[str, Order] = {}         # id -> Order
        self.fills: List[Fill] = []

    # ---- helpers ----
    def _new_id(self, prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex[:16]}"

    def _mk_price(self) -> float:
        return float(self.mark_price)

    # ---- public API ----
    def place_market_order(
        self, *, symbol: str, side: str, size: float, reduce_only: bool = False,
        client_oid: Optional[str] = None
    ) -> Dict:
        oid = self._new_id("mkt")
        price = self._mk_price()
        ts = now_ms()
        order = Order(
            id=oid, client_oid=client_oid, ts=ts, symbol=symbol, side=side.upper(),
            type="MARKET", size=float(size), price=price, reduce_only=reduce_only, status="filled"
        )
        self.orders[oid] = order

        # 체결 기록
        fill = Fill(id=self._new_id("fill"), order_id=oid, ts=ts, symbol=symbol,
                    side=side.upper(), size=float(size), price=price)
        self.fills.append(fill)

        # 포지션 반영
        pos = self.positions.get(symbol, 0.0)
        delta = size if side.upper() == "BUY" else -size
        if reduce_only:
            # 반대 방향으로만 줄이기
            if pos * delta < 0:
                new_pos = pos + delta
                # reduce_only인데 방향이 늘어나는 경우 0까지만 줄임
                if (pos > 0 and new_pos > 0) or (pos < 0 and new_pos < 0):
                    new_pos = 0.0
                self.positions[symbol] = new_pos
        else:
            self.positions[symbol] = pos + delta

        return {
            "order": asdict(order),
            "fill": asdict(fill),
            "position": {"symbol": symbol, "size": self.positions.get(symbol, 0.0)}
        }

    def place_limit_order(
        self, *, symbol: str, side: str, size: float, price: float,
        reduce_only: bool = False, client_oid: Optional[str] = None, note: Optional[str] = None
    ) -> Dict:
        oid = self._new_id("lmt")
        ts = now_ms()
        order = Order(
            id=oid, client_oid=client_oid, ts=ts, symbol=symbol, side=side.upper(),
            type="LIMIT", size=float(size), price=float(price),
            reduce_only=reduce_only, status="open", note=note
        )
        self.orders[oid] = order
        return {"order": asdict(order)}

    def close_all_positions(self, *, symbol: str) -> Dict:
        pos = self.positions.get(symbol, 0.0)
        if pos == 0:
            return {"ok": True, "message": "no position"}
        side = "SELL" if pos > 0 else "BUY"
        result = self.place_market_order(symbol=symbol, side=side, size=abs(pos), reduce_only=True)
        return {"ok": True, "result": result}

    # ---- queries ----
    def get_positions(self, symbol: str) -> Dict:
        return {"symbol": symbol, "size": self.positions.get(symbol, 0.0)}

    def get_open_orders(self, symbol: str) -> List[Dict]:
        return [asdict(o) for o in self.orders.values()
                if o.symbol == symbol and o.status == "open"]

    def get_order_history(self, symbol: str, pageSize: int = 50) -> List[Dict]:
        return [asdict(o) for o in list(self.orders.values())[::-1]
                if o.symbol == symbol][:pageSize]

    def get_fills(self, symbol: str, pageSize: int = 50) -> List[Dict]:
        return [asdict(f) for f in list(self.fills)[::-1]
                if f.symbol == symbol][:pageSize]


# 전역 데모 인스턴스 (서버에서 import 하여 사용)
DEMO = DemoExchange()
# === bitget.py (파일 하단에 추가) ==================================
import time
import logging
from typing import Optional, Union

# 간단한 Net 포지션 메모리(데모용). 실거래 붙일 땐 삭제하고 실제 조회/주문으로 교체.
_NET_POS = {}  # { "BTCUSDT": float }  # >0: 롱, <0: 숏, 0: 없음

async def get_net_position_size(symbol: str) -> float:
    """Net 모드 기준 현재 포지션 크기 반환. (롱=+, 숏=-, 없음=0)"""
    return float(_NET_POS.get(symbol, 0.0))

async def place_bitget_order(
    symbol: str,
    side: str,                 # "BUY" or "SELL"
    order_type: str,           # "MARKET" or "LIMIT"
    size: Union[float, str],
    price: Optional[float] = None,
    reduce_only: bool = False,
    client_oid: Optional[str] = None,
    note: Optional[str] = None,
) -> str:
    """데모용: 메모리상의 Net 포지션만 갱신. 실거래 시 Bitget REST 호출로 교체."""
    oid = client_oid or f"demo-{int(time.time()*1000)}"
    qty = 0.0 if size == "ALL" else float(size)
    cur = _NET_POS.get(symbol, 0.0)

    if reduce_only:
        # 리듀스온리: 방향에 맞춰 포지션 줄이기만 (엄격 검사는 생략)
        if side.upper() == "SELL":   # 보통 롱 감소
            cur -= qty
        else:                        # 보통 숏 감소
            cur += qty
    else:
        # 신규/증가
        if side.upper() == "BUY":
            cur += qty
        else:
            cur -= qty

    _NET_POS[symbol] = cur
    logging.info(f"[DEMO] place_order {symbol} {side} {order_type} size={size} reduce_only={reduce_only} -> net={cur}")
    return oid

async def close_bitget_position(
    symbol: str,
    side: str,                      # 청산 시 반대 사이드로 들어오게 호출됨
    size: Union[float, str] = "ALL",
    client_oid: Optional[str] = None,
) -> dict:
    """데모용: 포지션 줄이거나 전량 플랫."""
    cur = _NET_POS.get(symbol, 0.0)
    if size == "ALL":
        closed = abs(cur)
        _NET_POS[symbol] = 0.0
    else:
        qty = float(size)
        if side.upper() == "SELL":  # 보통 롱 줄이기
            cur -= qty
        else:                       # 보통 숏 줄이기
            cur += qty
        closed = qty
        _NET_POS[symbol] = cur

    logging.info(f"[DEMO] close_position {symbol} side={side} size={size} -> net={_NET_POS[symbol]}")
    return {"symbol": symbol, "closed": closed}
# ====================================================================
