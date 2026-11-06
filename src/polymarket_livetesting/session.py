"""One trading session. Feed it events and the clock.

A backtest and a live paper test differ only in where events come from, so a strategy behaves
the same way in both. Nothing here can reach an exchange: fills are simulated against the book.
"""

from __future__ import annotations

import dataclasses
import statistics
from collections import deque
from dataclasses import dataclass
from typing import Any

from .fill import plan_fill, validate_book
from .ledger import PaperLedger
from .models import ExecutionConfig, ExecutionResult, PaperSignal
from .state import MarketState, MarketView, ReferenceFeed
from .strategy import Order, Strategy

NS = 1_000_000_000
RETIRE_AFTER_SECONDS = 120.0
LAG_SAMPLE_EVERY = 200


@dataclass
class Pending:
    due_ns: int
    slug: str
    order: Order
    signal: PaperSignal


class Session:
    def __init__(
        self,
        strategy: Strategy,
        ledger: PaperLedger,
        config: ExecutionConfig,
        latency_ms: float,
        max_feed_lag_ms: float = 1000.0,
        winners: dict[str, str] | None = None,
        settle_grace_seconds: float = 30.0,
        series: tuple[str, ...] | None = None,
        slugs: tuple[str, ...] | None = None,
    ) -> None:
        self.strategy = strategy
        self.ledger = ledger
        self.config = config
        self.latency_ns = int(latency_ms * 1e6)
        self.max_feed_lag_ms = max_feed_lag_ms
        self.winners = winners  # known in advance in a backtest; arrives as events when live
        self.settle_grace = settle_grace_seconds
        self.want_series = set(series if series is not None else strategy.series)
        self.want_slugs = set(slugs if slugs is not None else strategy.slugs)
        self.markets: dict[str, MarketState] = {}
        self.token_market: dict[str, MarketState] = {}
        self.reference = ReferenceFeed()
        self.pending: list[Pending] = []
        self.holding: set[str] = set()
        self.attempts: dict[str, int] = {}
        self.lags: deque[float] = deque(maxlen=200)
        self.lag_samples: list[float] = []
        self.next_tick: dict[str, float] = {}
        self.counts = {"signals": 0, "fills": 0, "skips": 0}
        self.skip_reasons: dict[str, int] = {}
        self.settled = 0
        self.now_ns = 0
        self._messages = 0
        self._wakeup = 0

    # ------------------------------------------------------------ inputs
    def feed_lag_ms(self) -> float | None:
        return statistics.median(self.lags) if self.lags else None

    def handle(self, event: dict[str, Any]) -> None:
        """Process one event in time order. Anything due earlier happens first."""
        self.advance(event["t"])
        kind = event["k"]
        now = event["t"] / NS
        if kind == "pm":
            raw = event["m"]
            for message in raw if isinstance(raw, list) else (raw,):
                self._apply_pm(message, now, event["t"])
        elif kind == "ref":
            self.reference.apply(event["m"], now)
        elif kind == "market":
            self.add_market(event["m"])
        elif kind == "result" and self.winners is None:
            self.settle(event["m"]["slug"], event["m"]["winner"])

    def _apply_pm(self, message: dict[str, Any], now: float, t_ns: int) -> None:
        stamp = message.get("timestamp")
        if stamp:
            lag = t_ns / 1e6 - float(stamp)
            self.lags.append(lag)
            self._messages += 1
            if self._messages % LAG_SAMPLE_EVERY == 0:
                self.lag_samples.append(lag)
        kind = message.get("event_type")
        if kind == "price_change":
            for change in message.get("price_changes", ()):
                state = self.token_market.get(change.get("asset_id"))
                if state:
                    state.apply_change(change, now)
        elif kind == "book":
            state = self.token_market.get(message.get("asset_id"))
            if state:
                state.apply_book(message, now)
        elif kind == "last_trade_price":
            state = self.token_market.get(message.get("asset_id"))
            if state:
                state.apply_trade(message, now)

    def add_market(self, info: dict[str, Any]) -> None:
        if info["slug"] in self.markets or not self._wanted(info):
            return
        state = MarketState(info)
        self.markets[state.slug] = state
        for token in state.market.tokens.values():
            self.token_market[token] = state
        self.next_tick[state.slug] = 0.0
        self._wakeup = 0

    def _wanted(self, info: dict[str, Any]) -> bool:
        if not self.want_series and not self.want_slugs:
            return True
        return info.get("series") in self.want_series or info["slug"] in self.want_slugs

    # ------------------------------------------------------------ the clock
    def advance(self, now_ns: int) -> None:
        """Run everything scheduled up to ``now_ns``, in order, on the book as it stood then."""
        if now_ns < self._wakeup:
            self.now_ns = now_ns
            return
        while True:
            due = min((p.due_ns for p in self.pending), default=None)
            slug, tick_ns = self._next_tick_time()
            soonest = min(x for x in (due, tick_ns, now_ns + NS) if x is not None)
            if soonest > now_ns:
                self._wakeup = soonest
                break
            if due is not None and due == soonest:
                self._fill_due(due)
            else:
                self._tick(slug, tick_ns / NS)
        self.now_ns = now_ns
        self._settle_due(now_ns / NS)
        self._retire(now_ns / NS)

    def _next_tick_time(self) -> tuple[str | None, int | None]:
        best_slug, best = None, None
        pre_open = float(getattr(self.strategy, "pre_open_seconds", 0.0))
        for slug in list(self.next_tick):
            state = self.markets[slug]
            when = max(self.next_tick[slug], state.start - pre_open)
            if when >= state.end:
                del self.next_tick[slug]
                continue
            if best is None or when < best:
                best_slug, best = slug, when
        return best_slug, int(best * NS) if best is not None else None

    def _tick(self, slug: str, when: float) -> None:
        state = self.markets[slug]
        self.next_tick[slug] = when + max(float(self.strategy.tick_seconds), 0.05)
        if slug in self.holding:
            return
        if self.attempts.get(slug, 0) >= self._max_attempts():
            return
        view = MarketView(state, self.reference, when)
        if view.favourite() is None:
            return
        order = self.strategy.on_tick(view)
        if order is not None:
            self._submit(order, view, int(when * NS))

    def _max_attempts(self) -> int:
        return int(getattr(self.strategy, "max_attempts_per_market", 3))

    def _submit(self, order: Order, view: MarketView, decided_ns: int) -> None:
        outcome = order.outcome.casefold()
        state = self.markets[view.slug]
        if outcome not in state.books:
            raise ValueError(f"{view.slug} has no outcome {order.outcome!r}: {view.outcomes}")
        self.holding.add(view.slug)
        attempt = self.attempts[view.slug] = self.attempts.get(view.slug, 0) + 1
        self.counts["signals"] += 1
        mid = view.mid(outcome)
        stated = order.probability
        signal = PaperSignal(
            signal_id=f"{self.strategy.name}:{view.slug}:{attempt}",
            market_slug=view.slug,
            outcome=outcome,
            probability=min(max(float(stated if stated is not None else (mid or 0.5)), 0.0), 1.0),
            emitted_at_ns=decided_ns,
            expected_at_ns=decided_ns,
            model=self.strategy.name,
            metadata={
                "series": state.series,
                "start": state.start,
                "seconds_in": view.seconds_in,
                "ask_at_decision": view.best_ask(outcome),
                "mid_at_decision": mid,
                "stated_probability": stated,
                "note": dict(order.note),
            },
        )
        delay = self.latency_ns + int(state.market.taker_delay_ms * 1e6)
        self.pending.append(Pending(decided_ns + delay, view.slug, order, signal))

    def _fill_due(self, due: int) -> None:
        ready = next(p for p in self.pending if p.due_ns == due)
        self.pending.remove(ready)
        state = self.markets[ready.slug]
        self.ledger.register_validation(ready.signal)
        self.ledger.attach_validation_market(ready.signal.signal_id, state.market)
        outcome = ready.signal.outcome

        def skip(reason: str, book: Any = None) -> None:
            self.holding.discard(ready.slug)
            self.counts["skips"] += 1
            self.skip_reasons[reason] = self.skip_reasons.get(reason, 0) + 1
            self.ledger.record_result(
                ExecutionResult("skipped", reason, ready.signal, state.market, book)
            )

        if due / NS >= state.end:
            skip("market ended before the order arrived")
            return
        lag = self.feed_lag_ms()
        if lag is not None and lag > self.max_feed_lag_ms:
            skip("data feed too far behind to trust the book")
            return
        book = state.snapshot(outcome, due)
        invalid = validate_book(book, self.config)
        if invalid:
            skip(invalid, book)
            return
        config = self.config
        if ready.order.usd is not None:
            config = dataclasses.replace(
                config, max_trade_usd=min(config.max_trade_usd, float(ready.order.usd))
            )
        plan = plan_fill(
            book,
            outcome,
            ready.signal.probability,
            state.market.fee_rate,
            self.ledger.cash,
            self.ledger.equity_at_cost,
            config,
        )
        if plan is None:
            skip("not enough cash or displayed size at the ask", book)
            return
        self.counts["fills"] += 1
        self.ledger.record_result(
            ExecutionResult("filled", "simulated fill", ready.signal, state.market, book, plan)
        )

    # ------------------------------------------------------------ settlement
    def _settle_due(self, now: float) -> None:
        if self.winners is None:
            return
        for position in list(self.ledger.state.get("positions", {}).values()):
            if now >= float(position["end_epoch"]) + self.settle_grace:
                winner = self.winners.get(position["slug"])
                if winner:
                    self.settle(position["slug"], winner)

    def settle(self, slug: str, winner: str) -> None:
        for condition_id, position in list(self.ledger.state.get("positions", {}).items()):
            if position["slug"] == slug:
                record = self.ledger.settle(condition_id, winner)
                self.settled += 1
                self.strategy.on_settle(record)
        pending = self.ledger.state.setdefault("validation_pending", {})
        for signal_id, item in list(pending.items()):
            if item.get("market_slug") == slug:
                self.ledger.settle_validation(signal_id, winner)

    def _retire(self, now: float) -> None:
        busy = {p.slug for p in self.pending}
        for slug, state in list(self.markets.items()):
            if now > state.end + RETIRE_AFTER_SECONDS and slug not in busy:
                for token in state.market.tokens.values():
                    self.token_market.pop(token, None)
                del self.markets[slug]
                self.next_tick.pop(slug, None)

    def finish(self) -> None:
        """End of data: flush waiting orders and settle whatever has a known result."""
        self._wakeup = 0
        self.advance(self.now_ns + 5 * NS)
        if self.winners:
            pending = self.ledger.state.setdefault("validation_pending", {})
            positions = self.ledger.state.get("positions", {}).values()
            slugs = {p["slug"] for p in positions} | {i.get("market_slug") for i in pending.values()}
            for slug in slugs:
                if slug in self.winners:
                    self.settle(slug, self.winners[slug])


class MemoryLedger(PaperLedger):
    """The normal paper ledger with disk writes turned off, for fast repeated backtests."""

    def __init__(self, config: ExecutionConfig) -> None:
        super().__init__("__memory__/state.json", "__memory__/events.jsonl", config)

    def save(self) -> None:
        return None

    def append_event(self, value: dict[str, Any]) -> None:
        return None
