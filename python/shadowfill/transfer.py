"""Plan 4: does a fill-model correction learned on one book transfer to another?

A desk that believed the H1 result would not recompute ground truth for every
name it trades. It would calibrate once and carry the correction over. This
module measures whether that works, across symbols on one day ("cross-regime",
RESEARCH-SPEC §7) and across days for one symbol.

**The correction, fixed before any real data was looked at.** A calibration
factor per (horizon, queue-ahead stratum),

    R_s(h, k) = F*_s(h, k) / KM_s(h, k),

learned on a source s and applied to a target t's own stratified Kaplan-Meier:

    F_hat_t(h) = sum_k pi_t(k) * min(1, R_s(h, k) * KM_t(h, k)),

with pi_t the target's share of orders in each stratum. Multiplicative rather
than additive because fill levels differ sevenfold across the six names (0.07
for SAP, 0.49 for MSFT), and an additive shift learned on a high-fill book is
meaningless on a low-fill one. A stratum where the source's Kaplan-Meier is zero
gives no usable ratio and is left uncorrected.

**The fair baseline** is the target's *stratified* Kaplan-Meier, the same sum
with R = 1, so the calibration is the only thing that differs between the
corrected and uncorrected estimates. Self-transfer then reproduces the target's
stratified truth exactly wherever its Kaplan-Meier is positive.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .bias import (
    _FILL,
    DEFAULT_AHEAD_EDGES,
    DEFAULT_HORIZONS,
    fill_cdf,
    ground_truth_arrays,
    observational_arrays,
)
from .experiment import DEFAULT_BLOCK_NS, REGULAR_HOURS_NS
from .ground_truth import compute_outcomes, load_messages
from .ipcw import ahead_strata
from .lifetimes import extract_lifetimes
from .placements import place_matched_to_orders
from .provenance import environment, git_sha, sha256_file

N_STRATA = len(DEFAULT_AHEAD_EDGES) + 1


@dataclass(frozen=True)
class StratifiedCurves:
    """Truth and Kaplan-Meier per queue-ahead stratum, with the stratum mix."""

    horizons: np.ndarray
    truth: np.ndarray  # (strata, horizons)
    km: np.ndarray  # (strata, horizons)
    shares: np.ndarray  # (strata,), sums to 1

    def stratified_truth(self) -> np.ndarray:
        return np.asarray(self.shares @ self.truth)

    def stratified_km(self) -> np.ndarray:
        return np.asarray(self.shares @ self.km)


def apply_calibration(source: StratifiedCurves, target: StratifiedCurves) -> np.ndarray:
    """The target's fill curve, corrected with the source's calibration factors."""
    ratio = np.divide(source.truth, source.km, out=np.ones_like(source.km), where=source.km > 0)
    return np.asarray(target.shares @ np.minimum(1.0, ratio * target.km))


@dataclass(frozen=True)
class OrderArrays:
    """Per-shadow and per-real-order observations for one book, ready to resample."""

    t_dur: np.ndarray
    t_ev: np.ndarray
    t_ts: np.ndarray
    t_stratum: np.ndarray
    o_dur: np.ndarray
    o_fill: np.ndarray
    o_ts: np.ndarray
    o_stratum: np.ndarray


def order_arrays(events: np.ndarray, *, horizon_ns: int, engine: str = "cpp") -> OrderArrays:
    """Matched never-cancel truth and real-order lifetimes, each with its stratum."""
    placements = place_matched_to_orders(events, horizon_ns=horizon_ns)
    columns, _ = compute_outcomes(events, placements, engine)
    end_ts = int(events["ts_ns"][-1])
    t_dur, t_ev, t_ts, t_ahead = ground_truth_arrays(columns, horizon_ns=horizon_ns, end_ts=end_ts)
    o_dur, cause, o_ts, o_ahead = observational_arrays(
        extract_lifetimes(events), at_touch_only=False
    )
    return OrderArrays(
        t_dur=t_dur,
        t_ev=t_ev,
        t_ts=np.asarray(t_ts),
        t_stratum=ahead_strata(t_ahead),
        o_dur=o_dur,
        o_fill=(cause == _FILL).astype(float),
        o_ts=np.asarray(o_ts),
        o_stratum=ahead_strata(o_ahead),
    )


def stratified_curves(
    arrays: OrderArrays,
    horizons: np.ndarray,
    t_idx: np.ndarray | None = None,
    o_idx: np.ndarray | None = None,
) -> StratifiedCurves:
    """Per-stratum truth and Kaplan-Meier, optionally on a resample of each side."""
    ti = np.arange(len(arrays.t_dur)) if t_idx is None else t_idx
    oi = np.arange(len(arrays.o_dur)) if o_idx is None else o_idx
    t_s, o_s = arrays.t_stratum[ti], arrays.o_stratum[oi]
    truth = np.zeros((N_STRATA, len(horizons)))
    km = np.zeros((N_STRATA, len(horizons)))
    for k in range(N_STRATA):
        tk, ok = ti[t_s == k], oi[o_s == k]
        if len(tk):
            truth[k] = fill_cdf(arrays.t_dur[tk], arrays.t_ev[tk], horizons)
        if len(ok):
            km[k] = fill_cdf(arrays.o_dur[ok], arrays.o_fill[ok], horizons)
    shares = np.bincount(o_s, minlength=N_STRATA) / len(oi)
    return StratifiedCurves(horizons=np.asarray(horizons), truth=truth, km=km, shares=shares)


def pooled(curves: list[StratifiedCurves], sizes: list[np.ndarray]) -> StratifiedCurves:
    """Order-weighted mixture of several books' per-stratum curves.

    ``sizes[b][k]`` is book b's order count in stratum k. Fixed before any real
    data was seen: the leave-one-out source is this mixture of the other books,
    not a Kaplan-Meier refitted on their union, so a bootstrap replicate costs
    one pass per book.
    """
    weight = np.array(sizes, dtype=float)  # (books, strata)
    total = weight.sum(axis=0)
    share = np.divide(weight, total, out=np.zeros_like(weight), where=total > 0)
    truth = np.einsum("bk,bkh->kh", share, np.array([c.truth for c in curves]))
    km = np.einsum("bk,bkh->kh", share, np.array([c.km for c in curves]))
    return StratifiedCurves(
        horizons=curves[0].horizons, truth=truth, km=km, shares=total / total.sum()
    )


def _stratum_sizes(arrays: OrderArrays, o_idx: np.ndarray) -> np.ndarray:
    return np.bincount(arrays.o_stratum[o_idx], minlength=N_STRATA)


def run_transfer(
    *,
    books: dict[str, str | Path],
    out_dir: str | Path,
    horizon_ns: int = 60_000_000_000,
    horizons: np.ndarray = DEFAULT_HORIZONS,
    engine: str = "cpp",
    window_ns: tuple[int, int] | None = REGULAR_HOURS_NS,
    block_ns: int = DEFAULT_BLOCK_NS,
    n_replicates: int = 200,
    seed: int = 0,
) -> dict[str, Any]:
    """Calibrate on each book, apply to every other; and leave-one-out with CIs.

    ``books`` maps a label (a symbol, or symbol@date) to its events file.
    Pairs are reported as point estimates -- a descriptive matrix. The
    leave-one-out rows, where each book is corrected with the pooled calibration
    of all the others, carry paired block-bootstrap intervals: blocks are
    time-of-day windows drawn once per replicate and applied to every book.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    horizons = np.asarray(horizons, dtype=np.int64)

    arrays: dict[str, OrderArrays] = {}
    n_events: dict[str, int] = {}
    for label, path in books.items():
        events, _ = load_messages(path)
        if window_ns is not None:
            events = events[(events["ts_ns"] >= window_ns[0]) & (events["ts_ns"] < window_ns[1])]
        if len(events) == 0:
            raise ValueError(f"{path} has no events in window {window_ns}")
        arrays[label] = order_arrays(events, horizon_ns=horizon_ns, engine=engine)
        n_events[label] = len(events)
    labels = list(books)

    full = {b: stratified_curves(arrays[b], horizons) for b in labels}
    everything = {b: np.arange(len(arrays[b].o_dur)) for b in labels}

    rows: list[dict[str, Any]] = []
    improved = []
    for src in labels:
        for tgt in labels:
            if src == tgt:
                continue
            truth = full[tgt].stratified_truth()
            km = full[tgt].stratified_km()
            fixed = apply_calibration(full[src], full[tgt])
            for i, h in enumerate(horizons):
                better = abs(fixed[i] - truth[i]) < abs(km[i] - truth[i])
                improved.append(better)
                rows.append(
                    {
                        "mode": "pair",
                        "source": src,
                        "target": tgt,
                        "horizon_ns": int(h),
                        "truth": float(truth[i]),
                        "km_stratified": float(km[i]),
                        "transferred": float(fixed[i]),
                        "error_uncorrected": float(km[i] - truth[i]),
                        "error_transferred": float(fixed[i] - truth[i]),
                        "improved": bool(better),
                    }
                )

    def loo(curves: dict[str, StratifiedCurves], sizes: dict[str, np.ndarray]) -> dict[str, Any]:
        out = {}
        for tgt in labels:
            others = [b for b in labels if b != tgt]
            source = pooled([curves[b] for b in others], [sizes[b] for b in others])
            truth = curves[tgt].stratified_truth()
            out[tgt] = (
                curves[tgt].stratified_km() - truth,
                apply_calibration(source, curves[tgt]) - truth,
            )
        return out

    point = loo(full, {b: _stratum_sizes(arrays[b], everything[b]) for b in labels})

    blocks = np.unique(
        np.concatenate(
            [np.unique(a.t_ts // block_ns) for a in arrays.values()]
            + [np.unique(a.o_ts // block_ns) for a in arrays.values()]
        )
    )
    if len(blocks) < 2:
        raise ValueError(f"{len(blocks)} time block(s): too few to bootstrap")
    t_members = {
        b: {k: np.flatnonzero(arrays[b].t_ts // block_ns == k) for k in blocks} for b in labels
    }
    o_members = {
        b: {k: np.flatnonzero(arrays[b].o_ts // block_ns == k) for k in blocks} for b in labels
    }

    rng = np.random.default_rng(seed)
    draws: dict[str, list[tuple[np.ndarray, np.ndarray]]] = {b: [] for b in labels}
    for _ in range(n_replicates):
        drawn = rng.choice(blocks, size=len(blocks), replace=True)
        curves, sizes = {}, {}
        for b in labels:
            ti = np.concatenate([t_members[b][k] for k in drawn])
            oi = np.concatenate([o_members[b][k] for k in drawn])
            if len(ti) == 0 or len(oi) == 0:
                break
            curves[b] = stratified_curves(arrays[b], horizons, ti, oi)
            sizes[b] = _stratum_sizes(arrays[b], oi)
        else:
            for b, pair in loo(curves, sizes).items():
                draws[b].append(pair)

    for tgt in labels:
        unc = np.array([d[0] for d in draws[tgt]])
        fix = np.array([d[1] for d in draws[tgt]])
        gain = np.abs(unc) - np.abs(fix)
        for i, h in enumerate(horizons):
            rows.append(
                {
                    "mode": "leave_one_out",
                    "source": "pooled others",
                    "target": tgt,
                    "horizon_ns": int(h),
                    "error_uncorrected": float(point[tgt][0][i]),
                    "error_uncorrected_lo": float(np.percentile(unc[:, i], 2.5)),
                    "error_uncorrected_hi": float(np.percentile(unc[:, i], 97.5)),
                    "error_transferred": float(point[tgt][1][i]),
                    "error_transferred_lo": float(np.percentile(fix[:, i], 2.5)),
                    "error_transferred_hi": float(np.percentile(fix[:, i], 97.5)),
                    "abs_error_reduction": float(abs(point[tgt][0][i]) - abs(point[tgt][1][i])),
                    "abs_error_reduction_lo": float(np.percentile(gain[:, i], 2.5)),
                    "abs_error_reduction_hi": float(np.percentile(gain[:, i], 97.5)),
                }
            )

    manifest: dict[str, Any] = {
        "git_sha": git_sha(),
        "experiment": "transfer",
        "input_paths": {b: str(p) for b, p in books.items()},
        "input_sha256": {b: sha256_file(p) for b, p in books.items()},
        "n_events": n_events,
        "engine": engine,
        "environment": environment(),
        "config": {
            "correction": "per (horizon, queue-ahead stratum) ratio truth / KM, clipped at 1",
            "baseline": "target's stratified Kaplan-Meier",
            "leave_one_out_source": "order-weighted mixture of the other books' curves",
            "ahead_edges": [int(e) for e in DEFAULT_AHEAD_EDGES],
            "horizon_ns": horizon_ns,
            "horizons_ns": [int(h) for h in horizons],
            "window_ns": list(window_ns) if window_ns else None,
            "placement": "matched",
            "block_ns": block_ns,
            "n_replicates": min(len(v) for v in draws.values()),
            "n_blocks": len(blocks),
            "seed": seed,
        },
        "summary": {"share_of_pairs_improved": float(np.mean(improved)), "n_pairs": len(improved)},
        "bootstrap_scope": "time-of-day blocks, drawn once per replicate across all books",
        "rows": rows,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def format_table(rows: list[dict[str, Any]], horizon_ns: int) -> str:
    lines = [
        f"{'target':>10}  {'KM error':>9}  {'95% CI':>20}  {'transferred':>11}  "
        f"{'95% CI':>20}  {'|err| cut':>9}"
    ]
    for r in rows:
        if r["mode"] != "leave_one_out" or r["horizon_ns"] != horizon_ns:
            continue
        lines.append(
            f"{r['target']:>10}  {r['error_uncorrected']:>+9.4f}  "
            f"[{r['error_uncorrected_lo']:+.4f}, {r['error_uncorrected_hi']:+.4f}]  "
            f"{r['error_transferred']:>+11.4f}  "
            f"[{r['error_transferred_lo']:+.4f}, {r['error_transferred_hi']:+.4f}]  "
            f"{r['abs_error_reduction']:>+9.4f}"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Does a fill correction transfer between books?")
    parser.add_argument(
        "--book",
        action="append",
        required=True,
        metavar="LABEL=PATH",
        help="repeat once per book, e.g. AAPL=data/parquet/date=.../symbol=AAPL/events.parquet",
    )
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--n-replicates", type=int, default=200)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    books = dict(b.split("=", 1) for b in args.book)
    manifest = run_transfer(
        books=books, out_dir=args.out_dir, n_replicates=args.n_replicates, seed=args.seed
    )
    for h in manifest["config"]["horizons_ns"]:
        print(f"\nhorizon {h / 1e9:g} s -- each book corrected with the others' calibration")
        print(format_table(manifest["rows"], h))
    print(f"\npairs improved: {manifest['summary']['share_of_pairs_improved']:.0%}")


if __name__ == "__main__":
    main()
