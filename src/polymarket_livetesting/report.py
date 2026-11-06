"""Turn settled trades into an honest report: size of the edge, its uncertainty, and what breaks it."""

from __future__ import annotations

import csv
import html
import json
import math
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MIN_TRADES = 100


def _meta(trade: dict[str, Any]) -> dict[str, Any]:
    return (trade.get("signal") or {}).get("metadata") or {}


def _decided_at(trade: dict[str, Any]) -> float:
    return float((trade.get("signal") or {}).get("emitted_at_ns") or 0) / 1e9


def _clustered(values: list[tuple[Any, float]]) -> tuple[float, float, int]:
    """Mean and standard error, with trades in the same time window treated as one observation.

    Markets on different coins that end at the same second tend to win or lose together, so
    counting them separately would overstate how much evidence there is.
    """
    if not values:
        return math.nan, math.nan, 0
    groups: dict[Any, list[float]] = defaultdict(list)
    for key, value in values:
        groups[key].append(value)
    n, mean = sum(len(v) for v in groups.values()), sum(sum(v) for v in groups.values())
    mean /= n
    if len(groups) < 2:
        return mean, math.nan, len(groups)
    total = sum(sum(x - mean for x in v) ** 2 for v in groups.values())
    return mean, math.sqrt(total * len(groups) / (len(groups) - 1)) / n, len(groups)


