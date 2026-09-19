from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from typing import Any, Protocol

from .models import BinaryMarket, BookLevel, BookSnapshot

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"


def _json_field(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _epoch(value: Any) -> int:
    if isinstance(value, (int, float)):
        number = float(value)
        return int(number / 1_000 if number > 10_000_000_000 else number)
    if not value:
        raise ValueError("market end time is missing")
    text = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


class JsonTransport(Protocol):
    def get(self, url: str) -> tuple[Any, float]: ...


class PublicHttpTransport:
    """Small GET-only transport; it has no order-submission method or credentials."""

    def __init__(self, timeout: float = 5.0, attempts: int = 3) -> None:
        if timeout <= 0 or attempts < 1:
            raise ValueError("timeout and attempts must be positive")
        self.timeout = timeout
        self.attempts = attempts

    def get(self, url: str) -> tuple[Any, float]:
        if urllib.parse.urlsplit(url).scheme != "https":
            raise ValueError("public market requests require HTTPS")
        last_error: Exception | None = None
        for attempt in range(self.attempts):
            started = time.perf_counter_ns()
            try:
                request = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": "polymarket-livetesting/0.1",
                        "Accept": "application/json",
                    },
                    method="GET",
                )
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = response.read()
                return json.loads(body), (time.perf_counter_ns() - started) / 1_000_000
            except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError) as error:
                last_error = error
                if attempt + 1 < self.attempts:
                    time.sleep(0.05 * 2**attempt)
        raise ConnectionError(
            f"Polymarket GET failed for {urllib.parse.urlsplit(url).path}"
        ) from last_error


class PolymarketData:
    """Exact-slug discovery, order-book reads, and immutable resolution checks."""

    def __init__(self, transport: JsonTransport | None = None) -> None:
        self.transport = transport or PublicHttpTransport()
        self._market_cache: dict[str, BinaryMarket] = {}
        self._resolution_cache: dict[str, str] = {}

    def discover(self, slug: str, refresh: bool = False) -> BinaryMarket:
        if not slug or any(character.isspace() for character in slug):
            raise ValueError("market slug must be nonempty and contain no whitespace")
        if not refresh and slug in self._market_cache:
            return self._market_cache[slug]
        query = urllib.parse.urlencode({"slug": slug})
        markets, _ = self.transport.get(f"{GAMMA}/markets?{query}")
        exact = [market for market in markets if market.get("slug") == slug]
        if len(exact) != 1:
            raise LookupError(f"expected exactly one market for {slug}, got {len(exact)}")
        market = exact[0]
        outcomes = [str(value).casefold() for value in (_json_field(market.get("outcomes")) or [])]
        token_ids = [str(value) for value in (_json_field(market.get("clobTokenIds")) or [])]
        if len(outcomes) != 2 or len(token_ids) != 2:
            raise ValueError("market is not binary")
        tokens = dict(zip(outcomes, token_ids, strict=True))
        if len(tokens) != 2 or len(set(tokens.values())) != 2:
            raise ValueError("outcome token mapping is ambiguous")
        condition_id = str(market.get("conditionId") or "")
        market_id = str(market.get("id") or "")
        if not condition_id or not market_id:
            raise ValueError("market identity is incomplete")
        clob_info, _ = self.transport.get(
            f"{CLOB}/clob-markets/{urllib.parse.quote(condition_id, safe='')}"
        )
        clob_tokens = {
            str(item.get("o", "")).casefold(): str(item.get("t")) for item in clob_info.get("t", [])
        }
        if clob_tokens and clob_tokens != tokens:
            raise ValueError("Gamma and CLOB token mappings disagree")
        fees_enabled = bool(market.get("feesEnabled", True))
        fee_details = clob_info.get("fd") or {}
        fee_rate = float(fee_details.get("r", 0.07 if fees_enabled else 0.0))
        if not fees_enabled:
            fee_rate = 0.0
        end_value = market.get("endDate") or market.get("endDateIso") or market.get("endTime")
        result = BinaryMarket(
            slug=slug,
            condition_id=condition_id,
            market_id=market_id,
            question=str(market.get("question") or ""),
            tokens=tokens,
            active=bool(market.get("active", False)),
            closed=bool(market.get("closed", False)),
            accepting_orders=market.get("acceptingOrders") is not False,
            end_epoch=_epoch(end_value),
            fee_rate=fee_rate,
            tick_size=float(clob_info.get("mts") or 0.01),
            min_order_size=float(clob_info.get("mos") or 1.0),
            taker_delay_ms=250 if clob_info.get("itode") else 0,
            source=market,
        )
        self._market_cache[slug] = result
        return result

    def book(self, token_id: str, expected_condition: str) -> BookSnapshot:
        query = urllib.parse.urlencode({"token_id": token_id})
        raw, latency = self.transport.get(f"{CLOB}/book?{query}")
        fetched_at_ns = time.time_ns()
        bids = tuple(
            sorted(
                (
                    BookLevel(float(level["price"]), float(level["size"]))
                    for level in raw.get("bids", [])
                ),
                key=lambda level: level.price,
                reverse=True,
            )
        )
        asks = tuple(
            sorted(
                (
                    BookLevel(float(level["price"]), float(level["size"]))
                    for level in raw.get("asks", [])
                ),
                key=lambda level: level.price,
            )
        )
        condition = str(raw.get("market") or expected_condition)
        if condition.casefold() != expected_condition.casefold():
            raise ValueError("order book belongs to a different condition")
        if str(raw.get("asset_id") or token_id) != token_id:
            raise ValueError("order book belongs to a different token")
        raw_timestamp = raw.get("timestamp")
        return BookSnapshot(
            token_id=token_id,
            condition_id=condition,
            bids=bids,
            asks=asks,
            tick_size=float(raw.get("tick_size") or 0.01),
            min_order_size=float(raw.get("min_order_size") or 1.0),
            exchange_timestamp_ms=int(raw_timestamp) if raw_timestamp is not None else None,
            fetched_at_ns=fetched_at_ns,
            request_latency_ms=latency,
        )

    def resolved_outcome(
        self, market_id: str, expected_slug: str, expected_condition: str
    ) -> str | None:
        if market_id in self._resolution_cache:
            return self._resolution_cache[market_id]
        market, _ = self.transport.get(f"{GAMMA}/markets/{urllib.parse.quote(market_id, safe='')}")
        if str(market.get("id")) != market_id:
            raise ValueError("settlement market ID mismatch")
        if str(market.get("slug")) != expected_slug:
            raise ValueError("settlement market slug mismatch")
        if str(market.get("conditionId") or "").casefold() != expected_condition.casefold():
            raise ValueError("settlement condition ID mismatch")
        if not market.get("closed"):
            return None
        outcomes = [str(value).casefold() for value in (_json_field(market.get("outcomes")) or [])]
        prices = [float(value) for value in (_json_field(market.get("outcomePrices")) or [])]
        winners = [
            outcome for outcome, price in zip(outcomes, prices, strict=False) if price >= 0.999
        ]
        if len(winners) != 1:
            return None
        self._resolution_cache[market_id] = winners[0]
        return winners[0]
