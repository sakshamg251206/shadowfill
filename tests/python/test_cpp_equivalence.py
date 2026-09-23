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
        "assumed_ahead_events",
        "ahead_lt_1000_ts",
        "ahead_lt_100_ts",
        "ahead_lt_10_ts",
        "ahead_lt_1_ts",
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


@pytest.mark.parametrize("seed", [4, 23, 57])
@pytest.mark.parametrize("model", [0, 1, 2], ids=["front", "back", "proportional"])
def test_cpp_matches_python_under_every_l2_cancel_model(seed, model):
    """Invariant 5 extended to the L2 heuristics: on an ablated stream, where
    every cancel is anonymous and the model decides the whole queue evolution,
    the two engines must still agree field by field and on every diagnostic."""
    from shadowfill.ground_truth import OUTCOME_FIELDS, compute_outcomes
    from shadowfill.l2 import ablate_to_l2
    from shadowfill.placements import place_matched_to_orders

    events = ablate_to_l2(generate_synthetic_messages(n_events=30_000, seed=seed))
    placements = place_matched_to_orders(events, horizon_ns=5_000_000_000)

    py_cols, py_diag = compute_outcomes(events, placements, "python", cancel_model=model)
    cpp_cols, cpp_diag = compute_outcomes(events, placements, "cpp", cancel_model=model)

    for field in OUTCOME_FIELDS:
        np.testing.assert_array_equal(
            cpp_cols[field], py_cols[field], err_msg=f"{field}, model {model}, seed {seed}"
        )
    assert cpp_diag == py_diag
    assert (py_cols["status"] == int(Status.FILLED)).sum() > 0, "vacuous: nothing filled"
