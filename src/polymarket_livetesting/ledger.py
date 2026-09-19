from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import BinaryMarket, ExecutionConfig, ExecutionResult, PaperSignal


class PaperLedger:
    """Atomic account state plus an append-only JSONL audit trail."""

    def __init__(self, state_path: Path, events_path: Path, config: ExecutionConfig) -> None:
        self.state_path = Path(state_path)
        self.events_path = Path(events_path)
        self.config = config
        self._lock = threading.RLock()
        self.state = self._load()

    def _load(self) -> dict[str, Any]:
        if self.state_path.exists():
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if state.get("schema") != "polymarket-livetesting-ledger-v1":
                raise ValueError("unsupported paper ledger schema")
            return state
        return {
            "schema": "polymarket-livetesting-ledger-v1",
            "starting_capital": self.config.starting_capital,
            "cash": self.config.starting_capital,
            "realized_pnl": 0.0,
            "positions": {},
            "processed_signals": [],
            "settled": [],
            "validation_pending": {},
            "validation_settled": [],
            "created_at": datetime.now(UTC).isoformat(),
        }

    @property
    def cash(self) -> float:
        return float(self.state["cash"])

    @property
    def equity_at_cost(self) -> float:
        position_value = sum(
            float(position["total_debit"]) for position in self.state.get("positions", {}).values()
        )
        return self.cash + position_value

    def has_signal(self, signal_id: str) -> bool:
        with self._lock:
            return signal_id in self.state.get("processed_signals", [])

    def has_market(self, condition_id: str) -> bool:
        with self._lock:
            return condition_id in self.state.get("positions", {}) or any(
                item.get("condition_id") == condition_id for item in self.state.get("settled", [])
            )

    def register_validation(self, signal: PaperSignal) -> None:
        with self._lock:
            settled_ids = {
                str(item.get("signal_id"))
                for item in self.state.setdefault("validation_settled", [])
            }
            pending = self.state.setdefault("validation_pending", {})
            if signal.signal_id in settled_ids or signal.signal_id in pending:
                return
            pending[signal.signal_id] = {
                "signal_id": signal.signal_id,
                "market_slug": signal.market_slug,
                "outcome": signal.outcome,
                "probability": signal.probability,
                "model": signal.model,
                "emitted_at_ns": signal.emitted_at_ns,
                "market_id": None,
                "condition_id": None,
                "end_epoch": None,
            }
            self.save()

    def attach_validation_market(self, signal_id: str, market: BinaryMarket) -> None:
        with self._lock:
            pending = self.state.setdefault("validation_pending", {})
            if signal_id not in pending:
                return
            pending[signal_id].update(
                {
                    "market_id": market.market_id,
                    "market_slug": market.slug,
                    "condition_id": market.condition_id,
                    "end_epoch": market.end_epoch,
                }
            )
            self.save()

    def settle_validation(self, signal_id: str, winning_outcome: str) -> dict[str, Any]:
        with self._lock:
            item = self.state.setdefault("validation_pending", {}).pop(signal_id)
            record = {
                **item,
                "winning_outcome": winning_outcome.casefold(),
                "correct": item["outcome"] == winning_outcome.casefold(),
                "settled_at": datetime.now(UTC).isoformat(),
            }
            self.state.setdefault("validation_settled", []).append(record)
            self.append_event({"status": "validation_settled", **record})
            self.save()
            return record

    def record_result(self, result: ExecutionResult) -> None:
        with self._lock:
            processed = self.state.setdefault("processed_signals", [])
            if result.signal.signal_id not in processed:
                processed.append(result.signal.signal_id)
                del processed[:-10_000]
            if result.status == "filled" and result.market and result.plan:
                debit = result.plan.total_debit
                if debit > self.cash + 1e-9:
                    raise ValueError("fill debit exceeds paper cash")
                self.state["cash"] = self.cash - debit
                self.state.setdefault("positions", {})[result.market.condition_id] = {
                    "condition_id": result.market.condition_id,
                    "market_id": result.market.market_id,
                    "slug": result.market.slug,
                    "end_epoch": result.market.end_epoch,
                    "outcome": result.plan.outcome,
                    "token_id": result.plan.token_id,
                    "shares": result.plan.shares,
                    "cost": result.plan.cost,
                    "fee": result.plan.fee,
                    "total_debit": debit,
                    "vwap": result.plan.vwap,
                    "break_even_probability": result.plan.break_even_probability,
                    "probability": result.plan.probability,
                    "expected_profit": result.plan.expected_profit,
                    "opened_at": datetime.now(UTC).isoformat(),
                    "signal": asdict(result.signal),
                }
            self.append_event(result.to_dict())
            self.save()

    def settle(self, condition_id: str, winning_outcome: str) -> dict[str, Any]:
        with self._lock:
            position = self.state.get("positions", {}).pop(condition_id)
            winner = winning_outcome.casefold()
            payout = float(position["shares"]) if position["outcome"] == winner else 0.0
            pnl = payout - float(position["total_debit"])
            self.state["cash"] = self.cash + payout
            self.state["realized_pnl"] = float(self.state.get("realized_pnl", 0.0)) + pnl
            record = {
                **position,
                "winning_outcome": winner,
                "payout": payout,
                "pnl": pnl,
                "settled_at": datetime.now(UTC).isoformat(),
            }
            self.state.setdefault("settled", []).append(record)
            self.append_event({"status": "settled", **record})
            self.save()
            return record

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            validations = self.state.get("validation_settled", [])
            correct = sum(bool(item["correct"]) for item in validations)
            return {
                "schema": self.state["schema"],
                "starting_capital": self.state["starting_capital"],
                "cash": self.cash,
                "equity_at_cost": self.equity_at_cost,
                "realized_pnl": float(self.state.get("realized_pnl", 0.0)),
                "open_positions": len(self.state.get("positions", {})),
                "settled_positions": len(self.state.get("settled", [])),
                "validation_pending": len(self.state.get("validation_pending", {})),
                "validation_settled": len(validations),
                "validation_accuracy": correct / len(validations) if validations else None,
            }

    def append_event(self, value: dict[str, Any]) -> None:
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a", encoding="utf-8") as events:
            events.write(json.dumps(value, separators=(",", ":"), default=str) + "\n")
            events.flush()
            os.fsync(events.fileno())

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_name(
            self.state_path.name + "." + uuid.uuid4().hex + ".tmp"
        )
        try:
            with temporary.open("x", encoding="utf-8") as output:
                json.dump(self.state, output, indent=2, sort_keys=True)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.state_path)
            if os.name != "nt":
                descriptor = os.open(self.state_path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        finally:
            temporary.unlink(missing_ok=True)
