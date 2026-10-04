"""Measure trade-through fills an engine's outcomes do not credit.

Under price priority, an aggressor that executes at a *worse* price on a
shadow's side -- a sell hitting bids below a shadow bid, a buy lifting asks
above a shadow ask -- passed the shadow on the way and would have filled it.
This module finds how often that happens to a live, still-unfilled shadow, and
what crediting it would do to the never-cancel fill curve F*(h).

It sized the gap before the engines credited these fills (amendment AH, run
against commit 0e2f0b8), and on the fixed engines it is a regression check:
the lower bound must credit nothing (amendment AI). It only reads the outcome
columns an engine produces, and reports two bounds:

* **upper** -- every worse-priced execution in the shadow's live window counts.
  This includes moments when real orders at the shadow's own price were also
  skipped, which is a price-priority anomaly in the data itself rather than the
  gap, and which a fixed engine might reasonably not credit.
* **lower** -- only worse-priced executions *after* the shadow's queue-ahead
  reached zero count (``ahead_lt_1_ts``). Nothing visible is ahead of it at its
  price then, so any fixed engine has to fill it.

Hidden executions count: an aggressor that reached a hidden order at 99 passed
a shadow bid at 100 just the same. That does not touch invariant 1 -- nothing
here decrements a visible queue.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np

from .bias import DEFAULT_HORIZONS, fill_cdf, ground_truth_arrays
from .events import EventType, Side
from .experiment import REGULAR_HOURS_NS
from .ground_truth import compute_outcomes, load_messages
from .placements import place_matched_to_orders
from .provenance import environment, git_sha, sha256_file
from .replay import Placement, Status

EXECUTIONS = (int(EventType.EXECUTE), int(EventType.EXECUTE_HIDDEN))


def _first_below(key: np.ndarray, start: np.ndarray, target: np.ndarray) -> np.ndarray:
    """For each query, the first index j >= start with key[j] < target, else -1.

    A sparse table of range minima, searched by binary lifting for all queries
    at once: O(n log n) to build, O(log n) per query. Rejecting a whole block
    of 2**k elements needs only its minimum to be >= target.
    """
    n = len(key)
    out = np.full(len(start), -1, dtype=np.int64)
    if n == 0:
        return out
    # table[k][i] is min(key[i : i + 2**k]).
    table = [key]
    width = 1
    while width * 2 <= n:
        prev = table[-1]
        table.append(np.minimum(prev[:-width], prev[width:]))
        width *= 2
    pos = start.astype(np.int64).copy()
    for k in range(len(table) - 1, -1, -1):
        level = table[k]
        ok = pos < len(level)
        skip = np.zeros(len(pos), dtype=bool)
        skip[ok] = level[pos[ok]] >= target[ok]
        pos[skip] += 1 << k
    hit = pos < n
    hit[hit] = key[pos[hit]] < target[hit]
    out[hit] = pos[hit]
    return out


def first_worse_execution(
    events: np.ndarray,
    *,
    side: np.ndarray,
    price: np.ndarray,
    after_ts: np.ndarray,
    until_ts: np.ndarray,
) -> np.ndarray:
    """Timestamp of the first execution passing each (side, price), or -1.

    Counts an EXECUTE or EXECUTE_HIDDEN on ``side`` at a strictly worse price
    -- lower for a bid, higher for an ask -- with ``after_ts < ts <= until_ts``,
    matching the engines' strict activation and inclusive expiry.
    """
    out = np.full(len(side), -1, dtype=np.int64)
    is_exec = np.isin(events["type"], EXECUTIONS)
    for s in (int(Side.BID), int(Side.ASK)):
        q = np.flatnonzero(side == s)
        if len(q) == 0:
            continue
        ex = events[is_exec & (events["side"] == s)]
        ts = ex["ts_ns"].astype(np.int64)
        # Negating ask prices turns "higher than" into "lower than", so one
        # range-minimum search serves both sides.
        sign = 1 if s == int(Side.BID) else -1
        key = sign * ex["price"].astype(np.int64)
        start = np.searchsorted(ts, after_ts[q], side="right")
        j = _first_below(key, start, sign * price[q].astype(np.int64))
        found = j >= 0
        within = np.zeros(len(q), dtype=bool)
        within[found] = ts[j[found]] <= until_ts[q][found]
        out[q[within]] = ts[j[within]]
    return out


def credited_first_fill(
    events: np.ndarray,
    placements: Sequence[Placement],
    columns: dict[str, np.ndarray],
    *,
    require_front: bool,
) -> np.ndarray:
    """``first_fill_ts`` as it would read with trade-throughs credited.

    ``columns`` are engine outcomes ordered by shadow_id, as
    ``compute_outcomes`` returns them. Only fills *earlier* than the engine's
    own are credited; an unactivated shadow stays at -1.
    """
    ordered = sorted(placements, key=lambda p: p.shadow_id)
    side = np.array([p.side for p in ordered], dtype=np.int8)
    price = np.array([p.price for p in ordered], dtype=np.int64)
    horizon = np.array([p.horizon_ns for p in ordered], dtype=np.int64)

    status = np.asarray(columns["status"])
    insert_ts = np.asarray(columns["insert_ts"])
    first_fill = np.asarray(columns["first_fill_ts"]).astype(np.int64)
    activated = status != int(Status.NOT_ACTIVATED)

    after = insert_ts.astype(np.int64).copy()
    if require_front:
        front_ts = np.asarray(columns["ahead_lt_1_ts"])
        activated = activated & (front_ts >= 0)
        after = np.maximum(after, front_ts)
    until = insert_ts + horizon
    until = np.where(first_fill >= 0, np.minimum(until, first_fill - 1), until)

    idx = np.flatnonzero(activated)
    passed = first_worse_execution(
        events, side=side[idx], price=price[idx], after_ts=after[idx], until_ts=until[idx]
    )
    credited = first_fill.copy()
    hit = passed >= 0
    credited[idx[hit]] = passed[hit]
    return credited


def run_tradethrough(
    *,
    message_path: str | Path,
    out_dir: str | Path,
    horizon_ns: int = 60_000_000_000,
    horizons: np.ndarray = DEFAULT_HORIZONS,
    engine: str = "cpp",
    window_ns: tuple[int, int] | None = REGULAR_HOURS_NS,
) -> dict[str, Any]:
    """F*(h) as the engine computes it, and under each bound on the fix."""
    message_path, out_dir = Path(message_path), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    events, _ = load_messages(message_path)
    if window_ns is not None:
        events = events[(events["ts_ns"] >= window_ns[0]) & (events["ts_ns"] < window_ns[1])]
    if len(events) == 0:
        raise ValueError(f"{message_path} has no events in window {window_ns}")

    placements = place_matched_to_orders(events, horizon_ns=horizon_ns)
    columns, diagnostics = compute_outcomes(events, placements, engine)
    end_ts = int(events["ts_ns"][-1])
    horizons = np.asarray(horizons, dtype=np.int64)

    def curve(first_fill: np.ndarray) -> np.ndarray:
        cols = dict(columns, first_fill_ts=first_fill)
        dur, ev, _, _ = ground_truth_arrays(cols, horizon_ns=horizon_ns, end_ts=end_ts)
        return fill_cdf(dur, ev, horizons)

    engine_ff = np.asarray(columns["first_fill_ts"])
    activated = np.asarray(columns["status"]) != int(Status.NOT_ACTIVATED)
    bounds = {
        "lower": credited_first_fill(events, placements, columns, require_front=True),
        "upper": credited_first_fill(events, placements, columns, require_front=False),
    }
    truth = curve(engine_ff)
    rows = []
    for i, h in enumerate(horizons):
        row: dict[str, Any] = {"horizon_ns": int(h), "truth_engine": float(truth[i])}
        for name, ff in bounds.items():
            row[f"truth_{name}"] = float(curve(ff)[i])
            row[f"delta_{name}"] = row[f"truth_{name}"] - row["truth_engine"]
        rows.append(row)

    counts = {
        name: int(np.sum(activated & (ff >= 0) & ((engine_ff < 0) | (ff < engine_ff))))
        for name, ff in bounds.items()
    }
    is_exec = np.isin(events["type"], EXECUTIONS)
    manifest: dict[str, Any] = {
        "git_sha": git_sha(),
        "experiment": "trade-through-sizing",
        "input_path": str(message_path),
        "input_sha256": sha256_file(message_path),
        "n_events": len(events),
        "n_executions": int(is_exec.sum()),
        "n_hidden_executions": int((events["type"] == int(EventType.EXECUTE_HIDDEN)).sum()),
        "n_shadows_activated": int(activated.sum()),
        "n_shadows_credited_earlier_fill": counts,
        "engine": engine,
        "diagnostics": diagnostics,
        "environment": environment(),
        "config": {
            "placement": "matched",
            "horizon_ns": horizon_ns,
            "horizons_ns": [int(h) for h in horizons],
            "window_ns": list(window_ns) if window_ns else None,
            "lower_bound": "credited only after queue-ahead reached zero (ahead_lt_1_ts)",
            "upper_bound": "any worse-priced execution in the live window",
            "executions_counted": "EXECUTE and EXECUTE_HIDDEN",
        },
        "rows": rows,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Size the trade-through gap on one book")
    parser.add_argument("--message-path", required=True)
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args()
    m = run_tradethrough(message_path=args.message_path, out_dir=args.out_dir)
    print(f"{'horizon':>8}  {'engine F*':>9}  {'lower':>9}  {'upper':>9}")
    for r in m["rows"]:
        print(
            f"{r['horizon_ns'] / 1e9:>7g}s  {r['truth_engine']:>9.4f}  "
            f"{r['delta_lower']:>+9.4f}  {r['delta_upper']:>+9.4f}"
        )
    print(f"shadows credited an earlier fill: {m['n_shadows_credited_earlier_fill']}")


if __name__ == "__main__":
    main()
