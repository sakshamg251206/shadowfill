"""H3: markouts and expected passive edge."""

from __future__ import annotations

import numpy as np

from shadowfill.events import EventType, Side, empty_events
from shadowfill.markout import markout_bps, mid_at, top_of_book_series
from shadowfill.replay import top_of_book_series as oracle_top_of_book
from shadowfill.synthetic import generate_synthetic_messages

SEC = 1_000_000_000


def stream(rows):
    events = empty_events(len(rows))
    for i, (ts, oid, price, size, etype, side) in enumerate(rows):
        events[i] = (ts, i, oid, price, size, int(etype), int(side))
    return events


ADD = int(EventType.ADD)
DELETE = int(EventType.DELETE)
BID, ASK = int(Side.BID), int(Side.ASK)


def test_fast_top_of_book_matches_the_oracle():
    """A second implementation of the book's touch is only safe if it agrees.

    replay.top_of_book_series scans the level map linearly, which is the
    oracle's deliberate choice. This one keeps a heap because the touch is read
    after every event. Same answers, or the faster one is not usable.
    """
    events = generate_synthetic_messages(n_events=30_000, seed=61)
    fast = top_of_book_series(events)
    slow = oracle_top_of_book(events)
    np.testing.assert_array_equal(fast["best_bid"], slow["best_bid"])
    np.testing.assert_array_equal(fast["best_ask"], slow["best_ask"])
    np.testing.assert_array_equal(fast["ts_ns"], slow["ts_ns"])


def test_mid_is_a_step_function_of_the_last_event_at_or_before_the_query():
    events = stream(
        [
            (1 * SEC, 1, 100, 10, ADD, BID),
            (1 * SEC, 2, 110, 10, ADD, ASK),
            (5 * SEC, 3, 104, 10, ADD, BID),
        ]
    )
    tob = top_of_book_series(events)
    got = mid_at(tob, np.array([1 * SEC, 3 * SEC, 5 * SEC, 9 * SEC]))
    np.testing.assert_allclose(got, [105.0, 105.0, 107.0, 107.0])


def test_no_mid_is_invented_for_a_one_sided_book():
    """A fabricated mid would put a made-up price straight into a PnL number."""
    events = stream([(1 * SEC, 1, 100, 10, ADD, BID)])
    tob = top_of_book_series(events)
    assert np.isnan(mid_at(tob, np.array([2 * SEC]))[0])


def test_a_query_before_the_first_event_has_no_mid():
    events = stream([(5 * SEC, 1, 100, 10, ADD, BID), (5 * SEC, 2, 110, 10, ADD, ASK)])
    tob = top_of_book_series(events)
    assert np.isnan(mid_at(tob, np.array([1 * SEC]))[0])


def test_a_resting_bid_that_fills_before_the_mid_rises_made_money():
    events = stream(
        [
            (1 * SEC, 1, 1_000_000, 10, ADD, BID),
            (1 * SEC, 2, 1_000_200, 10, ADD, ASK),
            # The old levels must go, or the book crosses and the "mid" is
            # arithmetic on two prices that never coexisted.
            (3 * SEC, 1, 1_000_000, 10, DELETE, BID),
            (3 * SEC, 2, 1_000_200, 10, DELETE, ASK),
            (3 * SEC, 3, 1_001_000, 10, ADD, BID),
            (3 * SEC, 4, 1_001_200, 10, ADD, ASK),
        ]
    )
    tob = top_of_book_series(events)
    got = markout_bps(np.array([1 * SEC]), np.array([1_000_000]), np.array([BID]), tob, 2 * SEC)
    # mid moves 1_000_100 -> 1_001_100; a buyer at 1_000_000 gained 1,100 ticks
    np.testing.assert_allclose(got, [1_100 / 1_000_000 * 10_000], rtol=1e-9)
    assert got[0] > 0


def test_the_sign_convention_is_symmetric_for_a_resting_offer():
    """Positive must mean profit to the passive side on both sides of the book."""
    up = stream(
        [
            (1 * SEC, 1, 1_000_000, 10, ADD, BID),
            (1 * SEC, 2, 1_000_200, 10, ADD, ASK),
            # The old levels must go, or the book crosses and the "mid" is
            # arithmetic on two prices that never coexisted.
            (3 * SEC, 1, 1_000_000, 10, DELETE, BID),
            (3 * SEC, 2, 1_000_200, 10, DELETE, ASK),
            (3 * SEC, 3, 1_001_000, 10, ADD, BID),
            (3 * SEC, 4, 1_001_200, 10, ADD, ASK),
        ]
    )
    tob = top_of_book_series(up)
    sold = markout_bps(np.array([1 * SEC]), np.array([1_000_200]), np.array([ASK]), tob, 2 * SEC)
    # The same rise that helped the buyer hurt the seller.
    assert sold[0] < 0


def test_markout_is_undefined_rather_than_zero_when_the_future_has_no_mid():
    events = stream([(1 * SEC, 1, 1_000_000, 10, ADD, BID)])
    tob = top_of_book_series(events)
    got = markout_bps(np.array([1 * SEC]), np.array([1_000_000]), np.array([BID]), tob, 2 * SEC)
    assert np.isnan(got[0])
