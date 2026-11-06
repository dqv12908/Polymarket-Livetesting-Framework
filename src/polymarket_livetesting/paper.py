"""Record public market data, and run a strategy live against it with simulated fills."""

from __future__ import annotations

import json
import queue
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .ledger import PaperLedger
from .live import LiveFeed
from .models import ExecutionConfig
from .report import summarize
from .session import Session
from .strategy import load_strategy
from .tape import TapeWriter

STATUS_EVERY_SECONDS = 60.0


def _say(text: str) -> None:
    print(time.strftime("%H:%M:%S ") + text, flush=True)


def _status_line(feed: LiveFeed, writer: TapeWriter | None, extra: str = "") -> str:
    age = time.time() - feed.stats.last_pm if feed.stats.last_pm else None
    parts = [
        f"markets {len(feed.known)}",
        f"book messages {feed.stats.pm_messages:,}",
        f"reference {feed.stats.ref_messages:,}",
        f"last book message {age:.0f}s ago" if age is not None else "waiting for the first message",
    ]
    if writer is not None:
        parts.append(f"recorded {writer.events:,} events")
    if feed.stats.reconnects:
        parts.append(f"reconnects {feed.stats.reconnects}")
    if feed.stats.last_error:
        parts.append(f"last problem: {feed.stats.last_error}")
    return " | ".join(parts) + (f" | {extra}" if extra else "")


