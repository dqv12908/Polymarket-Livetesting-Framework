"""Minimal one-shot strategy plugin loaded with examples.simple_strategy:build."""

from __future__ import annotations

from typing import Any

from polymarket_livetesting import PaperSignal


class OneShotStrategy:
    def __init__(self, settings: dict[str, Any]) -> None:
        self.settings = settings
        self.emitted = False

    def next_signal(self, now_ns: int) -> PaperSignal | None:
        if self.emitted:
            return None
        self.emitted = True
        expected_at_ns = self.settings.get("expected_at_ns")
        return PaperSignal(
            signal_id=str(self.settings["signal_id"]),
            market_slug=str(self.settings["market_slug"]),
            outcome=str(self.settings["outcome"]).casefold(),
            probability=float(self.settings["probability"]),
            emitted_at_ns=now_ns,
            expected_at_ns=int(expected_at_ns) if expected_at_ns is not None else None,
            model=str(self.settings.get("model", "example")),
            trade_intent=bool(self.settings.get("trade_intent", True)),
        )


def build(settings: dict[str, Any]) -> OneShotStrategy:
    return OneShotStrategy(settings)
