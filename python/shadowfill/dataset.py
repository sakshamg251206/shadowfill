"""Materialise ITCH trading days as partitioned Parquet with a manifest.

Parsing 3.5 GB of gzip to answer one question is fine once and intolerable as
a habit, so a day is parsed once and stored as canonical events per symbol.
Nothing downstream ever re-reads the raw feed.

Layout, Hive-style so a whole dataset can be opened as one Parquet dataset::

    <root>/date=2019-12-30/manifest.json
    <root>/date=2019-12-30/symbol=AAPL/events.parquet

Timestamps stay as the feed defines them: nanoseconds since midnight
US/Eastern of the session date. They are not converted to wall-clock instants,
because every downstream calculation is a difference within one session and a
timezone conversion could only introduce error.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from .events import EVENT_DTYPE, FIELD_NAMES, empty_events
from .itch import parse_itch_symbols
from .provenance import environment, git_sha, sha256_file

#: Nasdaq names its daily files MMDDYYYY.NASDAQ_ITCH50.gz.
_FILENAME_DATE = re.compile(r"(\d{2})(\d{2})(\d{4})\.NASDAQ_ITCH50")

#: Arrow types matching EVENT_DTYPE field for field. Written out rather than
#: inferred: pandas would widen uint64 order ids, and an order reference that
#: loses its top bits stops matching the executions that name it.
_ARROW_SCHEMA = pa.schema(
    [
        ("ts_ns", pa.int64()),
        ("seq", pa.uint64()),
        ("order_id", pa.uint64()),
        ("price", pa.int64()),
        ("size", pa.int64()),
        ("type", pa.uint8()),
        ("side", pa.int8()),
    ]
)


def session_date_from_filename(path: str | Path) -> str:
    """``12302019.NASDAQ_ITCH50.gz`` -> ``2019-12-30``."""
    match = _FILENAME_DATE.search(Path(path).name)
    if match is None:
        raise ValueError(f"cannot infer session date from {Path(path).name!r}; pass it explicitly")
    month, day, year = match.groups()
    return f"{year}-{month}-{day}"


def partition_dir(root: str | Path, session_date: str, symbol: str) -> Path:
    return Path(root) / f"date={session_date}" / f"symbol={symbol}"


def write_events(events: np.ndarray, path: str | Path) -> None:
    """Write a canonical event array to Parquet under the pinned schema."""
    if events.dtype != EVENT_DTYPE:
        raise ValueError(f"expected EVENT_DTYPE, got {events.dtype}")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    table = pa.table({name: pa.array(events[name]) for name in FIELD_NAMES}, schema=_ARROW_SCHEMA)
    pq.write_table(table, path, compression="zstd")


def load_events(path: str | Path) -> np.ndarray:
    """Read canonical events back, with the exact dtype the engines expect."""
    table = pq.read_table(path, schema=_ARROW_SCHEMA)
    events = empty_events(table.num_rows)
    for name in FIELD_NAMES:
        events[name] = table[name].to_numpy(zero_copy_only=False)
    return events


def extract_itch_day(
    itch_path: str | Path,
    symbols: Sequence[str],
    out_root: str | Path,
    *,
    session_date: str | None = None,
    allow_truncated: bool = False,
) -> dict[str, Any]:
    """Parse one ITCH day for several symbols and write the partitions.

    Returns the manifest, which is also written to the day's directory.
    """
    itch_path = Path(itch_path)
    session_date = session_date or session_date_from_filename(itch_path)
    day_dir = Path(out_root) / f"date={session_date}"
    day_dir.mkdir(parents=True, exist_ok=True)

    parsed = parse_itch_symbols(itch_path, symbols, allow_truncated=allow_truncated)

    per_symbol: dict[str, Any] = {}
    for symbol, (events, diag) in sorted(parsed.items()):
        write_events(events, partition_dir(out_root, session_date, symbol) / "events.parquet")
        per_symbol[symbol] = {
            "n_events": len(events),
            "locate": diag.locate,
            "symbol_messages": diag.symbol_messages,
            "unresolved_refs": diag.unresolved_refs,
            "oversized_removals": diag.oversized_removals,
            "truncated": diag.truncated,
            "first_ts_ns": int(events["ts_ns"][0]) if len(events) else None,
            "last_ts_ns": int(events["ts_ns"][-1]) if len(events) else None,
        }

    any_diag = next(iter(parsed.values()))[1]
    manifest = {
        "git_sha": git_sha(),
        "source": "nasdaq-totalview-itch-5.0",
        "input_path": str(itch_path),
        "input_sha256": sha256_file(itch_path),
        "session_date": session_date,
        "timestamp_basis": "nanoseconds since midnight US/Eastern of session_date",
        "messages_in_file": any_diag.messages,
        "environment": environment(),
        "symbols": per_symbol,
    }
    (day_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Materialise an ITCH day as Parquet")
    parser.add_argument("--itch-path", required=True)
    parser.add_argument("--symbols", required=True, help="comma-separated, e.g. AAPL,MSFT")
    parser.add_argument("--out-root", default="data/parquet")
    parser.add_argument("--session-date", help="defaults to the date in the filename")
    parser.add_argument(
        "--allow-truncated",
        action="store_true",
        help="accept a byte-range prefix that ends mid-message",
    )
    args = parser.parse_args()

    manifest = extract_itch_day(
        args.itch_path,
        [s for s in args.symbols.split(",") if s.strip()],
        args.out_root,
        session_date=args.session_date,
        allow_truncated=args.allow_truncated,
    )
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
