from datetime import date
import unittest

from boll_strategy.providers import AStockDataProvider


class StubAStockDataProvider(AStockDataProvider):
    def __init__(self, payload):
        super().__init__(interval=0)
        self.payload = payload

    def _request(self, symbol, start, end):
        return self.payload


class AStockDataProviderTests(unittest.TestCase):
    def test_explicit_market_code_mapping(self):
        self.assertEqual(AStockDataProvider._symbol("SH.600519"), "sh600519")
        self.assertEqual(AStockDataProvider._symbol("SZ.000001"), "sz000001")
        with self.assertRaises(ValueError):
            AStockDataProvider._symbol("600519")

    def test_parses_tencent_qfq_daily_fields(self):
        payload = {
            "sz000001": {
                "qfqday": [
                    ["2026-09-17", "10.10", "10.20", "10.30", "10.00", "1200"],
                    ["2026-09-18", "10.20", "10.15", "10.25", "10.05", "1500"],
                ]
            }
        }
        provider = StubAStockDataProvider(payload)
        try:
            frame = provider.history("SZ.000001", "2026-09-17", "2026-09-18")
        finally:
            provider.close()
        self.assertEqual(frame["close"].tolist(), [10.2, 10.15])
        self.assertEqual(frame["volume"].tolist(), [1200.0, 1500.0])

    def test_rejects_nonpositive_adjusted_price(self):
        payload = {"sh600519": {"qfqday": [["2026-09-18", "1", "0", "1", "1", "1"]]}}
        with self.assertRaises(RuntimeError):
            AStockDataProvider._rows_from_payload(
                payload, "sh600519", date(2026, 9, 18), date(2026, 9, 18)
            )

    def test_parses_realtime_quote_fields(self):
        values = [""] * 53
        values[1] = "贵州茅台"
        values[3] = "1500.00"
        values[4] = "1490.00"
        values[5] = "1492.00"
        values[30] = "20260921142600"
        values[31] = "10.00"
        values[32] = "0.67"
        values[33] = "1510.00"
        values[34] = "1488.00"
        values[37] = "123456.00"
        values[38] = "0.50"
        values[39] = "25.00"
        values[43] = "1.48"
        values[44] = "18000.00"
        values[45] = "19000.00"
        values[46] = "8.00"
        values[47] = "1639.00"
        values[48] = "1341.00"
        values[49] = "1.20"
        payload = 'v_sh600519="' + "~".join(values) + '";'
        quotes = AStockDataProvider._parse_quotes(payload, {"sh600519": "SH.600519"})
        quote = quotes["SH.600519"]
        self.assertEqual(quote["realtime_price"], 1500.0)
        self.assertEqual(quote["realtime_change_pct"], 0.67)
        self.assertEqual(quote["quote_time"], "20260921142600")
        self.assertFalse(quote["realtime_is_stale"])


if __name__ == "__main__":
    unittest.main()
