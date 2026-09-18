import numpy as np

from shadowfill.events import EVENT_DTYPE, EventType, Side
from shadowfill.replay import Placement, Status, run_reference

SEC = 1_000_000_000


def make_events(rows):
    ev = np.zeros(len(rows), dtype=EVENT_DTYPE)
    for i, (ts, oid, price, size, etype, side) in enumerate(rows):
        ev[i] = (ts, i, oid, price, size, etype, side)
    return ev


def place(price=100, size=10, ts=0, latency=0, horizon=10 * SEC, side=Side.BID):
    return Placement(
        shadow_id=0,
        ts_ns=ts,
        latency_ns=latency,
        side=int(side),
        price=price,
        size=size,
        horizon_ns=horizon,
    )


def test_ahead_at_insert_is_resting_volume_at_that_price():
    events = make_events(
        [
            (0, 1, 100, 30, EventType.ADD, Side.BID),
            (1 * SEC, 2, 100, 20, EventType.ADD, Side.BID),
            (2 * SEC, 3, 100, 5, EventType.ADD, Side.BID),
        ]
    )
    out = run_reference(events, [place(ts=1 * SEC + 1)])[0]
    assert out.ahead_at_insert == 50
    assert out.insert_seq == 2


def test_orders_added_after_insertion_are_behind_and_never_count():
    events = make_events(
        [
            (0, 1, 100, 30, EventType.ADD, Side.BID),
            (1 * SEC, 2, 100, 999, EventType.ADD, Side.BID),
            (2 * SEC, 1, 100, 30, EventType.EXECUTE, Side.BID),
            (3 * SEC, 3, 100, 10, EventType.EXECUTE, Side.BID),
        ]
    )
    out = run_reference(events, [place(ts=0, size=10)])[0]
    assert out.ahead_at_insert == 30
    assert out.status == Status.FILLED
    assert out.first_fill_ts == 3 * SEC


def test_order_added_at_exactly_the_placement_timestamp_is_ahead():
    events = make_events(
        [
            (0, 1, 100, 30, EventType.ADD, Side.BID),
            (1 * SEC, 2, 100, 40, EventType.EXECUTE, Side.BID),
        ]
    )
    out = run_reference(events, [place(ts=0, size=10)])[0]
    assert out.ahead_at_insert == 30


def test_cancellation_ahead_reduces_queue_but_behind_does_not():
    events = make_events(
        [
            (0, 1, 100, 30, EventType.ADD, Side.BID),
            (1 * SEC, 2, 100, 40, EventType.ADD, Side.BID),
            (2 * SEC, 2, 100, 40, EventType.DELETE, Side.BID),
            (3 * SEC, 1, 100, 25, EventType.CANCEL_PARTIAL, Side.BID),
        ]
    )
    out = run_reference(events, [place(ts=500_000_000, size=10)])[0]
    assert out.ahead_at_insert == 30
    assert out.ahead_at_end == 5


def test_hidden_execution_never_consumes_the_queue():
    events = make_events(
        [
            (0, 1, 100, 10, EventType.ADD, Side.BID),
            (1 * SEC, 0, 100, 10, EventType.EXECUTE_HIDDEN, Side.BID),
            (2 * SEC, 0, 100, 10, EventType.EXECUTE_HIDDEN, Side.BID),
        ]
    )
    out = run_reference(events, [place(ts=0, size=10, horizon=1 * SEC)])[0]
    assert out.ahead_at_end == 10
    assert out.status == Status.EXPIRED


def test_partial_then_full_fill_records_both_timestamps():
    events = make_events(
        [
            (0, 1, 100, 5, EventType.ADD, Side.BID),
            (1 * SEC, 1, 100, 5, EventType.EXECUTE, Side.BID),
            (2 * SEC, 2, 100, 3, EventType.EXECUTE, Side.BID),
            (3 * SEC, 3, 100, 7, EventType.EXECUTE, Side.BID),
        ]
    )
    out = run_reference(events, [place(ts=0, size=10)])[0]
    assert out.first_fill_ts == 2 * SEC
    assert out.full_fill_ts == 3 * SEC
    assert out.filled_qty == 10
    assert out.status == Status.FILLED


