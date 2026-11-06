from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
import urllib.request
from dataclasses import fields
from pathlib import Path
from typing import Any

from . import __version__
from .backtest import DEFAULT_LATENCY_MS, run_backtest
from .demo import DemoMarketData, DemoStrategy
from .engine import PaperExecutionEngine
from .ledger import PaperLedger
from .models import ExecutionConfig
from .plugins import PluginRunner, load_plugin, load_settings
from .polymarket import PolymarketData, PublicHttpTransport
from .report import text_report, write_outputs
from .service import create_server
from .setup_tools import doctor, init_project
from .synth import generate


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


def _names(value: str | None) -> tuple[str, ...]:
    return tuple(item.strip() for item in (value or "").split(",") if item.strip())


def _settings(args: argparse.Namespace) -> dict[str, Any]:
    return load_settings(args.settings) if args.settings else {}


def command_doctor(args: argparse.Namespace) -> None:
    del args
    marks = {"ok": "ok     ", "problem": "PROBLEM", "info": "note   "}
    results = doctor()
    for row in results:
        print(f"  {marks[row['state']]}  {row['check']}: {row['detail']}")
    print()
    print("  This tool reads public data and simulates fills. It cannot place an order.")
    print("  You are responsible for following Polymarket's terms and your local laws.")
    if any(row["state"] == "problem" for row in results):
        raise SystemExit(1)


def command_init(args: argparse.Namespace) -> None:
    for path in init_project(args.folder):
        print(f"  wrote {path}")
    print(f"\n  Next: cd {args.folder} && pm-live demo --strategy strategy.py")


def command_demo(args: argparse.Namespace) -> None:
    out = args.out
    data = out / "data"
    if not data.exists():
        print(f"  Making {args.hours:g} hours of SYNTHETIC market data in {data} ...", flush=True)
        generate(data, hours=args.hours)
    result = run_backtest(
        data, args.strategy, config=_config(args.config), latency_ms=args.latency_ms
    )
    print(text_report(result))
    paths = write_outputs(result, out / "report")
    print(f"  Full report: {paths['html'].resolve()}")
    print("  Now try real data: pm-live record --series btc-updown-5m --out data")


def command_backtest(args: argparse.Namespace) -> None:
    result = run_backtest(
        args.data,
        args.strategy,
        _settings(args),
        _config(args.config),
        args.latency_ms,
        sweep=not args.no_sweep,
        series=_names(args.series) or None,
        slugs=_names(args.slug) or None,
        max_feed_lag_ms=args.max_feed_lag_ms,
        jobs=args.jobs,
    )
    print(text_report(result))
    out = args.out or Path("reports") / f"{result['strategy']}-{time.strftime('%Y%m%d-%H%M%S')}"
    paths = write_outputs(result, out)
    print(f"  Full report: {paths['html'].resolve()}")
    print(f"  Every trade: {paths['trades'].resolve()}")


def _needs_markets(series: tuple[str, ...], slugs: tuple[str, ...]) -> None:
    if not series and not slugs:
        raise SystemExit("Choose markets with --series btc-updown-5m or --slug exact-market-slug.")


def command_record(args: argparse.Namespace) -> None:
    from .paper import record

    series, slugs = _names(args.series), _names(args.slug)
    _needs_markets(series, slugs)
    record(args.out, series, slugs, reference=not args.no_reference, duration_seconds=args.seconds)


def command_paper(args: argparse.Namespace) -> None:
    from .paper import paper

    paper(
        args.strategy,
        args.state_dir,
        _names(args.series),
        _names(args.slug),
        _settings(args),
        _config(args.config),
        args.latency_ms,
        reference=not args.no_reference,
        keep_tape=not args.no_tape,
        host=args.host,
        port=args.port,
        max_feed_lag_ms=args.max_feed_lag_ms,
        duration_seconds=args.seconds,
    )


def command_report(args: argparse.Namespace) -> None:
    from .paper import report_from_state

    result = report_from_state(args.state_dir, _config(args.config))
    print(text_report(result))
    paths = write_outputs(result, args.out or args.state_dir / "report")
    print(f"  Full report: {paths['html'].resolve()}")


def command_results(args: argparse.Namespace) -> None:
    from .live import fetch_results

    _json(fetch_results(args.data))