def summarize(trades: list[dict[str, Any]], starting_capital: float = 0.0) -> dict[str, Any]:
    if not trades:
        return {"trades": 0}
    ordered = sorted(trades, key=_decided_at)
    rows = []
    for t in ordered:
        meta = _meta(t)
        shares = float(t["shares"])
        rows.append(
            {
                "key": meta.get("start") or int(_decided_at(t) // 300),
                "net": float(t["pnl"]) / shares,
                "won": 1.0 if t["outcome"] == t["winning_outcome"] else 0.0,
                "vwap": float(t["vwap"]),
                "fee": float(t["fee"]) / shares,
                "mid": meta.get("mid_at_decision"),
                "ask": meta.get("ask_at_decision"),
                "pnl": float(t["pnl"]),
                "day": datetime.fromtimestamp(_decided_at(t), UTC).strftime("%Y-%m-%d"),
            }
        )
    net, se, windows = _clustered([(r["key"], r["net"]) for r in rows])
    with_mid = [r for r in rows if r["mid"] is not None]
    skill, skill_se, _ = _clustered([(r["key"], r["won"] - float(r["mid"])) for r in with_mid])
    cost, _, _ = _clustered([(r["key"], r["vwap"] + r["fee"] - float(r["mid"])) for r in with_mid])
    equity, peak, drawdown = starting_capital, starting_capital, 0.0
    curve = []
    for r in rows:
        equity += r["pnl"]
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
        curve.append(equity)
    days: dict[str, float] = defaultdict(float)
    for r in rows:
        days[r["day"]] += r["pnl"]
    half = len(rows) // 2
    return {
        "trades": len(rows),
        "independent_windows": windows,
        "win_rate": sum(r["won"] for r in rows) / len(rows),
        "avg_price_paid": sum(r["vwap"] for r in rows) / len(rows),
        "avg_fee_per_share": sum(r["fee"] for r in rows) / len(rows),
        "net_cents_per_share": net * 100,
        "se_cents": se * 100,
        "t_stat": net / se if se and not math.isnan(se) and se > 0 else math.nan,
        "ci95_cents": None if math.isnan(se) else ((net - 1.96 * se) * 100, (net + 1.96 * se) * 100),
        "skill_cents": skill * 100,
        "skill_se_cents": skill_se * 100,
        "cost_cents": cost * 100,
        "total_pnl": sum(r["pnl"] for r in rows),
        "max_drawdown": drawdown,
        "days": len(days),
        "days_positive": sum(1 for v in days.values() if v > 0),
        "worst_day": min(days.values()),
        "best_day_share_of_profit": (
            max(days.values()) / sum(v for v in days.values() if v > 0)
            if any(v > 0 for v in days.values())
            else None
        ),
        "first_half_cents": sum(r["net"] for r in rows[:half]) / max(half, 1) * 100,
        "second_half_cents": sum(r["net"] for r in rows[half:]) / max(len(rows) - half, 1) * 100,
        "equity_curve": curve,
    }


def verdict(main: dict[str, Any], sweep: list[dict[str, Any]], synthetic: bool) -> tuple[str, list[str]]:
    notes: list[str] = []
    if synthetic:
        notes.append("This data is SYNTHETIC. It shows that the tool works, nothing about real markets.")
    n = main.get("trades", 0)
    if n == 0:
        return "NO TRADES", notes + ["The strategy never filled an order on this data."]
    if n < MIN_TRADES:
        notes.append(f"Only {n} trades. Under {MIN_TRADES} the numbers are mostly noise.")
    t = main.get("t_stat", math.nan)
    net = main.get("net_cents_per_share", 0.0)
    if (main.get("first_half_cents", 0) > 0) != (main.get("second_half_cents", 0) > 0):
        notes.append("The first and second half of the data disagree in sign, so it may not be stable.")
    best = main.get("best_day_share_of_profit")
    if best is not None and best > 0.5 and main.get("days", 0) >= 5:
        notes.append(f"One day made {best:.0%} of the gross profit.")
    slower = [row for row in sweep if row["latency_ms"] > main.get("latency_ms", 0)]
    if net > 0 and any(row["summary"].get("net_cents_per_share", 0) <= 0 for row in slower):
        notes.append("The profit disappears at a longer delay. Measure your real delay before trusting it.")
    if math.isnan(t) or n < MIN_TRADES:
        return "TOO LITTLE DATA TO SAY", notes
    if t <= -2:
        return "LOSING", notes
    if abs(t) < 2:
        return "NOT DISTINGUISHABLE FROM ZERO", notes
    notes.append(
        "Promising is not proof. If you tried many versions, the best one looks good partly by "
        "luck. Next step: run it unchanged on data it has never seen, then on `pm-live paper`."
    )
    return "PROMISING, NOT PROOF", notes


def _fmt(value: Any, suffix: str = "") -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n/a"
    return f"{value:+.2f}{suffix}"


def text_report(result: dict[str, Any]) -> str:
    m = result["summary"]
    title, notes = verdict({**m, "latency_ms": result["latency_ms"]}, result["sweep"], result["synthetic"])
    lines = [
        "",
        f"  Strategy   {result['strategy']}",
        f"  Data       {result['data']}" + ("   (SYNTHETIC)" if result["synthetic"] else ""),
        f"  Delay      {result['latency_ms']:.0f} ms from decision to fill",
        f"  Trial      #{result.get('trial', 1)} on this data",
        "",
        f"  VERDICT    {title}",
        "",
    ]
    if m.get("trades"):
        ci = m.get("ci95_cents") or (math.nan, math.nan)
        lines += [
            f"  Trades               {m['trades']}  ({m['independent_windows']} independent time windows)",
            f"  Won / avg price      {m['win_rate']:.1%} at {m['avg_price_paid']:.3f}",
            (
                f"  Net per share        {_fmt(m['net_cents_per_share'], 'c')}   "
                f"95% range {_fmt(ci[0], 'c')} to {_fmt(ci[1], 'c')}"
            ),
            f"    skill vs price     {_fmt(m['skill_cents'], 'c')} (how often you beat the market's own odds)",
            f"    cost to trade      {_fmt(-m['cost_cents'], 'c')} (fee, spread, and movement during the delay)",
            (
                f"  Total P&L            ${m['total_pnl']:+,.2f}   "
                f"worst day ${m['worst_day']:+,.2f}   max drawdown ${m['max_drawdown']:,.2f}"
            ),
            f"  Positive days        {m['days_positive']} of {m['days']}",
            f"  First / second half  {_fmt(m['first_half_cents'], 'c')} / {_fmt(m['second_half_cents'], 'c')}",
        ]
    c = result["counts"]
    lines += ["", f"  Signals {c['signals']}, filled {c['fills']}, not filled {c['skips']}"]
    for reason, count in sorted(result["skip_reasons"].items(), key=lambda x: -x[1]):
        lines.append(f"    {count:>6}  {reason}")
    missed = result.get("signal_check")
    if missed and missed.get("not_filled"):
        lines.append(
            f"  Win rate of signals: filled {missed['filled_win']:.1%}, not filled "
            f"{missed['not_filled_win']:.1%} ({missed['not_filled']} signals)"
        )
    if result["sweep"]:
        lines += [
            "",
            "  If your orders were slower or faster:",
            "    delay ms    trades   net c/share   std err",
        ]
        for row in result["sweep"]:
            s = row["summary"]
            lines.append(
                f"    {row['latency_ms']:>8.0f}  {s.get('trades', 0):>7}   "
                f"{_fmt(s.get('net_cents_per_share')):>11}   {_fmt(s.get('se_cents')):>7}"
            )
    feed = result.get("feed") or {}
    if feed:
        median, p95 = feed["median_ms"], feed["p95_ms"]
        lines += ["", f"  Data feed delay      median {median:.0f} ms, 95th pct {p95:.0f} ms"]
        if feed["p95_ms"] > 1000:
            notes.append(
                "The recorder was often over a second behind the exchange. Slow recording makes "
                "stale prices look tradable, which fakes profits. Treat these results with care."
            )
    if result.get("problems"):
        notes.append(f"{len(result['problems'])} data file(s) were damaged; the readable part was used.")
    unresolved = result.get("unsettled", 0)
    if unresolved:
        notes.append(
            f"{unresolved} position(s) have no recorded result and are not counted. "
            "Run `pm-live results` to fetch results for markets that have since resolved."
        )
    if result.get("trial", 1) > 3:
        notes.append(
            f"You have now tried {result['trial']} variations on this data. The more you try, the "
            "more likely the best one is just lucky."
        )
    if notes:
        lines += ["", "  Read this before believing the numbers:"] + [f"   - {n}" for n in notes]
    lines += [
        "",
        "  Simulated: taker orders at the displayed ask, holding to settlement. Real fills can be",
        "  worse. This is a research tool and not financial advice.",
        "",
    ]
    return "\n".join(lines)


def _svg_curve(curve: list[float], width: int = 760, height: int = 200) -> str:
    if len(curve) < 2:
        return ""
    low, high = min(curve), max(curve)
    span = (high - low) or 1.0
    points = " ".join(
        f"{i * width / (len(curve) - 1):.1f},{height - 8 - (v - low) * (height - 16) / span:.1f}"
        for i, v in enumerate(curve)
    )
    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Account value after each trade">'
        f'<polyline fill="none" stroke="var(--accent)" stroke-width="2" points="{points}"/></svg>'
        f'<p class="muted">Account value after each settled trade: ${curve[0]:,.2f} to ${curve[-1]:,.2f}</p>'
    )


