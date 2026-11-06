"""Baseline: pick a side at random halfway through each market.

This measures what trading itself costs you. Its result should be about minus the fee and half
the spread. A strategy that does no better than this has no information.
"""

from __future__ import annotations

import hashlib

from polymarket_livetesting import MarketView, Order, Strategy


class RandomBaseline(Strategy):
    name = "random_baseline"
    tick_seconds = 1.0

    def on_tick(self, market: MarketView) -> Order | None:
        if market.seconds_in < (market.end - market.start) / 2:
            return None
        digest = hashlib.sha256(market.slug.encode()).digest()[0]
        side = market.outcomes[digest % 2]
        ask = market.best_ask(side)
        if ask is None or not 0.05 <= ask <= 0.95:
            return None
        return Order(side, probability=0.5)
