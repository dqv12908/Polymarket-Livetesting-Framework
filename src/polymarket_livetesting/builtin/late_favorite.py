"""Baseline: buy whichever side is ahead late in the window, with no model at all.

A useful yardstick because it is simple and sounds clever. If your idea cannot beat this after
fees and delay, the idea is not adding anything.
"""

from __future__ import annotations

from polymarket_livetesting import MarketView, Order, Strategy


class LateFavorite(Strategy):
    name = "late_favorite"
    tick_seconds = 1.0

    def on_tick(self, market: MarketView) -> Order | None:
        if market.seconds_left > float(self.settings.get("seconds_left", 30)):
            return None
        side = market.favourite()
        ask = market.best_ask(side)
        spread = market.spread(side)
        if ask is None or spread is None or spread > float(self.settings.get("max_spread", 0.03)):
            return None
        if not float(self.settings.get("min_ask", 0.60)) <= ask <= float(
            self.settings.get("max_ask", 0.95)
        ):
            return None
        return Order(side)
