"""Public live data: market discovery, the order-book WebSocket, reference prices, and results.

Everything here only reads public data. It has no credentials and cannot place an order.
"""

from __future__ import annotations

import json
import queue
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

from .polymarket import PolymarketData, PublicHttpTransport

POLYMARKET_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
BINANCE_WS = "wss://stream.binance.com:9443/stream?streams="
SERIES_PATTERN = re.compile(r"^[a-z0-9-]+-(\d+)(m|h)$")
LEAD_SECONDS = 90.0
RESOLVE_GIVE_UP_SECONDS = 6 * 3600.0
SILENCE_RECONNECT_SECONDS = 45.0
REFERENCE_MIN_GAP_S = 0.1


def series_interval(series: str) -> int:
    match = SERIES_PATTERN.match(series)
    if not match:
        raise ValueError(
            f"{series!r} is not a rolling series such as btc-updown-5m. "
            "Use --slug for a single market."
        )
    return int(match[1]) * (60 if match[2] == "m" else 3600)


def market_info(data: PolymarketData, slug: str, series: str = "", start: int = 0) -> dict[str, Any]:
    """Look up one market and describe it in the tape's format."""
    market = data.discover(slug)
    end = market.end_epoch
    if series:
        end = start + series_interval(series)
    elif not start:
        started = (market.source or {}).get("startDate")
        if started:
            from .polymarket import _epoch

            start = _epoch(started)
    return {
        "slug": market.slug,
        "series": series,
        "condition_id": market.condition_id,
        "market_id": market.market_id,
        "question": market.question,
        "tokens": market.tokens,
        "start": start,
        "end": end,
        "fee_rate": market.fee_rate,
        "tick_size": market.tick_size,
        "min_order_size": market.min_order_size,
        "taker_delay_ms": market.taker_delay_ms,
    }


@dataclass
class Stats:
    pm_messages: int = 0
    ref_messages: int = 0
    reconnects: int = 0
    last_pm: float = 0.0
    last_error: str = ""


