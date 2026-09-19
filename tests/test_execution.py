from __future__ import annotations

import time

import pytest

from polymarket_livetesting import (
    ExecutionConfig,
    PaperExecutionEngine,
    PaperLedger,
    PaperSignal,
)
from polymarket_livetesting.demo import DemoMarketData
from polymarket_livetesting.fill import plan_fill, taker_fee


def engine(tmp_path, **changes):
    config = ExecutionConfig(route_latency_ms=0, settlement_grace_seconds=0, **changes)
    ledger = PaperLedger(tmp_path / "state.json", tmp_path / "events.jsonl", config)
    return PaperExecutionEngine(DemoMarketData(), ledger, config)


def signal(**changes) -> PaperSignal:
    now_ns = time.time_ns()
    values = {
        "signal_id": "test:1",
        "market_slug": DemoMarketData.slug,
        "outcome": "yes",
        "probability": 0.70,
        "emitted_at_ns": now_ns,
        "expected_at_ns": now_ns,
        "model": "test",
    }
    values.update(changes)
    return PaperSignal(**values)


def test_fee_and_fill_never_exceed_the_fixed_budget(tmp_path) -> None:
    instance = engine(tmp_path)
    market = instance.market_data.discover(DemoMarketData.slug)
    book = instance.market_data.book("demo-yes", "demo-condition")

    fill = plan_fill(book, "yes", 0.40, market.fee_rate, 200, 200, instance.config)

    assert taker_fee(100, 0.5, 0.07) == 1.75
    assert fill is not None
    assert 0 < fill.total_debit <= 20
    assert fill.expected_profit < 0  # Execution does not add an implied-edge filter.


def test_selected_signal_fills_and_validates_even_when_execution_is_independent(tmp_path) -> None:
    instance = engine(tmp_path)

    result = instance.execute(signal())
    settlements = instance.resolve(now_epoch=2)

    assert result.status == "filled"
    assert result.plan is not None and result.plan.fee > 0
    assert len(settlements["positions"]) == 1
    assert len(settlements["validations"]) == 1
    assert settlements["validations"][0]["correct"] is True
    assert instance.ledger.snapshot()["validation_accuracy"] == 1
    assert instance.ledger.cash > instance.config.starting_capital


def test_stale_signal_is_skipped_but_remains_in_validation_denominator(tmp_path) -> None:
    instance = engine(tmp_path, max_signal_age_ms=1)
    old = time.time_ns() - 2_000_000_000

    result = instance.execute(signal(emitted_at_ns=old, expected_at_ns=old))

    assert result.reason == "stale signal"
    assert instance.ledger.cash == instance.config.starting_capital
    assert instance.ledger.snapshot()["validation_pending"] == 1


def test_abstention_does_not_touch_market_or_validation(tmp_path) -> None:
    instance = engine(tmp_path)

    result = instance.execute(signal(signal_id="abstain", trade_intent=False))

    assert result.reason == "strategy abstained"
    assert instance.ledger.snapshot()["validation_pending"] == 0


def test_signal_and_market_are_deduplicated(tmp_path) -> None:
    instance = engine(tmp_path)
    first = instance.execute(signal())
    duplicate = instance.execute(signal())
    second_signal = instance.execute(signal(signal_id="test:2"))

    assert first.status == "filled"
    assert duplicate.reason == "duplicate signal"
    assert second_signal.reason == "market already traded"


def test_invalid_signal_values_fail_closed() -> None:
    with pytest.raises(ValueError, match="probability"):
        PaperSignal.from_dict(
            {
                "signal_id": "bad",
                "market_slug": "market",
                "outcome": "yes",
                "probability": float("nan"),
            }
        )

    with pytest.raises(ValueError, match="probability"):
        signal(probability=float("nan"))

    with pytest.raises(ValueError, match="max_book_age_ms"):
        ExecutionConfig(max_book_age_ms=float("nan"))
