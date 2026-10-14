"""Short-term Bollinger Band strategy for the supplied A-share universe."""

from .strategy import StrategyConfig, add_indicators, evaluate_latest, backtest_one

__all__ = ["StrategyConfig", "add_indicators", "evaluate_latest", "backtest_one"]
__version__ = "0.1.0"
