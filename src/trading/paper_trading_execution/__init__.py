"""Trading execution module."""

# Import from parent execution.py module (for backward compatibility with old code)
# This allows both execution.py (module) and execution/ (package) to coexist
import sys
import importlib
parent_module = importlib.import_module('trading.execution', package=None)
sys.modules['trading.execution'] = parent_module

# Then re-export paper trading components
from .paper_trading_engine import PaperTradingEngine, AlertSystem, PaperTrade

__all__ = ["PaperTradingEngine", "AlertSystem", "PaperTrade"]
