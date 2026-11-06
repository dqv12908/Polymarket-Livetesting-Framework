"""Example: trade a model trained with train_lightgbm.py, using the same features it learned from."""

from __future__ import annotations

import math

import lightgbm as lgb
import numpy as np

from polymarket_livetesting import MarketView, Order, Strategy
from polymarket_livetesting.dataset import features


class LightGBMStrategy(Strategy):
    name = "lightgbm"
    series = ("btc-updown-5m",)
    tick_seconds = 5.0

    def __init__(self, settings=None) -> None:
        super().__init__(settings)
        self.model = lgb.Booster(model_file=self.settings.get("model", "model.txt"))
        self.names = self.model.feature_name()
        self.min_edge = float(self.settings.get("min_edge", 0.03))

    def on_tick(self, market: MarketView) -> Order | None:
        row = features(market)
        mid = row["mid"]
        if mid is None or not 0.03 <= mid <= 0.97:
            return None
        x = np.array([[np.nan if row.get(n) is None else row[n] for n in self.names]], dtype=float)
        clipped = min(max(mid, 0.005), 0.995)
        raw = float(self.model.predict(x, raw_score=True)[0]) + math.log(clipped / (1 - clipped))
        chance = 1 / (1 + math.exp(-raw))
        a, b = market.outcomes
        for side, p in ((a, chance), (b, 1 - chance)):
            ask = market.best_ask(side)
            if ask is not None and p - ask - 0.07 * ask * (1 - ask) >= self.min_edge:
                return Order(side, probability=p)
        return None
