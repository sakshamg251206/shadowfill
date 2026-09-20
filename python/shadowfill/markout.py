"""H3: the error in expected passive edge, in basis points.

A fill rate on its own is not a decision. What a desk trades on is

    E[edge] = F(h) x E[m_h | filled]

the probability of being filled times what the fill was worth. The second
factor is biased too, and in the opposite direction to the first, so the error
in the product is not the error in either part and can be larger or smaller
than both. That product is the number this project exists to report.

**Why the conditional markout is biased observationally.** Real orders that
fill are a selected subset: the ones nobody cancelled. A trader who sees the
market turning against a resting order pulls it, and the fill that would have
followed -- a bad one, taken just before the price moved away -- never appears
in the data. Never-cancel orders take those fills. So the observational mean
markout is flattered, while the observational fill rate is understated (see
H1), and the two errors partly offset inside the product.

**Sign convention.** Markout is signed so that positive is profit to the
passive side, for either direction::

    m = side * (mid(t_fill + h) - fill_price)

A resting bid that trades and then sees the mid rise made money. A resting
offer that trades and then sees the mid fall made money. Reported in basis
points of the fill price.

**On look-ahead.** Markout deliberately reads prices after the fill. That is
what a markout *is* -- a realised forward return used to evaluate a decision
already taken -- not a feature available at decision time. Nothing here feeds
back into the fill computation, which remains strictly prefix-only.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np

from .bias import DEFAULT_HORIZONS, fill_cdf, ground_truth_arrays, observational_arrays
from .events import EventType, Side
from .experiment import DEFAULT_BLOCK_NS, REGULAR_HOURS_NS
from .ground_truth import compute_outcomes, load_messages
from .lifetimes import NO_FILL, TouchIndex, extract_lifetimes
from .placements import place_matched_to_orders
from .provenance import environment, git_sha, sha256_file
from .replay import RefBook

TOB_DTYPE = np.dtype([("ts_ns", "<i8"), ("best_bid", "<i8"), ("best_ask", "<i8")])


def top_of_book_series(events: np.ndarray) -> np.ndarray:
    """Best bid and ask after each event, with 0 for an empty side.

    ``replay.top_of_book_series`` does the same thing through ``RefBook``'s
    linear scan, which is deliberate there -- it is the obvious-by-inspection
    oracle. Reading the touch after every one of a few hundred thousand events
    makes that quadratic, so this keeps the same lazily purged heap the
    lifetime extractor uses and leaves the oracle alone.
    """
    book = RefBook()
    touch = TouchIndex(book)
    out = np.zeros(len(events), dtype=TOB_DTYPE)
    for i, e in enumerate(events):
        etype, side, price = int(e["type"]), int(e["side"]), int(e["price"])
        book.apply(
            int(e["ts_ns"]),
            int(e["seq"]),
            int(e["order_id"]),
            price,
            int(e["size"]),
            etype,
            side,
        )
        if etype == EventType.ADD:
            touch.note(side, price)
        out[i]["ts_ns"] = e["ts_ns"]
        out[i]["best_bid"] = touch.best(int(Side.BID))
        out[i]["best_ask"] = touch.best(int(Side.ASK))
    return out


def mid_at(tob: np.ndarray, query_ts: np.ndarray) -> np.ndarray:
    """Mid price prevailing at each query time; NaN where a side was empty.

    The book is a step function, so the mid at t is the mid after the last
    event at or before t. A one-sided book has no mid, and inventing one would
    put a fabricated price into a PnL number.
    """
    idx = np.searchsorted(tob["ts_ns"], np.asarray(query_ts, dtype=np.int64), side="right") - 1
    valid = idx >= 0
    idx = np.clip(idx, 0, None)
    bid = tob["best_bid"][idx].astype(float)
    ask = tob["best_ask"][idx].astype(float)
    mid = np.where(valid & (bid > 0) & (ask > 0), (bid + ask) / 2.0, np.nan)
    return mid


def markout_bps(
    fill_ts: np.ndarray,
    fill_price: np.ndarray,
    side: np.ndarray,
    tob: np.ndarray,
    horizon_ns: int,
) -> np.ndarray:
    """Signed markout in basis points of the fill price, NaN where undefined."""
    mid = mid_at(tob, np.asarray(fill_ts, dtype=np.int64) + horizon_ns)
    price = np.asarray(fill_price, dtype=float)
    return np.asarray(np.asarray(side, dtype=float) * (mid - price) / price * 10_000.0)


def _edge(
    durations: np.ndarray,
    events_flag: np.ndarray,
    fill_mask: np.ndarray,
    marks: np.ndarray,
    horizons: np.ndarray,
    horizon_ns_of: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(fill rate, mean markout given a fill, expected edge) per horizon.

    The fill rate comes from the product-limit fit, which handles the orders
    still resting when the window ended; the conditional markout is the plain
    mean over fills observed inside the horizon. Mixing the two is standard and
    is stated rather than hidden: a censored order contributes to F and, having
    no fill, contributes no markout.
    """
    rate = fill_cdf(durations, events_flag, horizons)
    mean = np.full(len(horizons), np.nan)
    for i, h in enumerate(horizons):
        sel = fill_mask & (horizon_ns_of <= h) & ~np.isnan(marks[i])
        if sel.any():
            mean[i] = float(np.mean(marks[i][sel]))
    return rate, mean, rate * mean


