"""Deterministic synthetic MBO stream, in LOBSTER message-file format.

Used so CI never depends on third-party data, and so Plan 4 has a stream whose
cancellation mechanism is independent of the fill outcome by construction.
"""

from __future__ import annotations

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
) -> np.ndarray:
    """Generate a canonical event array from a seeded zero-intelligence process."""
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

    def _add_live(oid: int, side: int) -> None:
        live_pos[side][oid] = len(live_ids[side])
        live_ids[side].append(oid)

    def _drop_live(oid: int, side: int) -> None:
        ids = live_ids[side]
        pos = live_pos[side].pop(oid)
        last = ids.pop()
        if last != oid:
            ids[pos] = last
            live_pos[side][last] = pos

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
            _add_live(oid, side)
            events[written] = (ts, written, oid, price, size, int(EventType.ADD), side)
            written += 1
            continue

        oid = int(live[int(rng.integers(0, len(live)))])
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
            events[written] = (ts, written, oid, price, qty, etype, side)
            written += 1
            continue

        if roll < 0.80:
            del resting[oid]
            _drop_live(oid, side)
            events[written] = (ts, written, oid, price, size, int(EventType.DELETE), side)
            written += 1
            continue

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
