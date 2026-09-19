from __future__ import annotations

import importlib
import inspect
import json
import threading
import time
from pathlib import Path
from typing import Any

from .engine import PaperExecutionEngine
from .models import PaperSignal
from .protocols import StrategyPlugin


def load_settings(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("plugin settings must be a JSON object")
    return value


def load_plugin(specification: str, settings: dict[str, Any]) -> StrategyPlugin:
    """Load ``module:attribute``. The attribute may be a plugin or a settings factory."""
    module_name, separator, attribute_name = specification.partition(":")
    if not separator or not module_name or not attribute_name:
        raise ValueError("plugin must use module:attribute syntax")
    attribute = getattr(importlib.import_module(module_name), attribute_name)
    if inspect.isclass(attribute):
        plugin = attribute(settings)
    elif hasattr(attribute, "next_signal"):
        plugin = attribute
    elif callable(attribute):
        plugin = attribute(settings)
    else:
        raise TypeError("plugin attribute must be an object, class, or factory")
    if not isinstance(plugin, StrategyPlugin):
        raise TypeError("loaded plugin does not implement next_signal(now_ns)")
    return plugin


class PluginRunner:
    def __init__(self, plugin: StrategyPlugin, engine: PaperExecutionEngine) -> None:
        self.plugin = plugin
        self.engine = engine

    def run_once(self) -> dict[str, Any]:
        now_ns = time.time_ns()
        signal = self.plugin.next_signal(now_ns)
        settlements = self.engine.resolve()
        if signal is None:
            return {"signal": None, "execution": None, "settlements": settlements}
        if not isinstance(signal, PaperSignal):
            raise TypeError("plugin returned a value other than PaperSignal or None")
        result = self.engine.execute(signal)
        return {
            "signal": signal.signal_id,
            "execution": result.to_dict(),
            "settlements": settlements,
        }

    def run_forever(self, poll_seconds: float, stop: threading.Event | None = None) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        stop = stop or threading.Event()
        while not stop.is_set():
            self.run_once()
            stop.wait(poll_seconds)
