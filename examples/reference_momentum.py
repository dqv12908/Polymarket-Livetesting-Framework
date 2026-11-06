"""Example: use an outside price to pick a side. This shows the API; it is not a recommendation.

    pm-live demo --strategy examples/reference_momentum.py
    pm-live backtest examples/reference_momentum.py --data data
    pm-live paper examples/reference_momentum.py
"""

from __future__ import annotations

from polymarket_livetesting import MarketView, Order, Strategy


class ReferenceMomentum(Strategy):
    name = "reference_momentum"
    series = ("btc-updown-5m", "eth-updown-5m")
    tick_seconds = 1.0

    def on_tick(self, market: MarketView) -> Order | None:
        if not 60 <= market.seconds_left <= 180:
            return None
        move = market.reference_return_bp(float(self.settings.get("lookback_seconds", 30)))
        threshold = float(self.settings.get("threshold_bp", 5))
        if move is None or abs(move) < threshold:
            return None
        side = "up" if move > 0 else "down"
        ask = market.best_ask(side)
        spread = market.spread(side)
        if ask is None or spread is None or spread > 0.03 or not 0.35 <= ask <= 0.75:
            return None
        return Order(side, note={"move_bp": round(move, 2)})
