"""ITCH 5.0 adapter tests, built on synthetic binary messages.

Every message here is assembled byte by byte from the published TotalView-ITCH
5.0 layouts, so these tests pin the offsets themselves. That matters more than
usual: a wrong offset in a binary parser does not raise, it silently yields a
plausible number, and every downstream queue position would inherit it.

No network, no sample file, no fixture on disk. The real-sample check lives in
test_itch_vs_nasdaq.py behind a marker.
"""

from __future__ import annotations

import gzip

import numpy as np
import pytest

from shadowfill.events import EVENT_DTYPE, EventType, Side
from shadowfill.itch import (
    ItchFormatError,
    ItchTruncatedError,
    load_itch_messages,
    parse_itch,
    parse_itch_symbols,
)

# --------------------------------------------------------------------------
# Message builders. Offsets are written out longhand rather than via struct
# format strings so that each field's width is visible at the call site.
# --------------------------------------------------------------------------


def _framed(body: bytes) -> bytes:
    """Prefix a message with the 2-byte big-endian length used by the daily files."""
    return len(body).to_bytes(2, "big") + body


def _head(code: bytes, locate: int, ts_ns: int) -> bytes:
    """Bytes 0..10: message type, stock locate, tracking number, 6-byte timestamp."""
    return code + locate.to_bytes(2, "big") + (0).to_bytes(2, "big") + ts_ns.to_bytes(6, "big")


def sysevent(code: bytes = b"O", ts_ns: int = 0) -> bytes:
    return _head(b"S", 0, ts_ns) + code  # 12 bytes


def directory(locate: int, symbol: str) -> bytes:
    return _head(b"R", locate, 0) + symbol.ljust(8).encode() + bytes(20)  # 39 bytes


def add(
    ref: int, side: bytes, shares: int, price: int, *, locate: int = 1, ts_ns: int = 0
) -> bytes:
    return (
        _head(b"A", locate, ts_ns)
        + ref.to_bytes(8, "big")
        + side
        + shares.to_bytes(4, "big")
        + b"TEST    "
        + price.to_bytes(4, "big")
    )  # 36 bytes


def add_mpid(ref: int, side: bytes, shares: int, price: int, **kw) -> bytes:
    return add(ref, side, shares, price, **kw).replace(b"A", b"F", 1) + b"MPID"  # 40 bytes


def execute(ref: int, shares: int, *, locate: int = 1, ts_ns: int = 0, match: int = 7) -> bytes:
    return (
        _head(b"E", locate, ts_ns)
        + ref.to_bytes(8, "big")
        + shares.to_bytes(4, "big")
        + match.to_bytes(8, "big")
    )  # 31 bytes


def execute_priced(
    ref: int, shares: int, price: int, *, printable: bytes = b"Y", locate: int = 1, ts_ns: int = 0
) -> bytes:
    return (
        _head(b"C", locate, ts_ns)
        + ref.to_bytes(8, "big")
        + shares.to_bytes(4, "big")
        + (7).to_bytes(8, "big")
        + printable
        + price.to_bytes(4, "big")
    )  # 36 bytes


def cancel(ref: int, shares: int, *, locate: int = 1, ts_ns: int = 0) -> bytes:
    return _head(b"X", locate, ts_ns) + ref.to_bytes(8, "big") + shares.to_bytes(4, "big")  # 23


def delete(ref: int, *, locate: int = 1, ts_ns: int = 0) -> bytes:
    return _head(b"D", locate, ts_ns) + ref.to_bytes(8, "big")  # 19 bytes


def replace(
    orig: int, new: int, shares: int, price: int, *, locate: int = 1, ts_ns: int = 0
) -> bytes:
    return (
        _head(b"U", locate, ts_ns)
        + orig.to_bytes(8, "big")
        + new.to_bytes(8, "big")
        + shares.to_bytes(4, "big")
        + price.to_bytes(4, "big")
    )  # 35 bytes


