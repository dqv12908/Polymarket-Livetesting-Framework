from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from .models import BookSnapshot, ExecutionConfig, FillPlan, PlannedLevel


def taker_fee(shares: float, price: float, fee_rate: float) -> float:
    """Polymarket crypto taker fee rounded to five decimal places."""
    raw = (
        Decimal(str(shares))
        * Decimal(str(fee_rate))
        * Decimal(str(price))
        * (Decimal(1) - Decimal(str(price)))
    )
    if raw < Decimal("0.000005"):
        return 0.0
    return float(raw.quantize(Decimal("0.00001"), rounding=ROUND_HALF_UP))


def affordable_shares(price: float, fee_rate: float, available: float, budget: float) -> float:
    """Largest displayed quantity whose rounded total debit stays within budget."""
    low = 0.0
    high = min(max(0.0, available), max(0.0, budget) / max(price, 1e-12))
    for _ in range(64):
        middle = (low + high) / 2
        debit = middle * price + taker_fee(middle, price, fee_rate)
        if debit <= budget:
            low = middle
        else:
            high = middle
    return low


def validate_book(book: BookSnapshot, config: ExecutionConfig) -> str | None:
    if not book.asks:
        return "empty ask book"
    if book.best_bid is not None and book.best_ask is not None and book.best_bid >= book.best_ask:
        return "crossed order book"
    if book.age_ms is not None and (book.age_ms < -2_000 or book.age_ms > config.max_book_age_ms):
        return f"stale order book ({book.age_ms:.0f}ms)"
    return None


def plan_fill(
    book: BookSnapshot,
    outcome: str,
    probability: float,
    fee_rate: float,
    cash: float,
    equity: float,
    config: ExecutionConfig,
) -> FillPlan | None:
    """Plan an IOC-style depth sweep without inventing unavailable liquidity."""
    if not book.asks or cash <= 0:
        return None
    best_price = book.asks[0].price
    budget = min(cash, config.max_trade_usd, equity * config.max_capital_fraction)
    if budget < 1:
        return None
    max_price = min(1.0, best_price + config.max_price_slippage)
    remaining = budget
    planned: list[PlannedLevel] = []
    for level in book.asks:
        if level.price > max_price + 1e-12:
            break
        available = max(0.0, level.size * config.depth_haircut)
        shares = affordable_shares(level.price, fee_rate, available, remaining)
        if shares <= 1e-9:
            continue
        cost = shares * level.price
        fee = taker_fee(shares, level.price, fee_rate)
        planned.append(PlannedLevel(level.price, shares, cost, fee))
        remaining -= cost + fee
        if remaining < 0.01:
            break
    shares = sum(level.shares for level in planned)
    cost = sum(level.cost for level in planned)
    fee = sum(level.fee for level in planned)
    total = cost + fee
    minimum_size = max(book.min_order_size, 1.0 / max(best_price, 1e-9))
    if shares + 1e-9 < minimum_size or total < 1:
        return None
    expected_profit = probability * shares - total
    return FillPlan(
        outcome=outcome,
        token_id=book.token_id,
        probability=probability,
        levels=tuple(planned),
        shares=shares,
        cost=cost,
        fee=fee,
        total_debit=total,
        vwap=cost / shares,
        break_even_probability=total / shares,
        expected_profit=expected_profit,
        expected_roi=expected_profit / total,
        win_profit=shares - total,
        loss=-total,
        budget=budget,
    )
