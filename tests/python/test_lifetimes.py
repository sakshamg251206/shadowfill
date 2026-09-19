"""Real-order lifetimes: the observational population every estimator is fitted on.

These orders are what the literature actually has. Their cancellations are
endogenous, which is exactly the bias ShadowFill measures -- so the one thing
that must be exact here is that a real order and a shadow order placed in the
same book state are described by the same numbers. Otherwise H1's gap measures
our own bookkeeping instead of the bias.
"""

from __future__ import annotations

import numpy as np

from shadowfill.events import EventType, Side, empty_events
from shadowfill.lifetimes import LIFETIME_DTYPE, ExitReason, extract_lifetimes
from shadowfill.replay import Placement, replay_reference
from shadowfill.synthetic import generate_synthetic_messages

SEC = 1_000_000_000


def stream(rows):
    """Build a canonical event array from (ts, oid, price, size, type, side) tuples."""
    events = empty_events(len(rows))
    for i, (ts, oid, price, size, etype, side) in enumerate(rows):
        events[i] = (ts, i, oid, price, size, int(etype), int(side))
    return events


BID = int(Side.BID)
ASK = int(Side.ASK)
ADD = int(EventType.ADD)
EXEC = int(EventType.EXECUTE)
DELETE = int(EventType.DELETE)
CANCEL = int(EventType.CANCEL_PARTIAL)


def test_fully_executed_order_is_filled(tmp_path=None):
    lives = extract_lifetimes(
        stream(
            [
                (1 * SEC, 10, 100, 50, ADD, BID),
                (2 * SEC, 10, 100, 20, EXEC, BID),
                (3 * SEC, 10, 100, 30, EXEC, BID),
            ]
        )
    )
    assert lives.dtype == LIFETIME_DTYPE
    assert len(lives) == 1
    assert int(lives[0]["exit_reason"]) == int(ExitReason.FILLED)
    assert int(lives[0]["first_fill_ts"]) == 2 * SEC
    assert int(lives[0]["exit_ts"]) == 3 * SEC
    assert int(lives[0]["filled_qty"]) == 50


def test_deleted_order_is_cancelled():
    lives = extract_lifetimes(
        stream([(1 * SEC, 10, 100, 50, ADD, BID), (4 * SEC, 10, 100, 50, DELETE, BID)])
    )
    assert int(lives[0]["exit_reason"]) == int(ExitReason.CANCELLED)
    assert int(lives[0]["exit_ts"]) == 4 * SEC
    assert int(lives[0]["first_fill_ts"]) == -1


def test_partial_fill_then_cancel_is_a_cancellation_that_remembers_the_fill():
    """The competing-risks case that makes the naive estimator wrong.

    Treating this as a clean censoring throws away the fact that the order was
    already trading when its owner pulled it.
    """
    lives = extract_lifetimes(
        stream(
            [
                (1 * SEC, 10, 100, 50, ADD, BID),
                (2 * SEC, 10, 100, 20, EXEC, BID),
                (5 * SEC, 10, 100, 30, DELETE, BID),
            ]
        )
    )
    assert int(lives[0]["exit_reason"]) == int(ExitReason.CANCELLED)
    assert int(lives[0]["first_fill_ts"]) == 2 * SEC
    assert int(lives[0]["filled_qty"]) == 20
    assert int(lives[0]["exit_ts"]) == 5 * SEC


def test_partial_cancel_does_not_end_the_order():
    lives = extract_lifetimes(
        stream(
            [
                (1 * SEC, 10, 100, 50, ADD, BID),
                (2 * SEC, 10, 100, 20, CANCEL, BID),
                (3 * SEC, 10, 100, 30, EXEC, BID),
            ]
        )
    )
    assert int(lives[0]["exit_reason"]) == int(ExitReason.FILLED)
    assert int(lives[0]["exit_ts"]) == 3 * SEC


def test_order_still_resting_at_the_end_is_censored():
    """Administrative censoring, which is the one kind that really is independent."""
    lives = extract_lifetimes(
        stream([(1 * SEC, 10, 100, 50, ADD, BID), (9 * SEC, 11, 100, 5, ADD, BID)])
    )
    by_id = {int(r["order_id"]): r for r in lives}
    assert int(by_id[10]["exit_reason"]) == int(ExitReason.CENSORED)
    assert int(by_id[10]["exit_ts"]) == 9 * SEC


