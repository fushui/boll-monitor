from __future__ import annotations

import json
import math
from datetime import date

import pandas as pd
import requests

from boll_strategy.providers import AStockDataProvider


class SinaKlineFallback:
    """Daily Sina history fallback from the a-stock-data routing notes.

    Sina K lines are unadjusted. The companion Sina qfq-factor endpoint is
    applied locally (price / factor) before the strategy sees the bars.
    """

    KLINE_URL = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"

    def __init__(self, session: requests.Session | None = None, timeout: float = 15):
        self.session = session or requests.Session()
        self.timeout = timeout
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
            "Referer": "https://finance.sina.com.cn/",
        })

    def history(self, code: str, start: str, end: str) -> pd.DataFrame:
        symbol = AStockDataProvider._symbol(code)
        response = self.session.get(self.KLINE_URL, params={
            "symbol": symbol, "scale": "240", "ma": "no", "datalen": "1023",
        }, timeout=self.timeout)
        response.raise_for_status()
        try:
            rows = response.json()
        except ValueError as exc:
            raise RuntimeError(f"新浪 K 线 JSON 无法解析: {code}") from exc
        if not isinstance(rows, list) or not rows:
            raise RuntimeError(f"新浪 K 线没有历史记录: {code}")

        factors = self._factors(symbol)
        first, last = date.fromisoformat(start), date.fromisoformat(end)
        parsed = []
        for item in rows:
            try:
                day = date.fromisoformat(str(item["day"])[:10])
                if not first <= day <= last:
                    continue
                factor = self._factor_on(factors, day)
                values = {key: float(item[key]) / factor for key in ("open", "high", "low", "close")}
                volume = float(item["volume"])
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise RuntimeError(f"新浪 K 线字段异常: {code}, {item!r}") from exc
            if not all(math.isfinite(value) and value > 0 for value in values.values()) or not math.isfinite(volume) or volume < 0:
                raise RuntimeError(f"新浪 K 线包含非法 OHLCV: {code}, {item!r}")
            if values["high"] < max(values["open"], values["close"], values["low"]) or values["low"] > min(values["open"], values["close"], values["high"]):
                raise RuntimeError(f"新浪 K 线 OHLC 关系异常: {code}, {day}")
            parsed.append({"date": day.isoformat(), **values, "volume": volume})
        if not parsed:
            raise RuntimeError(f"新浪 K 线在 {start}~{end} 内没有记录: {code}")
        return pd.DataFrame(parsed).sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)

    def _factors(self, symbol: str) -> list[tuple[date, float]]:
        response = self.session.get(f"https://finance.sina.com.cn/realstock/company/{symbol}/qfq.js", timeout=self.timeout)
        response.raise_for_status()
        text = response.text
        brace = text.find("{")
        if brace < 0:
            raise RuntimeError(f"新浪复权因子响应无 JSON: {symbol}")
        try:
            data, _ = json.JSONDecoder().raw_decode(text[brace:])
            records = data.get("data") or []
            factors = [(date.fromisoformat(str(row["d"])[:10]), float(row["f"])) for row in records]
        except (json.JSONDecodeError, AttributeError, KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"新浪复权因子解析失败: {symbol}") from exc
        factors.sort(key=lambda pair: pair[0])
        if not factors or any(not math.isfinite(value) or value <= 0 for _, value in factors):
            raise RuntimeError(f"新浪复权因子为空或非法: {symbol}")
        return factors

    @staticmethod
    def _factor_on(factors: list[tuple[date, float]], day: date) -> float:
        eligible = [value for factor_date, value in factors if factor_date <= day]
        if not eligible:
            raise RuntimeError(f"新浪复权因子未覆盖 K 线日期 {day}")
        return eligible[-1]


def get_history_with_fallback(primary: AStockDataProvider, fallback: SinaKlineFallback,
                              code: str, start: str, end: str) -> tuple[pd.DataFrame, str, str | None]:
    try:
        return primary.history(code, start, end), "tencent_qfq", None
    except Exception as primary_error:
        try:
            return fallback.history(code, start, end), "sina_qfq_factor", str(primary_error)
        except Exception as fallback_error:
            raise RuntimeError(f"腾讯日线失败: {primary_error}; 新浪前复权备胎失败: {fallback_error}") from fallback_error