def test_single_sweep_larger_than_queue_fills_the_shadow_immediately():
    events = make_events(
        [
            (0, 1, 100, 20, EventType.ADD, Side.BID),
            (1 * SEC, 1, 100, 100, EventType.EXECUTE, Side.BID),
        ]
    )
    out = run_reference(events, [place(ts=0, size=10)])[0]
    assert out.status == Status.FILLED
    assert out.first_fill_ts == out.full_fill_ts == 1 * SEC


def test_latency_puts_intervening_orders_ahead_of_the_shadow():
    events = make_events(
        [
            (0, 1, 100, 10, EventType.ADD, Side.BID),
            (1 * SEC, 2, 100, 40, EventType.ADD, Side.BID),
            (3 * SEC, 1, 100, 10, EventType.EXECUTE, Side.BID),
        ]
    )
    zero_latency = run_reference(events, [place(ts=0, latency=0)])[0]
    with_latency = run_reference(events, [place(ts=0, latency=2 * SEC)])[0]
    assert zero_latency.ahead_at_insert == 10
    assert with_latency.ahead_at_insert == 50


def test_horizon_expiry_marks_expired_not_filled():
    events = make_events(
        [
            (0, 1, 100, 10, EventType.ADD, Side.BID),
            (5 * SEC, 1, 100, 10, EventType.EXECUTE, Side.BID),
        ]
    )
    out = run_reference(events, [place(ts=0, horizon=2 * SEC)])[0]
    assert out.status == Status.EXPIRED
    assert out.first_fill_ts == -1


def test_still_resting_at_end_of_data_is_not_activated():
    events = make_events([(0, 1, 100, 10, EventType.ADD, Side.BID)])
    out = run_reference(events, [place(ts=0, horizon=10 * SEC)])[0]
    assert out.status == Status.NOT_ACTIVATED


def test_activated_and_still_resting_at_end_of_data_is_truncated():
    events = make_events(
        [
            (0, 1, 100, 10, EventType.ADD, Side.BID),
            (1 * SEC, 2, 100, 10, EventType.ADD, Side.BID),
        ]
    )
    out = run_reference(events, [place(ts=0, horizon=10 * SEC)])[0]
    assert out.status == Status.TRUNCATED
    assert out.ahead_at_insert == 10


def test_events_at_other_prices_and_sides_are_ignored():
    events = make_events(
        [
            (0, 1, 100, 10, EventType.ADD, Side.BID),
            (1 * SEC, 2, 101, 10, EventType.EXECUTE, Side.BID),
            (2 * SEC, 3, 100, 10, EventType.EXECUTE, Side.ASK),
        ]
    )
    out = run_reference(events, [place(ts=0, price=100, side=Side.BID)])[0]
    assert out.ahead_at_end == 10


def test_assumed_ahead_events_counts_unknown_id_cancels_that_moved_the_queue():
    events = make_events(
        [
            (0, 1, 100, 30, EventType.ADD, Side.BID),
            (1 * SEC, 2, 100, 10, EventType.ADD, Side.BID),
            (2 * SEC, 777, 100, 5, EventType.DELETE, Side.BID),
            (3 * SEC, 1, 100, 30, EventType.CANCEL_PARTIAL, Side.BID),
        ]
    )
    out = run_reference(events, [place(ts=0, size=10)])[0]
    assert out.assumed_ahead_events == 1
    assert out.ahead_at_end == 0


def test_assumed_ahead_events_is_zero_when_every_id_is_known():
    events = make_events(
        [
            (0, 1, 100, 30, EventType.ADD, Side.BID),
            (1 * SEC, 2, 100, 10, EventType.ADD, Side.BID),
            (2 * SEC, 1, 100, 30, EventType.DELETE, Side.BID),
        ]
    )
    out = run_reference(events, [place(ts=0, size=10)])[0]
    assert out.assumed_ahead_events == 0
