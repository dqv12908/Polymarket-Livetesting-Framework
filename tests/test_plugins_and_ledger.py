from __future__ import annotations

import json

from polymarket_livetesting import ExecutionConfig, PaperExecutionEngine, PaperLedger
from polymarket_livetesting.demo import DemoMarketData
from polymarket_livetesting.plugins import PluginRunner, load_plugin


def test_factory_plugin_runs_through_framework_and_persists(tmp_path) -> None:
    settings = {
        "signal_id": "plugin:1",
        "market_slug": DemoMarketData.slug,
        "outcome": "yes",
        "probability": 0.7,
    }
    plugin = load_plugin("examples.simple_strategy:build", settings)
    config = ExecutionConfig(route_latency_ms=0)
    state = tmp_path / "state.json"
    events = tmp_path / "events.jsonl"
    ledger = PaperLedger(state, events, config)
    result = PluginRunner(plugin, PaperExecutionEngine(DemoMarketData(), ledger, config)).run_once()

    reloaded = PaperLedger(state, events, config)
    audit = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]

    assert result["execution"]["status"] == "filled"
    assert reloaded.snapshot()["open_positions"] == 1
    assert audit[-1]["signal"]["signal_id"] == "plugin:1"


def test_class_plugin_is_instantiated_with_settings() -> None:
    plugin = load_plugin(
        "examples.simple_strategy:OneShotStrategy",
        {
            "signal_id": "plugin:class",
            "market_slug": DemoMarketData.slug,
            "outcome": "yes",
            "probability": 0.7,
        },
    )

    assert plugin.next_signal(1).signal_id == "plugin:class"


def test_plugin_none_still_runs_settlement_cycle(tmp_path) -> None:
    class Idle:
        def next_signal(self, now_ns):
            del now_ns

    config = ExecutionConfig(route_latency_ms=0)
    ledger = PaperLedger(tmp_path / "state.json", tmp_path / "events.jsonl", config)
    result = PluginRunner(Idle(), PaperExecutionEngine(DemoMarketData(), ledger, config)).run_once()

    assert result == {
        "signal": None,
        "execution": None,
        "settlements": {"positions": [], "validations": []},
    }
