import numpy as np

from shadowfill.events import EVENT_DTYPE, EventType, Side
from shadowfill.replay import Placement, Status, replay_reference
from shadowfill.tradethrough import credited_first_fill, first_worse_execution

SEC = 1_000_000_000


def make_events(rows):
    ev = np.zeros(len(rows), dtype=EVENT_DTYPE)
    for i, (ts, oid, price, size, etype, side) in enumerate(rows):
        ev[i] = (ts, i, oid, price, size, etype, side)
    return ev


def query(events, side, price, after, until):
    return first_worse_execution(
        events,
        side=np.array([side], dtype="int8"),
        price=np.array([price], dtype="int64"),
        after_ts=np.array([after], dtype="int64"),
        until_ts=np.array([until], dtype="int64"),
    )[0]


# The scenario pinned by the strict xfail in test_shadow_tracker.py: the only
# order at 100 is deleted, and a sell then executes against a bid at 99.
XFAIL_SCENARIO = [
    (0, 1, 100, 30, EventType.ADD, Side.BID),
    (1 * SEC, 2, 99, 50, EventType.ADD, Side.BID),
    (2 * SEC, 1, 100, 30, EventType.DELETE, Side.BID),
    (3 * SEC, 2, 99, 20, EventType.EXECUTE, Side.BID),
]


def test_finds_the_trade_through_the_engine_misses():
    events = make_events(XFAIL_SCENARIO)
    assert query(events, Side.BID, 100, after=0, until=10 * SEC) == 3 * SEC


def test_hidden_execution_at_a_worse_price_is_a_trade_through():
    # The aggressor passed 100 to reach a hidden bid at 99.
    events = make_events(
        [
            (0, 1, 100, 30, EventType.ADD, Side.BID),
            (1 * SEC, 0, 99, 20, EventType.EXECUTE_HIDDEN, Side.BID),
        ]
    )
    assert query(events, Side.BID, 100, after=0, until=10 * SEC) == 1 * SEC


def test_same_or_better_price_and_other_side_are_not_trade_throughs():
    events = make_events(
        [
            (1 * SEC, 1, 100, 10, EventType.EXECUTE, Side.BID),  # own price: engine's job
            (2 * SEC, 2, 101, 10, EventType.EXECUTE, Side.BID),  # better price
            (3 * SEC, 3, 99, 10, EventType.EXECUTE, Side.ASK),  # other side
            (4 * SEC, 4, 99, 10, EventType.DELETE, Side.BID),  # not an execution
        ]
    )
    assert query(events, Side.BID, 100, after=0, until=10 * SEC) == -1


def test_ask_side_is_mirrored():
    # A buy lifting asks at 101 passed a shadow ask at 100; one at 99 did not.
    events = make_events(
        [
            (1 * SEC, 1, 99, 10, EventType.EXECUTE, Side.ASK),
            (2 * SEC, 2, 101, 10, EventType.EXECUTE, Side.ASK),
        ]
    )
    assert query(events, Side.ASK, 100, after=0, until=10 * SEC) == 2 * SEC


def test_window_is_open_at_the_start_and_closed_at_the_end():
    # Activation is strict (ts > insert), expiry is not (ts <= insert + horizon).
    events = make_events(
        [
            (1 * SEC, 1, 99, 10, EventType.EXECUTE, Side.BID),
            (5 * SEC, 2, 99, 10, EventType.EXECUTE, Side.BID),
            (6 * SEC, 3, 99, 10, EventType.EXECUTE, Side.BID),
        ]
    )
    assert query(events, Side.BID, 100, after=1 * SEC, until=5 * SEC) == 5 * SEC
    assert query(events, Side.BID, 100, after=1 * SEC, until=5 * SEC - 1) == -1


