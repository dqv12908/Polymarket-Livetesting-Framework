from __future__ import annotations

import argparse
import json
import os
import tempfile
import urllib.request
from dataclasses import fields
from pathlib import Path
from typing import Any

from .demo import DemoMarketData, DemoStrategy
from .engine import PaperExecutionEngine
from .ledger import PaperLedger
from .models import ExecutionConfig
from .plugins import PluginRunner, load_plugin, load_settings
from .polymarket import PolymarketData, PublicHttpTransport
from .service import create_server


def _json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str), flush=True)


def _config(path: Path | None) -> ExecutionConfig:
    if path is None:
        return ExecutionConfig()
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("configuration must be a JSON object")
    value = value.get("execution", value)
    allowed = {item.name for item in fields(ExecutionConfig)}
    unknown = set(value) - allowed
    if unknown:
        raise ValueError("unknown execution settings: " + ", ".join(sorted(unknown)))
    return ExecutionConfig(**value)


def _engine(state_dir: Path, config: ExecutionConfig) -> PaperExecutionEngine:
    ledger = PaperLedger(state_dir / "state.json", state_dir / "events.jsonl", config)
    return PaperExecutionEngine(PolymarketData(PublicHttpTransport()), ledger, config)


def _token(path: Path | None) -> str:
    value = (
        path.read_text(encoding="utf-8").strip() if path else os.environ.get("PM_LIVE_TOKEN", "")
    )
    if len(value) < 32:
        raise ValueError("set PM_LIVE_TOKEN or provide a token file with at least 32 characters")
    return value


def command_serve(args: argparse.Namespace) -> None:
    engine = _engine(args.state_dir, _config(args.config))
    server, service = create_server(engine, _token(args.token_file), args.host, args.port)
    service.start_settlement_loop(args.settlement_interval)
    _json(
        {
            "status": "ready",
            "listen": f"http://{args.host}:{server.server_address[1]}",
            "paper_only": True,
            "order_submission_enabled": False,
        }
    )
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        service.stop()
        server.server_close()


def command_submit(args: argparse.Namespace) -> None:
    payload = (
        json.load(__import__("sys").stdin)
        if str(args.signal) == "-"
        else json.loads(args.signal.read_text(encoding="utf-8"))
    )
    body = json.dumps(payload).encode()
    request = urllib.request.Request(
        args.endpoint,
        data=body,
        headers={
            "Authorization": "Bearer " + _token(args.token_file),
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=args.timeout) as response:
        _json(json.load(response))


def command_status(args: argparse.Namespace) -> None:
    config = _config(args.config)
    ledger = PaperLedger(args.state_dir / "state.json", args.state_dir / "events.jsonl", config)
    _json(ledger.snapshot())


def command_plugin(args: argparse.Namespace) -> None:
    engine = _engine(args.state_dir, _config(args.config))
    plugin = load_plugin(args.plugin, load_settings(args.plugin_config))
    runner = PluginRunner(plugin, engine)
    if args.once:
        _json(runner.run_once())
    else:
        runner.run_forever(args.poll_seconds)


def command_smoke(args: argparse.Namespace) -> None:
    def exercise(state_dir: Path) -> dict[str, Any]:
        config = ExecutionConfig(route_latency_ms=0, settlement_grace_seconds=0)
        ledger = PaperLedger(state_dir / "state.json", state_dir / "events.jsonl", config)
        engine = PaperExecutionEngine(DemoMarketData(), ledger, config)
        result = PluginRunner(DemoStrategy(), engine).run_once()
        settlements = engine.resolve(now_epoch=2)
        snapshot = ledger.snapshot()
        if result["execution"]["status"] != "filled":
            raise RuntimeError("smoke signal did not fill")
        if len(settlements["positions"]) != 1 or snapshot["validation_accuracy"] != 1:
            raise RuntimeError("smoke signal did not settle correctly")
        return {"execution": result["execution"], "settlements": settlements, "account": snapshot}

    if args.state_dir:
        args.state_dir.mkdir(parents=True, exist_ok=False)
        _json(exercise(args.state_dir))
    else:
        with tempfile.TemporaryDirectory(prefix="pm-live-smoke-") as directory:
            _json(exercise(Path(directory)))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pm-live",
        description="Read-only Polymarket live-data paper execution framework",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="run authenticated signal ingress and settlement")
    serve.add_argument("--state-dir", type=Path, default=Path(".pm-live"))
    serve.add_argument("--config", type=Path)
    serve.add_argument("--token-file", type=Path)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--settlement-interval", type=float, default=15)
    serve.set_defaults(handler=command_serve)

    submit = subparsers.add_parser("submit", help="submit one JSON signal to a running service")
    submit.add_argument("signal", type=Path, help="JSON file, or - for stdin")
    submit.add_argument("--endpoint", default="http://127.0.0.1:8765/v1/signals")
    submit.add_argument("--token-file", type=Path)
    submit.add_argument("--timeout", type=float, default=10)
    submit.set_defaults(handler=command_submit)

    status = subparsers.add_parser("status", help="read local paper ledger status")
    status.add_argument("--state-dir", type=Path, default=Path(".pm-live"))
    status.add_argument("--config", type=Path)
    status.set_defaults(handler=command_status)

    plugin = subparsers.add_parser("run-plugin", help="run a module:factory strategy directly")
    plugin.add_argument("plugin")
    plugin.add_argument("--plugin-config", type=Path)
    plugin.add_argument("--state-dir", type=Path, default=Path(".pm-live"))
    plugin.add_argument("--config", type=Path)
    plugin.add_argument("--poll-seconds", type=float, default=1)
    plugin.add_argument("--once", action="store_true")
    plugin.set_defaults(handler=command_plugin)

    smoke = subparsers.add_parser("smoke", help="exercise deterministic fill and settlement")
    smoke.add_argument("--state-dir", type=Path)
    smoke.set_defaults(handler=command_smoke)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.handler(args)