def trade(shares: int, price: int, *, side: bytes = b"B", locate: int = 1, ts_ns: int = 0) -> bytes:
    return (
        _head(b"P", locate, ts_ns)
        + (0).to_bytes(8, "big")
        + side
        + shares.to_bytes(4, "big")
        + b"TEST    "
        + price.to_bytes(4, "big")
        + (7).to_bytes(8, "big")
    )  # 44 bytes


def cross(shares: int, price: int, *, kind: bytes = b"O", locate: int = 1, ts_ns: int = 0) -> bytes:
    return (
        _head(b"Q", locate, ts_ns)
        + shares.to_bytes(8, "big")
        + b"TEST    "
        + price.to_bytes(4, "big")
        + (7).to_bytes(8, "big")
        + kind
    )  # 40 bytes


PRELUDE = [sysevent(), directory(1, "TEST"), directory(2, "OTHER")]


def write_itch(tmp_path, bodies, *, name="sample.itch", prelude=True, compress=False):
    """Frame and write a message list, returning the path."""
    msgs = (PRELUDE if prelude else []) + list(bodies)
    blob = b"".join(_framed(m) for m in msgs)
    path = tmp_path / (name + (".gz" if compress else ""))
    if compress:
        path.write_bytes(gzip.compress(blob))
    else:
        path.write_bytes(blob)
    return path


def rows(tmp_path, bodies, symbol="TEST", **kw):
    return load_itch_messages(write_itch(tmp_path, bodies, **kw), symbol)


# --------------------------------------------------------------------------
# Field decoding
# --------------------------------------------------------------------------


def test_add_message_decodes_every_field(tmp_path):
    ev = rows(tmp_path, [add(100, b"B", 500, 1_234_500, ts_ns=11_072_057_543_747)])
    assert ev.dtype == EVENT_DTYPE
    assert len(ev) == 1
    assert int(ev[0]["ts_ns"]) == 11_072_057_543_747
    assert int(ev[0]["order_id"]) == 100
    assert int(ev[0]["price"]) == 1_234_500  # 4 implied decimals == LOBSTER ticks
    assert int(ev[0]["size"]) == 500
    assert int(ev[0]["type"]) == int(EventType.ADD)
    assert int(ev[0]["side"]) == int(Side.BID)


def test_sell_side_byte_maps_to_ask(tmp_path):
    ev = rows(tmp_path, [add(100, b"S", 500, 1_000_000)])
    assert int(ev[0]["side"]) == int(Side.ASK)


def test_attributed_add_is_identical_apart_from_the_mpid(tmp_path):
    """F is A plus a 4-byte attribution suffix; it must not shift any offset."""
    plain = rows(tmp_path, [add(100, b"B", 500, 1_000_000)])
    attributed = rows(tmp_path, [add_mpid(100, b"B", 500, 1_000_000)])
    assert plain.tolist() == attributed.tolist()


def test_sequence_is_dense_and_timestamps_are_non_decreasing(tmp_path):
    ev = rows(
        tmp_path,
        [
            add(1, b"B", 100, 1_000_000, ts_ns=10),
            add(2, b"B", 100, 1_000_000, ts_ns=20),
            execute(1, 100, ts_ns=30),
        ],
    )
    assert ev["seq"].tolist() == [0, 1, 2]
    assert np.all(np.diff(ev["ts_ns"]) >= 0)


# --------------------------------------------------------------------------
# The resolution table: E/C/X/D/U carry only an order reference
# --------------------------------------------------------------------------


def test_execute_inherits_side_and_price_from_the_add(tmp_path):
    ev = rows(tmp_path, [add(100, b"S", 500, 1_999_900), execute(100, 200)])
    assert int(ev[1]["type"]) == int(EventType.EXECUTE)
    assert int(ev[1]["order_id"]) == 100
    assert int(ev[1]["size"]) == 200
    assert int(ev[1]["price"]) == 1_999_900
    assert int(ev[1]["side"]) == int(Side.ASK)