def test_removals_of_orders_never_added_produce_no_lifetime():
    """A cancel for an order added before the window is not an observation."""
    lives = extract_lifetimes(stream([(1 * SEC, 77, 100, 50, DELETE, BID)]))
    assert len(lives) == 0


def test_queue_ahead_at_arrival_counts_only_the_same_side_and_price():
    lives = extract_lifetimes(
        stream(
            [
                (1 * SEC, 1, 100, 40, ADD, BID),
                (1 * SEC, 2, 99, 70, ADD, BID),  # different price
                (1 * SEC, 3, 100, 60, ADD, ASK),  # different side
                (2 * SEC, 10, 100, 50, ADD, BID),
            ]
        )
    )
    arriving = {int(r["order_id"]): r for r in lives}[10]
    assert int(arriving["ahead_at_arrival"]) == 40


def test_touch_is_recorded_at_arrival():
    lives = extract_lifetimes(
        stream(
            [
                (1 * SEC, 1, 100, 40, ADD, BID),
                (1 * SEC, 2, 105, 40, ADD, ASK),
                (2 * SEC, 10, 99, 50, ADD, BID),
            ]
        )
    )
    arriving = {int(r["order_id"]): r for r in lives}[10]
    assert int(arriving["best_bid_at_arrival"]) == 100
    assert int(arriving["best_ask_at_arrival"]) == 105


def test_touch_follows_the_book_as_levels_empty():
    """A stale best price would misclassify an order's depth, and depth drives fills."""
    lives = extract_lifetimes(
        stream(
            [
                (1 * SEC, 1, 100, 40, ADD, BID),
                (2 * SEC, 2, 98, 40, ADD, BID),
                (3 * SEC, 1, 100, 40, DELETE, BID),
                (4 * SEC, 10, 97, 50, ADD, BID),
            ]
        )
    )
    arriving = {int(r["order_id"]): r for r in lives}[10]
    assert int(arriving["best_bid_at_arrival"]) == 98


def test_ahead_at_arrival_equals_what_a_shadow_would_have_seen():
    """The load-bearing test: real and hypothetical orders measured identically.

    H1 compares an estimator fitted on real orders against the shadow ground
    truth. If "queue ahead" means something even slightly different on the two
    sides, the measured gap is partly our own inconsistency, and the headline
    number is not a bias at all.
    """
    events = stream(
        [
            (1 * SEC, 1, 100, 40, ADD, BID),
            (2 * SEC, 2, 100, 25, ADD, BID),
            (4 * SEC, 10, 100, 50, ADD, BID),
            (5 * SEC, 1, 100, 40, EXEC, BID),
        ]
    )
    lives = extract_lifetimes(events)
    real = {int(r["order_id"]): r for r in lives}[10]

    placement = Placement(
        shadow_id=1,
        ts_ns=3 * SEC,  # strictly between the previous event and the real arrival
        latency_ns=0,
        side=BID,
        price=100,
        size=50,
        horizon_ns=10 * SEC,
    )
    outcomes, _ = replay_reference(events, [placement])
    assert int(real["ahead_at_arrival"]) == outcomes[0].ahead_at_insert == 65


def test_lifetimes_use_no_information_from_after_the_exit():
    """Prefix invariance, invariant 4, restated for the observational extractor."""
    events = generate_synthetic_messages(n_events=20_000, seed=5)
    half = len(events) // 2
    cutoff = int(events[half]["ts_ns"])

    partial = {int(r["order_id"]): r for r in extract_lifetimes(events[:half])}
    full = {int(r["order_id"]): r for r in extract_lifetimes(events)}
    compared = 0
    for oid, row in partial.items():
        if int(row["exit_reason"]) == int(ExitReason.CENSORED):
            continue  # censoring is by construction a statement about the window
        assert int(row["exit_ts"]) < cutoff or oid in full
        assert row.tolist() == full[oid].tolist(), f"order {oid} changed"
        compared += 1
    assert compared > 100


def test_every_added_order_appears_exactly_once():
    events = generate_synthetic_messages(n_events=20_000, seed=8)
    lives = extract_lifetimes(events)
    added = events[events["type"] == int(EventType.ADD)]["order_id"]
    assert sorted(int(x) for x in lives["order_id"]) == sorted(int(x) for x in added)
    assert np.all(lives["exit_ts"] >= lives["arrival_ts"])
    assert np.all(lives["filled_qty"] <= lives["size"])
