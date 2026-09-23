"""H4: the level-2 ablation."""

from __future__ import annotations

import numpy as np
import pytest

from shadowfill.bias import fill_cdf, ground_truth_arrays
from shadowfill.events import EventType
from shadowfill.ground_truth import compute_outcomes
from shadowfill.l2 import ablate_to_l2
from shadowfill.placements import place_matched_to_orders
from shadowfill.synthetic import generate_synthetic_messages

SEC = 1_000_000_000
HORIZONS = np.array([SEC // 10, SEC, 5 * SEC], dtype=np.int64)


def test_only_cancel_identity_is_removed():
    """An L2 feed loses whose cancel it was; everything else is unchanged.

    Execution ids stay because price-time priority tells an L2 simulator that a
    trade took the front of the queue -- that much needs no identity.
    """
    events = generate_synthetic_messages(n_events=5_000, seed=51)
    ablated = ablate_to_l2(events)

    for field in ("ts_ns", "seq", "price", "size", "type", "side"):
        np.testing.assert_array_equal(ablated[field], events[field])

    anonymised = np.isin(events["type"], [int(EventType.CANCEL_PARTIAL), int(EventType.DELETE)])
    assert anonymised.sum() > 0
    assert np.all(ablated["order_id"][anonymised] == 0)
    np.testing.assert_array_equal(ablated["order_id"][~anonymised], events["order_id"][~anonymised])


def test_the_input_is_not_mutated():
    events = generate_synthetic_messages(n_events=2_000, seed=52)
    before = events.copy()
    ablate_to_l2(events)
    np.testing.assert_array_equal(events, before)


def test_cancel_from_front_can_never_underestimate_fills():
    """The heuristic assumes every cancel was ahead of you, so your queue can
    only shrink faster than it really did. The L2 fill curve must therefore sit
    at or above the truth at every horizon -- which is what makes the measured
    error a one-sided bound rather than just a discrepancy.
    """
    pytest.importorskip("shadowfill._core")
    events = generate_synthetic_messages(n_events=60_000, seed=53)
    placements = place_matched_to_orders(events, horizon_ns=5 * SEC)

    truth_cols, _ = compute_outcomes(events, placements, "cpp")
    l2_cols, _ = compute_outcomes(ablate_to_l2(events), placements, "cpp")

    end_ts = int(events["ts_ns"][-1])
    t_dur, t_ev, _, _ = ground_truth_arrays(truth_cols, horizon_ns=5 * SEC, end_ts=end_ts)
    l_dur, l_ev, _, _ = ground_truth_arrays(l2_cols, horizon_ns=5 * SEC, end_ts=end_ts)

    truth = fill_cdf(t_dur, t_ev, HORIZONS)
    l2 = fill_cdf(l_dur, l_ev, HORIZONS)
    assert np.all(l2 >= truth - 1e-12), f"L2 below truth: {l2} vs {truth}"


def test_ablation_shows_up_in_the_engine_diagnostics():
    """Every anonymised cancel becomes an assumed-ahead event, and is counted."""
    pytest.importorskip("shadowfill._core")
    events = generate_synthetic_messages(n_events=20_000, seed=54)
    placements = place_matched_to_orders(events, horizon_ns=2 * SEC)

    _, truth_diag = compute_outcomes(events, placements, "cpp")
    _, l2_diag = compute_outcomes(ablate_to_l2(events), placements, "cpp")
    assert l2_diag["unknown_order_assumed_ahead"] > truth_diag["unknown_order_assumed_ahead"]


def test_run_reports_every_heuristic_ordered_optimistic_to_pessimistic(tmp_path):
    """All three L2 guesses against the same L3 truth, on the same blocks.

    The per-shadow ordering front >= proportional >= back is proven in
    test_cancel_models.py, so the aggregate fill curves -- and therefore the
    errors against the one shared truth -- must order the same way.
    """
    pytest.importorskip("shadowfill._core")
    from shadowfill.l2 import HEURISTICS, run_l2_ablation
    from shadowfill.synthetic import write_synthetic_csv

    events = generate_synthetic_messages(n_events=30_000, seed=12)
    data_path = tmp_path / "synthetic_message_10.csv"
    write_synthetic_csv(events, data_path)

    manifest = run_l2_ablation(
        message_path=data_path,
        out_dir=tmp_path / "out",
        horizon_ns=5 * SEC,
        horizons=np.array([SEC, 5 * SEC], dtype=np.int64),
        window_ns=None,
        block_ns=2 * SEC,
        n_replicates=30,
    )

    assert {r["heuristic"] for r in manifest["rows"]} == set(HEURISTICS)
    by = {(r["heuristic"], r["horizon_ns"]): r for r in manifest["rows"]}
    for h in (SEC, 5 * SEC):
        front, prop, back = (by[(name, h)]["l2_error"] for name in HEURISTICS)
        assert front >= prop >= back, (h, front, prop, back)
        truths = {by[(name, h)]["l3_truth"] for name in HEURISTICS}
        assert len(truths) == 1, "every heuristic must be measured against one truth"
    assert manifest["config"]["heuristics"] == list(HEURISTICS)