def test_execute_with_price_still_consumes_the_queue_when_not_printable(tmp_path):
    """Non-printable means excluded from the tape, not that the shares survived.

    Treating a non-printable C as a no-op would leave phantom size resting in
    the queue ahead of every shadow order behind it.
    """
    ev = rows(
        tmp_path,
        [add(100, b"B", 500, 1_000_000), execute_priced(100, 200, 999_900, printable=b"N")],
    )
    assert int(ev[1]["type"]) == int(EventType.EXECUTE)
    assert int(ev[1]["size"]) == 200
    # The resting order's own price governs the queue, not the execution price.
    assert int(ev[1]["price"]) == 1_000_000


def test_partial_cancel_keeps_the_order_and_full_cancel_deletes_it(tmp_path):
    ev = rows(
        tmp_path,
        [add(100, b"B", 500, 1_000_000), cancel(100, 200), cancel(100, 300), delete(100)],
    )
    assert int(ev[1]["type"]) == int(EventType.CANCEL_PARTIAL)
    assert int(ev[1]["size"]) == 200
    # The second cancel takes the remaining 300, so the order is gone.
    assert int(ev[2]["type"]) == int(EventType.DELETE)
    assert int(ev[2]["size"]) == 300
    # ...which makes the trailing D unresolvable rather than a second removal.
    assert len(ev) == 3


def test_delete_carries_no_size_so_it_is_filled_in_from_the_table(tmp_path):
    ev = rows(tmp_path, [add(100, b"B", 500, 1_000_000), execute(100, 150), delete(100)])
    assert int(ev[2]["type"]) == int(EventType.DELETE)
    assert int(ev[2]["size"]) == 350
    assert int(ev[2]["price"]) == 1_000_000


def test_removals_never_exceed_the_remaining_shares(tmp_path):
    ev, diag = parse_itch(
        write_itch(tmp_path, [add(100, b"B", 100, 1_000_000), execute(100, 250)]), "TEST"
    )
    assert int(ev[1]["size"]) == 100
    assert diag.oversized_removals == 1


# --------------------------------------------------------------------------
# Replace: 11% of order flow in the measured sample, and it changes order ids
# --------------------------------------------------------------------------


def test_replace_becomes_a_delete_of_the_old_order_and_an_add_of_the_new(tmp_path):
    ev = rows(tmp_path, [add(100, b"S", 500, 1_000_000), replace(100, 101, 400, 1_000_100)])
    assert len(ev) == 3
    assert int(ev[1]["type"]) == int(EventType.DELETE)
    assert int(ev[1]["order_id"]) == 100
    assert int(ev[1]["size"]) == 500
    assert int(ev[1]["price"]) == 1_000_000

    assert int(ev[2]["type"]) == int(EventType.ADD)
    assert int(ev[2]["order_id"]) == 101
    assert int(ev[2]["size"]) == 400
    assert int(ev[2]["price"]) == 1_000_100
    # U carries no side byte: the replacement inherits it from the original.
    assert int(ev[2]["side"]) == int(Side.ASK)
    assert int(ev[1]["seq"]) + 1 == int(ev[2]["seq"])


def test_replaced_order_is_tracked_under_its_new_reference(tmp_path):
    ev = rows(
        tmp_path,
        [add(100, b"B", 500, 1_000_000), replace(100, 101, 400, 1_000_100), execute(101, 400)],
    )
    assert int(ev[3]["type"]) == int(EventType.EXECUTE)
    assert int(ev[3]["order_id"]) == 101
    assert int(ev[3]["size"]) == 400
    assert int(ev[3]["price"]) == 1_000_100


def test_old_reference_dies_with_the_replace(tmp_path):
    ev, diag = parse_itch(
        write_itch(
            tmp_path,
            [add(100, b"B", 500, 1_000_000), replace(100, 101, 400, 1_000_100), delete(100)],
        ),
        "TEST",
    )
    assert len(ev) == 3
    assert diag.unresolved_refs == 1


