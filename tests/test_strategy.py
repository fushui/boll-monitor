import numpy as np
import pandas as pd
import unittest

from boll_strategy.strategy import StrategyConfig, add_indicators, backtest_one, evaluate_latest


def bars(closes):
    closes = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {
            "date": pd.date_range("2025-01-01", periods=len(closes), freq="B"),
            "open": closes * 1.002,
            "high": closes * 1.02,
            "low": closes * 0.97,
            "close": closes,
            "volume": np.full(len(closes), 1_000_000),
        }
    )


def permissive_config(**overrides):
    values = dict(max_rsi=100, min_volume_ratio=0, max_volume_ratio=10, max_daily_drop=-1, min_close_location=0)
    values.update(overrides)
    return StrategyConfig(**values)


class StrategyTests(unittest.TestCase):
    def test_boll_uses_population_std(self):
        frame = bars(np.arange(1, 22))
        data = add_indicators(frame, StrategyConfig())
        expected = np.mean(np.arange(2, 22)) - 2 * np.std(np.arange(2, 22), ddof=0)
        self.assertTrue(np.isclose(data.iloc[-1]["lower"], expected))

    def test_buy_signal_requires_strictly_below_lower(self):
        frame = bars([10] * 20 + [8])
        result = evaluate_latest(frame, permissive_config())
        self.assertTrue(result["signal"])
        self.assertLess(result["close"], result["lower"])
        self.assertLess(result["buy_limit"], result["lower"])

        flat = bars([10] * 21)
        result = evaluate_latest(flat, permissive_config())
        self.assertFalse(result["signal"])
        self.assertFalse(result["below_lower"])

    def test_exit_target_is_selectable(self):
        frame = bars([10] * 20 + [8])
        mid = evaluate_latest(frame, permissive_config(exit_line="mid"))
        upper = evaluate_latest(frame, permissive_config(exit_line="upper"))
        self.assertEqual(mid["selected_sell_target"], mid["mid"])
        self.assertEqual(upper["selected_sell_target"], upper["upper"])

    def test_backtest_entry_never_exceeds_signal_lower(self):
        frame = bars([10] * 20 + [8, 7.8, 9.5, 10, 10])
        trades = backtest_one(frame, permissive_config(exit_line="mid", buy_discount=0.001))
        self.assertFalse(trades.empty)
        self.assertTrue((trades["entry_price"] < trades["entry_lower"]).all())


if __name__ == "__main__":
    unittest.main()
