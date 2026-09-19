"""Placement schedules for shadow orders."""

from __future__ import annotations

import numpy as np

from .events import Side
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