# --------------------------------------------------------------------------
# Non-queue messages
# --------------------------------------------------------------------------


def test_non_displayed_trade_is_a_hidden_execution(tmp_path):
    """P prints a trade against liquidity that never rested in the visible book.

    Invariant 1: it must not consume the queue, which the canonical
    EXECUTE_HIDDEN type is what guarantees downstream.
    """
    ev = rows(tmp_path, [add(100, b"B", 500, 1_000_000), trade(300, 999_900)])
    assert int(ev[1]["type"]) == int(EventType.EXECUTE_HIDDEN)
    assert int(ev[1]["size"]) == 300
    assert int(ev[1]["price"]) == 999_900
    assert int(ev[1]["order_id"]) == 0


def test_cross_trade_is_a_cross(tmp_path):
    ev = rows(tmp_path, [cross(50_000, 1_000_000)])
    assert int(ev[0]["type"]) == int(EventType.CROSS)
    assert int(ev[0]["size"]) == 50_000


# --------------------------------------------------------------------------
# Symbol scoping and unresolvable references
# --------------------------------------------------------------------------


def test_other_symbols_are_excluded(tmp_path):
    ev, diag = parse_itch(
        write_itch(
            tmp_path,
            [
                add(100, b"B", 500, 1_000_000, locate=1),
                add(200, b"B", 900, 5_000_000, locate=2),
                execute(200, 900, locate=2),
            ],
        ),
        "TEST",
    )
    assert [int(r["order_id"]) for r in ev] == [100]
    assert diag.locate == 1
    assert diag.messages == 6
    # Another symbol's orders are never entered, so they are not "unresolved".
    assert diag.unresolved_refs == 0


def test_reference_to_an_order_added_before_the_window_is_counted_not_guessed(tmp_path):
    """A prefix of a daily file starts mid-session; E can name an order we never saw.

    Side and price are unrecoverable, and a canonical event needs both, so the
    honest move is to drop the event and report it rather than emit a guess.
    """
    ev, diag = parse_itch(write_itch(tmp_path, [execute(999, 100), delete(998)]), "TEST")
    assert len(ev) == 0
    assert diag.unresolved_refs == 2


def test_unknown_symbol_is_an_error(tmp_path):
    with pytest.raises(ItchFormatError, match="NOSUCH"):
        rows(tmp_path, [add(100, b"B", 500, 1_000_000)], symbol="NOSUCH")


# --------------------------------------------------------------------------
# Framing
# --------------------------------------------------------------------------


def test_gzip_is_read_transparently(tmp_path):
    ev = rows(tmp_path, [add(100, b"B", 500, 1_000_000)], compress=True)
    assert len(ev) == 1


def test_truncated_final_message_raises(tmp_path):
    path = write_itch(tmp_path, [add(100, b"B", 500, 1_000_000)])
    path.write_bytes(path.read_bytes()[:-4])
    with pytest.raises(ItchFormatError, match="truncated"):
        load_itch_messages(path, "TEST")


def test_declared_length_that_disagrees_with_the_spec_raises(tmp_path):
    """A length prefix that does not match the message type means lost framing.

    Once the stream is misaligned every later message decodes as garbage, so
    this has to fail loudly at the first byte rather than produce numbers.
    """
    body = add(100, b"B", 500, 1_000_000)
    path = tmp_path / "bad.itch"
    path.write_bytes(b"".join(_framed(m) for m in PRELUDE) + _framed(body + b"\x00"))
    with pytest.raises(ItchFormatError, match="length"):
        load_itch_messages(path, "TEST")


def test_unknown_message_type_is_skipped_by_its_declared_length(tmp_path):
    """Forward compatibility: an unrecognised type must not desynchronise framing."""
    path = tmp_path / "mixed.itch"
    unknown = _framed(_head(b"?", 1, 5) + bytes(9))
    path.write_bytes(
        b"".join(_framed(m) for m in PRELUDE) + unknown + _framed(add(100, b"B", 500, 1_000_000))
    )
    ev, diag = parse_itch(path, "TEST")
    assert len(ev) == 1
    assert diag.skipped_types["?"] == 1


