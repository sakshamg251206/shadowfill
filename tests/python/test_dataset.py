"""Dataset materialisation: ITCH day -> partitioned Parquet + manifest."""

from __future__ import annotations

import json

import numpy as np
import pytest

from shadowfill.dataset import (
    extract_itch_day,
    load_events,
    partition_dir,
    session_date_from_filename,
    write_events,
)
from shadowfill.events import EVENT_DTYPE, EventType, Side
from shadowfill.synthetic import generate_synthetic_messages
from tests.python.test_itch import add, execute, write_itch


def test_events_round_trip_through_parquet_exactly(tmp_path):
    """Queue arithmetic is integer arithmetic; a lossy round-trip would break it.

    uint64 order ids are the specific risk: anything that routes them through
    a float, or through pandas' default integer handling, silently loses the
    top bits of a real exchange order reference.
    """
    events = generate_synthetic_messages(n_events=5_000, seed=7)
    events["order_id"][0] = 2**63 + 12345  # beyond int64, as a real ref can be

    path = tmp_path / "events.parquet"
    write_events(events, path)
    back = load_events(path)

    assert back.dtype == EVENT_DTYPE
    assert back.tolist() == events.tolist()


def test_session_date_comes_from_the_nasdaq_filename(tmp_path):
    assert session_date_from_filename("12302019.NASDAQ_ITCH50.gz") == "2019-12-30"
    assert session_date_from_filename("/a/b/01302020.NASDAQ_ITCH50.gz") == "2020-01-30"


def test_an_unparseable_filename_is_an_error_not_a_guess(tmp_path):
    with pytest.raises(ValueError, match="session date"):
        session_date_from_filename("sample.gz")


def test_extract_writes_one_partition_per_symbol(tmp_path):
    itch = write_itch(
        tmp_path,
        [
            add(100, b"B", 500, 1_000_000, locate=1),
            add(200, b"S", 900, 5_000_000, locate=2),
            execute(100, 200, locate=1),
        ],
    )
    out = tmp_path / "parquet"
    manifest = extract_itch_day(itch, ["TEST", "OTHER"], out, session_date="2019-12-30")

    for symbol, expected in (("TEST", 2), ("OTHER", 1)):
        events = load_events(partition_dir(out, "2019-12-30", symbol) / "events.parquet")
        assert len(events) == expected
        assert manifest["symbols"][symbol]["n_events"] == expected

    assert int(load_events(partition_dir(out, "2019-12-30", "TEST") / "events.parquet")[1]["type"])
    assert manifest["session_date"] == "2019-12-30"


def test_partitions_are_hive_style_so_the_day_is_readable_as_a_dataset(tmp_path):
    assert partition_dir("root", "2019-12-30", "AAPL").parts[-2:] == (
        "date=2019-12-30",
        "symbol=AAPL",
    )


def test_manifest_pins_provenance_and_diagnostics(tmp_path):
    """Invariant 7. A dataset nobody can trace back to an input is not evidence."""
    itch = write_itch(tmp_path, [add(100, b"B", 500, 1_000_000), execute(999, 5)])
    out = tmp_path / "parquet"
    manifest = extract_itch_day(itch, ["TEST"], out, session_date="2019-12-30")

    on_disk = json.loads((out / "date=2019-12-30" / "manifest.json").read_text())
    assert on_disk == manifest
    assert len(manifest["input_sha256"]) == 64
    assert manifest["git_sha"]
    assert manifest["environment"]["python"]
    # The unresolvable execution is reported, not absorbed.
    assert manifest["symbols"]["TEST"]["unresolved_refs"] == 1
    assert manifest["symbols"]["TEST"]["truncated"] is False


def test_extracted_events_are_the_same_as_parsing_the_symbol_directly(tmp_path):
    """Materialising must not change a single field, only where it lives."""
    from shadowfill.itch import load_itch_messages

    itch = write_itch(
        tmp_path,
        [add(100, b"B", 500, 1_000_000), execute(100, 200), add(101, b"S", 5, 1_100_000)],
    )
    out = tmp_path / "parquet"
    extract_itch_day(itch, ["TEST"], out, session_date="2019-12-30")

    direct = load_itch_messages(itch, "TEST")
    stored = load_events(partition_dir(out, "2019-12-30", "TEST") / "events.parquet")
    assert stored.tolist() == direct.tolist()


def test_an_empty_symbol_still_produces_a_partition(tmp_path):
    """A symbol that never traded is a finding, not a missing file."""
    itch = write_itch(tmp_path, [add(100, b"B", 500, 1_000_000, locate=1)])
    out = tmp_path / "parquet"
    manifest = extract_itch_day(itch, ["OTHER"], out, session_date="2019-12-30")

    events = load_events(partition_dir(out, "2019-12-30", "OTHER") / "events.parquet")
    assert len(events) == 0
    assert events.dtype == EVENT_DTYPE
    assert manifest["symbols"]["OTHER"]["n_events"] == 0


def test_stored_events_replay_through_the_reference_engine(tmp_path):
    """The point of materialising: what comes back must drive the engine unchanged."""
    from shadowfill.replay import RefBook

    events = generate_synthetic_messages(n_events=2_000, seed=3)
    path = tmp_path / "events.parquet"
    write_events(events, path)

    book = RefBook()
    for e in load_events(path):
        book.apply(*(int(e[f]) for f in EVENT_DTYPE.names))
    assert book.orders
    assert np.all(load_events(path)["type"] != 0)
    assert int(Side.BID) and int(EventType.ADD)  # schema constants still line up
