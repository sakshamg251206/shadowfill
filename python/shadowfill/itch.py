"""Adapter: Nasdaq TotalView-ITCH 5.0 binary -> canonical events.

ITCH is the raw feed LOBSTER is itself derived from, which is what makes it a
usable second source: prices carry four implied decimals, so a price is already
in LOBSTER ticks, and timestamps are nanoseconds since midnight, so they are
already ``ts_ns``. Neither needs a conversion that could round.

One structure here is unavoidable. Only ``A``/``F`` carry a symbol, a side and
a price; ``E``, ``C``, ``X``, ``D`` and ``U`` carry an *order reference* and
nothing else, and ``D`` does not even carry a size. A canonical event needs all
of those fields, so the adapter keeps

    order_ref -> (side, price, remaining shares)

This is **not** a second order book. It has no price levels, no aggregation, no
best bid or ask, and no notion of queue priority; it is a dictionary that
undoes the feed's compression. All book and queue reasoning stays in
``replay.py`` and the C++ engine, which see only canonical events.

References that were live before the recording window are unresolvable -- their
side and price were established by an ``A`` we never read. Those events are
dropped and counted in ``ItchDiagnostics.unresolved_refs`` rather than guessed,
because every downstream queue position would inherit the guess.
"""

from __future__ import annotations

import gzip
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

import numpy as np

from .events import EVENT_DTYPE, EventType, Side, empty_events


class ItchFormatError(ValueError):
    """The byte stream is not a well-formed ITCH 5.0 file."""


class ItchTruncatedError(ItchFormatError):
    """The stream ends mid-message.

    Distinct from a framing error because it is the *expected* ending of a
    byte-range prefix of a daily file, which is the only sample obtainable
    without paying. Tolerating it is opt-in and reported; tolerating it by
    default would let a failed download masquerade as a short session.
    """


#: Total message length in bytes, including the 1-byte type code but excluding
#: the 2-byte frame prefix. Checked on every message the adapter decodes: a
#: length that disagrees with the spec means the frame boundaries have been
#: lost, and every later message would decode as plausible garbage.
MESSAGE_LENGTHS = {
    b"S": 12, b"R": 39, b"H": 25, b"Y": 20, b"L": 26, b"V": 35, b"W": 12, b"K": 28, b"J": 35,
    b"A": 36, b"F": 40, b"E": 31, b"C": 36, b"X": 23, b"D": 19, b"U": 35,
    b"P": 44, b"Q": 40, b"B": 19, b"I": 50, b"N": 20,
}  # fmt: skip

_GZIP_MAGIC = b"\x1f\x8b"


@dataclass
class ItchDiagnostics:
    """Counts that must be reported rather than silently absorbed."""

    #: Every framed message in the file, all symbols and all types.
    messages: int = 0
    #: Messages carrying the requested symbol's stock locate.
    symbol_messages: int = 0
    #: E/C/X/D/U naming an order that was never added inside the window.
    unresolved_refs: int = 0
    #: Removals for more shares than the reference still had resting.
    oversized_removals: int = 0
    #: The stock locate the symbol resolved to, or -1 if never seen.
    locate: int = -1
    #: Message types the adapter does not decode, by type code.
    skipped_types: Counter[str] = field(default_factory=Counter)
    #: The stream ended mid-message and ``allow_truncated`` let it pass.
    truncated: bool = False
    #: Messages that priced or printed nothing, e.g. an opening cross for a
    #: symbol with no auction interest. Real data, but not an event.
    zero_size_messages: int = 0


@dataclass
class _Live:
    """What a resting order reference still needs to be resolvable."""

    side: int
    price: int
    shares: int


def _u(body: bytes, offset: int, width: int) -> int:
    return int.from_bytes(body[offset : offset + width], "big")


def _open(path: str | Path) -> BinaryIO:
    """Open plain or gzipped ITCH, decided by magic bytes rather than suffix.

    Nasdaq's daily files are gzipped and the prefixes we slice off them are too,
    but a decompressed file is the normal thing to have locally, so the choice
    is made from the bytes rather than from the name.
    """
    with open(path, "rb") as probe:
        gzipped = probe.read(2) == _GZIP_MAGIC
    if gzipped:
        return gzip.open(path, "rb")  # type: ignore[return-value]
    return Path(path).open("rb")


