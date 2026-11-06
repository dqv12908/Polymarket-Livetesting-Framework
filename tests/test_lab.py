"""Backtest, session, tape, report and command-line behaviour. No network is used."""

from __future__ import annotations

import gzip
import json
import re
from pathlib import Path

import pytest

from polymarket_livetesting import ExecutionConfig, Order, Strategy, load_strategy, run_backtest
from polymarket_livetesting import cli as cli_module
from polymarket_livetesting.dataset import export
from polymarket_livetesting.report import summarize, text_report, verdict, write_outputs
from polymarket_livetesting.session import MemoryLedger, Session
from polymarket_livetesting.synth import generate
from polymarket_livetesting.tape import TapeWriter, read_tape, scan_metadata

NS = 1_000_000_000
T0 = 1_800_000_000
SLUG = f"btc-updown-5m-{T0}"
UP, DOWN = "token-up", "token-down"
INFO = {
    "slug": SLUG,
    "series": "btc-updown-5m",
    "condition_id": "condition",
    "market_id": "market",
    "tokens": {"up": UP, "down": DOWN},
    "start": T0,
    "end": T0 + 300,
    "fee_rate": 0.07,
}


def book(token: str, bid: float, ask: float, t: float, size: float = 100.0) -> dict:
    return {
        "t": int(t * NS),
        "k": "pm",
        "m": {
            "event_type": "book",
            "asset_id": token,
            "timestamp": str(int(t * 1000) - 50),
            "bids": [{"price": str(bid), "size": str(size)}],
            "asks": [{"price": str(ask), "size": str(size)}],
        },
    }


class BuyUpOnce(Strategy):
    name = "buy_up"
    tick_seconds = 1.0

    def __init__(self, settings=None) -> None:
        super().__init__(settings)
        self.seen_asks: list[float] = []

    def on_tick(self, market):
        self.seen_asks.append(market.best_ask("up"))
        if market.seconds_in >= 100:
            return Order("up", probability=0.9)
        return None


def session(strategy=None, latency_ms=500.0, winners=None, config=None, lag=1000.0):
    config = config or ExecutionConfig()
    return (
        Session(strategy or BuyUpOnce(), MemoryLedger(config), config, latency_ms, lag, winners=winners),
        config,
    )


def test_fill_uses_the_book_after_the_delay_not_at_the_decision():
    s, _ = session(winners={SLUG: "up"})
    s.handle({"t": (T0 - 30) * NS, "k": "market", "m": INFO})
    s.handle(book(UP, 0.59, 0.60, T0 + 1))
    s.handle(book(DOWN, 0.40, 0.41, T0 + 1))
    s.handle(book(UP, 0.69, 0.70, T0 + 100.2))    # moves after the decision at 100.0, before arrival
    s.handle(book(UP, 0.79, 0.80, T0 + 101.0))    # moves after arrival, must not be used
    position = next(iter(s.ledger.state["positions"].values()))
    assert position["vwap"] == pytest.approx(0.70)
    meta = position["signal"]["metadata"]
    assert meta["ask_at_decision"] == pytest.approx(0.60)


def test_strategy_never_sees_data_from_after_its_tick():
    strategy = BuyUpOnce()
    s, _ = session(strategy)
    s.handle({"t": (T0 - 30) * NS, "k": "market", "m": INFO})
    s.handle(book(UP, 0.49, 0.50, T0 + 0.5))
    s.handle(book(DOWN, 0.49, 0.50, T0 + 0.5))
    s.handle(book(UP, 0.59, 0.60, T0 + 10.5))     # ticks up to T0+10 must all have seen 0.50
    assert strategy.seen_asks
    assert all(ask == 0.50 for ask in strategy.seen_asks)


def test_price_change_messages_update_book_and_best_prices():
    s, _ = session(BuyUpOnce())
    s.handle({"t": (T0 - 30) * NS, "k": "market", "m": INFO})
    s.handle(book(UP, 0.49, 0.50, T0 + 0.1))
    s.handle({
        "t": int((T0 + 0.2) * NS),
        "k": "pm",
        "m": {
            "event_type": "price_change",
            "timestamp": str((T0 * 1000) + 150),
            "price_changes": [
                {"asset_id": UP, "price": "0.52", "size": "33", "side": "SELL", "best_bid": "0.49", "best_ask": "0.52"},
                {"asset_id": UP, "price": "0.50", "size": "0", "side": "SELL", "best_bid": "0.49", "best_ask": "0.52"},
            ],
        },
    })
    state = s.markets[SLUG]
    assert state.books["up"].best_ask == 0.52
    assert state.books["up"].asks == {0.52: 33.0}


