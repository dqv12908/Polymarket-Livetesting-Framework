"""Synthetic market data, so the whole workflow can be tried offline in seconds.

The market here is fair by construction apart from a short reaction delay. It says nothing about
real markets. Every report built from it carries a banner saying so.
"""

from __future__ import annotations

import math
import random
from pathlib import Path

from .tape import TapeWriter

BASE_EPOCH = 1_767_225_600  # 2026-01-01 00:00 UTC, a fixed date so runs are repeatable
NS = 1_000_000_000
VOL_BP_PER_SECOND = {"btc": 0.8, "eth": 1.1}
START_PRICE = {"btc": 80_000.0, "eth": 3_000.0}


def _phi(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2))


def _levels(rng: random.Random, best: float, sign: int) -> list[dict[str, str]]:
    levels = []
    for i in range(5):
        price = round(best + sign * i * 0.01, 2)
        if 0 < price < 1:
            levels.append({"price": f"{price:.2f}", "size": f"{rng.lognormvariate(4.0, 0.8):.2f}"})
    return levels


def generate(directory: Path, hours: float = 6.0, seed: int = 7, interval: int = 300) -> dict:
    """Write a tape of ``hours`` of 5-minute Up/Down markets on two assets."""
    rng = random.Random(seed)
    writer = TapeWriter(Path(directory), synthetic=True)
    prices = dict(START_PRICE)
    markets = 0
    events: list[tuple[int, str, dict]] = []
    windows = int(hours * 3600 / interval)
    for w in range(windows):
        start = BASE_EPOCH + w * interval
        for asset, vol in VOL_BP_PER_SECOND.items():
            slug = f"{asset}-updown-5m-{start}"
            up, down = f"{slug}-up", f"{slug}-down"
            info = {
                "slug": slug,
                "series": f"{asset}-updown-5m",
                "condition_id": f"synthetic-{slug}",
                "market_id": f"synthetic-{slug}",
                "question": f"Synthetic {asset} market",
                "tokens": {"up": up, "down": down},
                "start": start,
                "end": start + interval,
                "fee_rate": 0.07,
                "tick_size": 0.01,
                "min_order_size": 1.0,
                "taker_delay_ms": 0,
            }
            events.append(((start - 30) * NS, "market", info))
            strike = prices[asset]
            price = strike
            seen_fair = 0.5
            for s in range(-20, interval):
                t = start + s
                if s >= 0:
                    price *= math.exp(rng.gauss(0, vol / 1e4))
                    left = max(interval - s, 1)
                    fair = _phi(math.log(price / strike) / (vol / 1e4 * math.sqrt(left)))
                    # the book follows the true chance with a short delay, plus some noise
                    seen_fair += 0.6 * (fair - seen_fair)
                mid = min(max(seen_fair + rng.gauss(0, 0.004), 0.02), 0.98)
                half = 0.01 if rng.random() < 0.7 else 0.02
                bid = round(mid - half, 2)
                ask = max(round(mid + half, 2), round(bid + 0.01, 2))
                lag = rng.uniform(60, 180)
                t_ns = int((t + rng.random() * 0.9) * NS)
                stamp = str(int(t_ns / 1e6 - lag))
                events.append(
                    (t_ns, "pm", {
                        "event_type": "book",
                        "asset_id": up,
                        "timestamp": stamp,
                        "bids": _levels(rng, bid, -1),
                        "asks": _levels(rng, ask, 1),
                    })
                )
                events.append(
                    (t_ns + 1, "pm", {
                        "event_type": "book",
                        "asset_id": down,
                        "timestamp": stamp,
                        "bids": _levels(rng, round(1 - ask, 2), -1),
                        "asks": _levels(rng, round(1 - bid, 2), 1),
                    })
                )
                events.append(
                    (t_ns + 2, "ref", {
                        "symbol": f"{asset}usdt",
                        "bid": price * 0.99999,
                        "ask": price * 1.00001,
                    })
                )
                if s >= 0 and rng.random() < 0.3:
                    events.append(
                        (t_ns + 3, "pm", {
                            "event_type": "last_trade_price",
                            "asset_id": up,
                            "timestamp": stamp,
                            "price": f"{ask:.2f}",
                            "size": f"{rng.lognormvariate(2.5, 1.0):.2f}",
                            "side": "BUY" if rng.random() < 0.5 else "SELL",
                        })
                    )
            prices[asset] = price
            winner = "up" if price >= strike else "down"
            events.append(((start + interval + 60) * NS, "result", {"slug": slug, "winner": winner}))
            markets += 1
    events.sort(key=lambda item: item[0])
    for t_ns, kind, payload in events:
        writer.write(t_ns, kind, payload)
    writer.close()
    return {"markets": markets, "events": writer.events}
