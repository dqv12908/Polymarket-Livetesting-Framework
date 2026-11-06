"""The strategy interface shared by backtests and live paper tests."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import inspect
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .state import MarketView


@dataclass(frozen=True)
class Order:
    """Buy one outcome at the ask and hold to settlement.

    ``probability`` is your estimate that the outcome wins. It is logged for analysis and never
    decides the fill. ``usd`` lowers the size below the configured budget.
    """

    outcome: str
    probability: float | None = None
    usd: float | None = None
    note: dict[str, Any] = field(default_factory=dict)


class Strategy:
    """Subclass this and implement ``on_tick``. The same file runs in backtest and paper modes."""

    name = "strategy"
    series: tuple[str, ...] = ()  # default markets, for example ("btc-updown-5m",)
    slugs: tuple[str, ...] = ()  # or exact market slugs
    tick_seconds = 1.0  # how often on_tick runs for each open market
    pre_open_seconds = 0.0  # start calling on_tick this long before a market's window opens
    max_attempts_per_market = 3  # new tries allowed after an order fails to fill

    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self.settings = settings or {}

    def on_tick(self, market: MarketView) -> Order | None:
        raise NotImplementedError

    def on_settle(self, record: dict[str, Any]) -> None:
        """Called when one of your positions settles."""


BUILTIN = ("late_favorite", "random_baseline")


def _load_file(path: Path) -> Any:
    digest = hashlib.sha1(str(path.resolve()).encode()).hexdigest()[:12]
    name = f"pm_live_user_{digest}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load strategy file {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    sys.path.insert(0, str(path.resolve().parent))  # so a strategy can import its own helpers
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def load_strategy(spec: str, settings: dict[str, Any] | None = None) -> Strategy:
    """Load ``file.py``, ``file.py:ClassName``, ``module:ClassName`` or ``builtin:name``."""
    settings = settings or {}
    attribute = None
    if spec.startswith("builtin:"):
        name = spec.split(":", 1)[1]
        if name not in BUILTIN:
            raise ValueError(f"unknown builtin strategy {name!r}; choose from {list(BUILTIN)}")
        module = importlib.import_module(f"polymarket_livetesting.builtin.{name}")
    else:
        target = spec
        head, separator, tail = spec.rpartition(":")
        # the length check keeps a Windows drive letter such as C:\ out of the split
        if separator and tail.isidentifier() and len(head) > 1:
            target, attribute = head, tail
        path = Path(target)
        if path.suffix == ".py" or path.exists():
            if not path.exists():
                raise FileNotFoundError(f"strategy file not found: {path}")
            module = _load_file(path)
        else:
            module = importlib.import_module(target)
    if attribute:
        chosen = getattr(module, attribute)
    else:
        found = [
            item
            for _, item in inspect.getmembers(module, inspect.isclass)
            if issubclass(item, Strategy) and item is not Strategy
            and item.__module__ == module.__name__
        ]
        if len(found) != 1:
            names = [item.__name__ for item in found]
            raise ValueError(f"expected one Strategy class in {spec}, found {names}. Use file.py:Name")
        chosen = found[0]
    strategy = chosen(settings) if callable(chosen) else chosen
    if not hasattr(strategy, "on_tick"):
        raise TypeError(f"{spec} does not provide on_tick(market)")
    return strategy