def record(
    out: Path,
    series: tuple[str, ...],
    slugs: tuple[str, ...] = (),
    reference: bool = True,
    duration_seconds: float | None = None,
) -> None:
    out = Path(out)
    feed = LiveFeed(series, slugs, reference)
    writer = TapeWriter(out)
    feed.start()
    _say(f"Recording to {out.resolve()}. Press Ctrl+C to stop; recording can resume at any time.")
    next_status = time.monotonic() + 10
    finish = time.monotonic() + duration_seconds if duration_seconds else None
    try:
        while finish is None or time.monotonic() < finish:
            try:
                event = feed.events.get(timeout=0.5)
            except queue.Empty:
                event = None
            if event is not None:
                writer.write(event["t"], event["k"], event["m"])
            if time.monotonic() >= next_status:
                free = shutil.disk_usage(out).free / 1e9
                _say(_status_line(feed, writer, f"disk free {free:.0f} GB"))
                next_status = time.monotonic() + STATUS_EVERY_SECONDS
    except KeyboardInterrupt:
        _say("Stopping.")
    finally:
        feed.stop()
        writer.close()
    _say(f"Saved {writer.events:,} events. Next: pm-live backtest your_strategy.py --data {out}")


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Paper test</title><style>
:root{--bg:#fff;--fg:#1a1a1a;--muted:#666;--line:#ddd}
@media (prefers-color-scheme:dark){:root{--bg:#111;--fg:#eee;--muted:#999;--line:#333}}
body{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;max-width:900px;margin:0 auto;padding:20px 16px}
table{border-collapse:collapse;width:100%;font-size:13px}td,th{border-bottom:1px solid var(--line);padding:5px 8px;text-align:left}
.g{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:16px 0}.g b{display:block;font-size:20px}
.m{color:var(--muted);font-size:13px}.w{overflow-x:auto}</style></head><body>
<h1 id="n">Paper test</h1><p class="m">Simulated fills on live data. No real orders are placed.</p><div class="g" id="g"></div>
<h2>Open positions</h2><div class="w"><table id="o"></table></div><h2>Recent settled</h2><div class="w"><table id="s"></table></div>
<script>
const esc=v=>String(v??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const money=v=>v==null?"n/a":(v<0?"-$":"$")+Math.abs(v).toFixed(2);
async function t(){try{const d=await(await fetch("/api/status")).json();
document.getElementById("n").textContent="Paper test: "+d.strategy;
const c=[["Cash",money(d.account.cash)],["Realized P&L",money(d.account.realized_pnl)],["Open",d.account.open_positions],["Settled",d.account.settled_positions],
["Win rate",d.summary.win_rate==null?"n/a":(d.summary.win_rate*100).toFixed(1)+"%"],["Net per share",d.summary.net_cents_per_share==null?"n/a":d.summary.net_cents_per_share.toFixed(2)+"c"],
["Feed delay",d.feed_lag_ms==null?"n/a":d.feed_lag_ms.toFixed(0)+" ms"],["Markets",d.markets]];
document.getElementById("g").innerHTML=c.map(x=>`<div><span class="m">${esc(x[0])}</span><b>${esc(x[1])}</b></div>`).join("");
document.getElementById("o").innerHTML="<tr><th>Market</th><th>Side</th><th>Price</th><th>Cost</th></tr>"+d.open.map(p=>`<tr><td>${esc(p.slug)}</td><td>${esc(p.outcome)}</td><td>${p.vwap.toFixed(3)}</td><td>${money(p.total_debit)}</td></tr>`).join("");
document.getElementById("s").innerHTML="<tr><th>Market</th><th>Side</th><th>Winner</th><th>Price</th><th>P&amp;L</th></tr>"+d.settled.map(p=>`<tr><td>${esc(p.slug)}</td><td>${esc(p.outcome)}</td><td>${esc(p.winning_outcome)}</td><td>${p.vwap.toFixed(3)}</td><td>${money(p.pnl)}</td></tr>`).join("");
}catch(e){}}t();setInterval(t,3000);
</script></body></html>"""


def _serve_status(status: Any, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/api/status":
                body, kind = json.dumps(status(), default=str).encode(), "application/json"
            elif path == "/":
                body, kind = PAGE.encode(), "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True).start()
    return server


def paper(
    spec: str,
    state_dir: Path,
    series: tuple[str, ...] = (),
    slugs: tuple[str, ...] = (),
    settings: dict[str, Any] | None = None,
    config: ExecutionConfig | None = None,
    latency_ms: float = 450.0,
    reference: bool = True,
    keep_tape: bool = True,
    host: str = "127.0.0.1",
    port: int = 8765,
    max_feed_lag_ms: float = 1000.0,
    duration_seconds: float | None = None,
) -> dict[str, Any]:
    config = config or ExecutionConfig()
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    strategy = load_strategy(spec, settings)
    series = tuple(series) or tuple(strategy.series)
    slugs = tuple(slugs) or tuple(strategy.slugs)
    ledger = PaperLedger(state_dir / "state.json", state_dir / "events.jsonl", config)
    open_positions = list(ledger.state.get("positions", {}).values())
    session = Session(strategy, ledger, config, latency_ms, max_feed_lag_ms, series=series, slugs=slugs)
    session.holding |= {p["slug"] for p in open_positions}
    session.holding |= {t["slug"] for t in ledger.state.get("settled", [])}
    watch = [
        {
            "slug": p["slug"],
            "market_id": p["market_id"],
            "condition_id": p["condition_id"],
            "end": p["end_epoch"],
            "series": (p.get("signal") or {}).get("metadata", {}).get("series", ""),
            "tokens": {},
        }
        for p in open_positions
    ]
    feed = LiveFeed(series, slugs, reference, watch=watch)
    writer = TapeWriter(state_dir / "tape") if keep_tape else None

    def status() -> dict[str, Any]:
        with ledger._lock:
            settled = list(ledger.state.get("settled", []))
            opened = list(ledger.state.get("positions", {}).values())
            account = ledger.snapshot()
        summary = {k: v for k, v in summarize(settled, config.starting_capital).items() if k != "equity_curve"}
        keep = ("slug", "outcome", "vwap", "total_debit", "winning_outcome", "pnl")
        return {
            "strategy": strategy.name,
            "paper_only": True,
            "order_submission_enabled": False,
            "account": account,
            "summary": summary,
            "counts": session.counts,
            "skip_reasons": session.skip_reasons,
            "feed_lag_ms": session.feed_lag_ms(),
            "markets": len(feed.known),
            "open": [{k: p.get(k) for k in keep} for p in opened],
            "settled": [{k: p.get(k) for k in keep} for p in settled[-30:]][::-1],
        }

    (state_dir / "session.json").write_text(
        json.dumps({"strategy": strategy.name, "latency_ms": latency_ms}), encoding="utf-8"
    )
    server = _serve_status(status, host, port) if port else None
    feed.start()
    _say(f"Paper test of {strategy.name}: simulated fills on live data, {latency_ms:.0f} ms delay.")
    _say(f"No real orders can be placed. State and audit log: {state_dir.resolve()}")
    if server:
        _say(f"Dashboard: http://{host}:{server.server_address[1]}/   Press Ctrl+C to stop.")
    next_status = time.monotonic() + 10
    finish = time.monotonic() + duration_seconds if duration_seconds else None
    try:
        while finish is None or time.monotonic() < finish:
            try:
                event = feed.events.get(timeout=0.2)
            except queue.Empty:
                session.advance(time.time_ns())
                continue
            if writer is not None:
                writer.write(event["t"], event["k"], event["m"])
            session.handle(event)
            if time.monotonic() >= next_status:
                snap = ledger.snapshot()
                lags = session.feed_lag_ms()
                _say(
                    _status_line(
                        feed,
                        writer,
                        f"filled {session.counts['fills']} of {session.counts['signals']} signals | "
                        f"settled {snap['settled_positions']} | P&L ${snap['realized_pnl']:+.2f}"
                        + (f" | feed delay {lags:.0f} ms" if lags is not None else ""),
                    )
                )
                next_status = time.monotonic() + STATUS_EVERY_SECONDS
    except KeyboardInterrupt:
        _say("Stopping. Open positions stay in the ledger and resume settling on the next run.")
    finally:
        feed.stop()
        if writer is not None:
            writer.close()
        if server:
            server.shutdown()
            server.server_close()
    return status()


def report_from_state(state_dir: Path, config: ExecutionConfig | None = None) -> dict[str, Any]:
    """Build the same report as a backtest from a paper session's ledger."""
    config = config or ExecutionConfig()
    state_dir = Path(state_dir)
    ledger = PaperLedger(state_dir / "state.json", state_dir / "events.jsonl", config)
    trades = list(ledger.state.get("settled", []))
    names = {(t.get("signal") or {}).get("model") for t in trades}
    reasons: dict[str, int] = {}
    signals = fills = skips = 0
    info_path = state_dir / "session.json"
    info = json.loads(info_path.read_text(encoding="utf-8")) if info_path.exists() else {}
    events = state_dir / "events.jsonl"
    if events.exists():
        for line in events.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            status = row.get("status")
            if status == "filled":
                signals += 1
                fills += 1
            elif status == "skipped":
                signals += 1
                skips += 1
                reasons[row.get("reason", "?")] = reasons.get(row.get("reason", "?"), 0) + 1
    return {
        "strategy": ", ".join(sorted(str(n) for n in names)) or "paper session",
        "data": str(state_dir),
        "latency_ms": float(info.get("latency_ms", 0.0)),
        "trades": trades,
        "summary": summarize(trades, config.starting_capital),
        "counts": {"signals": signals, "fills": fills, "skips": skips},
        "skip_reasons": reasons,
        "unsettled": len(ledger.state.get("positions", {})),
        "signal_check": None,
        "feed": None,
        "problems": [],
        "sweep": [],
        "synthetic": False,
        "trial": 1,
    }
