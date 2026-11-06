"""Replay recorded data through a strategy, using the same session code as live paper testing."""

from __future__ import annotations

import hashlib
import json
import statistics
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .models import ExecutionConfig
from .report import summarize
from .session import MemoryLedger, Session
from .strategy import load_strategy
from .tape import read_tape, scan_metadata

DEFAULT_LATENCY_MS = 450.0
SWEEP_MS = (100.0, 250.0, 500.0, 1000.0, 2000.0)


def replay(
    data: Path,
    spec: str,
    settings: dict[str, Any] | None,
    config: ExecutionConfig,
    latency_ms: float,
    winners: dict[str, str],
    series: tuple[str, ...] | None = None,
    slugs: tuple[str, ...] | None = None,
    max_feed_lag_ms: float = 1000.0,
) -> dict[str, Any]:
    strategy = load_strategy(spec, settings)
    ledger = MemoryLedger(config)
    session = Session(
        strategy,
        ledger,
        config,
        latency_ms,
        max_feed_lag_ms=max_feed_lag_ms,
        winners=winners,
        series=series,
        slugs=slugs,
    )
    problems: list[str] = []
    for event in read_tape(data, problems):
        session.handle(event)
    session.finish()
    trades = list(ledger.state.get("settled", []))
    checks = ledger.state.get("validation_settled", [])
    filled = {t["signal"]["signal_id"] for t in trades}
    in_filled = [c["correct"] for c in checks if c["signal_id"] in filled]
    not_filled = [c["correct"] for c in checks if c["signal_id"] not in filled]
    return {
        "strategy": strategy.name,
        "latency_ms": latency_ms,
        "trades": trades,
        "summary": summarize(trades, config.starting_capital),
        "counts": session.counts,
        "skip_reasons": session.skip_reasons,
        "unsettled": len(ledger.state.get("positions", {})),
        "signal_check": {
            "filled": len(in_filled),
            "filled_win": sum(in_filled) / len(in_filled) if in_filled else None,
            "not_filled": len(not_filled),
            "not_filled_win": sum(not_filled) / len(not_filled) if not_filled else None,
        },
        "feed": _feed(session.lag_samples),
        "problems": problems,
    }


def _feed(samples: list[float]) -> dict[str, float] | None:
    if len(samples) < 20:
        return None
    ordered = sorted(samples)
    return {
        "median_ms": statistics.median(ordered),
        "p95_ms": ordered[int(len(ordered) * 0.95)],
    }


def _one(kwargs: dict[str, Any]) -> dict[str, Any]:
    kwargs["config"] = ExecutionConfig(**kwargs["config"])
    return replay(**kwargs)


def _note_trial(data: Path, spec: str, settings: dict[str, Any] | None) -> int:
    """Count distinct strategy versions tried on this data, to warn about trying too many."""
    data = Path(data)
    folder = data if data.is_dir() else data.parent
    log = folder / "trials.json"
    try:
        path = Path(spec.rsplit(":", 1)[0] if ":" in spec[2:] else spec)
        source = path.read_bytes() if path.is_file() else spec.encode()
        key = hashlib.sha256(source + json.dumps(settings or {}, sort_keys=True).encode()).hexdigest()
        seen = json.loads(log.read_text(encoding="utf-8")) if log.exists() else []
        if key not in seen:
            seen.append(key)
            log.write_text(json.dumps(seen), encoding="utf-8")
        return len(seen)
    except OSError:
        return 1


def run_backtest(
    data: Path,
    spec: str,
    settings: dict[str, Any] | None = None,
    config: ExecutionConfig | None = None,
    latency_ms: float = DEFAULT_LATENCY_MS,
    sweep: bool = True,
    series: tuple[str, ...] | None = None,
    slugs: tuple[str, ...] | None = None,
    max_feed_lag_ms: float = 1000.0,
    jobs: int = 4,
) -> dict[str, Any]:
    config = config or ExecutionConfig()
    meta = scan_metadata(data)
    common = {
        "data": data,
        "spec": spec,
        "settings": settings,
        "config": asdict(config),
        "winners": meta["winners"],
        "series": series,
        "slugs": slugs,
        "max_feed_lag_ms": max_feed_lag_ms,
    }
    delays = [latency_ms] + ([d for d in SWEEP_MS if d != latency_ms] if sweep else [])
    tasks = [{**common, "latency_ms": d} for d in delays]
    if jobs > 1 and len(tasks) > 1:
        with ProcessPoolExecutor(max_workers=min(jobs, len(tasks))) as pool:
            runs = list(pool.map(_one, tasks))
    else:
        runs = [_one(task) for task in tasks]
    main = runs[0]
    main["sweep"] = sorted(
        ({"latency_ms": r["latency_ms"], "summary": r["summary"]} for r in runs),
        key=lambda row: row["latency_ms"],
    )
    for row in main["sweep"]:
        row["summary"] = {k: v for k, v in row["summary"].items() if k != "equity_curve"}
    main["data"] = str(data)
    main["synthetic"] = meta["synthetic"]
    main["markets_in_data"] = len(meta["markets"])
    main["results_in_data"] = len(meta["winners"])
    main["problems"] = sorted(set(main["problems"]) | set(meta["problems"]))
    main["trial"] = _note_trial(data, spec, settings)
    return main
