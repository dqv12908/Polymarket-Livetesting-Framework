"""`pm-live init` writes a starter project; `pm-live doctor` checks that this machine is ready."""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path
from typing import Any

from .polymarket import CLOB, GAMMA, PublicHttpTransport

TEMPLATE = '''"""Your strategy. The same file runs in `pm-live backtest` and `pm-live paper`."""

from polymarket_livetesting import MarketView, Order, Strategy


class MyStrategy(Strategy):
    name = "my_strategy"
    series = ("btc-updown-5m",)   # markets to follow; see `pm-live record --help`
    tick_seconds = 1.0            # on_tick runs this often for each open market

    def on_tick(self, market: MarketView) -> Order | None:
        """Return an Order to buy one outcome, or None to do nothing this second.

        You get at most one position per market, held until it settles.
        """
        if market.seconds_left > 60:
            return None
        up_ask = market.best_ask("up")
        if up_ask is None or not 0.55 <= up_ask <= 0.90:
            return None
        move = market.reference_return_bp(30)     # reference price change over 30 s, or None
        if move is not None and move > 2:
            return Order("up", probability=up_ask + 0.02)
        return None
'''

README = """# My Polymarket strategy

Edit `strategy.py`, then:

```bash
pm-live record --series btc-updown-5m --out data   # collect real data; leave it running
pm-live backtest strategy.py --data data           # test on what you recorded
pm-live paper strategy.py                          # test live with simulated fills
```

Try the workflow first with made-up data:

```bash
pm-live demo --strategy strategy.py
```
"""


def init_project(folder: Path) -> list[Path]:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    for name, content in (("strategy.py", TEMPLATE), ("README.md", README)):
        path = folder / name
        if path.exists():
            raise FileExistsError(f"{path} already exists; nothing was overwritten")
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return written


def doctor() -> list[dict[str, Any]]:
    """Each check returns ok, problem, or info. Nothing here is signed or sent anywhere private."""
    checks: list[dict[str, Any]] = []

    def note(name: str, state: str, detail: str) -> None:
        checks.append({"check": name, "state": state, "detail": detail})

    good = sys.version_info >= (3, 11)
    note("Python", "ok" if good else "problem", sys.version.split()[0] + ("" if good else ": 3.11+ is required"))
    try:
        import websockets

        note("websockets", "ok", websockets.__version__)
    except ImportError:
        note("websockets", "problem", "not installed. Run: pip install websockets")
    free = shutil.disk_usage(".").free / 1e9
    note("Disk space", "ok" if free > 10 else "problem", f"{free:.0f} GB free. Recording uses about 1 GB per coin per day, and several GB for busy markets such as BTC.")
    http = PublicHttpTransport(timeout=8, attempts=1)
    for name, url in (("Polymarket market list", f"{GAMMA}/markets?limit=1"), ("Polymarket order books", f"{CLOB}/time")):
        try:
            _, ms = http.get(url)
            note(name, "ok", f"reachable in {ms:.0f} ms")
        except (ConnectionError, OSError, ValueError) as error:
            note(name, "problem", f"not reachable: {type(error).__name__}")
    try:
        from websockets.sync.client import connect

        from .live import POLYMARKET_WS

        started = time.perf_counter()
        with connect(POLYMARKET_WS, open_timeout=8, close_timeout=2):
            note("Polymarket live feed", "ok", f"connected in {(time.perf_counter() - started) * 1000:.0f} ms")
    except Exception as error:  # noqa: BLE001 - any failure here is the answer to the check
        note("Polymarket live feed", "problem", f"cannot connect: {type(error).__name__}")
    try:
        from websockets.sync.client import connect

        from .live import BINANCE_WS

        with connect(BINANCE_WS + "btcusdt@bookTicker", open_timeout=8, close_timeout=2):
            note("Reference prices (optional)", "ok", "Binance feed reachable")
    except Exception as error:  # noqa: BLE001
        note(
            "Reference prices (optional)",
            "info",
            f"Binance feed unavailable here ({type(error).__name__}). Use --no-reference; "
            "strategies then get None from market.reference().",
        )
    return checks
