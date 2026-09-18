import numpy as np
import pytest

from shadowfill.events import EventType, Side
from shadowfill.replay import Placement, RefBook, RefShadowTracker, Status, run_reference
from shadowfill.synthetic import generate_synthetic_messages

SEC = 1_000_000_000


def grid_placements(events, *, every=500, size=5, horizon=2 * SEC):
    """Place one shadow per side every ``every`` events at the prevailing best."""
    book = RefBook()
    placements = []
    sid = 0
    for i, e in enumerate(events):
        if i % every == 0:
            for side, price in ((Side.BID, book.best_bid()), (Side.ASK, book.best_ask())):
                if price is None:
                    continue
                placements.append(
                    Placement(
                        shadow_id=sid,
                        ts_ns=int(e["ts_ns"]),
                        latency_ns=0,
                        side=int(side),
                        price=price,
                        size=size,
                        horizon_ns=horizon,
                    )
                )
                sid += 1
        book.apply(
            int(e["ts_ns"]),
            int(e["seq"]),
            int(e["order_id"]),
            int(e["price"]),
            int(e["size"]),
            int(e["type"]),
            int(e["side"]),
        )
    return placements


@pytest.fixture(scope="module")
def stream():
    events = generate_synthetic_messages(n_events=50_000, seed=42)
    return events, grid_placements(events)


def test_every_placement_gets_exactly_one_terminal_outcome(stream):
    events, placements = stream
    outcomes = run_reference(events, placements)
    assert len(outcomes) == len(placements)
    assert {o.shadow_id for o in outcomes} == {p.shadow_id for p in placements}
    assert all(o.status != int(Status.RESTING) for o in outcomes)


def test_queue_ahead_is_monotone_non_increasing():
    events = generate_synthetic_messages(n_events=20_000, seed=11)
    placements = grid_placements(events, every=2000)
    tracker = RefShadowTracker(RefBook(), placements)
    seen: dict[int, int] = {}
    for e in events:
        tracker.on_event(
            int(e["ts_ns"]),
            int(e["seq"]),
            int(e["order_id"]),
            int(e["price"]),
            int(e["size"]),
            int(e["type"]),
            int(e["side"]),
        )
        for a in tracker._active:
            sid = a.outcome.shadow_id
            assert a.ahead <= seen.get(sid, a.ahead)
            assert a.ahead >= 0
            seen[sid] = a.ahead


def test_fill_requires_queue_to_have_been_fully_consumed(stream):
    events, placements = stream
    for outcome in run_reference(events, placements):
        if outcome.status == int(Status.FILLED):
            assert outcome.ahead_at_end == 0
            assert 0 <= outcome.first_fill_ts <= outcome.full_fill_ts


def test_filled_quantity_never_exceeds_placement_size(stream):
    events, placements = stream
    by_id = {p.shadow_id: p for p in placements}
    for outcome in run_reference(events, placements):
        assert 0 <= outcome.filled_qty <= by_id[outcome.shadow_id].size


def test_prefix_invariance_no_future_information_leaks(stream):
    """An outcome settled by event k must not change when later events are added."""
    events, placements = stream
    half = len(events) // 2
    cutoff_ts = int(events[half]["ts_ns"])

    early = [p for p in placements if p.ts_ns + p.horizon_ns < cutoff_ts]
    assert early, "fixture produced no placements that settle before the cutoff"

    partial = {o.shadow_id: o for o in run_reference(events[:half], early)}
    full = {o.shadow_id: o for o in run_reference(events, early)}
    for sid, outcome in partial.items():
        if outcome.status in (int(Status.FILLED), int(Status.EXPIRED)):
            assert outcome == full[sid], f"shadow {sid} changed when future events were added"


def test_replay_is_deterministic_across_runs(stream):
    events, placements = stream
    first = run_reference(events, placements)
    second = run_reference(events, placements)
    assert first == second


def test_longer_horizon_weakly_increases_fill_rate():
    events = generate_synthetic_messages(n_events=50_000, seed=3)
    base = grid_placements(events, every=500, horizon=1 * SEC)
    longer = [Placement(**{**p.__dict__, "horizon_ns": 8 * SEC}) for p in base]
    fills_short = sum(o.status == int(Status.FILLED) for o in run_reference(events, base))
    fills_long = sum(o.status == int(Status.FILLED) for o in run_reference(events, longer))
    assert fills_long >= fills_short


def test_larger_order_weakly_decreases_full_fill_rate():
    events = generate_synthetic_messages(n_events=50_000, seed=4)
    small = grid_placements(events, every=500, size=1)
    large = [Placement(**{**p.__dict__, "size": 200}) for p in small]
    assert sum(o.status == int(Status.FILLED) for o in run_reference(events, large)) <= sum(
        o.status == int(Status.FILLED) for o in run_reference(events, small)
    )


def test_hidden_executions_alone_never_fill_anything():
    events = generate_synthetic_messages(n_events=30_000, seed=9)
    placements = grid_placements(events, every=1000)
    baseline = run_reference(events, placements)

    hidden_only = events.copy()
    mask = np.isin(hidden_only["type"], [int(EventType.EXECUTE)])
    hidden_only["type"][mask] = int(EventType.EXECUTE_HIDDEN)
    muted = run_reference(hidden_only, placements)

    assert sum(o.status == int(Status.FILLED) for o in muted) == 0
    assert sum(o.status == int(Status.FILLED) for o in baseline) > 0
