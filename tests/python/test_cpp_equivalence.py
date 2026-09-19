import numpy as np
import pytest

from shadowfill.replay import Status, run_reference
from shadowfill.synthetic import generate_synthetic_messages

from .test_invariants import grid_placements

core = pytest.importorskip("shadowfill._core")


def run_core(events, placements):
    return core.replay(
        events["ts_ns"],
        events["seq"],
        events["order_id"],
        events["price"],
        events["size"],
        events["type"],
        events["side"],
        np.array([p.shadow_id for p in placements], dtype=np.uint64),
        np.array([p.ts_ns for p in placements], dtype=np.int64),
        np.array([p.latency_ns for p in placements], dtype=np.int64),
        np.array([p.side for p in placements], dtype=np.int8),
        np.array([p.price for p in placements], dtype=np.int64),
        np.array([p.size for p in placements], dtype=np.int64),
        np.array([p.horizon_ns for p in placements], dtype=np.int64),
    )


@pytest.mark.parametrize("seed", [1, 2, 3, 17, 99])
def test_cpp_matches_python_reference_field_by_field(seed):
    events = generate_synthetic_messages(n_events=40_000, seed=seed)
    placements = grid_placements(events, every=250)
    assert placements

    expected = run_reference(events, placements)
    actual = run_core(events, placements)

    assert len(actual["shadow_id"]) == len(expected)
    for field in (
        "status",
        "insert_ts",
        "insert_seq",
        "ahead_at_insert",
        "ahead_at_end",
        "first_fill_ts",
        "full_fill_ts",
        "filled_qty",
    ):
        np.testing.assert_array_equal(
            actual[field],
            np.array([getattr(o, field) for o in expected]),
            err_msg=f"mismatch in field {field} for seed {seed}",
        )


def test_cpp_reports_the_same_diagnostics():
    events = generate_synthetic_messages(n_events=40_000, seed=5)
    placements = grid_placements(events, every=250)
    actual = run_core(events, placements)
    assert actual["fifo_violations"] == 0
    assert actual["unknown_order_assumed_ahead"] >= 0


def test_cpp_produces_some_fills_so_the_comparison_is_meaningful():
    events = generate_synthetic_messages(n_events=40_000, seed=2)
    placements = grid_placements(events, every=250)
    actual = run_core(events, placements)
    assert (actual["status"] == int(Status.FILLED)).sum() > 0