def test_vectorised_search_matches_a_brute_force_loop():
    rng = np.random.default_rng(7)
    n = 3_000
    types = rng.choice(
        [EventType.ADD, EventType.DELETE, EventType.EXECUTE, EventType.EXECUTE_HIDDEN], size=n
    )
    rows = [
        (
            int(t),
            i,
            int(rng.integers(95, 106)),
            10,
            int(types[i]),
            int(rng.choice([Side.BID, Side.ASK])),
        )
        for i, t in enumerate(np.sort(rng.integers(0, 10_000, size=n)))
    ]
    events = make_events(rows)
    m = 500
    side = rng.choice([Side.BID, Side.ASK], size=m).astype("int8")
    price = rng.integers(95, 106, size=m).astype("int64")
    after = rng.integers(0, 10_000, size=m).astype("int64")
    until = after + rng.integers(0, 3_000, size=m)

    got = first_worse_execution(events, side=side, price=price, after_ts=after, until_ts=until)

    is_exec = np.isin(events["type"], [EventType.EXECUTE, EventType.EXECUTE_HIDDEN])
    for k in range(m):
        worse = events["price"] < price[k] if side[k] == Side.BID else events["price"] > price[k]
        hit = (
            is_exec
            & (events["side"] == side[k])
            & worse
            & (events["ts_ns"] > after[k])
            & (events["ts_ns"] <= until[k])
        )
        want = int(events["ts_ns"][hit][0]) if hit.any() else -1
        assert got[k] == want, k


def _columns(outcomes):
    fields = ("status", "insert_ts", "first_fill_ts", "ahead_lt_1_ts")
    return {f: np.array([getattr(o, f) for o in outcomes], dtype="int64") for f in fields}


def test_credit_on_the_xfail_scenario_moves_the_shadow_to_filled_in_both_bounds():
    # Outcome columns as an engine that misses trade-throughs reports them:
    # activated at 0, front of its level from 2 s (the delete), never filled.
    events = make_events(XFAIL_SCENARIO)
    placement = Placement(0, 0, 0, int(Side.BID), 100, 10, 10 * SEC)
    cols = {
        "status": np.array([int(Status.EXPIRED)]),
        "insert_ts": np.array([0]),
        "first_fill_ts": np.array([-1]),
        "ahead_lt_1_ts": np.array([2 * SEC]),
    }
    for require_front in (False, True):
        credited = credited_first_fill(events, [placement], cols, require_front=require_front)
        assert credited[0] == 3 * SEC


def test_nothing_is_left_to_credit_once_the_engine_fills_trade_throughs():
    # The engine now fills this shadow at 3 s itself, so the measurement's
    # window closes before then and the lower bound credits nothing new.
    events = make_events(XFAIL_SCENARIO)
    placement = Placement(0, 0, 0, int(Side.BID), 100, 10, 10 * SEC)
    outcomes, _ = replay_reference(events, [placement])
    cols = _columns(outcomes)
    assert cols["first_fill_ts"][0] == 3 * SEC
    credited = credited_first_fill(events, [placement], cols, require_front=True)
    assert credited[0] == 3 * SEC


def test_lower_bound_ignores_a_trade_through_while_real_orders_are_still_ahead():
    # The bid at 100 is still resting ahead of the shadow when the sell hits 99,
    # so the book itself was traded through. Only the upper bound credits it.
    events = make_events(
        [
            (0, 1, 100, 30, EventType.ADD, Side.BID),
            (1 * SEC, 2, 99, 50, EventType.ADD, Side.BID),
            (2 * SEC, 2, 99, 20, EventType.EXECUTE, Side.BID),
        ]
    )
    placement = Placement(0, 0, 0, int(Side.BID), 100, 10, 10 * SEC)
    outcomes, _ = replay_reference(events, [placement])
    cols = _columns(outcomes)

    upper = credited_first_fill(events, [placement], cols, require_front=False)
    lower = credited_first_fill(events, [placement], cols, require_front=True)
    assert upper[0] == 2 * SEC
    assert lower[0] == -1


def test_credit_never_delays_a_real_fill_and_ignores_unactivated_shadows():
    events = make_events(
        [
            (0, 1, 100, 10, EventType.ADD, Side.BID),
            (1 * SEC, 1, 100, 20, EventType.EXECUTE, Side.BID),  # fills the shadow
            (2 * SEC, 2, 99, 20, EventType.EXECUTE, Side.BID),  # later trade-through
        ]
    )
    placements = [
        Placement(0, 0, 0, int(Side.BID), 100, 10, 10 * SEC),
        Placement(1, 5 * SEC, 0, int(Side.BID), 100, 10, 10 * SEC),  # after the data
    ]
    outcomes, _ = replay_reference(events, placements)
    cols = _columns(outcomes)
    assert cols["status"][1] == int(Status.NOT_ACTIVATED)

    credited = credited_first_fill(events, placements, cols, require_front=False)
    assert credited[0] == 1 * SEC
    assert credited[1] == -1