def test_fill_is_refused_while_the_feed_is_too_far_behind():
    s, _ = session(lag=500.0)
    s.handle({"t": (T0 - 30) * NS, "k": "market", "m": INFO})
    late = book(UP, 0.59, 0.60, T0 + 1)
    late["m"]["timestamp"] = str(int((T0 + 1) * 1000) - 5000)
    s.handle(late)
    late_down = book(DOWN, 0.40, 0.41, T0 + 1)
    late_down["m"]["timestamp"] = str(int((T0 + 1) * 1000) - 5000)
    s.handle(late_down)
    s.handle(book(UP, 0.59, 0.60, T0 + 101) | {"m": {**late["m"]}})
    assert s.counts["fills"] == 0
    assert "data feed too far behind to trust the book" in s.skip_reasons


def test_only_one_position_per_market_and_settlement_pays_shares():
    s, config = session(winners={SLUG: "up"})
    s.handle({"t": (T0 - 30) * NS, "k": "market", "m": INFO})
    s.handle(book(UP, 0.59, 0.60, T0 + 1))
    s.handle(book(DOWN, 0.40, 0.41, T0 + 1))
    s.handle(book(UP, 0.59, 0.60, T0 + 200))
    assert s.counts["fills"] == 1 and s.counts["signals"] == 1
    s.handle(book(UP, 0.59, 0.60, T0 + 340))
    settled = s.ledger.state["settled"]
    assert len(settled) == 1
    assert settled[0]["pnl"] == pytest.approx(settled[0]["shares"] - settled[0]["total_debit"])
    assert s.ledger.cash == pytest.approx(config.starting_capital + settled[0]["pnl"])


def test_a_market_that_ends_before_the_order_arrives_is_skipped():
    class Late(Strategy):
        name = "late"

        def on_tick(self, market):
            return Order("up") if market.seconds_left <= 1 else None

    s, _ = session(Late(), latency_ms=5000.0)
    s.handle({"t": (T0 - 30) * NS, "k": "market", "m": INFO})
    s.handle(book(UP, 0.59, 0.60, T0 + 1))
    s.handle(book(DOWN, 0.40, 0.41, T0 + 1))
    s.handle(book(UP, 0.59, 0.60, T0 + 310))
    assert s.counts["fills"] == 0
    assert s.skip_reasons["market ended before the order arrived"] >= 1


def test_tape_round_trip_and_damaged_file_keeps_the_readable_part(tmp_path):
    writer = TapeWriter(tmp_path)
    for i in range(50):
        writer.write(T0 * NS + i, "ref", {"symbol": "btcusdt", "bid": 1, "ask": 2})
    writer.write(T0 * NS + 100, "result", {"slug": SLUG, "winner": "Up"})
    writer.close()
    files = list(tmp_path.rglob("tape-*.jsonl.gz"))
    assert len(files) == 1
    assert sum(1 for e in read_tape(tmp_path) if e["k"] == "ref") == 50
    assert scan_metadata(tmp_path)["winners"] == {SLUG: "up"}
    raw = files[0].read_bytes()
    files[0].write_bytes(raw[: len(raw) - 12])
    problems: list[str] = []
    list(read_tape(tmp_path, problems))
    assert problems


def test_missing_data_gives_a_helpful_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="pm-live record"):
        run_backtest(tmp_path, "builtin:late_favorite", sweep=False)


def test_backtest_on_synthetic_data_is_repeatable_and_labelled(tmp_path):
    generate(tmp_path / "data", hours=2)
    first = run_backtest(tmp_path / "data", "builtin:random_baseline", sweep=False, jobs=1)
    second = run_backtest(tmp_path / "data", "builtin:random_baseline", sweep=False, jobs=1)
    assert first["summary"]["trades"] > 0
    assert first["summary"]["total_pnl"] == second["summary"]["total_pnl"]
    assert first["synthetic"] is True
    assert "SYNTHETIC" in text_report({**first, "sweep": []})
    paths = write_outputs({**first, "sweep": []}, tmp_path / "out")
    assert "Synthetic data" in paths["html"].read_text(encoding="utf-8")
    assert paths["trades"].read_text(encoding="utf-8").startswith("slug,outcome")


