"""Extract real-order lifetimes from a canonical event stream.

This is the *observational* population: every order someone actually sent,
with how long it rested and how it left the book. It is what the survival
literature fits on, and its defining weakness is that cancellation is
endogenous -- informed traders pull orders ahead of adverse moves, market
makers pull to avoid being picked off -- so treating cancellation as
independent censoring is exactly the mistake ShadowFill measures.

Nothing here corrects for that. The correction is the ground-truth engine's
job. This module's only obligation is to describe real orders with **the same
numbers** the shadow tracker uses for hypothetical ones: ``ahead_at_arrival``
is defined identically to ``Outcome.ahead_at_insert``, and both are read from
the book before the arriving event is applied. If those two drifted apart, the
gap H1 reports would partly be our own bookkeeping.

Strictly streaming and prefix-only, per invariant 4: an order's row is settled
by the event that removes it and never revised.
"""

from __future__ import annotations

import heapq
from dataclasses import astuple, dataclass, fields
from enum import IntEnum

import numpy as np

from .events import CONSUMES_VISIBLE_QUEUE, EventType, Side
from .replay import RefBook


class ExitReason(IntEnum):
    """How an order left the book.

    FILLED and CANCELLED are competing risks; CENSORED is the administrative
    kind produced by the recording window ending, which is the only censoring
    here that is genuinely independent of the order's prospects.
    """

    FILLED = 1
    CANCELLED = 2
    CENSORED = 3


LIFETIME_DTYPE = np.dtype(
    [
        ("order_id", "<u8"),
        ("side", "i1"),
        ("price", "<i8"),
        ("size", "<i8"),
        ("arrival_ts", "<i8"),
        ("arrival_seq", "<u8"),
        ("ahead_at_arrival", "<i8"),
        ("best_bid_at_arrival", "<i8"),
        ("best_ask_at_arrival", "<i8"),
        ("first_fill_ts", "<i8"),
        ("exit_ts", "<i8"),
        ("filled_qty", "<i8"),
        ("exit_reason", "u1"),
    ]
)

#: ``first_fill_ts`` when the order never traded. Matches ``Outcome``'s
#: convention: -1 is a real observation ("did not fill"), not missingness.
NO_FILL = -1


@dataclass
class _Life:
    """One order's row while it is still being written.

    Field order is load-bearing: rows are emitted with ``astuple`` straight
    into ``LIFETIME_DTYPE``, and the assertion below keeps the two in step. The
    alternative, indexing a bare list by position, is how a queue statistic
    ends up in the timestamp column without anything raising.
    """

    order_id: int
    side: int
    price: int
    size: int
    arrival_ts: int
    arrival_seq: int
    ahead_at_arrival: int
    best_bid_at_arrival: int
    best_ask_at_arrival: int
    first_fill_ts: int = NO_FILL
    exit_ts: int = -1
    filled_qty: int = 0
    exit_reason: int = int(ExitReason.CENSORED)


assert tuple(f.name for f in fields(_Life)) == LIFETIME_DTYPE.names


class _TouchIndex:
    """Best price per side, maintained beside the book rather than inside it.

    ``RefBook.best_bid`` is a linear scan on purpose -- it is the oracle and
    optimises for being obviously correct. That is fine per call and quadratic
    here, where the touch is read on every arrival. So this keeps a heap of
    candidate prices and treats the book's own level map as the truth: a price
    is live exactly while it has size. Stale entries are discarded from the top
    on demand, each at most once, so the cost is O(1) amortised.
    """

    def __init__(self, book: RefBook) -> None:
        self._book = book
        self._heaps: dict[int, list[int]] = {int(Side.BID): [], int(Side.ASK): []}
        self._known: dict[int, set[int]] = {int(Side.BID): set(), int(Side.ASK): set()}

    @staticmethod
    def _key(side: int, price: int) -> int:
        """Bids negate so the best (highest) price sits at the heap root."""
        return -price if side == int(Side.BID) else price

    def note(self, side: int, price: int) -> None:
        if price not in self._known[side]:
            heapq.heappush(self._heaps[side], self._key(side, price))
            self._known[side].add(price)

    def best(self, side: int) -> int:
        """Best live price on this side, or 0 if the side is empty."""
        levels = self._book.bids if side == int(Side.BID) else self._book.asks
        heap = self._heaps[side]
        while heap:
            price = abs(heap[0])
            if levels.get(price, 0) > 0:
                return price
            heapq.heappop(heap)
            self._known[side].discard(price)
        return 0


def extract_lifetimes(events: np.ndarray) -> np.ndarray:
    """One row per order added inside the window, in arrival order.

    Orders removed by an event but never added inside the window produce no
    row: their arrival state is unknown, so they are not observations. That is
    the same population rule the ground-truth engine applies when it counts
    ``unknown_order_events`` instead of inventing a queue position for them.
    """
    book = RefBook()
    touch = _TouchIndex(book)

    lives: list[_Life] = []
    index: dict[int, _Life] = {}
    last_ts = 0

    for e in events:
        ts_ns = int(e["ts_ns"])
        seq = int(e["seq"])
        order_id = int(e["order_id"])
        price = int(e["price"])
        size = int(e["size"])
        etype = int(e["type"])
        side = int(e["side"])
        last_ts = ts_ns

        if etype == EventType.ADD:
            # Read before the book applies the event: the arriving order is not
            # part of the queue ahead of itself.
            life = _Life(
                order_id=order_id,
                side=side,
                price=price,
                size=size,
                arrival_ts=ts_ns,
                arrival_seq=seq,
                ahead_at_arrival=book.level_size(side, price),
                best_bid_at_arrival=touch.best(int(Side.BID)),
                best_ask_at_arrival=touch.best(int(Side.ASK)),
                exit_ts=ts_ns,
            )
            lives.append(life)
            index[order_id] = life
            touch.note(side, price)

        elif etype in CONSUMES_VISIBLE_QUEUE:
            life_opt = index.get(order_id)
            resting = book.find(order_id)
            if life_opt is not None and resting is not None:
                removed = min(size, resting.size)
                if etype == EventType.EXECUTE:
                    if life_opt.first_fill_ts == NO_FILL:
                        life_opt.first_fill_ts = ts_ns
                    life_opt.filled_qty += removed
                if removed >= resting.size:
                    life_opt.exit_ts = ts_ns
                    life_opt.exit_reason = int(
                        ExitReason.FILLED if etype == EventType.EXECUTE else ExitReason.CANCELLED
                    )

        book.apply(ts_ns, seq, order_id, price, size, etype, side)

    # Whatever is still resting was cut off by the window, not by a decision.
    for life in lives:
        if life.exit_reason == int(ExitReason.CENSORED):
            life.exit_ts = last_ts

    out = np.zeros(len(lives), dtype=LIFETIME_DTYPE)
    for i, life in enumerate(lives):
        out[i] = astuple(life)
    return out
