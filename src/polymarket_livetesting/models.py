from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass, field
from itertools import pairwise
from typing import Any


def _finite(value: float, name: str) -> float:
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


@dataclass(frozen=True)
class BookLevel:
    price: float
    size: float

    def __post_init__(self) -> None:
        price = _finite(self.price, "price")
        size = _finite(self.size, "size")
        if not 0 < price < 1:
            raise ValueError("price must be strictly between zero and one")
        if size <= 0:
            raise ValueError("size must be positive")
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "size", size)


@dataclass(frozen=True)
class BookSnapshot:
    token_id: str
    condition_id: str
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    tick_size: float
    min_order_size: float
    exchange_timestamp_ms: int | None
    fetched_at_ns: int
    request_latency_ms: float

    def __post_init__(self) -> None:
        bids, asks = tuple(self.bids), tuple(self.asks)
        if not self.token_id or not self.condition_id:
            raise ValueError("book token and condition identities are required")
        if not all(isinstance(level, BookLevel) for level in (*bids, *asks)):
            raise TypeError("book levels must be BookLevel instances")
        if any(left.price < right.price for left, right in pairwise(bids)):
            raise ValueError("bids must be sorted from highest to lowest price")
        if any(left.price > right.price for left, right in pairwise(asks)):
            raise ValueError("asks must be sorted from lowest to highest price")
        tick_size = _finite(self.tick_size, "tick_size")
        min_order_size = _finite(self.min_order_size, "min_order_size")
        request_latency_ms = _finite(self.request_latency_ms, "request_latency_ms")
        if tick_size <= 0 or min_order_size <= 0 or request_latency_ms < 0:
            raise ValueError("invalid book execution parameters")
        if self.fetched_at_ns <= 0 or (
            self.exchange_timestamp_ms is not None and self.exchange_timestamp_ms <= 0
        ):
            raise ValueError("book timestamps must be positive")
        object.__setattr__(self, "bids", bids)
        object.__setattr__(self, "asks", asks)
        object.__setattr__(self, "tick_size", tick_size)
        object.__setattr__(self, "min_order_size", min_order_size)
        object.__setattr__(self, "request_latency_ms", request_latency_ms)

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def spread(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    @property
    def age_ms(self) -> float | None:
        if self.exchange_timestamp_ms is None:
            return None
        return self.fetched_at_ns / 1_000_000 - self.exchange_timestamp_ms


@dataclass(frozen=True)
class BinaryMarket:
    slug: str
    condition_id: str
    market_id: str
    question: str
    tokens: dict[str, str]
    active: bool
    closed: bool
    accepting_orders: bool
    end_epoch: int
    fee_rate: float
    tick_size: float
    min_order_size: float
    taker_delay_ms: int
    source: dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        normalized = {str(name).casefold(): str(token) for name, token in self.tokens.items()}
        fee_rate = _finite(self.fee_rate, "fee_rate")
        tick_size = _finite(self.tick_size, "tick_size")
        min_order_size = _finite(self.min_order_size, "min_order_size")
        if len(normalized) != 2 or len(set(normalized.values())) != 2:
            raise ValueError("a binary market requires two distinct outcomes and token IDs")
        if not self.slug or not self.condition_id or not self.market_id:
            raise ValueError("market slug, condition ID, and market ID are required")
        if self.end_epoch <= 0:
            raise ValueError("market end time must be positive")
        if not 0 <= fee_rate <= 1:
            raise ValueError("fee_rate must be between zero and one")
        if tick_size <= 0 or min_order_size <= 0 or self.taker_delay_ms < 0:
            raise ValueError("invalid market execution parameters")
        object.__setattr__(self, "tokens", normalized)
        object.__setattr__(self, "fee_rate", fee_rate)
        object.__setattr__(self, "tick_size", tick_size)
        object.__setattr__(self, "min_order_size", min_order_size)


@dataclass(frozen=True)
class PaperSignal:
    signal_id: str
    market_slug: str
    outcome: str
    probability: float
    emitted_at_ns: int
    model: str
    expected_at_ns: int | None = None
    trade_intent: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        probability = _finite(self.probability, "probability")
        if not 0 <= probability <= 1:
            raise ValueError("probability must be between zero and one")
        if not self.signal_id or not self.market_slug or not self.outcome:
            raise ValueError("signal_id, market_slug, and outcome are required")
        if self.emitted_at_ns <= 0 or (
            self.expected_at_ns is not None and self.expected_at_ns <= 0
        ):
            raise ValueError("signal timestamps must be positive")
        if not isinstance(self.metadata, dict):
            raise TypeError("signal metadata must be a dictionary")
        object.__setattr__(self, "outcome", self.outcome.casefold())
        object.__setattr__(self, "probability", probability)
        object.__setattr__(self, "metadata", dict(self.metadata))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> PaperSignal:
        emitted_at_ns = int(value.get("emitted_at_ns") or time.time_ns())
        expected = value.get("expected_at_ns")
        return cls(
            signal_id=str(value["signal_id"]),
            market_slug=str(value["market_slug"]),
            outcome=str(value["outcome"]),
            probability=float(value["probability"]),
            emitted_at_ns=emitted_at_ns,
            expected_at_ns=int(expected) if expected is not None else None,
            model=str(value.get("model") or "unknown"),
            trade_intent=bool(value.get("trade_intent", True)),
            metadata=dict(value.get("metadata") or {}),
        )


@dataclass(frozen=True)
class ExecutionConfig:
    starting_capital: float = 200.0
    max_capital_fraction: float = 0.10
    max_trade_usd: float = 20.0
    max_price_slippage: float = 0.02
    depth_haircut: float = 0.90
    max_book_age_ms: float = 2_500.0
    route_latency_ms: int = 80
    max_signal_age_ms: float = 5_000.0
    signal_timing_tolerance_ms: float = 1_500.0
    settlement_grace_seconds: int = 30

    def __post_init__(self) -> None:
        for name in (
            "starting_capital",
            "max_capital_fraction",
            "max_trade_usd",
            "max_price_slippage",
            "depth_haircut",
            "max_book_age_ms",
            "max_signal_age_ms",
            "signal_timing_tolerance_ms",
        ):
            object.__setattr__(self, name, _finite(getattr(self, name), name))
        route_latency_ms = int(self.route_latency_ms)
        settlement_grace_seconds = int(self.settlement_grace_seconds)
        if (
            route_latency_ms != self.route_latency_ms
            or settlement_grace_seconds != self.settlement_grace_seconds
        ):
            raise ValueError("integer timing limits must be whole numbers")
        object.__setattr__(self, "route_latency_ms", route_latency_ms)
        object.__setattr__(self, "settlement_grace_seconds", settlement_grace_seconds)
        if self.starting_capital <= 0 or self.max_trade_usd <= 0:
            raise ValueError("capital and trade budget must be positive")
        if not 0 < self.max_capital_fraction <= 1:
            raise ValueError("max_capital_fraction must be in (0, 1]")
        if not 0 <= self.max_price_slippage <= 1:
            raise ValueError("max_price_slippage must be in [0, 1]")
        if not 0 < self.depth_haircut <= 1:
            raise ValueError("depth_haircut must be in (0, 1]")
        if (
            min(
                self.max_book_age_ms,
                self.route_latency_ms,
                self.max_signal_age_ms,
                self.signal_timing_tolerance_ms,
                self.settlement_grace_seconds,
            )
            < 0
        ):
            raise ValueError("timing limits cannot be negative")


@dataclass(frozen=True)
class PlannedLevel:
    price: float
    shares: float
    cost: float
    fee: float


@dataclass(frozen=True)
class FillPlan:
    outcome: str
    token_id: str
    probability: float
    levels: tuple[PlannedLevel, ...]
    shares: float
    cost: float
    fee: float
    total_debit: float
    vwap: float
    break_even_probability: float
    expected_profit: float
    expected_roi: float
    win_profit: float
    loss: float
    budget: float


@dataclass(frozen=True)
class ExecutionResult:
    status: str
    reason: str
    signal: PaperSignal
    market: BinaryMarket | None = None
    book: BookSnapshot | None = None
    plan: FillPlan | None = None
    timing: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        if value.get("market"):
            value["market"].pop("source", None)
        return value