class LiveFeed:
    """Runs background threads and puts time-stamped events on ``self.events``."""

    def __init__(
        self,
        series: tuple[str, ...] = (),
        slugs: tuple[str, ...] = (),
        reference: bool = True,
        watch: list[dict[str, Any]] | None = None,
        data: PolymarketData | None = None,
    ) -> None:
        if not series and not slugs:
            raise ValueError("choose at least one --series or --slug")
        for name in series:
            series_interval(name)
        self.series = tuple(series)
        self.slugs = tuple(slugs)
        self.reference = reference
        self.data = data or PolymarketData(PublicHttpTransport(timeout=10))
        self.events: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=500_000)
        self.stop_event = threading.Event()
        self.stats = Stats()
        self.known: dict[str, dict[str, Any]] = {}
        self.unresolved: dict[str, dict[str, Any]] = {m["slug"]: m for m in (watch or [])}
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    def start(self) -> None:
        for target in (self._discover_loop, self._book_loop, self._resolve_loop):
            self._spawn(target)
        if self.reference:
            self._spawn(self._reference_loop)

    def stop(self) -> None:
        self.stop_event.set()

    def _spawn(self, target: Any, *args: Any) -> threading.Thread:
        thread = threading.Thread(target=target, args=args, daemon=True)
        thread.start()
        self._threads.append(thread)
        return thread

    def _put(self, kind: str, payload: Any) -> None:
        self.events.put({"t": time.time_ns(), "k": kind, "m": payload})

    # ------------------------------------------------------------ markets
    def _discover_loop(self) -> None:
        while not self.stop_event.is_set():
            now = time.time()
            for slug in self.slugs:
                self._learn(slug, "", 0)
            for name in self.series:
                interval = series_interval(name)
                current = int(now // interval) * interval
                self._learn(f"{name}-{current}", name, current)
                if current + interval - now <= LEAD_SECONDS:
                    self._learn(f"{name}-{current + interval}", name, current + interval)
            self.stop_event.wait(5.0)

    def _learn(self, slug: str, series: str, start: int) -> None:
        if slug in self.known:
            return
        try:
            info = market_info(self.data, slug, series, start)
        except (ConnectionError, LookupError, OSError, TimeoutError, ValueError) as error:
            self.stats.last_error = f"market {slug}: {type(error).__name__}"
            return
        with self._lock:
            self.known[slug] = info
            self.unresolved[slug] = info
        self._put("market", info)

    def wanted_assets(self) -> list[str]:
        now = time.time()
        with self._lock:
            tokens = [
                token
                for info in self.known.values()
                if now < float(info["end"]) + 30
                for token in info["tokens"].values()
            ]
        return sorted(tokens)

    # ------------------------------------------------------------ order books
    def _book_loop(self) -> None:
        """One connection per token set. A new one opens before the old one closes."""
        current: threading.Event | None = None
        assets: list[str] = []
        while not self.stop_event.is_set():
            wanted = self.wanted_assets()
            if wanted and wanted != assets:
                ready, stop = threading.Event(), threading.Event()
                self._spawn(self._book_worker, wanted, ready, stop)
                ready.wait(8.0)
                if current is not None:
                    current.set()
                current, assets = stop, wanted
            self.stop_event.wait(2.0)
        if current is not None:
            current.set()

    def _book_worker(self, assets: list[str], ready: threading.Event, stop: threading.Event) -> None:
        from websockets.exceptions import ConnectionClosed
        from websockets.sync.client import connect

        pause = 1.0
        while not stop.is_set() and not self.stop_event.is_set():
            try:
                with connect(POLYMARKET_WS, open_timeout=10, close_timeout=2, max_size=None) as ws:
                    ws.send(json.dumps({"assets_ids": assets, "type": "market"}))
                    pause = 1.0
                    last_ping = last_message = time.monotonic()
                    while not stop.is_set() and not self.stop_event.is_set():
                        now = time.monotonic()
                        if now - last_ping >= 10:
                            ws.send("PING")
                            last_ping = now
                        if now - last_message > SILENCE_RECONNECT_SECONDS:
                            raise TimeoutError("no messages")
                        try:
                            text = ws.recv(timeout=1.0)
                        except TimeoutError:
                            continue
                        if text == "PONG" or not text:
                            continue
                        last_message = time.monotonic()
                        try:
                            payload = json.loads(text)
                        except ValueError:
                            continue
                        self.stats.pm_messages += 1
                        self.stats.last_pm = time.time()
                        ready.set()
                        self._put("pm", payload)
            except (ConnectionClosed, OSError, TimeoutError, ValueError) as error:
                self.stats.reconnects += 1
                self.stats.last_error = f"order-book feed: {type(error).__name__}"
                ready.set()
                stop.wait(pause)
                pause = min(pause * 2, 30.0)

    # ------------------------------------------------------------ reference prices
    def _reference_loop(self) -> None:
        current: set[str] = set()
        stop = threading.Event()
        while not self.stop_event.is_set():
            with self._lock:
                symbols = {
                    f"{slug.split('-', 1)[0]}usdt" for slug, info in self.known.items() if info["series"]
                }
            if symbols and symbols != current:
                stop.set()
                stop = threading.Event()
                current = symbols
                self._spawn(self._reference_worker, sorted(symbols), stop)
            self.stop_event.wait(10.0)
        stop.set()

    def _reference_worker(self, symbols: list[str], stop: threading.Event) -> None:
        from websockets.exceptions import ConnectionClosed
        from websockets.sync.client import connect

        streams = "/".join(f"{symbol}@bookTicker" for symbol in symbols)
        last: dict[str, float] = {}
        while not stop.is_set() and not self.stop_event.is_set():
            try:
                with connect(BINANCE_WS + streams, open_timeout=10, close_timeout=2, max_size=None) as ws:
                    while not stop.is_set() and not self.stop_event.is_set():
                        try:
                            text = ws.recv(timeout=1.0)
                        except TimeoutError:
                            continue
                        data = json.loads(text).get("data") or {}
                        if "b" not in data:
                            continue
                        symbol = str(data["s"]).casefold()
                        now = time.monotonic()
                        if now - last.get(symbol, 0.0) < REFERENCE_MIN_GAP_S:
                            continue
                        last[symbol] = now
                        self.stats.ref_messages += 1
                        self._put("ref", {"symbol": symbol, "bid": data["b"], "ask": data["a"]})
            except (ConnectionClosed, OSError, TimeoutError, ValueError) as error:
                # reference data is optional and some regions cannot reach this source
                self.stats.last_error = f"reference feed: {type(error).__name__}"
                stop.wait(60.0)

    # ------------------------------------------------------------ results
    def _resolve_loop(self) -> None:
        while not self.stop_event.is_set():
            now = time.time()
            with self._lock:
                ended = [m for m in self.unresolved.values() if now >= float(m["end"]) + 20]
            for info in ended:
                try:
                    winner = self.data.resolved_outcome(
                        str(info["market_id"]), info["slug"], info["condition_id"]
                    )
                except (ConnectionError, LookupError, OSError, TimeoutError, ValueError) as error:
                    self.stats.last_error = f"result lookup: {type(error).__name__}"
                    continue
                if winner:
                    self._put("result", {"slug": info["slug"], "winner": winner})
                elif now < float(info["end"]) + RESOLVE_GIVE_UP_SECONDS:
                    continue
                with self._lock:
                    self.unresolved.pop(info["slug"], None)
            self.stop_event.wait(15.0)


def fetch_results(folder: Any, data: PolymarketData | None = None) -> dict[str, Any]:
    """Look up results for recorded markets that ended without one, and add them to the tape."""
    from pathlib import Path

    from .tape import TapeWriter, scan_metadata

    folder = Path(folder)
    meta = scan_metadata(folder)
    if meta["synthetic"]:
        raise ValueError("this tape is synthetic, so there are no real results to fetch")
    data = data or PolymarketData(PublicHttpTransport(timeout=10))
    now = time.time()
    missing = [
        info
        for slug, info in meta["markets"].items()
        if slug not in meta["winners"] and now >= float(info["end"])
    ]
    writer = TapeWriter(folder)
    found = failed = 0
    for info in missing:
        try:
            winner = data.resolved_outcome(str(info["market_id"]), info["slug"], info["condition_id"])
        except (ConnectionError, LookupError, OSError, TimeoutError, ValueError):
            failed += 1
            continue
        if winner:
            writer.write(time.time_ns(), "result", {"slug": info["slug"], "winner": winner})
            found += 1
    writer.close()
    return {
        "markets": len(meta["markets"]),
        "already_known": len(meta["winners"]),
        "looked_up": len(missing),
        "found": found,
        "not_resolved_yet": len(missing) - found - failed,
        "lookup_failed": failed,
    }
