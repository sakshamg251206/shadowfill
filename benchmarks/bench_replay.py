"""Throughput benchmark. Fails the build if the C++ engine regresses."""

from __future__ import annotations

import sys
import time

from shadowfill import _core
from shadowfill.ground_truth import _run_cpp
from shadowfill.placements import place_top_of_book_grid
from shadowfill.synthetic import generate_synthetic_messages

MIN_EVENTS_PER_SECOND = 2_000_000


def main() -> int:
    assert _core is not None
    events = generate_synthetic_messages(n_events=2_000_000, seed=20260919)
    placements = place_top_of_book_grid(
        events, grid_ns=10_000_000, size=100,
        horizon_ns=10_000_000_000, latency_ns=0,
    )
    print(f"events={len(events):,} placements={len(placements):,}")

    start = time.perf_counter()
    _run_cpp(events, placements)
    elapsed = time.perf_counter() - start
    rate = len(events) / elapsed
    print(f"replay: {elapsed:.3f}s -> {rate:,.0f} events/s")

    if rate < MIN_EVENTS_PER_SECOND:
        print(f"FAIL: below gate of {MIN_EVENTS_PER_SECOND:,} events/s")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