def command_dataset(args: argparse.Namespace) -> None:
    from .dataset import export

    _json(
        export(args.data, args.out, args.every, args.entry_delay_ms / 1000, _names(args.series) or None)
    )


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, help="JSON file with execution limits")
    parser.add_argument("--settings", type=Path, help="JSON file passed to your strategy")
    parser.add_argument(
        "--latency-ms",
        type=float,
        default=DEFAULT_LATENCY_MS,
        help="delay from decision to fill (default %(default)s)",
    )
    parser.add_argument(
        "--max-feed-lag-ms",
        type=float,
        default=1000.0,
        help="refuse fills while the data feed is further behind than this (default %(default)s)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pm-live",
        description=(
            "Backtest and live paper test Polymarket strategies on your own machine. "
            "Uses public data and simulated fills. It cannot place real orders."
        ),
    )
    parser.add_argument("--version", action="version", version=f"pm-live {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    demo = subparsers.add_parser("demo", help="try everything offline on synthetic data")
    demo.add_argument("--strategy", default="builtin:late_favorite")
    demo.add_argument("--hours", type=float, default=12.0)
    demo.add_argument("--out", type=Path, default=Path("pm-live-demo"))
    demo.add_argument("--config", type=Path)
    demo.add_argument("--latency-ms", type=float, default=DEFAULT_LATENCY_MS)
    demo.set_defaults(handler=command_demo)

    init = subparsers.add_parser("init", help="create a starter strategy project")
    init.add_argument("folder", type=Path)
    init.set_defaults(handler=command_init)

    check = subparsers.add_parser("doctor", help="check that this machine is ready")
    check.set_defaults(handler=command_doctor)

    rec = subparsers.add_parser("record", help="save live public market data for backtests")
    rec.add_argument("--series", help="rolling markets, comma separated: btc-updown-5m,eth-updown-5m")
    rec.add_argument("--slug", help="exact market slugs, comma separated")
    rec.add_argument("--out", type=Path, default=Path("data"))
    rec.add_argument("--no-reference", action="store_true", help="skip Binance reference prices")
    rec.add_argument("--seconds", type=float, help="stop after this long (default: until Ctrl+C)")
    rec.set_defaults(handler=command_record)

    back = subparsers.add_parser("backtest", help="run a strategy on recorded data")
    back.add_argument("strategy", help="file.py, file.py:ClassName, or builtin:late_favorite")
    back.add_argument("--data", type=Path, default=Path("data"))
    back.add_argument("--series", help="only these series")
    back.add_argument("--slug", help="only these exact market slugs")
    back.add_argument("--out", type=Path, help="report folder")
    back.add_argument("--no-sweep", action="store_true", help="skip the delay comparison")
    back.add_argument("--jobs", type=int, default=4, help="parallel runs for the delay comparison")
    _add_common(back)
    back.set_defaults(handler=command_backtest)

    live = subparsers.add_parser("paper", help="run a strategy live with simulated fills")
    live.add_argument("strategy", help="file.py, file.py:ClassName, or builtin:late_favorite")
    live.add_argument("--series", help="rolling markets (default: the strategy's own list)")
    live.add_argument("--slug", help="exact market slugs")
    live.add_argument("--state-dir", type=Path, default=Path("paper-session"))
    live.add_argument("--no-reference", action="store_true", help="skip Binance reference prices")
    live.add_argument("--no-tape", action="store_true", help="do not keep the recorded data")
    live.add_argument("--host", default="127.0.0.1")
    live.add_argument("--port", type=int, default=8765, help="dashboard port, 0 to turn it off")
    live.add_argument("--seconds", type=float, help="stop after this long (default: until Ctrl+C)")
    _add_common(live)
    live.set_defaults(handler=command_paper)

    rep = subparsers.add_parser("report", help="build a report from a paper session")
    rep.add_argument("--state-dir", type=Path, default=Path("paper-session"))
    rep.add_argument("--out", type=Path)
    rep.add_argument("--config", type=Path)
    rep.set_defaults(handler=command_report)

    done = subparsers.add_parser("results", help="fetch results for recorded markets that ended")
    done.add_argument("--data", type=Path, default=Path("data"))
    done.set_defaults(handler=command_results)

    table = subparsers.add_parser("dataset", help="export recorded data for model training")
    table.add_argument("--data", type=Path, default=Path("data"))
    table.add_argument("--out", type=Path, default=Path("dataset.csv.gz"))
    table.add_argument("--every", type=float, default=5.0, help="seconds between rows per market")
    table.add_argument("--entry-delay-ms", type=float, default=450.0)
    table.add_argument("--series", help="only these series")
    table.set_defaults(handler=command_dataset)

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
    try:
        args.handler(args)
    except (FileNotFoundError, FileExistsError, ValueError, ImportError) as error:
        raise SystemExit(f"pm-live: {error}") from error
