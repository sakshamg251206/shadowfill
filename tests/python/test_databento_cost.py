import pytest

from shadowfill.databento_cost import (
    BudgetExceededError,
    CostEstimate,
    assert_affordable,
    cheapest,
    download_guarded,
    estimate_cost,
)


class FakeClient:
    """Records what it was asked, and whether anything was actually downloaded."""

    def __init__(self, usd, billable_bytes=1000):
        self._usd = usd
        self._bytes = billable_bytes
        self.priced = []
        self.downloaded = []
        outer = self

        class _Metadata:
            def get_cost(self, **kwargs):
                outer.priced.append(kwargs)
                return outer._usd

            def get_billable_size(self, **kwargs):
                return outer._bytes

        class _Timeseries:
            def get_range(self, **kwargs):
                outer.downloaded.append(kwargs)

        self.metadata = _Metadata()
        self.timeseries = _Timeseries()


QUERY = dict(
    dataset="XNAS.ITCH",
    symbols=["AAPL"],
    schema="mbo",
    start="2024-06-03T14:30:00",
    end="2024-06-03T14:31:00",
)


def test_estimate_prices_without_downloading():
    client = FakeClient(usd=0.0123, billable_bytes=2_500_000)
    estimate = estimate_cost(client, **QUERY)
    assert estimate.usd == 0.0123
    assert estimate.megabytes == 2.5
    assert client.priced, "cost must actually be queried"
    assert not client.downloaded, "pricing must never download"


def test_a_request_over_budget_is_refused():
    estimate = CostEstimate(usd=5.0, billable_bytes=1, query=QUERY)
    with pytest.raises(BudgetExceededError, match=r"\$5.0000 exceeds"):
        assert_affordable(estimate, budget_usd=1.0)


def test_a_request_within_budget_is_allowed():
    assert_affordable(CostEstimate(usd=0.5, billable_bytes=1, query=QUERY), budget_usd=0.5)


def test_zero_budget_permits_only_free_requests():
    """A 0.0 ceiling must not quietly allow a rounding-error charge."""
    assert_affordable(CostEstimate(usd=0.0, billable_bytes=0, query=QUERY), budget_usd=0.0)
    with pytest.raises(BudgetExceededError):
        assert_affordable(CostEstimate(usd=0.0001, billable_bytes=1, query=QUERY), budget_usd=0.0)


def test_guarded_download_does_not_spend_when_over_budget(tmp_path):
    """The whole point: an overrun must cost nothing, not be corrected later."""
    client = FakeClient(usd=99.0)
    with pytest.raises(BudgetExceededError):
        download_guarded(client, budget_usd=1.0, path=tmp_path / "out.dbn", **QUERY)
    assert client.priced, "it must have asked the price"
    assert not client.downloaded, "nothing may be downloaded once it is refused"


def test_guarded_download_downloads_when_affordable(tmp_path):
    client = FakeClient(usd=0.02)
    estimate = download_guarded(client, budget_usd=1.0, path=tmp_path / "out.dbn", **QUERY)
    assert estimate.usd == 0.02
    assert len(client.downloaded) == 1
    assert client.downloaded[0]["path"] == str(tmp_path / "out.dbn")
    assert client.downloaded[0]["schema"] == "mbo"


def test_cheapest_picks_the_smallest_bill():
    a = CostEstimate(usd=0.10, billable_bytes=10, query={"n": "a"})
    b = CostEstimate(usd=0.01, billable_bytes=99, query={"n": "b"})
    assert cheapest([a, b]) is b


def test_cheapest_rejects_an_empty_comparison():
    with pytest.raises(ValueError, match="no estimates"):
        cheapest([])


def test_describe_does_not_hide_the_number():
    estimate = CostEstimate(usd=0.0123, billable_bytes=2_500_000, query=QUERY)
    text = estimate.describe()
    assert "$0.0123" in text and "2.500 MB" in text and "mbo" in text
