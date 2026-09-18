"""Canonical MBO event schema.

Every data adapter converts vendor formats into this schema. Nothing downstream
knows about LOBSTER, Databento, or exchange websocket payloads.

Units:
    ts_ns    nanoseconds since local midnight of the session
    price    integer price ticks (LOBSTER convention: dollars * 10_000)
    size     integer shares/contracts/base units
    seq      monotone arrival sequence, defines price-time priority
"""

from __future__ import annotations

from enum import IntEnum

import numpy as np


class EventType(IntEnum):
    """Event codes, aligned with LOBSTER message types."""

    ADD = 1
    CANCEL_PARTIAL = 2
    DELETE = 3
    EXECUTE = 4
    EXECUTE_HIDDEN = 5
    CROSS = 6
    HALT = 7


class Side(IntEnum):
    """Side of the *resting* order the event refers to."""

    BID = 1
    ASK = -1


EVENT_DTYPE = np.dtype(
    [
        ("ts_ns", "<i8"),
        ("seq", "<u8"),
        ("order_id", "<u8"),
        ("price", "<i8"),
        ("size", "<i8"),
        ("type", "u1"),
        ("side", "i1"),
    ]
)

#: Event types that remove size from the *visible* queue. EXECUTE_HIDDEN is
#: excluded deliberately: a hidden order never sat in the visible book, so it
#: must not decrement a shadow order's queue-ahead counter.
CONSUMES_VISIBLE_QUEUE = frozenset({EventType.CANCEL_PARTIAL, EventType.DELETE, EventType.EXECUTE})


def empty_events(n: int) -> np.ndarray:
    """Allocate a zero-filled canonical event array of length ``n``."""
    return np.zeros(n, dtype=EVENT_DTYPE)
