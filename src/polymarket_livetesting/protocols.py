from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import BinaryMarket, BookSnapshot, PaperSignal


@runtime_checkable
class MarketData(Protocol):
    """Read-only market-data boundary required by the execution engine."""

    def discover(self, slug: str, refresh: bool = False) -> BinaryMarket: ...

    def book(self, token_id: str, expected_condition: str) -> BookSnapshot: ...

    def resolved_outcome(
        self, market_id: str, expected_slug: str, expected_condition: str
    ) -> str | None: ...


@runtime_checkable
class StrategyPlugin(Protocol):
    """A strategy owns its model and data; the framework owns paper execution."""

    def next_signal(self, now_ns: int) -> PaperSignal | None: ...
