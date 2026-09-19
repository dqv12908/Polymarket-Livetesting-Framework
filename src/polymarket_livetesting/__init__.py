"""Read-only Polymarket live-testing framework."""

from .engine import PaperExecutionEngine
from .ledger import PaperLedger
from .models import (
    BinaryMarket,
    BookLevel,
    BookSnapshot,
    ExecutionConfig,
    ExecutionResult,
    FillPlan,
    PaperSignal,
)
from .polymarket import PolymarketData, PublicHttpTransport
from .protocols import MarketData, StrategyPlugin

__all__ = [
    "BinaryMarket",
    "BookLevel",
    "BookSnapshot",
    "ExecutionConfig",
    "ExecutionResult",
    "FillPlan",
    "MarketData",
    "PaperExecutionEngine",
    "PaperLedger",
    "PaperSignal",
    "PolymarketData",
    "PublicHttpTransport",
    "StrategyPlugin",
]

__version__ = "0.1.0"
