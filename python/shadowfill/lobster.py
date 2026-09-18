"""Adapter: LOBSTER message/orderbook CSV files -> canonical events."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from .events import EVENT_DTYPE, Side, empty_events

MESSAGE_COLUMNS = ["time", "type", "order_id", "size", "price", "direction"]

# `time` is read as text on purpose -- see parse_seconds_to_ns. The annotation
# keeps the mixed str-literal/type values from collapsing to `object`, which
# pandas-stubs rejects.
MESSAGE_DTYPES: dict[str, str | type[str]] = {
    "time": str,
    "type": "int64",
    "order_id": "int64",
    "size": "int64",
    "price": "int64",
    "direction": "int64",
}


def parse_seconds_to_ns(col: pd.Series) -> np.ndarray:
    """Convert 'seconds-after-midnight' decimal strings to int64 nanoseconds.

    Parsing via float64 loses nanosecond precision for intraday timestamps,
    so the integer and fractional parts are handled separately as strings.
    """
    parts = col.astype(str).str.strip().str.split(".", n=1, expand=True)
    seconds = parts[0].astype("int64").to_numpy(dtype="int64")
    if parts.shape[1] == 1:
        frac = np.zeros(len(col), dtype="int64")
    else:
        frac_str = parts[1].fillna("").str.slice(0, 9).str.ljust(9, "0")
        frac = frac_str.replace("", "0").astype("int64").to_numpy(dtype="int64")
    return seconds * 1_000_000_000 + frac


def load_lobster_messages(path: str | Path) -> np.ndarray:
    """Load a LOBSTER message file into a canonical event array."""
    df = pd.read_csv(
        path,
        header=None,
        names=MESSAGE_COLUMNS,
        dtype=MESSAGE_DTYPES,
    )

    ts_ns = parse_seconds_to_ns(df["time"])
    if np.any(np.diff(ts_ns) < 0):
        raise ValueError(f"{path}: message file is not sorted by timestamp")

    ev = empty_events(len(df))
    ev["ts_ns"] = ts_ns
    ev["seq"] = np.arange(len(df), dtype=np.uint64)
    ev["order_id"] = df["order_id"].to_numpy(dtype="uint64")
    ev["price"] = df["price"].to_numpy(dtype="int64")
    ev["size"] = df["size"].to_numpy(dtype="int64")
    ev["type"] = df["type"].to_numpy(dtype="uint8")
    ev["side"] = np.where(df["direction"].to_numpy() == 1, int(Side.BID), int(Side.ASK)).astype(
        "int8"
    )
    assert ev.dtype == EVENT_DTYPE
    return ev


def load_lobster_orderbook(path: str | Path, levels: int) -> pd.DataFrame:
    """Load a LOBSTER orderbook snapshot file (one row per message row).

    Columns repeat as ask_price_1, ask_size_1, bid_price_1, bid_size_1, ...
    """
    names: list[str] = []
    for i in range(1, levels + 1):
        names += [f"ask_price_{i}", f"ask_size_{i}", f"bid_price_{i}", f"bid_size_{i}"]
    return pd.read_csv(path, header=None, names=names, dtype="int64")
