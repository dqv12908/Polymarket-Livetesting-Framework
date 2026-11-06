"""Export recorded data as a table for training your own models.

Each row is one market at one moment. Features use only data received before that moment; the
last columns are the labels, which your model must never see as inputs.
"""

from __future__ import annotations

import csv
import gzip
from pathlib import Path
from typing import Any

from .models import ExecutionConfig
from .session import MemoryLedger, Session
from .state import MarketView
from .strategy import Order, Strategy
from .tape import read_tape, scan_metadata

BACK = (1, 5, 15, 30, 60, 120)
REF_BACK = (1, 5, 15, 30, 60, 120, 300)
FLOW = (5, 15, 30, 60)


def features(m: MarketView) -> dict[str, Any]:
    """The first outcome listed by the market is the row's point of view (for example, up)."""
    a = m.outcomes[0]
    row: dict[str, Any] = {
        "seconds_in": round(m.seconds_in, 2),
        "seconds_left": round(m.seconds_left, 2),
        "bid": m.best_bid(a),
        "ask": m.best_ask(a),
        "mid": m.mid(a),
        "spread": m.spread(a),
        "depth_bid_2c": m.depth(a, "bid", 0.02),
        "depth_ask_2c": m.depth(a, "ask", 0.02),
        "depth_bid_5c": m.depth(a, "bid", 0.05),
        "depth_ask_5c": m.depth(a, "ask", 0.05),
    }
    for k in BACK:
        row[f"mid_change_{k}s"] = m.mid_change(a, k)
    for k in FLOW:
        row[f"flow_{k}s"] = m.flow(a, k) - m.flow(m.outcomes[1], k)
    for k in REF_BACK:
        row[f"ref_return_{k}s_bp"] = m.reference_return_bp(k)
    return row


class _Collector(Strategy):
    name = "dataset"
    tick_seconds = 0.25

    def __init__(self, every: float, entry_delay: float, winners: dict[str, str]) -> None:
        super().__init__({})
        self.every = every
        self.entry_delay = entry_delay
        self.winners = winners
        self.rows: list[dict[str, Any]] = []
        self.next_row: dict[str, float] = {}
        self.waiting: list[tuple[float, dict[str, Any]]] = []

    def on_tick(self, market: MarketView) -> Order | None:
        now = market.now
        for item in [w for w in self.waiting if w[1]["slug"] == market.slug and w[0] <= now]:
            a, b = market.outcomes
            item[1]["label_ask_a_after_delay"] = market.best_ask(a)
            item[1]["label_ask_b_after_delay"] = market.best_ask(b)
            self.waiting.remove(item)
        if now < self.next_row.get(market.slug, 0.0):
            return None
        self.next_row[market.slug] = now + self.every
        winner = self.winners.get(market.slug)
        if winner is None:
            return None
        row = {"slug": market.slug, "series": market.series, "t": round(now, 3), "outcome_a": market.outcomes[0]}
        row.update(features(market))
        row["label_a_won"] = 1 if winner == market.outcomes[0] else 0
        row["label_ask_a_after_delay"] = row["label_ask_b_after_delay"] = None
        self.rows.append(row)
        self.waiting.append((now + self.entry_delay, row))
        return None


def export(
    data: Path,
    out: Path,
    every_seconds: float = 5.0,
    entry_delay_seconds: float = 0.45,
    series: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    meta = scan_metadata(data)
    collector = _Collector(every_seconds, entry_delay_seconds, meta["winners"])
    config = ExecutionConfig()
    session = Session(collector, MemoryLedger(config), config, 0.0, winners=meta["winners"], series=series)
    for event in read_tape(data):
        session.handle(event)
    session.finish()
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = collector.rows
    if not rows:
        raise ValueError("no rows: the data has no markets with a recorded result yet")
    columns = list(rows[0])
    opener = gzip.open if out.suffix == ".gz" else open
    with opener(out, "wt", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, columns)
        writer.writeheader()
        writer.writerows(rows)
    return {
        "rows": len(rows),
        "markets": len({r["slug"] for r in rows}),
        "path": str(out),
        "synthetic": meta["synthetic"],
    }
