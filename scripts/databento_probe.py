#!/usr/bin/env python
"""Smallest viable MBO probe: does Databento's `F` action name the resting order?

ShadowFill's whole claim is that a hypothetical order's fill time is *computed*
from the queue ahead of it. That needs one thing from the data: when a trade
consumes a resting order, the feed must say **which** order was consumed. If it
only reports that a trade happened, queue position has to be inferred and the
ground truth stops being ground truth.

Databento's MBO schema separates `T` (trade, aggressor side) from `F` (fill, the
resting order consumed). This script checks that on real data rather than on
documentation, using the smallest request that can answer the question.

It does not spend anything by default. Pricing is a free metadata call, so the
default run prices several candidate windows and stops. Downloading requires
both --download and an explicit --budget, and is refused if the price exceeds it.

    export DATABENTO_API_KEY=...
    python scripts/databento_probe.py                       # price only, free
    python scripts/databento_probe.py --download --budget 0.50
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from shadowfill.databento_cost import (
    BudgetExceededError,
    CostEstimate,
    cheapest,
    client_from_env,
    download_guarded,
    estimate_cost,
)

# Nasdaq TotalView-ITCH: carries per-order IDs, and is the same venue as the
# LOBSTER sample Plan 1 already validates against, so the two are comparable.
DATASET = "XNAS.ITCH"

SYMBOL = "AAPL"
START = "2024-06-03T14:30:00"
DEFAULT_WINDOWS = "10s,1m,5m,15m,1h,4h"

# Measured 2026-09-19: 10s, 1m and 5m all price at $0.00599 / 5.3558 MB. A
# request that does not begin at UTC midnight has a synthetic snapshot of the
# whole book prepended, and that snapshot dominates the bill, so below some
# window length the duration is free. Pricing is itself free, so the sensible
# move is to find where that floor ends rather than assume a window length.
_UNITS = {"s": 1, "m": 60, "h": 3600}


def parse_window(text: str) -> int:
    """Seconds from a duration like '10s', '5m', '4h'."""
    unit = text[-1]
    if unit not in _UNITS or not text[:-1].isdigit():
        raise ValueError(f"bad window {text!r}; use e.g. 30s, 5m, 2h")
    return int(text[:-1]) * _UNITS[unit]


def build_candidates(windows: str) -> list[tuple[str, str, str]]:
    from datetime import datetime, timedelta

    start = datetime.fromisoformat(START)
    out = []
    for label in [w.strip() for w in windows.split(",") if w.strip()]:
        end = start + timedelta(seconds=parse_window(label))
        out.append((label, START, end.isoformat()))
    return out


def build_query(start: str, end: str) -> dict[str, object]:
    return {
        "dataset": DATASET,
        "symbols": [SYMBOL],
        "schema": "mbo",
        "start": start,
        "end": end,
    }


def preflight(client, candidates) -> list[CostEstimate]:
    """Price every candidate. Free: Databento bills metadata endpoints at $0.00."""
    estimates = []
    print(f"pricing {len(candidates)} candidate windows (free, nothing downloaded)\n")
    for label, start, end in candidates:
        estimate = estimate_cost(client, **build_query(start, end))
        estimates.append(estimate)
        print(f"  {label:>4}  ${estimate.usd:>9.5f}  {estimate.megabytes:>10.4f} MB")
    return estimates


def action_of(record: object) -> str:
    """The action as a single character.

    DBN carries it as a byte in some bindings and a str in others, so this
    normalises rather than assuming which one this version hands back.
    """
    raw = record.action  # type: ignore[attr-defined]
    return chr(raw) if isinstance(raw, int) else str(raw)


def analyse(path: Path) -> int:
    """Answer the question the probe exists to answer."""
    import databento

    store = databento.DBNStore.from_file(path)
    records = list(store)
    if not records:
        print("\nFAIL: the file is empty; nothing can be concluded")
        return 1

    actions = Counter(action_of(r) for r in records)
    print(f"\n{len(records):,} records")
    print("action counts:", dict(actions))

    fills = [r for r in records if action_of(r) == "F"]
    trades = [r for r in records if action_of(r) == "T"]
    adds = {r.order_id for r in records if action_of(r) == "A"}

    print(f"\nF (fill, resting side): {len(fills):,}")
    print(f"T (trade, aggressor):   {len(trades):,}")

    if not fills:
        print(
            "\nFAIL: no F records. Executions do not name the resting order in this\n"
            "dataset, so queue position would have to be inferred and this source is\n"
            "no better than a public crypto feed. Do not build on it."
        )
        return 1

    matched = sum(1 for r in fills if r.order_id in adds)
    print(f"F records whose order_id was Added earlier in-window: {matched:,}/{len(fills):,}")
    print(
        "\nPASS: executions name the resting order. The queue arithmetic in Plan 1\n"
        "can be driven from this feed."
    )
    print(
        "(Some F order_ids will not match an in-window Add because the order was\n"
        "resting before the window opened -- expected, and the same condition\n"
        "Plan 1 already reports as unknown_order_events.)"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--download", action="store_true", help="actually fetch the cheapest candidate"
    )
    parser.add_argument(
        "--budget",
        type=float,
        default=None,
        help="hard ceiling in USD; required with --download, never defaulted",
    )
    parser.add_argument("--out", type=Path, default=Path("data/databento/probe.dbn.zst"))
    parser.add_argument(
        "--windows",
        default=DEFAULT_WINDOWS,
        help="comma-separated durations to price, e.g. 10s,1m,1h",
    )
    parser.add_argument(
        "--pick",
        default=None,
        help="download this window instead of the cheapest; same price at the floor",
    )
    args = parser.parse_args()

    try:
        client = client_from_env()
    except RuntimeError as exc:
        print(f"{exc}\n\nSet it first:  export DATABENTO_API_KEY=...")
        return 2

    candidates = build_candidates(args.windows)
    estimates = preflight(client, candidates)
    best = cheapest(estimates)

    # At the cost floor every window bills the same, so the cheapest is not the
    # most useful -- take the longest window sharing that price, which buys more
    # trades to validate against for no extra money.
    at_floor = [e for e in estimates if e.usd == best.usd]
    best = at_floor[-1] if at_floor else best
    if args.pick:
        chosen = [
            e for e, (label, _, _) in zip(estimates, candidates, strict=True) if label == args.pick
        ]
        if not chosen:
            print(f"\n--pick {args.pick} is not among --windows")
            return 2
        best = chosen[0]
    print(f"\nselected: {best.describe()}")
    print(f"({len(at_floor)} of {len(estimates)} windows share the floor price)")

    if not args.download:
        print("\npriced only; nothing downloaded. Re-run with --download --budget <usd>.")
        return 0

    if args.budget is None:
        print("\n--download requires --budget. Refusing to spend an unstated amount.")
        return 2

    args.out.parent.mkdir(parents=True, exist_ok=True)
    try:
        spent = download_guarded(client, budget_usd=args.budget, path=args.out, **best.query)
    except BudgetExceededError as exc:
        print(f"\n{exc}")
        return 1

    print(f"\ndownloaded {args.out} for ${spent.usd:.5f}")
    return analyse(args.out)


if __name__ == "__main__":
    sys.exit(main())
