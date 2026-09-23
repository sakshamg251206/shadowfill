"""Reference (oracle) implementations of book reconstruction and shadow tracking.

Correctness over speed. The C++ engine in ``shadowfill._core`` must produce
byte-identical outcomes to this module; where they differ, this module is right.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import IntEnum

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
        self.fifo_violations: int = 0
        # Arrival-ordered order ids per (side, price). Cancels can remove an
        # order from the middle, so the queue is purged lazily from the front;
        # each id is discarded at most once, so this stays O(1) amortised.
        self._level_q: dict[tuple[int, int], deque[int]] = {}

    def _oldest_resting(self, side: int, price: int) -> Resting | None:
        """The earliest-arrived order still resting at this price, or None."""
        q = self._level_q.get((side, price))
        if q is None:
            return None
        while q and q[0] not in self.orders:
            q.popleft()
        return self.orders[q[0]] if q else None

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
            self._level_q.setdefault((side, price), deque()).append(order_id)
            return

        if etype in (EventType.CANCEL_PARTIAL, EventType.DELETE, EventType.EXECUTE):
            order = self.orders.get(order_id)
            if etype == EventType.EXECUTE and order is not None:
                # Price-time priority says a marketable order hits the front of
                # the level. If an order that arrived earlier is still resting
                # here, the exchange did not match FIFO as reconstructed, which
                # on real data means hidden liquidity, an order type this
                # reconstruction does not model, or a genuine gap in it. A
                # correct FIFO stream gives zero. Deliberately says nothing
                # about any shadow: it is a property of the data alone.
                oldest = self._oldest_resting(order.side, order.price)
                if oldest is not None and oldest.seq < order.seq:
                    self.fifo_violations += 1
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


class Status(IntEnum):
    """Terminal state of a shadow order.

    TRUNCATED and NOT_ACTIVATED are deliberately distinct. TRUNCATED means the
    order was live and still resting when the stream ended -- a censored
    observation. NOT_ACTIVATED means its effective timestamp fell past the last
    event, so it never existed at all, and it must not enter any fill-rate
    denominator.
    """

    RESTING = 0
    FILLED = 1
    EXPIRED = 2
    TRUNCATED = 3
    NOT_ACTIVATED = 4


@dataclass(frozen=True)
class Placement:
    """A hypothetical passive order to evaluate."""

    shadow_id: int
    ts_ns: int
    latency_ns: int
    side: int
    price: int
    size: int
    horizon_ns: int

    @property
    def effective_ts(self) -> int:
        return self.ts_ns + self.latency_ns


@dataclass
class Outcome:
    """Ground-truth result for one never-cancel shadow order.

    Sentinel conventions, which the Parquet writer relies on:

    * ``first_fill_ts``/``full_fill_ts`` of -1 on an activated order means
      "did not fill". That is a real observation and is written as -1.
    * Every field of a NOT_ACTIVATED outcome is -1 or 0 because the order never
      existed. Those are genuinely missing and are written as Arrow nulls.
    """

    shadow_id: int
    status: int = int(Status.RESTING)
    insert_ts: int = -1
    insert_seq: int = -1
    ahead_at_insert: int = -1
    ahead_at_end: int = -1
    first_fill_ts: int = -1
    full_fill_ts: int = -1
    filled_qty: int = 0
    assumed_ahead_events: int = 0


@dataclass
class _Active:
    placement: Placement
    outcome: Outcome
    ahead: int
    expiry_ts: int


class CancelModel(IntEnum):
    """How an *anonymous* cancel is attributed to the queue around a shadow.

    Only matters on an L2-ablated stream, where cancels carry ``order_id == 0``
    and nobody can say whether the removed size sat ahead of the shadow or
    behind it. With ``L`` shares at the level and ``a`` of them ahead, a cancel
    of ``q`` removes from ahead:

    ``FRONT``         ``min(q, a)`` -- optimistic, the queue shrinks as fast as
                      it possibly could. The engine's original behaviour.
    ``BACK``          ``max(0, q - (L - a))`` -- pessimistic, everything behind
                      goes first.
    ``PROPORTIONAL``  ``floor(q * a / L)`` -- the expected value when the
                      cancelled share is uniform over the level's shares.

    Integer floor so the Python oracle and the C++ engine agree bit for bit:
    both operands are non-negative, so Python ``//`` and C++ ``/`` coincide.
    "Uniform over orders" is not offered: it needs an order count, which an L2
    feed does not carry, so on L2 it collapses into ``PROPORTIONAL``.
    """

    FRONT = 0
    BACK = 1
    PROPORTIONAL = 2


#: The order id ``l2.ablate_to_l2`` writes onto every cancel and delete.
ANONYMOUS_ORDER_ID = 0


def anonymous_removal_from_ahead(model: int, qty: int, ahead: int, level: int) -> int:
    """Shares of an anonymous cancel of ``qty`` that came from ahead of a shadow."""
    if model == CancelModel.BACK:
        behind = max(0, level - ahead)
        taken = max(0, qty - behind)
    elif model == CancelModel.PROPORTIONAL:
        taken = qty * ahead // level if level > 0 else 0
    else:
        taken = qty
    # Clamped because a windowed session can leave a level holding less than
    # the cancel removes -- orders resting from before the window are absent.
    return min(ahead, taken)


class RefShadowTracker:
    """Never-cancel shadow-order accounting over a canonical event stream.

    Per event, in exactly this order:

    1. activate pending placements with ``effective_ts < ev.ts_ns``
    2. expire actives whose horizon elapsed before ``ev.ts_ns``
    3. match the event against the remaining actives
    4. harvest filled
    5. apply the event to the book

    Matching must precede the book update: deciding whether a cancelled order
    was ahead requires looking up its arrival sequence before it is deleted.
    Activation must precede expiry: a placement whose entire life falls inside a
    quiet gap between two events has to activate and then expire, rather than
    surviving to the end of the stream.

    Tie convention: an order arriving at exactly the placement's effective
    timestamp is **ahead** of the shadow. Sequence numbers decide priority and
    the shadow has none, so we take the pessimistic side. Never flatter the
    hypothetical order.
    """

    def __init__(
        self,
        book: RefBook,
        placements: list[Placement],
        cancel_model: int = CancelModel.FRONT,
    ) -> None:
        self.book = book
        self.cancel_model = int(cancel_model)
        self._pending = sorted(placements, key=lambda p: (p.effective_ts, p.shadow_id))
        self._next = 0
        self._active: list[_Active] = []
        self.outcomes: dict[int, Outcome] = {}
        self.unknown_order_assumed_ahead = 0

    @property
    def fifo_violations(self) -> int:
        """Price-time priority breaches seen in the data (see RefBook).

        It lives on the book, not here, because it is a property of the event
        stream alone. The earlier shadow-relative version of this counter fired
        whenever a shadow reached the front of its queue and was filled there --
        the normal path, not a defect -- so it could never be zero on a correct
        FIFO stream and told us nothing about the data.
        """
        return self.book.fifo_violations

    def _activate(self, now_ts: int, now_seq: int) -> None:
        while self._next < len(self._pending):
            p = self._pending[self._next]
            if p.effective_ts >= now_ts:
                break
            outcome = Outcome(
                shadow_id=p.shadow_id,
                insert_ts=p.effective_ts,
                insert_seq=now_seq,
                ahead_at_insert=self.book.level_size(p.side, p.price),
            )
            self._active.append(
                _Active(
                    placement=p,
                    outcome=outcome,
                    ahead=outcome.ahead_at_insert,
                    expiry_ts=p.effective_ts + p.horizon_ns,
                )
            )
            self._next += 1

    def _expire(self, now_ts: int) -> None:
        still: list[_Active] = []
        for a in self._active:
            if a.expiry_ts < now_ts:
                a.outcome.status = int(Status.EXPIRED)
                a.outcome.ahead_at_end = a.ahead
                self.outcomes[a.outcome.shadow_id] = a.outcome
            else:
                still.append(a)
        self._active = still

    def _is_ahead(self, order_id: int, insert_seq: int) -> bool:
        """Non-counting priority lookup. Unknown ids are assumed ahead."""
        order = self.book.find(order_id)
        if order is None:
            return True
        return order.seq < insert_seq

    def _match(
        self, ts_ns: int, order_id: int, price: int, size: int, etype: int, side: int
    ) -> None:
        if etype not in (EventType.CANCEL_PARTIAL, EventType.DELETE, EventType.EXECUTE):
            return  # ADD is behind us; hidden/cross/halt touch no visible queue
        for a in self._active:
            p = a.placement
            if p.side != side or p.price != price:
                continue
            if etype in (EventType.CANCEL_PARTIAL, EventType.DELETE):
                if order_id == ANONYMOUS_ORDER_ID and self.cancel_model != CancelModel.FRONT:
                    # An L2 heuristic, not the unknown-id assumption, so it
                    # does not count towards assumed_ahead_events. FRONT stays
                    # on the original path below, which keeps every committed
                    # result -- H4 included -- bit-identical.
                    level = self.book.level_size(side, price)
                    a.ahead -= anonymous_removal_from_ahead(self.cancel_model, size, a.ahead, level)
                    continue
                if not self._is_ahead(order_id, a.outcome.insert_seq):
                    continue
                if a.ahead > 0 and self.book.find(order_id) is None:
                    # The decrement rests on the unverifiable assumption that an
                    # id absent from the book was resting ahead of the shadow.
                    self.unknown_order_assumed_ahead += 1
                    a.outcome.assumed_ahead_events += 1
                a.ahead = max(0, a.ahead - size)
                continue
            # EXECUTE: price-time priority means the front of the queue is hit
            # first, so whatever this execution does not consume of `ahead`
            # reaches the shadow.
            consumed = min(size, a.ahead)
            a.ahead -= consumed
            residual = size - consumed
            if residual <= 0:
                continue
            remaining = p.size - a.outcome.filled_qty
            fill = min(residual, remaining)
            if fill <= 0:
                continue
            if a.outcome.filled_qty == 0:
                a.outcome.first_fill_ts = ts_ns
            a.outcome.filled_qty += fill
            if a.outcome.filled_qty >= p.size:
                a.outcome.full_fill_ts = ts_ns
                a.outcome.status = int(Status.FILLED)
                a.outcome.ahead_at_end = a.ahead

    def _harvest_filled(self) -> None:
        still: list[_Active] = []
        for a in self._active:
            if a.outcome.status == int(Status.FILLED):
                self.outcomes[a.outcome.shadow_id] = a.outcome
            else:
                still.append(a)
        self._active = still

    def on_event(
        self,
        ts_ns: int,
        seq: int,
        order_id: int,
        price: int,
        size: int,
        etype: int,
        side: int,
    ) -> None:
        self._activate(ts_ns, seq)
        self._expire(ts_ns)
        self._match(ts_ns, order_id, price, size, etype, side)
        self._harvest_filled()
        self.book.apply(ts_ns, seq, order_id, price, size, etype, side)

    def finalize(self) -> None:
        for a in self._active:
            a.outcome.status = int(Status.TRUNCATED)
            a.outcome.ahead_at_end = a.ahead
            self.outcomes[a.outcome.shadow_id] = a.outcome
        self._active = []
        while self._next < len(self._pending):
            p = self._pending[self._next]
            self.outcomes[p.shadow_id] = Outcome(
                shadow_id=p.shadow_id, status=int(Status.NOT_ACTIVATED)
            )
            self._next += 1


def replay_reference(
    events: np.ndarray,
    placements: list[Placement],
    cancel_model: int = CancelModel.FRONT,
) -> tuple[list[Outcome], dict[str, int]]:
    """Replay ``events`` and return (outcomes ordered by shadow_id, diagnostics).

    The diagnostics carry the same three counters the C++ engine reports, so a
    run is traceable whichever engine produced it.
    """
    tracker = RefShadowTracker(RefBook(), placements, cancel_model)
    for e in events:
        tracker.on_event(
            int(e["ts_ns"]),
            int(e["seq"]),
            int(e["order_id"]),
            int(e["price"]),
            int(e["size"]),
            int(e["type"]),
            int(e["side"]),
        )
    tracker.finalize()
    outcomes = [
        tracker.outcomes[p.shadow_id] for p in sorted(placements, key=lambda x: x.shadow_id)
    ]
    diagnostics = {
        "unknown_order_assumed_ahead": tracker.unknown_order_assumed_ahead,
        "fifo_violations": tracker.fifo_violations,
        "unknown_order_events": tracker.book.unknown_order_events,
    }
    return outcomes, diagnostics


def run_reference(
    events: np.ndarray,
    placements: list[Placement],
    cancel_model: int = CancelModel.FRONT,
) -> list[Outcome]:
    """Replay ``events`` and return one Outcome per placement, ordered by shadow_id."""
    return replay_reference(events, placements, cancel_model)[0]