def test_live_mode_and_backtest_mode_make_the_same_trades(tmp_path):
    """Live paper mode learns results as events; the trades must match a replay of the same data."""
    generate(tmp_path / "data", hours=1)
    meta = scan_metadata(tmp_path / "data")
    config = ExecutionConfig()

    def run(winners):
        strategy = load_strategy("builtin:late_favorite")
        s = Session(strategy, MemoryLedger(config), config, 450.0, winners=winners)
        for event in read_tape(tmp_path / "data"):
            s.handle(event)
        s.finish()
        return [(t["slug"], t["outcome"], t["shares"], t["vwap"], t["pnl"]) for t in s.ledger.state["settled"]]

    assert run(None) == run(meta["winners"]) or sorted(run(None)) == sorted(run(meta["winners"]))
    assert run(None)


def test_a_real_edge_is_detected_and_a_coin_flip_is_not(tmp_path):
    """Guards against a pipeline that says "no edge" whatever it is given."""
    generate(tmp_path / "data", hours=12)
    winners = scan_metadata(tmp_path / "data")["winners"]
    cheat = tmp_path / "cheat.py"
    cheat.write_text(
        "import json\n"
        "from polymarket_livetesting import Order, Strategy\n"
        "class Cheat(Strategy):\n"
        "    name = 'cheat'\n"
        "    def on_tick(self, market):\n"
        "        if market.seconds_in < 150:\n"
        "            return None\n"
        "        side = self.settings['winners'].get(market.slug)\n"
        "        ask = market.best_ask(side) if side else None\n"
        "        return Order(side) if ask is not None and ask < 0.9 else None\n",
        encoding="utf-8",
    )
    known = run_backtest(tmp_path / "data", str(cheat), {"winners": winners}, sweep=False, jobs=1)
    title, _ = verdict({**known["summary"], "latency_ms": 450}, [], False)
    assert known["summary"]["net_cents_per_share"] > 10
    assert title == "PROMISING, NOT PROOF"
    coin = run_backtest(tmp_path / "data", "builtin:random_baseline", sweep=False, jobs=1)
    assert coin["summary"]["net_cents_per_share"] < known["summary"]["net_cents_per_share"] - 10
    title, _ = verdict({**coin["summary"], "latency_ms": 450}, [], False)
    assert title != "PROMISING, NOT PROOF"


def test_summary_counts_markets_in_the_same_time_window_as_one_observation():
    def trade(start, won):
        shares = 10.0
        debit = 5.0
        return {
            "shares": shares, "pnl": shares - debit if won else -debit, "outcome": "up",
            "winning_outcome": "up" if won else "down", "vwap": 0.5, "fee": 0.0,
            "signal": {"emitted_at_ns": int(start * NS), "metadata": {"start": start, "mid_at_decision": 0.5}},
        }

    together = [trade(i * 300 + T0, i % 2 == 0) for i in range(20) for _ in range(5)]
    apart = [trade(i * 300 + T0, i % 2 == 0) for i in range(100)]
    assert summarize(together)["independent_windows"] == 20
    assert summarize(together)["se_cents"] > summarize(apart)["se_cents"]


def test_verdict_refuses_to_call_small_samples_anything():
    title, notes = verdict({"trades": 10, "t_stat": 9.0, "net_cents_per_share": 50.0}, [], False)
    assert title == "TOO LITTLE DATA TO SAY"
    assert any("noise" in note for note in notes)
    title, _ = verdict({"trades": 500, "t_stat": 0.5, "net_cents_per_share": 1.0}, [], False)
    assert title == "NOT DISTINGUISHABLE FROM ZERO"
    title, _ = verdict({"trades": 500, "t_stat": -4.0, "net_cents_per_share": -3.0}, [], False)
    assert title == "LOSING"


def test_load_strategy_forms(tmp_path):
    (tmp_path / "helper_mod.py").write_text("VALUE = 7\n", encoding="utf-8")
    path = tmp_path / "mine.py"
    path.write_text(
        "from helper_mod import VALUE\n"
        "from polymarket_livetesting import Strategy\n"
        "class Mine(Strategy):\n    name = 'mine'\n    value = VALUE\n    def on_tick(self, market):\n        return None\n"
        "class Other(Strategy):\n    name = 'other'\n    def on_tick(self, market):\n        return None\n",
        encoding="utf-8",
    )
    assert load_strategy(f"{path}:Other").name == "other"
    with pytest.raises(ValueError, match="expected one Strategy"):
        load_strategy(str(path))
    assert load_strategy("builtin:late_favorite").name == "late_favorite"
    with pytest.raises(ValueError):
        load_strategy("builtin:nope")
    with pytest.raises(FileNotFoundError):
        load_strategy(str(tmp_path / "missing.py"))