def run_h3(
    *,
    message_path: str | Path,
    out_dir: str | Path,
    horizon_ns: int = 60_000_000_000,
    markout_ns: int = 1_000_000_000,
    latency_ns: int = 0,
    engine: str = "cpp",
    horizons: np.ndarray = DEFAULT_HORIZONS,
    window_ns: tuple[int, int] | None = REGULAR_HOURS_NS,
    block_ns: int = DEFAULT_BLOCK_NS,
    n_replicates: int = 200,
    seed: int = 0,
) -> dict[str, Any]:
    """Compare true and observationally-estimated passive edge, in bps."""
    message_path = Path(message_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    events, input_format = load_messages(message_path)
    if window_ns is not None:
        events = events[(events["ts_ns"] >= window_ns[0]) & (events["ts_ns"] < window_ns[1])]
    if len(events) == 0:
        raise ValueError(f"{message_path} has no events in window {window_ns}")

    placements = place_matched_to_orders(events, horizon_ns=horizon_ns, latency_ns=latency_ns)
    columns, diagnostics = compute_outcomes(events, placements, engine)
    lives = extract_lifetimes(events)
    tob = top_of_book_series(events)
    end_ts = int(events["ts_ns"][-1])
    horizons = np.asarray(horizons, dtype=np.int64)

    # Ground truth: the shadow's own price and side come from its placement,
    # which the outcome table does not carry.
    keep = np.asarray(columns["status"]) != 4  # Status.NOT_ACTIVATED
    shadow_price = np.array([p.price for p in placements], dtype=np.int64)[keep]
    shadow_side = np.array([p.side for p in placements], dtype=np.int8)[keep]
    gt_dur, gt_ev, gt_ts, _ = ground_truth_arrays(columns, horizon_ns=horizon_ns, end_ts=end_ts)
    gt_fill_ts = np.asarray(columns["first_fill_ts"])[keep]
    gt_filled = gt_fill_ts >= 0
    gt_marks = np.array(
        [markout_bps(gt_fill_ts, shadow_price, shadow_side, tob, markout_ns) for _ in horizons]
    )

    obs_dur, obs_cause, obs_ts, _ = observational_arrays(lives, at_touch_only=False)
    obs_filled = np.asarray(lives["first_fill_ts"]) != NO_FILL
    obs_marks = np.array(
        [
            markout_bps(lives["first_fill_ts"], lives["price"], lives["side"], tob, markout_ns)
            for _ in horizons
        ]
    )
    obs_ev = (obs_cause == 1).astype(float)

    def compute(
        idx_gt: np.ndarray, idx_obs: np.ndarray
    ) -> tuple[
        tuple[np.ndarray, np.ndarray, np.ndarray], tuple[np.ndarray, np.ndarray, np.ndarray]
    ]:
        t = _edge(
            gt_dur[idx_gt],
            gt_ev[idx_gt],
            gt_filled[idx_gt],
            gt_marks[:, idx_gt],
            horizons,
            gt_dur[idx_gt],
        )
        o = _edge(
            obs_dur[idx_obs],
            obs_ev[idx_obs],
            obs_filled[idx_obs],
            obs_marks[:, idx_obs],
            horizons,
            obs_dur[idx_obs],
        )
        return t, o

    all_gt = np.arange(len(gt_dur))
    all_obs = np.arange(len(obs_dur))
    (t_rate, t_mark, t_edge), (o_rate, o_mark, o_edge) = compute(all_gt, all_obs)

    blocks = np.union1d(np.unique(gt_ts // block_ns), np.unique(obs_ts // block_ns))
    if len(blocks) < 2:
        raise ValueError(f"{len(blocks)} time block(s): too few to bootstrap")
    gt_members = {b: np.flatnonzero(gt_ts // block_ns == b) for b in blocks}
    obs_members = {b: np.flatnonzero(obs_ts // block_ns == b) for b in blocks}

    rng = np.random.default_rng(seed)
    errs = []
    for _ in range(n_replicates):
        drawn = rng.choice(blocks, size=len(blocks), replace=True)
        gi = np.concatenate([gt_members[b] for b in drawn])
        oi = np.concatenate([obs_members[b] for b in drawn])
        if len(gi) == 0 or len(oi) == 0:
            continue
        (_, _, te), (_, _, oe) = compute(gi, oi)
        errs.append(oe - te)
    err = np.array(errs)

    rows = [
        {
            "horizon_ns": int(h),
            "markout_ns": markout_ns,
            "true_fill_rate": float(t_rate[i]),
            "true_markout_bps": float(t_mark[i]),
            "true_edge_bps": float(t_edge[i]),
            "est_fill_rate": float(o_rate[i]),
            "est_markout_bps": float(o_mark[i]),
            "est_edge_bps": float(o_edge[i]),
            "edge_error_bps": float(o_edge[i] - t_edge[i]),
            "edge_error_lo": float(np.nanpercentile(err[:, i], 2.5)),
            "edge_error_hi": float(np.nanpercentile(err[:, i], 97.5)),
        }
        for i, h in enumerate(horizons)
    ]

    manifest = {
        "git_sha": git_sha(),
        "experiment": "H3-passive-edge",
        "input_path": str(message_path),
        "input_format": input_format,
        "input_sha256": sha256_file(message_path),
        "n_events": len(events),
        "n_placements": len(placements),
        "environment": environment(),
        "diagnostics": diagnostics,
        "config": {
            "horizon_ns": horizon_ns,
            "markout_ns": markout_ns,
            "horizons_ns": [int(h) for h in horizons],
            "window_ns": list(window_ns) if window_ns else None,
            "block_ns": block_ns,
            "n_replicates": len(err),
            "n_blocks": len(blocks),
            "seed": seed,
            "engine": engine,
        },
        "rows": rows,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def format_table(rows: list[dict[str, Any]]) -> str:
    header = (
        f"{'horizon':>9} {'F*':>7} {'m* bps':>8} {'edge* bps':>10} "
        f"{'F^':>7} {'m^ bps':>8} {'edge^ bps':>10} {'err bps':>9}  {'95% CI':>20}"
    )
    lines = [header, "-" * len(header)]
    for r in rows:
        h = r["horizon_ns"] / 1e9
        label = f"{h:g}s" if h >= 1 else f"{h * 1000:g}ms"
        ci = f"[{r['edge_error_lo']:+.3f},{r['edge_error_hi']:+.3f}]"
        lines.append(
            f"{label:>9} {r['true_fill_rate']:>7.4f} {r['true_markout_bps']:>8.3f} "
            f"{r['true_edge_bps']:>10.3f} {r['est_fill_rate']:>7.4f} "
            f"{r['est_markout_bps']:>8.3f} {r['est_edge_bps']:>10.3f} "
            f"{r['edge_error_bps']:>+9.3f}  {ci:>20}"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure the error in expected passive edge")
    parser.add_argument("--message-path", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--horizon-ns", type=int, default=60_000_000_000)
    parser.add_argument("--markout-ns", type=int, default=1_000_000_000)
    parser.add_argument("--block-ns", type=int, default=DEFAULT_BLOCK_NS)
    parser.add_argument("--n-replicates", type=int, default=200)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--no-window", action="store_true")
    args = parser.parse_args()

    manifest = run_h3(
        message_path=args.message_path,
        out_dir=args.out_dir,
        horizon_ns=args.horizon_ns,
        markout_ns=args.markout_ns,
        block_ns=args.block_ns,
        n_replicates=args.n_replicates,
        seed=args.seed,
        window_ns=None if args.no_window else REGULAR_HOURS_NS,
    )
    print(format_table(manifest["rows"]))


if __name__ == "__main__":
    main()
