from __future__ import annotations

import time

from .models import BinaryMarket, BookLevel, BookSnapshot, PaperSignal


class DemoMarketData:
    """Deterministic in-memory adapter used only by the smoke command and tests."""

    slug = "demo-binary-market"

    def discover(self, slug: str, refresh: bool = False) -> BinaryMarket:
        del refresh
        if slug != self.slug:
            raise LookupError(slug)
        return BinaryMarket(
            slug=self.slug,
            condition_id="demo-condition",
            market_id="demo-market",
            question="Will the deterministic smoke scenario pass?",
            tokens={"yes": "demo-yes", "no": "demo-no"},
            active=True,
            closed=False,
            accepting_orders=True,
            end_epoch=1,
            fee_rate=0.07,
            tick_size=0.01,
            min_order_size=1,
            taker_delay_ms=0,
        )

    def book(self, token_id: str, expected_condition: str) -> BookSnapshot:
        if token_id != "demo-yes" or expected_condition != "demo-condition":
            raise ValueError("unexpected demo book identity")
        now_ns = time.time_ns()
        return BookSnapshot(
            token_id=token_id,
            condition_id=expected_condition,
            bids=(BookLevel(0.49, 100),),
            asks=(BookLevel(0.50, 20), BookLevel(0.51, 20)),
            tick_size=0.01,
            min_order_size=1,
            exchange_timestamp_ms=now_ns // 1_000_000,
            fetched_at_ns=now_ns,
            request_latency_ms=1,
        )

    def resolved_outcome(
        self, market_id: str, expected_slug: str, expected_condition: str
    ) -> str | None:
        if (market_id, expected_slug, expected_condition) != (
            "demo-market",
            self.slug,
            "demo-condition",
        ):
            raise ValueError("unexpected demo settlement identity")
        return "yes"


class DemoStrategy:
    def __init__(self) -> None:
        self.emitted = False

    def next_signal(self, now_ns: int) -> PaperSignal | None:
        if self.emitted:
            return None
        self.emitted = True
        return PaperSignal(
            signal_id="demo:1",
            market_slug=DemoMarketData.slug,
            outcome="yes",
            probability=0.70,
            emitted_at_ns=now_ns,
            expected_at_ns=now_ns,
            model="deterministic-smoke",
        )
