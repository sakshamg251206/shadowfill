"""Plan 3: inverse-probability-of-censoring weighting (IPCW).

Naive Kaplan-Meier treats a cancelled order as if it would have gone on to fill
at the same rate as the orders still resting. If cancellation depends only on
things you can observe, that failure is repairable: model the cancellation
hazard given those observables, and weight every fill by the inverse of the
probability that its order was still uncancelled when it filled,

    F_ipcw(h) = (1/n) * sum_i 1{fill_i, T_i <= h} / G(T_i- | x_i),

where G(t | x) is the probability of remaining uncancelled past t given x. The
reweighted curve then targets the never-cancel fill law -- exactly the ground
truth this repository computes, which is what makes the question testable.

**What it can and cannot fix.** IPCW removes the part of the bias that runs
through the covariates in the censoring model. Whatever remains is cancellation
driven by something the model does not see -- private information, a view on
the next trade -- and no reweighting on observables can recover that. So the
gap left after IPCW is itself a measurement: of how much of the bias is
unobservable from the data a backtester has.

**The censoring model here** is non-parametric and stratified on queue-ahead at
arrival, the strongest baseline predictor of a fill. Within a stratum, G is the
Kaplan-Meier survival of "not yet cancelled or window-censored", with fills
treated as censoring of that process. With this choice IPCW is algebraically
the size-weighted average of the per-stratum Kaplan-Meier fill curves (Satten &
Datta 2001), which ``tests/python/test_ipcw.py`` checks to 1e-12. Explicit
weights are kept anyway, so a richer censoring model -- time-varying queue
position, a parametric hazard -- slots in by changing ``G`` alone.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
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
from .lifetimes import extract_lifetimes
from .placements import place_matched_to_orders
from .provenance import environment, git_sha, sha256_file

#: The fill cause code shared with ``bias.observational_arrays``.
FILL = _FILL


def censoring_survival_before(durations: np.ndarray, cause: np.ndarray) -> np.ndarray:
    """G(T_i-): probability each observation was still uncensored just before T_i.

    The censoring process is "cancelled or window-censored"; a fill ends
    observation of it. Evaluated at the left limit because a fill at T must
    have survived censoring *up to* T, not through it.

    **Ties decide whether the estimator is right.** The fill Kaplan-Meier
    treats a cancel tied with a fill at time s as still at risk -- it leaves
    *after* the fill. G has to use the same ordering, so its risk set at s
    excludes the fills at s:

        G jumps by (1 - c_s / (Y_s - d_s)),  not  (1 - c_s / Y_s).

    Then the product telescopes, (1 - d/Y)(1 - c/(Y - d)) = (Y - d - c) / Y,
    and S(t-) G(t-) is exactly the fraction still at risk. Using a plain
    Kaplan-Meier for G, as the first version of this function did, leaves a
    d*c/Y^2 error at every tied time; with integer durations that disagreed
    with Kaplan-Meier by 0.045 at the longest horizon, and the identity test
    caught it.
    """
    durations = np.asarray(durations, dtype=float)
    times = np.unique(durations)
    slot = np.searchsorted(times, durations)
    leaving = np.bincount(slot, minlength=times.size)
    at_risk = len(durations) - np.concatenate(([0], np.cumsum(leaving)[:-1]))
    fills = np.bincount(slot, weights=(cause == FILL).astype(float), minlength=times.size)
    censors = leaving - fills
    remaining = at_risk - fills
    # remaining == 0 only when every observation at s filled, so censors == 0.
    factor = np.where(remaining > 0, 1.0 - censors / np.where(remaining > 0, remaining, 1), 1.0)
    after = np.cumprod(factor)
    before = np.concatenate(([1.0], after[:-1]))
    return before[slot]


def ipcw_fill_curve(
    durations: np.ndarray,
    cause: np.ndarray,
    strata: np.ndarray,
    horizons: np.ndarray,
) -> np.ndarray:
    """IPCW fill CDF at each horizon, with a censoring model stratified on ``strata``."""
    durations = np.asarray(durations, dtype=float)
    cause = np.asarray(cause)
    strata = np.asarray(strata)
    weights = np.zeros(len(durations))
    for s in np.unique(strata):
        member = strata == s
        g = censoring_survival_before(durations[member], cause[member])
        filled = cause[member] == FILL
        # G > 0 wherever a fill is observed: a fill at T means the order was in
        # the risk set just before T, so the censoring KM cannot have hit zero.
        weights[np.flatnonzero(member)[filled]] = 1.0 / g[filled]
    return np.array([weights[durations <= h].sum() for h in horizons]) / len(durations)


def ahead_strata(
    ahead_at_arrival: np.ndarray, edges: Sequence[int] = DEFAULT_AHEAD_EDGES
) -> np.ndarray:
    """Bin index of each order's queue-ahead at arrival."""
    return np.searchsorted(np.asarray(edges), np.asarray(ahead_at_arrival), side="right")


