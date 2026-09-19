"""Cost preflight for Databento historical requests.

This project has no budget. The free signup credit is the entire allowance, so
an overrun is not a mistake that can be corrected later -- the money is simply
gone and the experiment stops. Every download therefore goes through a guard
that first asks Databento what the request costs and refuses if the answer is
above an explicitly stated ceiling.

The preflight is free: Databento bills its metadata endpoints, including
``get_cost`` and ``get_billable_size``, at $0.00. So there is no reason to
reason about pricing from documentation when the exact number can be measured,
and no reason to ever issue a download whose price was not checked first.

The budget ceiling is never defaulted. A default would be a guess about someone
else's money.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class BudgetExceededError(RuntimeError):
    """A request was priced above the ceiling it was given, and was not sent."""


class _Metadata(Protocol):
    def get_cost(self, **kwargs: Any) -> float: ...
    def get_billable_size(self, **kwargs: Any) -> int: ...


class _Timeseries(Protocol):
    def get_range(self, **kwargs: Any) -> Any: ...


class HistoricalClient(Protocol):
    """The slice of databento.Historical this module uses.

    A Protocol rather than the real class so the guard is testable without a
    key, a network or a billable account.
    """

    @property
    def metadata(self) -> _Metadata: ...
    @property
    def timeseries(self) -> _Timeseries: ...


@dataclass(frozen=True)
class CostEstimate:
    """What Databento says a request costs, before it is sent."""

    usd: float
    billable_bytes: int
    query: dict[str, Any]

    @property
    def megabytes(self) -> float:
        return self.billable_bytes / 1e6

    def describe(self) -> str:
        window = f"{self.query.get('start')} -> {self.query.get('end')}"
        return (
            f"{self.query.get('dataset')} {self.query.get('schema')} "
            f"{self.query.get('symbols')} [{window}]  "
            f"${self.usd:.4f}  {self.megabytes:.3f} MB"
        )


def estimate_cost(client: HistoricalClient, **query: Any) -> CostEstimate:
    """Price a request without sending it. Free: metadata endpoints bill $0.00."""
    usd = float(client.metadata.get_cost(**query))
    billable = int(client.metadata.get_billable_size(**query))
    return CostEstimate(usd=usd, billable_bytes=billable, query=dict(query))


def assert_affordable(estimate: CostEstimate, *, budget_usd: float) -> None:
    """Raise unless the priced request fits the stated ceiling.

    Strictly greater-than fails, so a budget of 0.0 permits only genuinely free
    requests rather than quietly allowing a rounding-error charge.
    """
    if estimate.usd > budget_usd:
        raise BudgetExceededError(
            f"request priced at ${estimate.usd:.4f} exceeds the ${budget_usd:.4f} "
            f"ceiling and was not sent: {estimate.describe()}"
        )


def download_guarded(
    client: HistoricalClient,
    *,
    budget_usd: float,
    path: str | Path,
    **query: Any,
) -> CostEstimate:
    """Price a request, refuse it if it is too expensive, otherwise download it.

    The only function in this project that spends money, and it cannot be
    reached without passing a ceiling.
    """
    estimate = estimate_cost(client, **query)
    assert_affordable(estimate, budget_usd=budget_usd)
    client.timeseries.get_range(path=str(path), **query)
    return estimate


def cheapest(estimates: list[CostEstimate]) -> CostEstimate:
    """The least expensive of several candidate queries."""
    if not estimates:
        raise ValueError("no estimates to compare")
    return min(estimates, key=lambda e: (e.usd, e.billable_bytes))


def client_from_env() -> HistoricalClient:
    """Build a real client from DATABENTO_API_KEY, failing loudly if unset.

    The key is read here and nowhere else, and is never logged or written to
    any artifact this project produces.
    """
    key = os.environ.get("DATABENTO_API_KEY")
    if not key:
        raise RuntimeError("DATABENTO_API_KEY is not set in the environment")
    import databento

    return databento.Historical(key)  # type: ignore[no-any-return]
