"""Deterministic synthetic MBO stream, in LOBSTER message-file format.

Used so CI never depends on third-party data, and so Plan 4 has a stream whose
cancellation mechanism is independent of the fill outcome by construction.
"""

from __future__ import annotations

import heapq
from collections import deque
from pathlib import Path

import numpy as np

from .events import EventType, Side, empty_events


def generate_synthetic_messages(
    n_events: int,
    seed: int,
    *,
    tick: int = 100,
    mid_start: int = 1_000_000,
    max_levels: int = 5,
    mean_size: int = 20,
    informed_cancel: float = 0.0,
) -> np.ndarray:
    """Generate a canonical event array from a seeded zero-intelligence process.

    ``informed_cancel`` injects a cancellation mechanism with a *known* sign,
    for Plan 4's known-bias injection test. It is 0 by default, which leaves
    the stream byte-identical to the zero-intelligence process and keeps the
    placebo's independent-censoring guarantee intact.

    The two mechanisms RESEARCH-SPEC H2 says oppose each other:

    ``informed_cancel > 0`` -- *picked-off avoidance*. With this probability a
        cancel targets the front order at the best price, which is the order
        most likely to trade next. Cancellation then removes fill-prone orders
        from the risk set, so a censoring estimator sees survivors adversely
        selected against filling and **understates** fill probability. The
        measured error ``KM - truth`` must come out **negative**.

    ``informed_cancel < 0`` -- *hopeless-queue cancellation*. With this
        probability a cancel targets an order away from the best price, which
        is unlikely to trade. Survivors are then enriched for fillable orders
        and the estimator **overstates**. The error must come out **positive**.

    The magnitude is not analytically known, so the injection test pins the
    sign and the monotonicity in strength rather than a closed-form number.
    """
    if not -1.0 <= informed_cancel <= 1.0:
        raise ValueError(f"informed_cancel must lie in [-1, 1], got {informed_cancel}")
    rng = np.random.default_rng(seed)
    events = empty_events(n_events)

    resting: dict[int, tuple[int, int, int]] = {}  # oid -> (price, size, side)
    # Per-side live-order indices. `live_ids[side]` is a dense list for O(1)
    # uniform choice; `live_pos[side]` maps oid -> its slot so removal is a
    # swap with the last element instead of a scan. Rebuilding these lists per
    # event, as the plan did, is O(resting) and the resting set grows without
    # bound, which made the generator super-linear: 40k events took 43 s and
    # the 2M-event benchmark stream was unreachable.
    live_ids: dict[int, list[int]] = {int(Side.BID): [], int(Side.ASK): []}
    live_pos: dict[int, dict[int, int]] = {int(Side.BID): {}, int(Side.ASK): {}}

    # Arrival-ordered queue per (side, price), plus a heap of prices holding at
    # least one live order, so the front of the book is O(log n) to find.
    # Executions consume the front; cancels do not, so a queue may hold ids that
    # are no longer resting. They are purged lazily from the front, which costs
    # O(1) amortised because each id is discarded at most once.
    level_q: dict[int, dict[int, deque[int]]] = {int(Side.BID): {}, int(Side.ASK): {}}
    best_heap: dict[int, list[int]] = {int(Side.BID): [], int(Side.ASK): []}
    heaped: dict[int, set[int]] = {int(Side.BID): set(), int(Side.ASK): set()}

    def _key(side: int, price: int) -> int:
        """Heap key. Bids negate so the best (highest) price is the heap root."""
        return -price if side == int(Side.BID) else price

    def _add_live(oid: int, side: int, price: int) -> None:
        live_pos[side][oid] = len(live_ids[side])
        live_ids[side].append(oid)
        level_q[side].setdefault(price, deque()).append(oid)
        if price not in heaped[side]:
            heapq.heappush(best_heap[side], _key(side, price))
            heaped[side].add(price)

    def _drop_live(oid: int, side: int) -> None:
        ids = live_ids[side]
        pos = live_pos[side].pop(oid)
        last = ids.pop()
        if last != oid:
            ids[pos] = last
            live_pos[side][last] = pos

    def _front(side: int, price: int) -> int | None:
        """Oldest still-resting order at this price, purging cancelled ids."""
        q = level_q[side].get(price)
        if q is None:
            return None
        while q and q[0] not in resting:
            q.popleft()
        return q[0] if q else None

    def _best(side: int) -> int | None:
        """Best price on this side that still has a live order."""
        heap = best_heap[side]
        while heap:
            price = abs(heap[0])
            if _front(side, price) is not None:
                return price
            heapq.heappop(heap)
            heaped[side].discard(price)
        return None

    def _back(side: int, price: int) -> int | None:
        """Newest still-resting order at this price, purging cancelled ids."""
        q = level_q[side].get(price)
        if q is None:
            return None
        while q and q[-1] not in resting:
            q.pop()
        return q[-1] if q else None

    def _informed_target(side: int, pick_off: bool, fallback: int) -> int:
        """Choose a cancel target with a known relationship to fill prospects.

        Both arms act at the *best* price, so the only thing that differs is
        queue position. Targeting deep price levels instead was tried first and
        measured no bias at all: an order five levels down does not fill inside
        the horizon whether or not anyone cancels it, so removing it moves
        neither curve. Queue position at the touch is where fill prospects
        actually live.

        ``pick_off`` targets the front -- the order about to trade. Otherwise
        targets the back -- the order with the whole queue ahead of it. Falls
        back to the uniform draw whenever the level is empty, so the injection
        weakens toward the zero-intelligence process rather than distorting the
        book.
        """
        best = _best(side)
        if best is None:
            return fallback
        chosen = _front(side, best) if pick_off else _back(side, best)
        return fallback if chosen is None else chosen

    next_oid = 1
    ts = 0
    mid = mid_start
    written = 0

    while written < n_events:
        ts += int(rng.exponential(1_000_000)) + 1
        side = int(Side.BID) if rng.random() < 0.5 else int(Side.ASK)
        live = live_ids[side]
        roll = rng.random()

        if roll < 0.55 or not live:
            level = int(rng.integers(0, max_levels))
            price = mid - tick * (level + 1) if side == Side.BID else mid + tick * (level + 1)
            size = int(rng.poisson(mean_size)) + 1
            oid = next_oid
            next_oid += 1
            resting[oid] = (price, size, side)
            _add_live(oid, side, price)
            events[written] = (ts, written, oid, price, size, int(EventType.ADD), side)
            written += 1
            continue

        if roll < 0.80:
            # Cancels are zero-intelligence by default: uniform over live
            # orders at any depth. Plan 4's placebo needs a cancellation
            # mechanism independent of fill outcome by construction, so the
            # default path must not look at the queue front.
            oid = int(live[int(rng.integers(0, len(live)))])
            if informed_cancel != 0.0 and rng.random() < abs(informed_cancel):
                oid = _informed_target(side, informed_cancel > 0.0, oid)
            price, size, _ = resting[oid]
            if roll < 0.70:
                qty = max(1, size // 2)
                if qty >= size:
                    del resting[oid]
                    _drop_live(oid, side)
                    etype = int(EventType.DELETE)
                    qty = size
                else:
                    resting[oid] = (price, size - qty, side)
                    etype = int(EventType.CANCEL_PARTIAL)
            else:
                del resting[oid]
                _drop_live(oid, side)
                etype = int(EventType.DELETE)
                qty = size
            events[written] = (ts, written, oid, price, qty, etype, side)
            written += 1
            continue

        # Executions respect price-time priority: a marketable order crosses the
        # spread and hits the oldest resting order at the best price. Picking a
        # uniform live order instead, as this generator first did, put only 0.3%
        # of executions at the best price and 3.2% at the front of their own
        # level, which is not a matching engine and makes every queue-position
        # statistic derived from the fixture unsafe to reason about.
        best = _best(side)
        assert best is not None, "live orders exist on this side, so a best price must too"
        price = best
        front = _front(side, price)
        assert front is not None
        oid = front
        size = resting[oid][1]

        if roll < 0.95:
            qty = min(size, int(rng.integers(1, mean_size + 1)))
            if qty >= size:
                del resting[oid]
                _drop_live(oid, side)
            else:
                resting[oid] = (price, size - qty, side)
            events[written] = (ts, written, oid, price, qty, int(EventType.EXECUTE), side)
            written += 1
            mid += tick if side == Side.ASK else -tick
            continue

        # Hidden execution: at the best price, and by construction it never
        # touches the visible queue.
        qty = int(rng.integers(1, mean_size + 1))
        events[written] = (ts, written, 0, price, qty, int(EventType.EXECUTE_HIDDEN), side)
        written += 1

    return events


def write_synthetic_csv(events: np.ndarray, path: str | Path) -> None:
    """Write a canonical event array in LOBSTER message-file format."""
    lines = []
    for e in events:
        seconds, frac = divmod(int(e["ts_ns"]), 1_000_000_000)
        direction = 1 if int(e["side"]) == int(Side.BID) else -1
        lines.append(
            f"{seconds}.{frac:09d},{int(e['type'])},{int(e['order_id'])},"
            f"{int(e['size'])},{int(e['price'])},{direction}"
        )
    Path(path).write_text("\n".join(lines) + "\n")
