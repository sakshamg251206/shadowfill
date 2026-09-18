"""Reference (oracle) implementations of book reconstruction and shadow tracking.

Correctness over speed. The C++ engine in ``shadowfill._core`` must produce
byte-identical outcomes to this module; where they differ, this module is right.
"""

from __future__ import annotations

from dataclasses import dataclass

from .events import EventType, Side


@dataclass
class Resting:
    """A live order in the visible book."""

    price: int
    size: int
    seq: int
    side: int


class RefBook:
    """Order-by-order book state keyed by exchange order id.

    ``best_bid``/``best_ask`` are linear scans. That is deliberate: this is the
    oracle, so it optimises for being obviously correct, and the C++ engine is
    the fast path.
    """

    def __init__(self) -> None:
        self.orders: dict[int, Resting] = {}
        self.bids: dict[int, int] = {}
        self.asks: dict[int, int] = {}
        self.unknown_order_events: int = 0

    def _levels(self, side: int) -> dict[int, int]:
        return self.bids if side == Side.BID else self.asks

    def _reduce_level(self, side: int, price: int, qty: int) -> None:
        levels = self._levels(side)
        remaining = levels.get(price, 0) - qty
        if remaining > 0:
            levels[price] = remaining
        else:
            levels.pop(price, None)

    def apply(
        self,
        ts_ns: int,
        seq: int,
        order_id: int,
        price: int,
        size: int,
        etype: int,
        side: int,
    ) -> None:
        """Apply one canonical event to the visible book."""
        if etype == EventType.ADD:
            self.orders[order_id] = Resting(price=price, size=size, seq=seq, side=side)
            levels = self._levels(side)
            levels[price] = levels.get(price, 0) + size
            return

        if etype in (EventType.CANCEL_PARTIAL, EventType.DELETE, EventType.EXECUTE):
            order = self.orders.get(order_id)
            if order is None:
                # Added before the recording window or outside the level band.
                self.unknown_order_events += 1
                self._reduce_level(side, price, size)
                return
            order.size -= size
            self._reduce_level(order.side, order.price, size)
            if order.size <= 0:
                del self.orders[order_id]
            return

        # EXECUTE_HIDDEN, CROSS, HALT leave the visible book unchanged.
        return

    def find(self, order_id: int) -> Resting | None:
        return self.orders.get(order_id)

    def level_size(self, side: int, price: int) -> int:
        return self._levels(side).get(price, 0)

    def best_bid(self) -> int | None:
        return max(self.bids) if self.bids else None

    def best_ask(self) -> int | None:
        return min(self.asks) if self.asks else None
