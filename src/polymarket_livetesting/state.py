"""Live view of markets built from raw events. Backtests and live paper tests share this code."""

from __future__ import annotations

import bisect
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .models import BinaryMarket, BookLevel, BookSnapshot


@dataclass
class TokenBook:
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    best_bid: float | None = None
    best_ask: float | None = None
    history_t: list[float] = field(default_factory=list)
    history_mid: list[float] = field(default_factory=list)
    trades: deque[tuple[float, float, float, bool]] = field(
        default_factory=lambda: deque(maxlen=5000)
    )
    seen_trades: set[tuple[Any, ...]] = field(default_factory=set)

    def update_best(self, best_bid: float | None, best_ask: float | None, now: float) -> None:
        if best_bid == self.best_bid and best_ask == self.best_ask:
            return
        self.best_bid, self.best_ask = best_bid, best_ask
        if best_bid is not None and best_ask is not None:
            self.history_t.append(now)
            self.history_mid.append((best_bid + best_ask) / 2)


def market_from_dict(value: dict[str, Any]) -> BinaryMarket:
    return BinaryMarket(
        slug=value["slug"],
        condition_id=value["condition_id"],
        market_id=str(value["market_id"]),
        question=value.get("question", ""),
        tokens=value["tokens"],
        active=True,
        closed=False,
        accepting_orders=True,
        end_epoch=int(value["end"]),
        fee_rate=float(value.get("fee_rate", 0.07)),
        tick_size=float(value.get("tick_size", 0.01)),
        min_order_size=float(value.get("min_order_size", 1.0)),
        taker_delay_ms=int(value.get("taker_delay_ms", 0)),
    )


class MarketState:
    """Everything known about one market at the current moment."""

    def __init__(self, info: dict[str, Any]) -> None:
        self.info = info
        self.slug: str = info["slug"]
        self.series: str = info.get("series") or ""
        self.start: float = float(info.get("start") or 0)
        self.end: float = float(info["end"])
        self.market = market_from_dict(info)
        self.books = {outcome: TokenBook() for outcome in self.market.tokens}
        self.token_outcome = {token: name for name, token in self.market.tokens.items()}

    def apply_change(self, change: dict[str, Any], now: float) -> None:
        outcome = self.token_outcome.get(change.get("asset_id"))
        if outcome is None:
            return
        book = self.books[outcome]
        levels = book.bids if change.get("side") == "BUY" else book.asks
        price, size = float(change["price"]), float(change["size"])
        if size > 0:
            levels[price] = size
        else:
            levels.pop(price, None)
        best_bid, best_ask = change.get("best_bid"), change.get("best_ask")
        if best_bid is not None and best_ask is not None:
            book.update_best(float(best_bid), float(best_ask), now)

    def apply_book(self, message: dict[str, Any], now: float) -> None:
        outcome = self.token_outcome.get(message.get("asset_id"))
        if outcome is None:
            return
        book = self.books[outcome]
        book.bids = {float(x["price"]): float(x["size"]) for x in message.get("bids", ())}
        book.asks = {float(x["price"]): float(x["size"]) for x in message.get("asks", ())}
        book.update_best(max(book.bids, default=None), min(book.asks, default=None), now)

    def apply_trade(self, message: dict[str, Any], now: float) -> None:
        outcome = self.token_outcome.get(message.get("asset_id"))
        if outcome is None:
            return
        book = self.books[outcome]
        key = (
            message.get("timestamp"),
            message.get("price"),
            message.get("size"),
            message.get("transaction_hash"),
        )
        if key in book.seen_trades:  # overlapping connections repeat a message
            return
        book.seen_trades.add(key)
        book.trades.append(
            (now, float(message["price"]), float(message["size"]), message.get("side") == "BUY")
        )

    def snapshot(self, outcome: str, now_ns: int) -> BookSnapshot:
        book = self.books[outcome]

        def levels(side: dict[float, float], reverse: bool, best: float | None) -> tuple[BookLevel, ...]:
            # the exchange's reported best price wins over any stale level kept locally
            return tuple(
                BookLevel(price, size)
                for price, size in sorted(side.items(), reverse=reverse)
                if 0 < price < 1 and size > 0
                and (best is None or (price <= best if reverse else price >= best))
            )

        return BookSnapshot(
            token_id=self.market.tokens[outcome],
            condition_id=self.market.condition_id,
            bids=levels(book.bids, True, book.best_bid),
            asks=levels(book.asks, False, book.best_ask),
            tick_size=self.market.tick_size,
            min_order_size=self.market.min_order_size,
            exchange_timestamp_ms=None,
            fetched_at_ns=now_ns,
            request_latency_ms=0.0,
        )


