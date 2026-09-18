from shadowfill.events import EventType, Side
from shadowfill.replay import RefBook


def add(book, seq, oid, price, size, side):
    book.apply(seq * 1000, seq, oid, price, size, EventType.ADD, side)


def test_add_accumulates_level_size():
    b = RefBook()
    add(b, 0, 1, 100, 10, Side.BID)
    add(b, 1, 2, 100, 5, Side.BID)
    assert b.level_size(Side.BID, 100) == 15
    assert b.best_bid() == 100


def test_delete_removes_order_and_level_when_empty():
    b = RefBook()
    add(b, 0, 1, 100, 10, Side.BID)
    b.apply(1000, 1, 1, 100, 10, EventType.DELETE, Side.BID)
    assert b.level_size(Side.BID, 100) == 0
    assert b.best_bid() is None
    assert b.find(1) is None


def test_partial_cancel_reduces_size_but_keeps_order():
    b = RefBook()
    add(b, 0, 1, 100, 10, Side.BID)
    b.apply(1000, 1, 1, 100, 4, EventType.CANCEL_PARTIAL, Side.BID)
    assert b.level_size(Side.BID, 100) == 6
    assert b.find(1).size == 6


def test_execute_consumes_resting_size():
    b = RefBook()
    add(b, 0, 1, 100, 10, Side.ASK)
    b.apply(1000, 1, 1, 100, 10, EventType.EXECUTE, Side.ASK)
    assert b.level_size(Side.ASK, 100) == 0
    assert b.best_ask() is None


def test_hidden_execution_does_not_touch_the_book():
    b = RefBook()
    add(b, 0, 1, 100, 10, Side.ASK)
    b.apply(1000, 1, 0, 100, 7, EventType.EXECUTE_HIDDEN, Side.ASK)
    assert b.level_size(Side.ASK, 100) == 10


def test_unknown_order_still_decrements_level_and_is_counted():
    b = RefBook()
    add(b, 0, 1, 100, 10, Side.BID)
    b.apply(1000, 1, 999, 100, 4, EventType.EXECUTE, Side.BID)
    assert b.level_size(Side.BID, 100) == 6
    assert b.unknown_order_events == 1


def test_best_bid_and_ask_pick_correct_extremes():
    b = RefBook()
    add(b, 0, 1, 100, 10, Side.BID)
    add(b, 1, 2, 101, 10, Side.BID)
    add(b, 2, 3, 105, 10, Side.ASK)
    add(b, 3, 4, 104, 10, Side.ASK)
    assert b.best_bid() == 101
    assert b.best_ask() == 104


def test_arrival_seq_is_recorded_for_priority():
    b = RefBook()
    add(b, 0, 1, 100, 10, Side.BID)
    add(b, 7, 2, 100, 10, Side.BID)
    assert b.find(1).seq == 0
    assert b.find(2).seq == 7


def test_top_of_book_series_reports_state_after_each_event():
    """Covers top_of_book_series without LOBSTER data present.

    This does NOT replace test_refbook_vs_lobster_orderbook.py, which checks the
    same function against an independently produced snapshot file.
    """
    import numpy as np

    from shadowfill.events import EVENT_DTYPE
    from shadowfill.replay import top_of_book_series

    rows = [
        (0, 0, 1, 100, 10, EventType.ADD, Side.BID),
        (1, 1, 2, 105, 10, EventType.ADD, Side.ASK),
        (2, 2, 3, 101, 10, EventType.ADD, Side.BID),
        (3, 3, 3, 101, 10, EventType.DELETE, Side.BID),
        (4, 4, 0, 105, 5, EventType.EXECUTE_HIDDEN, Side.ASK),
    ]
    events = np.array(rows, dtype=EVENT_DTYPE)
    tob = top_of_book_series(events)

    assert tob["best_bid"].tolist() == [100, 100, 101, 100, 100]
    assert tob["best_ask"].tolist() == [0, 105, 105, 105, 105]
    assert tob["seq"].tolist() == [0, 1, 2, 3, 4]