def _iter_messages(stream: BinaryIO) -> Iterator[bytes]:
    """Yield message bodies from the 2-byte big-endian length framing.

    The daily files Nasdaq publishes are framed; the raw multicast feed is not.
    A short read is an error, never a clean end: silently stopping would make a
    truncated download look like a short session.
    """
    while True:
        try:
            header = stream.read(2)
            if not header:
                return
            if len(header) < 2:
                raise ItchTruncatedError("truncated frame header at end of stream")
            length = int.from_bytes(header, "big")
            body = stream.read(length)
        except EOFError as exc:
            # gzip raises this when the member ends without its trailer, which
            # is what a byte-range prefix of a compressed file always looks like.
            raise ItchTruncatedError(f"compressed stream ends mid-member: {exc}") from exc
        if len(body) < length:
            raise ItchTruncatedError(
                f"truncated message: declared {length} bytes, read {len(body)}"
            )
        yield body


def _build(rows: list[tuple[int, int, int, int, int, int, int]]) -> np.ndarray:
    events = empty_events(len(rows))
    for i, row in enumerate(rows):
        events[i] = row
    assert events.dtype == EVENT_DTYPE
    return events


class _SymbolState:
    """Per-symbol parse state. One of these exists per requested symbol.

    Each symbol gets its own ``seq`` counter because ``seq`` defines price-time
    priority *within a book*; numbering it across the whole file would make one
    symbol's arrivals shift another's priority.
    """

    def __init__(self, diag: ItchDiagnostics) -> None:
        self.diag = diag
        self.live: dict[int, _Live] = {}
        self.rows: list[tuple[int, int, int, int, int, int, int]] = []

    def emit(self, ts_ns: int, order_id: int, price: int, size: int, etype: int, side: int) -> None:
        """Append one canonical event, dropping the ones that say nothing.

        Nasdaq prints a ``Q`` cross with zero shares at 09:30 for a symbol with
        no auction interest -- observed once for UN on 2019-12-30. It is valid
        data and it is not an event: it has no size and no price, it moves no
        queue, and carried through it would appear in every count and every
        aggregate as a trade that never happened. Dropped here rather than at
        each call site so no future message type can reintroduce it, and
        counted so the drop is visible in the manifest.
        """
        if size <= 0:
            self.diag.zero_size_messages += 1
            return
        self.rows.append((ts_ns, len(self.rows), order_id, price, size, etype, side))

    def remove(
        self, ref: int, ts_ns: int, qty: int | None, etype: int, *, promote_on_empty: bool = False
    ) -> None:
        """Apply a cancel/execute/delete to a reference, or count it unresolved.

        ``qty is None`` means "all of it", which is what a ``D`` message says.

        ``promote_on_empty`` exists only for ``X``: LOBSTER splits cancels into
        partial (type 2) and total (type 3), while ITCH uses one message and
        leaves the distinction implicit in the share count. Executions are
        never promoted -- LOBSTER type 4 covers a full execution as well as a
        partial one, and relabelling a full execution as a deletion would hide
        a fill from the shadow tracker, which is the one signal it exists to
        detect.
        """
        order = self.live.get(ref)
        if order is None:
            self.diag.unresolved_refs += 1
            return
        wanted = order.shares if qty is None else qty
        if wanted > order.shares:
            self.diag.oversized_removals += 1
            wanted = order.shares
        order.shares -= wanted
        if order.shares <= 0:
            del self.live[ref]
            if promote_on_empty:
                etype = int(EventType.DELETE)
        self.emit(ts_ns, ref, order.price, wanted, etype, order.side)


def parse_itch_symbols(
    path: str | Path, symbols: Sequence[str], *, allow_truncated: bool = False
) -> dict[str, tuple[np.ndarray, ItchDiagnostics]]:
    """Parse several symbols out of an ITCH 5.0 file in one pass.

    A full trading day is ~3.5 GB of gzip that cannot be seeked into, so the
    file is read once and dispatched by stock locate. Each symbol comes out
    exactly as a single-symbol parse would.

    ``allow_truncated`` stops cleanly at a partial final message instead of
    raising, and records it in every symbol's diagnostics. Use it for
    byte-range prefixes, never for a file that is supposed to be complete.
    """
    wanted = {s.strip().upper().encode(): s.strip().upper() for s in symbols}
    if not wanted:
        raise ValueError("no symbols requested")
    states: dict[int, _SymbolState] = {}
    found: dict[str, _SymbolState] = {}
    # File-level, not per-symbol: a symbol's state is created when its stock
    # directory message appears, which is long after the first message.
    total = 0
    skipped: Counter[str] = Counter()

    with _open(path) as stream:
        try:
            for raw in _iter_messages(stream):
                total += 1
                code = raw[:1]

                expected = MESSAGE_LENGTHS.get(code)
                if expected is None:
                    skipped[code.decode("latin-1")] += 1
                    continue
                if len(raw) != expected:
                    raise ItchFormatError(
                        f"message {code!r} has length {len(raw)}, spec says {expected}; "
                        "frame alignment lost"
                    )

                if code == b"R":
                    name = raw[11:19].strip()
                    if name in wanted and wanted[name] not in found:
                        locate = _u(raw, 1, 2)
                        diag = ItchDiagnostics(locate=locate)
                        state = _SymbolState(diag)
                        states[locate] = state
                        found[wanted[name]] = state
                    continue

                state_opt = states.get(_u(raw, 1, 2))
                if state_opt is None:
                    continue
                _decode(raw, code, state_opt)
        except ItchTruncatedError:
            if not allow_truncated:
                raise
            for state in states.values():
                state.diag.truncated = True

    for state in found.values():
        state.diag.messages = total
        state.diag.skipped_types.update(skipped)

    missing = [s.strip().upper() for s in symbols if s.strip().upper() not in found]
    if missing:
        raise ItchFormatError(f"symbols never appeared in the stock directory: {missing}")
    return {name: (_build(state.rows), state.diag) for name, state in found.items()}


