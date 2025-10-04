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