@dataclass(frozen=True)
class IpcwComparison:
    """Truth, the naive estimator, and the IPCW correction, on one population."""

    horizons_ns: np.ndarray
    ground_truth: np.ndarray
    naive_km: np.ndarray
    ipcw: np.ndarray
    n_orders: int

    @property
    def naive_error(self) -> np.ndarray:
        return np.asarray(self.naive_km - self.ground_truth)

    @property
    def ipcw_error(self) -> np.ndarray:
        return np.asarray(self.ipcw - self.ground_truth)

    @property
    def share_of_bias_removed(self) -> np.ndarray:
        """1 - |IPCW error| / |naive error|: 1 closes the gap, 0 changes nothing."""
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.asarray(1.0 - np.abs(self.ipcw_error) / np.abs(self.naive_error))


def compare_ipcw(
    events: np.ndarray,
    *,
    horizon_ns: int,
    horizons: np.ndarray,
    engine: str = "cpp",
    edges: Sequence[int] = DEFAULT_AHEAD_EDGES,
) -> IpcwComparison:
    """Matched never-cancel truth vs naive KM vs stratified IPCW, on one stream.

    Uses the same matched placement as H1: one shadow per real order, so the
    truth and both estimators describe one population.
    """
    placements = place_matched_to_orders(events, horizon_ns=horizon_ns)
    columns, _ = compute_outcomes(events, placements, engine)
    end_ts = int(events["ts_ns"][-1])

    t_dur, t_ev, _, _ = ground_truth_arrays(columns, horizon_ns=horizon_ns, end_ts=end_ts)
    truth = fill_cdf(t_dur, t_ev, horizons)

    durations, cause, _, ahead = observational_arrays(
        extract_lifetimes(events), at_touch_only=False
    )
    naive = fill_cdf(durations, (cause == FILL).astype(float), horizons)
    corrected = ipcw_fill_curve(durations, cause, ahead_strata(ahead, edges), horizons)
    return IpcwComparison(
        horizons_ns=np.asarray(horizons, dtype=np.int64),
        ground_truth=truth,
        naive_km=naive,
        ipcw=corrected,
        n_orders=len(durations),
    )