def _decode(raw: bytes, code: bytes, state: _SymbolState) -> None:
    """Turn one message for a tracked symbol into canonical events."""
    state.diag.symbol_messages += 1
    ts_ns = _u(raw, 5, 6)

    if code in (b"A", b"F"):
        ref = _u(raw, 11, 8)
        side_byte = raw[19:20]
        if side_byte not in (b"B", b"S"):
            raise ItchFormatError(f"bad buy/sell indicator {side_byte!r} in {code!r}")
        side = int(Side.BID) if side_byte == b"B" else int(Side.ASK)
        shares = _u(raw, 20, 4)
        price = _u(raw, 32, 4)
        state.live[ref] = _Live(side=side, price=price, shares=shares)
        state.emit(ts_ns, ref, price, shares, int(EventType.ADD), side)

    elif code in (b"E", b"C"):
        # C carries its own execution price and a printable flag. Both are
        # ignored on purpose: the queue is consumed at the resting order's
        # price, and a non-printable execution is merely absent from the tape
        # -- the shares still left the book.
        state.remove(_u(raw, 11, 8), ts_ns, _u(raw, 19, 4), int(EventType.EXECUTE))

    elif code == b"X":
        state.remove(
            _u(raw, 11, 8), ts_ns, _u(raw, 19, 4), int(EventType.CANCEL_PARTIAL),
            promote_on_empty=True,
        )  # fmt: skip

    elif code == b"D":
        state.remove(_u(raw, 11, 8), ts_ns, None, int(EventType.DELETE))

    elif code == b"U":
        # Replace retires one reference and issues another. It is 11% of order
        # flow in the measured sample, and the exchange treats it as a fresh
        # arrival, so it must become DELETE + ADD: modelling it as an amendment
        # would wrongly preserve queue priority.
        orig, new = _u(raw, 11, 8), _u(raw, 19, 8)
        order = state.live.get(orig)
        if order is None:
            state.diag.unresolved_refs += 1
            return
        side = order.side
        state.remove(orig, ts_ns, None, int(EventType.DELETE))
        shares, price = _u(raw, 27, 4), _u(raw, 31, 4)
        state.live[new] = _Live(side=side, price=price, shares=shares)
        state.emit(ts_ns, new, price, shares, int(EventType.ADD), side)

    elif code == b"P":
        # A trade against non-displayed liquidity. Invariant 1: it never sat in
        # the visible queue, so it must not consume it.
        side = int(Side.BID) if raw[19:20] == b"B" else int(Side.ASK)
        state.emit(
            ts_ns, 0, _u(raw, 32, 4), _u(raw, 20, 4), int(EventType.EXECUTE_HIDDEN), side
        )  # fmt: skip

    elif code == b"Q":
        # Auction cross. Side is 0: a cross has no resting side.
        state.emit(ts_ns, 0, _u(raw, 27, 4), _u(raw, 11, 8), int(EventType.CROSS), 0)

    else:
        state.diag.skipped_types[code.decode("latin-1")] += 1


def parse_itch(
    path: str | Path, symbol: str, *, allow_truncated: bool = False
) -> tuple[np.ndarray, ItchDiagnostics]:
    """Parse one symbol out of an ITCH 5.0 file into canonical events.

    Returns the event array and the diagnostics that belong in the run manifest.
    """
    return parse_itch_symbols(path, [symbol], allow_truncated=allow_truncated)[
        symbol.strip().upper()
    ]


def load_itch_messages(
    path: str | Path, symbol: str, *, allow_truncated: bool = False
) -> np.ndarray:
    """Canonical events for one symbol, mirroring ``load_lobster_messages``."""
    return parse_itch(path, symbol, allow_truncated=allow_truncated)[0]
