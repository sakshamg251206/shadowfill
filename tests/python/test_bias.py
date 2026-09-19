"""The H1 measurement: ground truth against estimators fitted observationally."""

from __future__ import annotations

import numpy as np
import pytest

from shadowfill.bias import (
    block_bootstrap_errors,
    compare_fill_curves,
    fill_curve_ground_truth,
    fill_curves_observational,
)
from shadowfill.ground_truth import OUTCOME_FIELDS
from shadowfill.lifetimes import LIFETIME_DTYPE, ExitReason, extract_lifetimes
from shadowfill.placements import place_top_of_book_grid
from shadowfill.replay import Status, replay_reference
from shadowfill.synthetic import generate_synthetic_messages

SEC = 1_000_000_000
HORIZONS = np.array([SEC // 10, SEC, 10 * SEC], dtype=np.int64)


def outcomes(rows):
    """Build the column dict the runner produces, from (status, insert, first_fill)."""
    columns = {f: np.zeros(len(rows), dtype="int64") for f in OUTCOME_FIELDS}
    columns["shadow_id"] = np.arange(len(rows), dtype="int64")
    for i, (status, insert_ts, first_fill_ts) in enumerate(rows):
        columns["status"][i] = int(status)
        columns["insert_ts"][i] = insert_ts
        columns["first_fill_ts"][i] = first_fill_ts
    return columns


def lives(rows):
    """Build lifetimes from (price, best_bid, arrival, first_fill, exit, reason)."""
    out = np.zeros(len(rows), dtype=LIFETIME_DTYPE)
    for i, (price, best_bid, arrival, first_fill, exit_ts, reason) in enumerate(rows):
        out[i]["order_id"] = i + 1
        out[i]["side"] = 1
        out[i]["price"] = price
        out[i]["size"] = 100
        out[i]["arrival_ts"] = arrival
        out[i]["best_bid_at_arrival"] = best_bid
        out[i]["best_ask_at_arrival"] = best_bid + 100
        out[i]["first_fill_ts"] = first_fill
        out[i]["exit_ts"] = exit_ts
        out[i]["exit_reason"] = int(reason)
    return out


def test_ground_truth_curve_counts_a_partial_fill_as_a_fill():
    """The event of interest is "did this order trade at all", on both sides.

    A shadow that traded 30 of 100 shares did get filled; requiring the full
    size would make the ground truth answer a different question from the one
    the observational estimator answers, and the gap would be meaningless.
    """
    columns = outcomes(
        [
            (Status.EXPIRED, 0, 500_000_000),  # partial fill, then the horizon passed
            (Status.EXPIRED, 0, -1),  # never traded
        ]
    )
    curve = fill_curve_ground_truth(
        columns, horizon_ns=10 * SEC, end_ts=60 * SEC, horizons=HORIZONS
    )
    np.testing.assert_allclose(curve, [0.0, 0.5, 0.5])


def test_never_activated_placements_stay_out_of_the_denominator():
    """They never existed. Counting them would deflate every fill rate."""
    columns = outcomes(
        [(Status.FILLED, 0, SEC), (Status.NOT_ACTIVATED, 0, -1), (Status.NOT_ACTIVATED, 0, -1)]
    )
    curve = fill_curve_ground_truth(
        columns, horizon_ns=10 * SEC, end_ts=60 * SEC, horizons=HORIZONS
    )
    assert curve[1] == 1.0


def test_a_truncated_shadow_leaves_the_risk_set_when_the_recording_stops():
    """Truncation is censoring: informative while live, silent afterwards.

    Three shadows: one fills at 1s, one fills at 3s, one is still resting when
    the stream ends. Where the stream ends decides whether that third order was
    at risk at 3s, and the estimate at 5s moves accordingly. Counting it as a
    non-fill instead would understate the truth, which is the error this
    project exists to avoid making in the other direction.
    """
    horizons = np.array([5 * SEC], dtype=np.int64)
    rows = [(Status.FILLED, 0, SEC), (Status.FILLED, 0, 3 * SEC), (Status.TRUNCATED, 0, -1)]

    stops_early = fill_curve_ground_truth(
        outcomes(rows), horizon_ns=10 * SEC, end_ts=2 * SEC, horizons=horizons
    )
    # Censored at 2s, so not at risk at 3s: 1 - (2/3 * 0/1) = 1.0
    assert stops_early[0] == 1.0

    runs_on = fill_curve_ground_truth(
        outcomes(rows), horizon_ns=10 * SEC, end_ts=20 * SEC, horizons=horizons
    )
    # Still at risk at 3s and did not fill: 1 - (2/3 * 1/2) = 2/3
    np.testing.assert_allclose(runs_on[0], 2 / 3)


def test_observational_population_can_be_restricted_to_the_touch():
    """Shadows are placed at the best price, so deeper real orders are not comparable.

    Mixing depths would let a depth effect masquerade as the cancellation bias.
    """
    table = lives(
        [
            (1_000, 1_000, 0, SEC, SEC, ExitReason.FILLED),  # at the touch
            (900, 1_000, 0, -1, SEC, ExitReason.CANCELLED),  # one tick behind
        ]
    )
    naive, cif, n = fill_curves_observational(table, horizons=HORIZONS, at_touch_only=True)
    assert n == 1
    assert naive[1] == 1.0 and cif[1] == 1.0

    _, _, n_all = fill_curves_observational(table, horizons=HORIZONS, at_touch_only=False)
    assert n_all == 2


def test_the_naive_estimator_never_reads_below_the_competing_risks_one():
    """Its bias has a known sign; a violation means the arithmetic is wrong."""
    events = generate_synthetic_messages(n_events=30_000, seed=21)
    naive, cif, n = fill_curves_observational(
        extract_lifetimes(events), horizons=HORIZONS, at_touch_only=False
    )
    assert n > 1_000
    assert np.all(naive >= cif - 1e-12)


def test_comparison_runs_end_to_end_on_a_synthetic_stream():
    events = generate_synthetic_messages(n_events=30_000, seed=13)
    placements = place_top_of_book_grid(
        events, grid_ns=20_000_000, size=5, horizon_ns=10 * SEC, latency_ns=0
    )
    settled, _ = replay_reference(events, placements)
    columns = {f: np.array([getattr(o, f) for o in settled], dtype="int64") for f in OUTCOME_FIELDS}

    result = compare_fill_curves(
        columns,
        extract_lifetimes(events),
        horizon_ns=10 * SEC,
        end_ts=int(events["ts_ns"][-1]),
        horizons=HORIZONS,
    )
    assert result.n_shadows > 0 and result.n_orders > 0
    for curve in (result.ground_truth, result.naive_km, result.competing_risks):
        assert curve.shape == HORIZONS.shape
        assert np.all((curve >= 0) & (curve <= 1))
        assert np.all(np.diff(curve) >= -1e-12), "a fill curve cannot decrease with horizon"
    np.testing.assert_allclose(result.naive_error, result.naive_km - result.ground_truth)


def _synthetic_pair(seed, n_events=30_000, horizon=10 * SEC):
    events = generate_synthetic_messages(n_events=n_events, seed=seed)
    placements = place_top_of_book_grid(
        events, grid_ns=20_000_000, size=5, horizon_ns=horizon, latency_ns=0
    )
    settled, _ = replay_reference(events, placements)
    columns = {f: np.array([getattr(o, f) for o in settled], dtype="int64") for f in OUTCOME_FIELDS}
    return events, columns, extract_lifetimes(events)


def test_bootstrap_interval_contains_the_point_estimate():
    events, columns, table = _synthetic_pair(17)
    end_ts = int(events["ts_ns"][-1])
    kwargs = dict(horizon_ns=10 * SEC, end_ts=end_ts, horizons=HORIZONS, at_touch_only=False)

    point = compare_fill_curves(columns, table, **kwargs)
    ci = block_bootstrap_errors(
        columns, table, **kwargs, block_ns=end_ts // 20, n_replicates=60, seed=1
    )
    assert int(ci["n_blocks"]) >= 20
    assert np.all(ci["naive_error_lo"] <= point.naive_error + 1e-9)
    assert np.all(ci["naive_error_hi"] >= point.naive_error - 1e-9)


def test_bootstrap_is_reproducible_from_its_seed():
    """Invariant: a reported interval must be regenerable from the manifest."""
    events, columns, table = _synthetic_pair(19)
    kwargs = dict(
        horizon_ns=10 * SEC,
        end_ts=int(events["ts_ns"][-1]),
        horizons=HORIZONS,
        at_touch_only=False,
        block_ns=int(events["ts_ns"][-1]) // 20,
        n_replicates=40,
    )
    first = block_bootstrap_errors(columns, table, **kwargs, seed=7)
    second = block_bootstrap_errors(columns, table, **kwargs, seed=7)
    third = block_bootstrap_errors(columns, table, **kwargs, seed=8)
    np.testing.assert_array_equal(first["naive_error_lo"], second["naive_error_lo"])
    assert not np.array_equal(first["naive_error_lo"], third["naive_error_lo"])


def test_too_few_blocks_is_refused_rather_than_silently_narrow():
    """One block resampled with replacement is the same block every time.

    It would return a zero-width interval that looks like enormous precision.
    """
    events, columns, table = _synthetic_pair(23)
    with pytest.raises(ValueError, match="too few"):
        block_bootstrap_errors(
            columns,
            table,
            horizon_ns=10 * SEC,
            end_ts=int(events["ts_ns"][-1]),
            horizons=HORIZONS,
            block_ns=10**18,
        )


def test_matched_shadows_see_exactly_the_queue_their_real_order_saw():
    """The property that makes the matched comparison mean anything.

    If a shadow's queue-ahead differed from its order's by even its own size,
    the measured difference would be part bookkeeping. One nanosecond of offset
    is what buys this: the shadow activates on the arriving ADD event, before
    the book applies it.
    """
    from shadowfill.lifetimes import extract_lifetimes
    from shadowfill.placements import place_matched_to_orders

    events = generate_synthetic_messages(n_events=20_000, seed=41)
    placements = place_matched_to_orders(events, horizon_ns=10 * SEC)
    table = extract_lifetimes(events)
    assert len(placements) == len(table)

    settled, _ = replay_reference(events, placements)
    by_id = {o.shadow_id: o for o in settled}
    compared = 0
    for i, row in enumerate(table):
        outcome = by_id[i]
        if outcome.status == int(Status.NOT_ACTIVATED):
            continue
        assert outcome.ahead_at_insert == int(row["ahead_at_arrival"]), f"order {i}"
        compared += 1
    assert compared > len(table) * 0.9
