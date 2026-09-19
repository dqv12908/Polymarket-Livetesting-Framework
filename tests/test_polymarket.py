from __future__ import annotations

import time

import pytest

from polymarket_livetesting.polymarket import PolymarketData


class FakeTransport:
    def __init__(self) -> None:
        self.closed = False
        self.mismatch = False

    def get(self, url: str):
        market = {
            "id": "7",
            "slug": "exact-slug",
            "question": "Will the test pass?",
            "conditionId": "0xcondition",
            "clobTokenIds": '["11","22"]',
            "outcomes": '["Yes","No"]',
            "outcomePrices": '["1","0"]' if self.closed else '["0.5","0.5"]',
            "active": not self.closed,
            "closed": self.closed,
            "acceptingOrders": not self.closed,
            "feesEnabled": True,
            "endDate": "2030-01-01T00:00:00Z",
        }
        if "gamma-api" in url:
            return (market if "/markets/7" in url else [market]), 1.0
        if "clob-markets" in url:
            tokens = [{"t": "11", "o": "Yes"}, {"t": "99" if self.mismatch else "22", "o": "No"}]
            return {"t": tokens, "mos": 5, "mts": 0.01, "fd": {"r": 0.07}}, 1.0
        if "/book?" in url:
            return {
                "market": "0xcondition",
                "asset_id": "11",
                "timestamp": str(time.time_ns() // 1_000_000),
                "bids": [{"price": "0.49", "size": "100"}],
                "asks": [{"price": "0.55", "size": "10"}, {"price": "0.54", "size": "20"}],
            }, 2.0
        raise AssertionError(url)


def test_exact_market_book_and_resolution_identity() -> None:
    transport = FakeTransport()
    adapter = PolymarketData(transport)

    market = adapter.discover("exact-slug")
    book = adapter.book("11", market.condition_id)
    transport.closed = True
    outcome = adapter.resolved_outcome("7", "exact-slug", "0xcondition")

    assert market.tokens == {"yes": "11", "no": "22"}
    assert [level.price for level in book.asks] == [0.54, 0.55]
    assert outcome == "yes"


def test_gamma_and_clob_token_disagreement_fails_closed() -> None:
    transport = FakeTransport()
    transport.mismatch = True

    with pytest.raises(ValueError, match="token mappings disagree"):
        PolymarketData(transport).discover("exact-slug")


def test_fuzzy_or_missing_slug_is_never_selected() -> None:
    class WrongSlug(FakeTransport):
        def get(self, url: str):
            value, latency = super().get(url)
            if isinstance(value, list):
                value[0]["slug"] = "similar-but-wrong"
            return value, latency

    with pytest.raises(LookupError, match="exactly one"):
        PolymarketData(WrongSlug()).discover("exact-slug")
