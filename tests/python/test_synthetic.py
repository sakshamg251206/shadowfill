import numpy as np

from shadowfill.events import EventType
from shadowfill.lobster import load_lobster_messages
from shadowfill.replay import RefBook
from shadowfill.synthetic import generate_synthetic_messages, write_synthetic_csv


def test_generation_is_deterministic_for_a_seed():
    a = generate_synthetic_messages(n_events=2000, seed=7)
    b = generate_synthetic_messages(n_events=2000, seed=7)
    np.testing.assert_array_equal(a, b)


def test_generation_differs_across_seeds():
    a = generate_synthetic_messages(n_events=2000, seed=7)
    b = generate_synthetic_messages(n_events=2000, seed=8)
    assert not np.array_equal(a, b)


def test_stream_never_produces_negative_level_sizes(tmp_path):
    events = generate_synthetic_messages(n_events=20000, seed=1)
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
        assert all(v > 0 for v in book.bids.values())
        assert all(v > 0 for v in book.asks.values())


def test_csv_roundtrips_through_the_lobster_adapter(tmp_path):
    events = generate_synthetic_messages(n_events=500, seed=3)
    path = tmp_path / "synthetic_message_10.csv"
    write_synthetic_csv(events, path)
    reloaded = load_lobster_messages(path)
    np.testing.assert_array_equal(events, reloaded)


def test_stream_contains_every_event_type_we_care_about():
    events = generate_synthetic_messages(n_events=20000, seed=5)
    present = set(events["type"].tolist())
    for required in (
        EventType.ADD,
        EventType.CANCEL_PARTIAL,
        EventType.DELETE,
        EventType.EXECUTE,
        EventType.EXECUTE_HIDDEN,
    ):
        assert int(required) in present
