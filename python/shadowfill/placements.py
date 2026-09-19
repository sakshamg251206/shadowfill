"""Placement schedules for shadow orders."""

from __future__ import annotations

import numpy as np

from .events import EventType, Side
from .replay import Placement, RefBook


def place_top_of_book_grid(
    events: np.ndarray,
    *,
    grid_ns: int,
    size: int,
    horizon_ns: int,
    latency_ns: int,
) -> list[Placement]:
    """One shadow per side at each grid time, at the prevailing best price.

    Top-of-book only: deeper levels can leave the recorded level band, which
    would silently truncate the event stream feeding the queue arithmetic.
    """
    book = RefBook()
    placements: list[Placement] = []
    shadow_id = 0
    next_grid: int | None = None

    for e in events:
        ts = int(e["ts_ns"])
        if next_grid is None:
            next_grid = ts
        while next_grid is not None and ts >= next_grid:
            for side, price in (
                (Side.BID, book.best_bid()),
                (Side.ASK, book.best_ask()),
            ):
                if price is None:
                    continue
                placements.append(
                    Placement(
                        shadow_id=shadow_id,
                        ts_ns=next_grid,
                        latency_ns=latency_ns,
                        side=int(side),
                        price=price,
                        size=size,
                        horizon_ns=horizon_ns,
                    )
                )
                shadow_id += 1
            next_grid += grid_ns

        book.apply(
            ts,
            int(e["seq"]),
            int(e["order_id"]),
            int(e["price"]),
            int(e["size"]),
            int(e["type"]),
            int(e["side"]),
        )

    return placements


def place_matched_to_orders(
    events: np.ndarray,
    *,
    horizon_ns: int,
    latency_ns: int = 0,
    size: int | None = None,
) -> list[Placement]:
    """One shadow per real order, at that order's own time, price and side.

    This is the comparison the project is named for: the shadow shadows a real
    order, so the two populations are identical by construction and the only
    difference between them is that the shadow never cancels.

    The time-grid placer cannot support that claim. Measured on the synthetic
    fixture, grid shadows sat behind a median queue of 259 shares while real
    orders at the touch sat behind 42.5 -- six times shallower. Queue position
    is the dominant determinant of a fill, so an unmatched comparison reports
    that composition difference as if it were the cancellation bias, and the
    placebo test fails on a stream whose censoring is independent by
    construction.

    The placement is timestamped one nanosecond before the order it shadows.
    Activation is strict (``effective_ts < now_ts``), so this activates on the
    arriving ADD event itself, and shadow accounting runs before the book
    applies that event -- which makes ``ahead_at_insert`` exactly the real
    order's ``ahead_at_arrival``, rather than that plus the order's own size.

    ``size`` defaults to the shadowed order's own size.
    """
    adds = events[events["type"] == int(EventType.ADD)]
    return [
        Placement(
            shadow_id=int(i),
            ts_ns=int(e["ts_ns"]) - 1,
            latency_ns=latency_ns,
            side=int(e["side"]),
            price=int(e["price"]),
            size=size if size is not None else int(e["size"]),
            horizon_ns=horizon_ns,
        )
        for i, e in enumerate(adds)
    ]
