"""Backtest and live paper test Polymarket strategies on your own machine. No real orders."""

from .backtest import run_backtest
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
from .state import MarketView
from .strategy import Order, Strategy, load_strategy

__all__ = [
    "BinaryMarket",
    "BookLevel",
    "BookSnapshot",
    "ExecutionConfig",
    "ExecutionResult",
    "FillPlan",
    "MarketData",
    "MarketView",
    "Order",
    "PaperExecutionEngine",
    "PaperLedger",
    "PaperSignal",
    "PolymarketData",
    "PublicHttpTransport",
    "Strategy",
    "StrategyPlugin",
    "load_strategy",
    "run_backtest",
]

__version__ = "0.2.0"
