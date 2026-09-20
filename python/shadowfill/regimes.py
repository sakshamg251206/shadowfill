"""H2: does the sign of the bias depend on the tick regime?

Two mechanisms push in opposite directions. Traders cancel when the queue is
hopeless, which makes censoring-based estimators read *high*; and traders
cancel to avoid being picked off, which makes them read *low*. The spec is
agnostic about which dominates and predicts the first wins in
small-relative-tick, thin-queue books and the second in large-relative-tick,
deep ones.

The contrast is taken *within* one venue and one session, from relative tick
size -- a $290 stock and a $47 stock on the same $0.01 tick differ sixfold in
tick as a fraction of price, and therefore in how deep a queue at the touch
tends to be. Varying venue instead would vary the matching rules at the same
time and confound the two.

Each symbol is measured by the same ``run_h1`` the single-session experiment
uses, so a regime comparison cannot drift from the headline number.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np

from .bias import DEFAULT_HORIZONS
from .dataset import load_events
from .events import EventType
from .experiment import DEFAULT_BLOCK_NS, REGULAR_HOURS_NS, run_h1
from .provenance import environment, git_sha

#: A $0.01 tick in canonical price units (dollars x 10,000).
TICK = 100

#: Below this many in-window events a symbol is not measured. Thin names --
#: ADRs whose real liquidity is in European hours, for instance -- would
#: otherwise contribute a regime point built on a few hundred orders.
MIN_EVENTS = 50_000


def relative_tick_bps(events: np.ndarray) -> float:
    """One tick as a fraction of price, in basis points.

    The regime variable. Uses the median price of arriving orders rather than
    a single snapshot, so one wide quote cannot set the regime for a session.
    """
    adds = events[events["type"] == int(EventType.ADD)]
    if len(adds) == 0:
        raise ValueError("no adds, cannot establish a price level")
    return float(TICK / np.median(adds["price"]) * 10_000.0)


def run_h2(
    dataset_root: str | Path,
    session_date: str,
    symbols: list[str],
    out_dir: str | Path,
    *,
    horizons: np.ndarray = DEFAULT_HORIZONS,
    horizon_ns: int = 60_000_000_000,
    window_ns: tuple[int, int] | None = REGULAR_HOURS_NS,
    block_ns: int = DEFAULT_BLOCK_NS,
    n_replicates: int = 200,
    seed: int = 0,
    engine: str = "cpp",
) -> dict[str, Any]:
    """Measure H1's bias per symbol and tabulate it against relative tick size."""
    dataset_root = Path(dataset_root)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    skipped: dict[str, str] = {}

    for symbol in symbols:
        path = dataset_root / f"date={session_date}" / f"symbol={symbol}" / "events.parquet"
        if not path.exists():
            skipped[symbol] = "no partition"
            continue
        events = load_events(path)
        if window_ns is not None:
            in_window = (events["ts_ns"] >= window_ns[0]) & (events["ts_ns"] < window_ns[1])
            events = events[in_window]
        if len(events) < MIN_EVENTS:
            skipped[symbol] = f"only {len(events):,} in-window events"
            continue

        tick_bps = relative_tick_bps(events)
        manifest = run_h1(
            message_path=path,
            out_dir=out_dir / symbol,
            horizon_ns=horizon_ns,
            engine=engine,
            horizons=horizons,
            window_ns=window_ns,
            block_ns=block_ns,
            n_replicates=n_replicates,
            seed=seed,
        )
        for row in manifest["rows"]:
            if row["stratum"] != "all":
                continue
            rows.append(
                {
                    "symbol": symbol,
                    "relative_tick_bps": tick_bps,
                    "median_price": float(
                        np.median(events[events["type"] == int(EventType.ADD)]["price"]) / 1e4
                    ),
                    "n_events": len(events),
                    **{
                        k: row[k]
                        for k in (
                            "horizon_ns",
                            "ground_truth",
                            "naive_km",
                            "naive_error",
                            "naive_error_lo",
                            "naive_error_hi",
                            "n_shadows",
                        )
                    },
                }
            )

    rows.sort(key=lambda r: (r["relative_tick_bps"], r["horizon_ns"]))
    result = {
        "git_sha": git_sha(),
        "experiment": "H2-tick-regime",
        "session_date": session_date,
        "dataset_root": str(dataset_root),
        "environment": environment(),
        "skipped": skipped,
        "config": {
            "horizons_ns": [int(h) for h in horizons],
            "horizon_ns": horizon_ns,
            "window_ns": list(window_ns) if window_ns else None,
            "block_ns": block_ns,
            "n_replicates": n_replicates,
            "seed": seed,
            "min_events": MIN_EVENTS,
        },
        "rows": rows,
    }
    (out_dir / "manifest.json").write_text(json.dumps(result, indent=2))
    return result


def format_table(rows: list[dict[str, Any]], horizon_ns: int) -> str:
    """One line per symbol at a chosen horizon, ordered by tick regime."""
    selected = [r for r in rows if r["horizon_ns"] == horizon_ns]
    header = (
        f"{'symbol':>7} {'price':>9} {'tick bps':>9} {'F*':>8} {'KM':>8} "
        f"{'error':>9}  {'95% CI':>20}"
    )
    lines = [header, "-" * len(header)]
    for r in selected:
        ci = f"[{r['naive_error_lo']:+.4f},{r['naive_error_hi']:+.4f}]"
        lines.append(
            f"{r['symbol']:>7} {r['median_price']:>9.2f} {r['relative_tick_bps']:>9.2f} "
            f"{r['ground_truth']:>8.4f} {r['naive_km']:>8.4f} {r['naive_error']:>+9.4f}  {ci:>20}"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure H1's bias across tick regimes")
    parser.add_argument("--dataset-root", default="data/parquet")
    parser.add_argument("--session-date", required=True)
    parser.add_argument("--symbols", required=True, help="comma-separated")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--horizon-ns", type=int, default=60_000_000_000)
    parser.add_argument("--block-ns", type=int, default=DEFAULT_BLOCK_NS)
    parser.add_argument("--n-replicates", type=int, default=200)
    parser.add_argument("--report-horizon-ns", type=int, default=60_000_000_000)
    args = parser.parse_args()

    result = run_h2(
        args.dataset_root,
        args.session_date,
        [s for s in args.symbols.split(",") if s.strip()],
        args.out_dir,
        horizon_ns=args.horizon_ns,
        block_ns=args.block_ns,
        n_replicates=args.n_replicates,
    )
    print(format_table(result["rows"], args.report_horizon_ns))
    if result["skipped"]:
        print("\nskipped:", ", ".join(f"{k} ({v})" for k, v in result["skipped"].items()))


if __name__ == "__main__":
    main()
