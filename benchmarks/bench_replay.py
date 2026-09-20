"""Throughput benchmark. Fails the build if the C++ engine regresses.

Two gates, because they answer different questions:

``main()``           absolute events/s, enforced locally on known hardware.
``--ci``             speedup over the pure-Python reference engine, measured
                     on the same machine in the same run, so it holds on any
                     runner without assuming its absolute speed.

The absolute rate is reported either way; only the pass/fail criterion differs.
"""

from __future__ import annotations

import sys
import time

from shadowfill import _core
from shadowfill.ground_truth import _run_cpp
from shadowfill.placements import place_top_of_book_grid
from shadowfill.replay import replay_reference
from shadowfill.synthetic import generate_synthetic_messages

MIN_EVENTS_PER_SECOND = 2_000_000

# The reference engine is ~4.5k events/s, so the calibration slice is sized to
# keep it near ten seconds rather than minutes. Both engines replay this same
# slice, so runner speed divides out of the ratio.
CALIBRATION_EVENTS = 50_000

# Observed ~900x on developer hardware. The floor is set well below that: this
# is an order-of-magnitude check, and a shared runner is too noisy to justify
# anything tighter. If it trips, the engine changed -- not the machine.
MIN_SPEEDUP_VS_REFERENCE = 200.0


def _placements(events):
    return place_top_of_book_grid(
        events, grid_ns=10_000_000, size=100,
        horizon_ns=10_000_000_000, latency_ns=0,
    )


def _time_cpp(events, placements) -> float:
    start = time.perf_counter()
    _run_cpp(events, placements)
    return time.perf_counter() - start


def main(ci: bool = False) -> int:
    assert _core is not None
    events = generate_synthetic_messages(n_events=2_000_000, seed=20260919)
    placements = _placements(events)
    print(f"events={len(events):,} placements={len(placements):,}")

    elapsed = _time_cpp(events, placements)
    rate = len(events) / elapsed
    print(f"replay: {elapsed:.3f}s -> {rate:,.0f} events/s")

    if not ci:
        if rate < MIN_EVENTS_PER_SECOND:
            print(f"FAIL: below gate of {MIN_EVENTS_PER_SECOND:,} events/s")
            return 1
        print("OK")
        return 0

    slice_events = events[:CALIBRATION_EVENTS]
    slice_placements = _placements(slice_events)

    start = time.perf_counter()
    replay_reference(slice_events, slice_placements)
    ref_elapsed = time.perf_counter() - start
    cpp_elapsed = _time_cpp(slice_events, slice_placements)

    speedup = ref_elapsed / cpp_elapsed
    print(
        f"calibration on {len(slice_events):,} events: "
        f"reference {len(slice_events) / ref_elapsed:,.0f} events/s, "
        f"cpp {len(slice_events) / cpp_elapsed:,.0f} events/s"
    )
    print(f"speedup over reference: {speedup:,.0f}x")

    if speedup < MIN_SPEEDUP_VS_REFERENCE:
        print(f"FAIL: below gate of {MIN_SPEEDUP_VS_REFERENCE:,.0f}x over reference")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(ci="--ci" in sys.argv[1:]))
