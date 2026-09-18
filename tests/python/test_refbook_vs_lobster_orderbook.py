import numpy as np
import pytest

from shadowfill.lobster import load_lobster_messages, load_lobster_orderbook
from shadowfill.replay import top_of_book_series

SENTINEL = 9_999_999_999


@pytest.mark.needs_lobster
def test_reconstructed_top_of_book_matches_lobster_snapshots(lobster_pair):
    message_path, orderbook_path, levels = lobster_pair
    events = load_lobster_messages(message_path)
    snapshots = load_lobster_orderbook(orderbook_path, levels)
    assert len(events) == len(snapshots)

    tob = top_of_book_series(events)

    expected_ask = snapshots["ask_price_1"].to_numpy()
    expected_bid = snapshots["bid_price_1"].to_numpy()
    in_band = (expected_ask != SENTINEL) & (expected_bid != -SENTINEL)
    in_band &= (tob["best_ask"] != 0) & (tob["best_bid"] != 0)

    assert in_band.mean() > 0.95, "level band excludes too much of the session"
    np.testing.assert_array_equal(tob["best_ask"][in_band], expected_ask[in_band])
    np.testing.assert_array_equal(tob["best_bid"][in_band], expected_bid[in_band])


@pytest.mark.needs_lobster
def test_unknown_order_rate_is_small(lobster_pair):
    from shadowfill.replay import RefBook

    message_path, _, _ = lobster_pair
    events = load_lobster_messages(message_path)
    book = RefBook()
    for e in events:
        book.apply(
            int(e["ts_ns"]),
            int(e["seq"]),
            int(e["order_id"]),
            int(e["price"]),
            int(e["size"]),
            int(e["type"]),
            int(e["side"]),
        )
    assert book.unknown_order_events / len(events) < 0.05