def test_truncation_can_be_accepted_explicitly_and_is_reported(tmp_path):
    """A byte-range prefix always ends mid-message; that has to be opt-in.

    Silently tolerating it would make a failed download indistinguishable from
    a short session, and every rate computed from it would be wrong.
    """
    path = write_itch(tmp_path, [add(100, b"B", 500, 1_000_000), add(101, b"B", 400, 999_900)])
    path.write_bytes(path.read_bytes()[:-10])

    ev, diag = parse_itch(path, "TEST", allow_truncated=True)
    assert [int(r["order_id"]) for r in ev] == [100]
    assert diag.truncated is True


def test_truncation_is_not_tolerated_by_default(tmp_path):
    path = write_itch(tmp_path, [add(100, b"B", 500, 1_000_000)])
    path.write_bytes(path.read_bytes()[:-4])
    with pytest.raises(ItchTruncatedError):
        parse_itch(path, "TEST")


def test_a_framing_error_is_never_swallowed_by_truncation_tolerance(tmp_path):
    """Lost alignment must fail even when a partial tail is allowed."""
    path = tmp_path / "bad.itch"
    path.write_bytes(
        b"".join(_framed(m) for m in PRELUDE) + _framed(add(100, b"B", 500, 1_000_000) + b"\x00")
    )
    with pytest.raises(ItchFormatError, match="length"):
        parse_itch(path, "TEST", allow_truncated=True)


def test_gzip_member_ending_without_its_trailer_is_a_truncation(tmp_path):
    """What a ranged GET of a .gz file actually produces."""
    path = write_itch(tmp_path, [add(100, b"B", 500, 1_000_000)], compress=True)
    path.write_bytes(path.read_bytes()[:-12])
    with pytest.raises(ItchTruncatedError):
        parse_itch(path, "TEST")


# --------------------------------------------------------------------------
# Many symbols, one pass
# --------------------------------------------------------------------------


def test_many_symbols_come_out_of_a_single_pass(tmp_path):
    """A full day is 3.5 GB of gzip, so re-reading it per symbol is not an option.

    Each symbol must come out exactly as a single-symbol parse would, which is
    what stops the multi-symbol path from becoming a second, divergent parser.
    """
    bodies = [
        add(100, b"B", 500, 1_000_000, locate=1),
        add(200, b"S", 900, 5_000_000, locate=2),
        execute(100, 200, locate=1),
        execute(200, 400, locate=2),
    ]
    path = write_itch(tmp_path, bodies)

    both = parse_itch_symbols(path, ["TEST", "OTHER"])
    assert set(both) == {"TEST", "OTHER"}
    for symbol in ("TEST", "OTHER"):
        alone, alone_diag = parse_itch(path, symbol)
        together, together_diag = both[symbol]
        assert together.tolist() == alone.tolist()
        assert together_diag.locate == alone_diag.locate
        assert together_diag.unresolved_refs == alone_diag.unresolved_refs


def test_each_symbol_keeps_its_own_sequence_numbering(tmp_path):
    """seq defines price-time priority *within* a book, so it must not be global."""
    both = parse_itch_symbols(
        write_itch(
            tmp_path,
            [
                add(100, b"B", 500, 1_000_000, locate=1),
                add(200, b"S", 900, 5_000_000, locate=2),
                add(101, b"B", 300, 999_900, locate=1),
            ],
        ),
        ["TEST", "OTHER"],
    )
    assert both["TEST"][0]["seq"].tolist() == [0, 1]
    assert both["OTHER"][0]["seq"].tolist() == [0]


def test_a_symbol_that_never_appears_is_named_in_the_error(tmp_path):
    with pytest.raises(ItchFormatError, match="NOSUCH"):
        parse_itch_symbols(write_itch(tmp_path, [add(100, b"B", 5, 1_000_000)]), ["TEST", "NOSUCH"])