def run_ipcw(
    *,
    message_path: str | Path,
    out_dir: str | Path,
    horizon_ns: int = 60_000_000_000,
    horizons: np.ndarray = DEFAULT_HORIZONS,
    engine: str = "cpp",
    edges: Sequence[int] = DEFAULT_AHEAD_EDGES,
    window_ns: tuple[int, int] | None = REGULAR_HOURS_NS,
    block_ns: int = DEFAULT_BLOCK_NS,
    n_replicates: int = 200,
    seed: int = 0,
) -> dict[str, Any]:
    """Truth vs naive KM vs IPCW on one session, with paired block-bootstrap CIs.

    The censoring model is refitted inside every replicate. Holding it fixed
    at the full-sample fit would treat the weights as known and understate
    the interval.
    """
    message_path = Path(message_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    events, input_format = load_messages(message_path)
    if window_ns is not None:
        events = events[(events["ts_ns"] >= window_ns[0]) & (events["ts_ns"] < window_ns[1])]
    if len(events) == 0:
        raise ValueError(f"{message_path} has no events in window {window_ns}")
    horizons = np.asarray(horizons, dtype=np.int64)

    placements = place_matched_to_orders(events, horizon_ns=horizon_ns)
    columns, diagnostics = compute_outcomes(events, placements, engine)
    end_ts = int(events["ts_ns"][-1])
    t_dur, t_ev, t_ts, _ = ground_truth_arrays(columns, horizon_ns=horizon_ns, end_ts=end_ts)
    o_dur, cause, o_ts, ahead = observational_arrays(extract_lifetimes(events), at_touch_only=False)
    strata = ahead_strata(ahead, edges)
    fills = (cause == FILL).astype(float)

    def curves(ti: np.ndarray, oi: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        truth = fill_cdf(t_dur[ti], t_ev[ti], horizons)
        naive = fill_cdf(o_dur[oi], fills[oi], horizons)
        corrected = ipcw_fill_curve(o_dur[oi], cause[oi], strata[oi], horizons)
        return truth, naive, corrected

    truth, naive, corrected = curves(np.arange(len(t_dur)), np.arange(len(o_dur)))

    # Paired blocks: shadows by insert time, real orders by arrival time, drawn
    # together so truth and estimate describe the same resampled periods.
    t_block, o_block = t_ts // block_ns, o_ts // block_ns
    blocks = np.union1d(np.unique(t_block), np.unique(o_block))
    if len(blocks) < 2:
        raise ValueError(f"{len(blocks)} time block(s): too few to bootstrap")
    t_members = {b: np.flatnonzero(t_block == b) for b in blocks}
    o_members = {b: np.flatnonzero(o_block == b) for b in blocks}

    rng = np.random.default_rng(seed)
    naive_err, ipcw_err = [], []
    for _ in range(n_replicates):
        drawn = rng.choice(blocks, size=len(blocks), replace=True)
        ti = np.concatenate([t_members[b] for b in drawn])
        oi = np.concatenate([o_members[b] for b in drawn])
        if len(ti) == 0 or len(oi) == 0:
            continue
        rt, rn, rc = curves(ti, oi)
        naive_err.append(rn - rt)
        ipcw_err.append(rc - rt)
    ne, ie = np.array(naive_err), np.array(ipcw_err)

    rows = [
        {
            "horizon_ns": int(h),
            "ground_truth": float(truth[i]),
            "naive_km": float(naive[i]),
            "ipcw": float(corrected[i]),
            "naive_error": float(naive[i] - truth[i]),
            "naive_error_lo": float(np.percentile(ne[:, i], 2.5)),
            "naive_error_hi": float(np.percentile(ne[:, i], 97.5)),
            "ipcw_error": float(corrected[i] - truth[i]),
            "ipcw_error_lo": float(np.percentile(ie[:, i], 2.5)),
            "ipcw_error_hi": float(np.percentile(ie[:, i], 97.5)),
            "n_shadows": len(t_dur),
            "n_orders": len(o_dur),
        }
        for i, h in enumerate(horizons)
    ]

    manifest: dict[str, Any] = {
        "git_sha": git_sha(),
        "experiment": "ipcw",
        "input_path": str(message_path),
        "input_format": input_format,
        "input_sha256": sha256_file(message_path),
        "n_events": len(events),
        "engine": engine,
        "environment": environment(),
        "diagnostics": diagnostics,
        "config": {
            "censoring_model": "stratified Kaplan-Meier on queue-ahead at arrival",
            "ahead_edges": [int(e) for e in edges],
            "horizon_ns": horizon_ns,
            "horizons_ns": [int(h) for h in horizons],
            "window_ns": list(window_ns) if window_ns else None,
            "placement": "matched",
            "block_ns": block_ns,
            "n_replicates": len(ne),
            "n_blocks": len(blocks),
            "seed": seed,
        },
        "bootstrap_scope": "within-session blocks only; censoring model refitted per replicate",
        "rows": rows,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def format_table(rows: list[dict[str, Any]]) -> str:
    lines = [
        f"{'horizon':>8}  {'truth':>7}  {'KM err':>8}  {'95% CI':>20}  "
        f"{'IPCW err':>9}  {'95% CI':>20}"
    ]
    for r in rows:
        h = r["horizon_ns"] / 1e9
        label = f"{h:g}s" if h >= 1 else f"{h * 1000:g}ms"
        lines.append(
            f"{label:>8}  {r['ground_truth']:>7.4f}  {r['naive_error']:>+8.4f}  "
            f"[{r['naive_error_lo']:+.4f}, {r['naive_error_hi']:+.4f}]  "
            f"{r['ipcw_error']:>+9.4f}  [{r['ipcw_error_lo']:+.4f}, {r['ipcw_error_hi']:+.4f}]"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="IPCW correction measured against ground truth")
    parser.add_argument("--message-path", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--n-replicates", type=int, default=200)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    manifest = run_ipcw(
        message_path=args.message_path,
        out_dir=args.out_dir,
        n_replicates=args.n_replicates,
        seed=args.seed,
    )
    print(format_table(manifest["rows"]))


if __name__ == "__main__":
    main()
