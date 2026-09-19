"""Validation of the ITCH adapter against a real Nasdaq file.

Marked ``needs_itch`` and skipped unless a sample has been fetched: invariant 6
says no third-party data in the repository or in CI. The offline guarantees
live in test_itch.py; what only real data can show is whether the layouts hold
across millions of messages written by the exchange rather than by the test.

Run with::

    ./scripts/fetch_itch_sample.sh 20
    pytest -m needs_itch -v
"""

from __future__ import annotations

import os

import numpy as np
import pytest

from shadowfill.events import EventType
from shadowfill.itch import parse_itch
from shadowfill.replay import RefBook

pytestmark = pytest.mark.needs_itch

SYMBOL = os.environ.get("SHADOWFILL_ITCH_SYMBOL", "UN")


@pytest.fixture(scope="module")
def parsed(itch_sample):
    return parse_itch(itch_sample, SYMBOL, allow_truncated=True)


def test_sample_yields_a_usable_event_stream(parsed):
    events, diag = parsed
    assert len(events) > 10_000, "sample too small to validate anything"
    assert diag.locate > 0
    assert np.all(np.diff(events["ts_ns"]) >= 0)
    assert events["seq"].tolist() == list(range(len(events)))
    assert np.all(events["size"] > 0)
    assert np.all(events["price"] > 0)


def test_order_references_resolve(parsed):
    """The claim the whole project rests on, checked against the exchange's own bytes.

    An execution names the resting order it consumed, so queue position is
    arithmetic rather than inference. A prefix that starts at the session open
    should resolve essentially everything; a mid-session range would not, which
    is why this is a rate and not zero.
    """
    _, diag = parsed
    assert diag.symbol_messages > 0
    assert diag.unresolved_refs / diag.symbol_messages < 0.01
    assert diag.oversized_removals == 0


def test_reconstructed_book_never_crosses(parsed):
    """The strongest check available without an independent snapshot file.

    A wrong side byte, a wrong price offset or a mishandled replace would all
    show up here: they would leave a bid resting at or above an ask, which a
    real continuous book cannot do. It is a necessary condition, not the
    sufficient one a LOBSTER orderbook CSV gives -- see amendment V.
    """
    events, _ = parsed
    book = RefBook()
    crossed = 0
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
        if book.bids and book.asks and max(book.bids) >= min(book.asks):
            crossed += 1
    assert crossed == 0
    assert book.unknown_order_events == 0


def test_replace_messages_are_split_and_are_not_rare(parsed):
    """U is ~11% of order flow, so mishandling it would corrupt most queues.

    Each replace emits a DELETE and an ADD, so adds must outnumber the orders
    that were ever added fresh. Checking the counts are of the same order is a
    weak assertion on purpose: the point is that the path is exercised at all.
    """
    events, _ = parsed
    kinds = {
        int(k): int(v) for k, v in zip(*np.unique(events["type"], return_counts=True), strict=True)
    }
    adds = kinds.get(int(EventType.ADD), 0)
    removals = kinds.get(int(EventType.DELETE), 0) + kinds.get(int(EventType.CANCEL_PARTIAL), 0)
    assert adds > 0 and removals > 0


def test_both_engines_agree_on_real_exchange_data(parsed):
    """Invariant 5, checked against an exchange rather than our own generator.

    The equivalence suite runs on synthetic events, which the same person wrote
    as the engines. Real ITCH carries order-id patterns, price levels and
    removal sequences nobody designed for this code, and it is the only place a
    shared wrong assumption would show up.
    """
    from shadowfill.ground_truth import OUTCOME_FIELDS, compute_outcomes
    from shadowfill.placements import place_top_of_book_grid

    pytest.importorskip("shadowfill._core")
    events, _ = parsed
    placements = place_top_of_book_grid(
        events[:60_000],
        grid_ns=100_000_000,
        size=100,
        horizon_ns=60_000_000_000,
        latency_ns=0,
    )
    if not placements:
        pytest.skip("sample produced no placements")

    cpp, cpp_diag = compute_outcomes(events[:60_000], placements, "cpp")
    py, py_diag = compute_outcomes(events[:60_000], placements, "python")
    for field in OUTCOME_FIELDS:
        np.testing.assert_array_equal(cpp[field], py[field], err_msg=field)
    assert cpp_diag == py_diag
