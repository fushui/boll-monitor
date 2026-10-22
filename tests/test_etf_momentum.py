import unittest

import numpy as np
import pandas as pd

from etf_momentum.strategy import MomentumConfig, analyze_latest, backtest


def history(values):
    return pd.DataFrame({"date": pd.date_range("2025-01-01", periods=len(values), freq="B"), "close": values})


class EtfMomentumTests(unittest.TestCase):
    def test_selects_top_momentum_only_from_above_ma_assets(self):
        falling = np.linspace(100, 60, 40)
        rising = np.linspace(10, 13, 40)
        report = analyze_latest(
            {"SH.1": history(falling), "SH.2": history(rising)},
            {"SH.1": {"name": "回撤", "category": "指数"}, "SH.2": {"name": "上涨", "category": "黄金"}},
        )
        self.assertEqual(report["target_code"], "SH.2")
        self.assertEqual(report["action"], "BUY")
        rows = {row["code"]: row for row in report["ranked"]}
        self.assertFalse(rows["SH.1"]["trend_ok"])
        self.assertTrue(rows["SH.2"]["trend_ok"])

    def test_cash_when_no_asset_above_trend_average(self):
        falling = np.linspace(100, 50, 40)
        report = analyze_latest({"SH.1": history(falling)}, {"SH.1": {"name": "回撤", "category": "指数"}})
        self.assertIsNone(report["target_code"])
        self.assertEqual(report["action"], "CASH")

    def test_held_top_asset_stays_and_other_top_asset_rotates(self):
        rising = np.linspace(10, 13, 40)
        meta = {"SH.1": {"name": "上涨", "category": "指数"}}
        self.assertEqual(analyze_latest({"SH.1": history(rising)}, meta, "SH.1")["action"], "HOLD")
        self.assertEqual(analyze_latest({"SH.1": history(rising)}, meta, "SH.0")["action"], "ROTATE")

    def test_backtest_emits_fee_net_metrics_and_delayed_signal_curve(self):
        trend = np.linspace(10, 20, 100)
        histories = {"SH.1": history(trend), "SH.2": history(trend[::-1] + 30)}
        result = backtest(histories, MomentumConfig())
        self.assertTrue(result["available"])
        self.assertGreater(result["switches"], 0)
        self.assertEqual(len(result["nav"]), 100)
        self.assertIn("下一交易日", result["assumptions"])


if __name__ == "__main__":
    unittest.main()
