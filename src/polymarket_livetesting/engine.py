from __future__ import annotations

import math
import threading
import time
from typing import Any

from .fill import plan_fill, validate_book
from .ledger import PaperLedger
from .models import ExecutionConfig, ExecutionResult, PaperSignal
from .protocols import MarketData


class PaperExecutionEngine:
    """Execute strategy-selected signals against delayed public order-book data."""

    def __init__(
        self,
        market_data: MarketData,
        ledger: PaperLedger,
        config: ExecutionConfig,
    ) -> None:
        self.market_data = market_data
        self.ledger = ledger
        self.config = config
        self._lock = threading.RLock()

    def resolve(self, now_epoch: int | None = None) -> dict[str, list[dict[str, Any]]]:
        now_epoch = int(time.time()) if now_epoch is None else now_epoch
        positions: list[dict[str, Any]] = []
        validations: list[dict[str, Any]] = []
        with self._lock:
            for condition_id, position in list(self.ledger.state.get("positions", {}).items()):
                if now_epoch < int(position["end_epoch"]) + self.config.settlement_grace_seconds:
                    continue
                outcome = self.market_data.resolved_outcome(
                    str(position["market_id"]),
                    str(position["slug"]),
                    str(position["condition_id"]),
                )
                if outcome:
                    positions.append(self.ledger.settle(condition_id, outcome))
            pending = self.ledger.state.setdefault("validation_pending", {})
            for signal_id, item in list(pending.items()):
                if not item.get("market_id"):
                    market = self.market_data.discover(str(item["market_slug"]))
                    self.ledger.attach_validation_market(signal_id, market)
                    item = pending[signal_id]
                if now_epoch < int(item["end_epoch"]) + self.config.settlement_grace_seconds:
                    continue
                outcome = self.market_data.resolved_outcome(
                    str(item["market_id"]),
                    str(item["market_slug"]),
                    str(item["condition_id"]),
                )
                if outcome:
                    validations.append(self.ledger.settle_validation(signal_id, outcome))
        return {"positions": positions, "validations": validations}

    def execute(self, signal: PaperSignal, allow_stale: bool = False) -> ExecutionResult:
        with self._lock:
            return self._execute(signal, allow_stale)

    def _execute(self, signal: PaperSignal, allow_stale: bool) -> ExecutionResult:
        started_ns = time.time_ns()
        if self.ledger.has_signal(signal.signal_id):
            return ExecutionResult("skipped", "duplicate signal", signal)
        if not signal.trade_intent:
            result = ExecutionResult("skipped", "strategy abstained", signal)
            self.ledger.record_result(result)
            return result

        self.ledger.register_validation(signal)
        timing_error_ms = (
            (signal.emitted_at_ns - signal.expected_at_ns) / 1_000_000
            if signal.expected_at_ns is not None
            else 0.0
        )
        age_ms = (started_ns - signal.emitted_at_ns) / 1_000_000
        if (
            not allow_stale
            and signal.expected_at_ns is not None
            and abs(timing_error_ms) > self.config.signal_timing_tolerance_ms
        ):
            result = ExecutionResult("skipped", "signal missed its expected decision time", signal)
            self.ledger.record_result(result)
            return result
        if not allow_stale and age_ms > self.config.max_signal_age_ms:
            result = ExecutionResult("skipped", "stale signal", signal)
            self.ledger.record_result(result)
            return result
        if not allow_stale and age_ms < -250:
            result = ExecutionResult("skipped", "signal timestamp is in the future", signal)
            self.ledger.record_result(result)
            return result

        discovery_started = time.perf_counter_ns()
        try:
            market = self.market_data.discover(signal.market_slug)
        except (
            ConnectionError,
            LookupError,
            OSError,
            TimeoutError,
            TypeError,
            ValueError,
        ) as error:
            result = ExecutionResult(
                "skipped", f"market discovery failed: {type(error).__name__}", signal
            )
            self.ledger.record_result(result)
            return result
        discovery_ms = (time.perf_counter_ns() - discovery_started) / 1_000_000
        self.ledger.attach_validation_market(signal.signal_id, market)
        if signal.outcome not in market.tokens:
            result = ExecutionResult(
                "skipped", "signal outcome is not in the market", signal, market
            )
            self.ledger.record_result(result)
            return result
        if not market.active or market.closed or not market.accepting_orders:
            result = ExecutionResult("skipped", "market is not accepting orders", signal, market)
            self.ledger.record_result(result)
            return result
        if self.ledger.has_market(market.condition_id):
            result = ExecutionResult("skipped", "market already traded", signal, market)
            self.ledger.record_result(result)
            return result

        token_id = market.tokens[signal.outcome]
        delay_ms = self.config.route_latency_ms + market.taker_delay_ms
        elapsed_ms = (time.time_ns() - started_ns) / 1_000_000
        if delay_ms > elapsed_ms:
            time.sleep((delay_ms - elapsed_ms) / 1_000)
        try:
            book = self.market_data.book(token_id, market.condition_id)
        except (
            ConnectionError,
            LookupError,
            OSError,
            TimeoutError,
            TypeError,
            ValueError,
        ) as error:
            result = ExecutionResult(
                "skipped",
                f"order book fetch failed: {type(error).__name__}",
                signal,
                market,
            )
            self.ledger.record_result(result)
            return result

        timing = {
            "signal_age_ms_at_start": age_ms,
            "signal_timing_error_ms": timing_error_ms,
            "market_discovery_ms": discovery_ms,
            "configured_route_and_taker_delay_ms": float(delay_ms),
            "book_request_ms": book.request_latency_ms,
            "decision_to_snapshot_ms": (book.fetched_at_ns - signal.emitted_at_ns) / 1_000_000,
            "book_age_ms": book.age_ms if book.age_ms is not None else math.nan,
        }
        invalid = validate_book(book, self.config)
        if invalid:
            result = ExecutionResult("skipped", invalid, signal, market, book, timing=timing)
            self.ledger.record_result(result)
            return result
        plan = plan_fill(
            book,
            signal.outcome,
            signal.probability,
            market.fee_rate,
            self.ledger.cash,
            self.ledger.equity_at_cost,
            self.config,
        )
        if plan is None:
            result = ExecutionResult(
                "skipped",
                "insufficient executable IOC liquidity",
                signal,
                market,
                book,
                timing=timing,
            )
            self.ledger.record_result(result)
            return result
        result = ExecutionResult("filled", "paper IOC fill", signal, market, book, plan, timing)
        self.ledger.record_result(result)
        return result