class ReferenceFeed:
    """Most recent reference price per symbol, with a short history."""

    def __init__(self) -> None:
        self.t: dict[str, list[float]] = {}
        self.mid: dict[str, list[float]] = {}

    def apply(self, message: dict[str, Any], now: float) -> None:
        symbol = str(message["symbol"]).casefold()
        times = self.t.setdefault(symbol, [])
        mids = self.mid.setdefault(symbol, [])
        mid = (float(message["bid"]) + float(message["ask"])) / 2
        if times and now - times[-1] < 0.1:
            mids[-1] = mid
            return
        times.append(now)
        mids.append(mid)
        if len(times) > 40000:  # about an hour at the sampling limit
            del times[:20000], mids[:20000]

    def price(self, symbol: str, seconds_ago: float, now: float) -> float | None:
        times = self.t.get(symbol.casefold())
        if not times:
            return None
        index = bisect.bisect_right(times, now - seconds_ago) - 1
        return self.mid[symbol.casefold()][index] if index >= 0 else None


class MarketView:
    """What a strategy may see. It only exposes data received before the current moment."""

    def __init__(self, state: MarketState, reference: ReferenceFeed, now: float) -> None:
        self._s = state
        self._reference = reference
        self.now = now

    @property
    def slug(self) -> str:
        return self._s.slug

    @property
    def series(self) -> str:
        return self._s.series

    @property
    def asset(self) -> str:
        """First part of the slug, for example ``btc`` in ``btc-updown-5m-1790000000``."""
        return self._s.slug.split("-", 1)[0]

    @property
    def outcomes(self) -> tuple[str, ...]:
        return tuple(self._s.books)

    @property
    def start(self) -> float:
        return self._s.start

    @property
    def end(self) -> float:
        return self._s.end

    @property
    def seconds_in(self) -> float:
        return self.now - self._s.start

    @property
    def seconds_left(self) -> float:
        return self._s.end - self.now

    def best_bid(self, outcome: str) -> float | None:
        return self._s.books[outcome.casefold()].best_bid

    def best_ask(self, outcome: str) -> float | None:
        return self._s.books[outcome.casefold()].best_ask

    def mid(self, outcome: str) -> float | None:
        bid, ask = self.best_bid(outcome), self.best_ask(outcome)
        return (bid + ask) / 2 if bid is not None and ask is not None else None

    def spread(self, outcome: str) -> float | None:
        bid, ask = self.best_bid(outcome), self.best_ask(outcome)
        return ask - bid if bid is not None and ask is not None else None

    def favourite(self) -> str | None:
        """The outcome with the highest mid price, or None before books are available."""
        known = {name: self.mid(name) for name in self.outcomes}
        known = {name: value for name, value in known.items() if value is not None}
        return max(known, key=known.get) if known else None

    def depth(self, outcome: str, side: str = "ask", within: float = 0.02) -> float:
        """Shares displayed within ``within`` of the best price on one side of the book."""
        book = self._s.books[outcome.casefold()]
        if side == "ask":
            if book.best_ask is None:
                return 0.0
            return sum(q for p, q in book.asks.items() if p <= book.best_ask + within + 1e-9)
        if book.best_bid is None:
            return 0.0
        return sum(q for p, q in book.bids.items() if p >= book.best_bid - within - 1e-9)

    def mid_ago(self, outcome: str, seconds: float) -> float | None:
        book = self._s.books[outcome.casefold()]
        index = bisect.bisect_right(book.history_t, self.now - seconds) - 1
        return book.history_mid[index] if index >= 0 else None

    def mid_change(self, outcome: str, seconds: float) -> float | None:
        now, then = self.mid(outcome), self.mid_ago(outcome, seconds)
        return now - then if now is not None and then is not None else None

    def trades(self, outcome: str, seconds: float) -> list[tuple[float, float, float, bool]]:
        """Recent (time, price, shares, taker_bought) trades on one outcome."""
        cutoff = self.now - seconds
        return [t for t in self._s.books[outcome.casefold()].trades if t[0] >= cutoff]

    def flow(self, outcome: str, seconds: float) -> float:
        """Shares bought minus sold by takers on one outcome over the last ``seconds``."""
        return sum(q if buy else -q for _, _, q, buy in self.trades(outcome, seconds))

    def reference(self, symbol: str | None = None, seconds_ago: float = 0.0) -> float | None:
        """Reference price for ``symbol`` (default: this market's asset in USDT)."""
        return self._reference.price(symbol or f"{self.asset}usdt", seconds_ago, self.now)

    def reference_return_bp(self, seconds: float, symbol: str | None = None) -> float | None:
        now, then = self.reference(symbol), self.reference(symbol, seconds)
        return (now / then - 1) * 1e4 if now and then else None