def test_dataset_rows_have_labels_and_use_only_past_data(tmp_path):
    generate(tmp_path / "data", hours=1)
    info = export(tmp_path / "data", tmp_path / "rows.csv.gz", every_seconds=30)
    assert info["rows"] > 50 and info["synthetic"] is True
    with gzip.open(tmp_path / "rows.csv.gz", "rt", encoding="utf-8") as handle:
        header = handle.readline().strip().split(",")
    assert "label_a_won" in header and "mid" in header
    assert not [c for c in header if c.startswith("label") and "after_delay" not in c and c != "label_a_won"]


def test_command_line_demo_and_init(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["pm-live", "init", "bot"])
    cli_module.main()
    assert (tmp_path / "bot" / "strategy.py").exists()
    monkeypatch.setattr("sys.argv", ["pm-live", "init", "bot"])
    with pytest.raises(SystemExit):
        cli_module.main()
    monkeypatch.setattr(
        "sys.argv",
        ["pm-live", "demo", "--strategy", str(tmp_path / "bot" / "strategy.py"), "--hours", "1", "--out", "d"],
    )
    cli_module.main()
    out = capsys.readouterr().out
    assert "VERDICT" in out
    assert (tmp_path / "d" / "report" / "report.html").exists()


def test_source_has_no_path_to_real_orders():
    source = Path(__file__).resolve().parents[1] / "src" / "polymarket_livetesting"
    banned = re.compile(r"private_key|py_clob_client|create_order|post_order|eth_account|web3|mnemonic", re.IGNORECASE)
    for path in source.rglob("*.py"):
        assert not banned.search(path.read_text(encoding="utf-8")), path
    live = (source / "live.py").read_text(encoding="utf-8") + (source / "polymarket.py").read_text(encoding="utf-8")
    assert 'method="POST"' not in live


def test_summary_file_is_valid_json(tmp_path):
    generate(tmp_path / "data", hours=1)
    result = run_backtest(tmp_path / "data", "builtin:random_baseline", sweep=False, jobs=1)
    paths = write_outputs({**result, "sweep": []}, tmp_path / "o")
    assert json.loads(paths["json"].read_text(encoding="utf-8"))["counts"]["signals"] > 0


class FakeFeed:
    """Stands in for the network: a live-timed market that gets a fill, then a result."""

    def __init__(self, series=(), slugs=(), reference=True, watch=None) -> None:
        import queue
        import time

        from polymarket_livetesting.live import Stats

        self.events = queue.Queue()
        self.stats = Stats()
        self.known = {}
        now = time.time()
        slug = f"btc-updown-5m-{int(now)}"
        info = {
            **INFO,
            "slug": slug,
            "start": int(now) - 5,
            "end": int(now) + 2,
            "tokens": {"up": UP, "down": DOWN},
        }
        self.known[slug] = info
        self.events.put({"t": time.time_ns(), "k": "market", "m": info})
        self.events.put(book(UP, 0.59, 0.60, now))
        self.events.put(book(DOWN, 0.39, 0.41, now))
        self.slug = slug

    def start(self) -> None:
        import threading
        import time

        def later() -> None:
            time.sleep(1.0)
            now = time.time()
            self.events.put(book(UP, 0.59, 0.60, now))
            self.events.put(book(DOWN, 0.39, 0.41, now))
            time.sleep(0.8)
            self.events.put({"t": time.time_ns(), "k": "result", "m": {"slug": self.slug, "winner": "up"}})

        threading.Thread(target=later, daemon=True).start()

    def stop(self) -> None:
        return None


def test_paper_session_fills_settles_and_persists(tmp_path, monkeypatch):
    from polymarket_livetesting import paper as paper_module

    class Always(Strategy):
        name = "always"
        series = ("btc-updown-5m",)
        tick_seconds = 0.2

        def on_tick(self, market):
            return Order("up")

    monkeypatch.setattr(paper_module, "LiveFeed", FakeFeed)
    monkeypatch.setattr(paper_module, "load_strategy", lambda spec, settings=None: Always(settings))
    state = tmp_path / "session"
    status = paper_module.paper("x", state, latency_ms=50, port=0, duration_seconds=2.6)
    assert status["paper_only"] is True and status["order_submission_enabled"] is False
    assert status["account"]["settled_positions"] == 1
    assert status["account"]["realized_pnl"] > 0
    assert (state / "state.json").exists() and (state / "events.jsonl").exists()
    assert list((state / "tape").rglob("tape-*.jsonl.gz"))
    report = paper_module.report_from_state(state)
    assert report["summary"]["trades"] == 1 and report["latency_ms"] == 50
