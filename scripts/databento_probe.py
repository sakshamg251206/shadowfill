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

# For H2 the quantity that matters is the RELATIVE tick, tick divided by price.
# Every US equity has a $0.01 tick, so price alone sets the regime: a very
# expensive share has a minuscule relative tick, many price levels and a thin
# queue at the touch; a cheap one has a coarse relative tick, few levels and a
# deep queue. These four span roughly three orders of magnitude of relative
# tick while holding the venue and matching rules fixed, which is the
# comparison amendment U argued for.
SURVEY_SYMBOLS = [
    ("BKNG", "very small relative tick"),
    ("AAPL", "small"),
    ("F", "large"),
    ("SIRI", "very large relative tick"),
]

# Block bootstrap is over sessions, never over individual orders, so the
# experiment needs several distinct days rather than a long single one.
SURVEY_DAYS = ["2024-06-03", "2024-06-04", "2024-06-05", "2024-06-06", "2024-06-07"]

# A regular-trading-hours slice. Starting away from UTC midnight means a book
# snapshot is prepended, which is wanted: it identifies orders already resting.
SURVEY_WINDOW = ("T13:30:00", "T20:00:00")
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


def build_query(start: str, end: str, symbol: str = SYMBOL) -> dict[str, object]:
    return {
        "dataset": DATASET,
        "symbols": [symbol],
        "schema": "mbo",
        "start": start,
        "end": end,
    }


def survey(client, budget_usd: float) -> int:
    """Price the whole eventual experiment. Free, and downloads nothing.

    Knowing what the real run costs before committing to a probe is the
    difference between a budget and a hope.
    """
    print(f"pricing {len(SURVEY_SYMBOLS)} symbols x {len(SURVEY_DAYS)} sessions")
    print("regular trading hours, MBO, nothing downloaded\n")
    print(f"  {'symbol':<8}{'regime':<28}{'per session':>13}{'x' + str(len(SURVEY_DAYS)):>10}")

    total = 0.0
    failures = []
    for symbol, regime in SURVEY_SYMBOLS:
        per_day = []
        for day in SURVEY_DAYS:
            try:
                estimate = estimate_cost(
                    client, **build_query(day + SURVEY_WINDOW[0], day + SURVEY_WINDOW[1], symbol)
                )
                per_day.append(estimate.usd)
            except Exception as exc:  # report and continue; one bad day must not abort
                failures.append(f"{symbol} {day}: {type(exc).__name__}: {exc}")
        if not per_day:
            print(f"  {symbol:<8}{regime:<28}{'unpriced':>13}")
            continue
        subtotal = sum(per_day)
        total += subtotal
        one = "$" + format(per_day[0], ".4f")
        many = "$" + format(subtotal, ".4f")
        print(f"  {symbol:<8}{regime:<28}{one:>13}{many:>10}")

    print(f"\n  {'TOTAL':<36}{'':>13}{'$' + format(total, '.4f'):>10}")
    print(f"  {'against free credit':<36}{'':>13}{'$' + format(budget_usd, '.2f'):>10}")
    if total > budget_usd:
        print(f"\n  OVER BUDGET by ${total - budget_usd:.4f}. Cut symbols or sessions.")
    else:
        print(
            f"\n  fits, with ${budget_usd - total:.4f} left over "
            f"({100 * total / budget_usd:.1f}% of the credit)"
        )
    for line in failures:
        print(f"  ! {line}")
    return 0


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


def explain_vendor_error(exc: Exception) -> int:
    """Turn a vendor rejection into something actionable.

    The guard's contract is that no unpriced or over-budget request is ever
    sent. It cannot promise the vendor will accept a request it approved, so a
    refusal here is expected behaviour, not a bug -- but it should read like an
    explanation rather than a stack trace.
    """
    text = str(exc)
    print(f"\nDatabento refused the request: {type(exc).__name__}")
    print(f"  {text.splitlines()[0] if text else exc}")
    print("\nNothing was downloaded and nothing was charged: the request was")
    print("rejected before it was created.")

    if "insufficient_funds" in text or "402" in text:
        print("\nThis says 'insufficient budget' even when free credits remain.")
        print("Known things to check in the portal, cheapest first:")
        print("  1. Is a payment method on file? Databento asks for one at")
        print("     registration to verify the account. Requests can be refused")
        print("     without one even though the card is not charged while credits")
        print("     last.")
        print("  2. Is there a spending limit or budget set to 0 for the")
        print("     dataset or the team?")
        print("  3. Is XNAS.ITCH entitled on this plan? An unentitled dataset can")
        print("     present as a budget failure rather than a permissions one.")
        print("\n  https://databento.com/docs/portal/billing")
        print("\nPricing still works, so the survey and window scans remain")
        print("available while this is sorted out.")
    return 1


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
        "--survey",
        action="store_true",
        help="price the full experiment (symbols x sessions) instead of probe windows",
    )
    parser.add_argument(
        "--credit",
        type=float,
        default=125.0,
        help="free credit available, for the survey total to be compared against",
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

    if args.survey:
        return survey(client, args.credit)

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
    except Exception as exc:  # the vendor refused; explain rather than traceback
        return explain_vendor_error(exc)

    print(f"\ndownloaded {args.out} for ${spent.usd:.5f}")
    return analyse(args.out)


if __name__ == "__main__":
    sys.exit(main())
