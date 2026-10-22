import unittest

from etf_momentum.sina_fallback import SinaKlineFallback


class Response:
    def __init__(self, payload=None, text=""):
        self.payload = payload
        self.text = text

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class Session:
    def __init__(self):
        self.headers = {}

    def get(self, url, **kwargs):
        if url == SinaKlineFallback.KLINE_URL:
            return Response([{"day": "2025-01-02", "open": "10", "high": "12", "low": "9", "close": "11", "volume": "100"}])
        return Response(text='var sh513100qfq={"data":[{"d":"1900-01-01","f":"2"}]}/*tail*/')


class SinaFallbackTests(unittest.TestCase):
    def test_parses_kline_and_applies_qfq_factor(self):
        provider = SinaKlineFallback(session=Session())
        frame = provider.history("SH.513100", "2025-01-01", "2025-01-03")
        row = frame.iloc[0]
        self.assertEqual(row["close"], 5.5)
        self.assertEqual(row["open"], 5.0)
        self.assertEqual(row["volume"], 100)

    def test_rejects_missing_factor_coverage(self):
        with self.assertRaisesRegex(RuntimeError, "未覆盖"):
            SinaKlineFallback._factor_on([], __import__("datetime").date(2025, 1, 1))


if __name__ == "__main__":
    unittest.main()