def html_report(result: dict[str, Any]) -> str:
    text = html.escape(text_report(result))
    curve = _svg_curve(result["summary"].get("equity_curve") or [])
    banner = '<p class="banner">Synthetic data. This shows the tool working, not a real result.</p>' if result["synthetic"] else ""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>Backtest: {html.escape(result['strategy'])}</title><style>"
        ":root{--bg:#fff;--fg:#1a1a1a;--muted:#666;--accent:#2563eb;--warn:#fef3c7;--warnfg:#78350f}"
        "@media (prefers-color-scheme:dark){:root{--bg:#111;--fg:#eee;--muted:#999;--accent:#60a5fa;--warn:#422006;--warnfg:#fde68a}}"
        "body{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;max-width:800px;margin:0 auto;padding:24px 16px}"
        "pre{white-space:pre-wrap;font:13px/1.5 ui-monospace,Consolas,monospace}svg{width:100%;height:auto}"
        ".muted{color:var(--muted);font-size:13px}.banner{background:var(--warn);color:var(--warnfg);padding:10px 14px;border-radius:6px}"
        f"</style></head><body><h1>Backtest report</h1>{banner}{curve}<pre>{text}</pre></body></html>"
    )


def write_outputs(result: dict[str, Any], folder: Path) -> dict[str, Path]:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "report.txt").write_text(text_report(result), encoding="utf-8")
    (folder / "report.html").write_text(html_report(result), encoding="utf-8")
    clean = {k: v for k, v in result.items() if k != "trades"}
    (folder / "summary.json").write_text(json.dumps(clean, indent=2, default=str), encoding="utf-8")
    with (folder / "trades.csv").open("w", newline="", encoding="utf-8") as output:
        writer = csv.writer(output)
        writer.writerow(
            ["slug", "outcome", "winner", "seconds_in", "ask_at_decision", "mid_at_decision",
             "stated_probability", "avg_price", "shares", "cost", "fee", "pnl"]
        )
        for t in result["trades"]:
            meta = _meta(t)
            writer.writerow(
                [t["slug"], t["outcome"], t["winning_outcome"], meta.get("seconds_in"),
                 meta.get("ask_at_decision"), meta.get("mid_at_decision"), meta.get("stated_probability"),
                 t["vwap"], t["shares"], t["cost"], t["fee"], t["pnl"]]
            )
    return {
        "text": folder / "report.txt",
        "html": folder / "report.html",
        "json": folder / "summary.json",
        "trades": folder / "trades.csv",
    }
