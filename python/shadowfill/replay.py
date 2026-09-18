"""Reference (oracle) implementations of book reconstruction and shadow tracking.

Correctness over speed. The C++ engine in ``shadowfill._core`` must produce
byte-identical outcomes to this module; where they differ, this module is right.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

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


TOB_DTYPE = np.dtype([("ts_ns", "<i8"), ("seq", "<u8"), ("best_bid", "<i8"), ("best_ask", "<i8")])


def top_of_book_series(events: np.ndarray) -> np.ndarray:
    """Replay ``events`` and return top-of-book *after* each event.

    A best price of 0 means that side of the book was empty.
    """
    book = RefBook()
    out = np.zeros(len(events), dtype=TOB_DTYPE)
    for i, e in enumerate(events):
        book.apply(
            int(e["ts_ns"]),
            int(e["seq"]),
            int(e["order_id"]),
            int(e["price"]),
            int(e["size"]),
            int(e["type"]),
            int(e["side"]),
        )
        out[i]["ts_ns"] = e["ts_ns"]
        out[i]["seq"] = e["seq"]
        out[i]["best_bid"] = book.best_bid() or 0
        out[i]["best_ask"] = book.best_ask() or 0
    return out
