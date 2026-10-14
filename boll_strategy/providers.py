from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Protocol

import pandas as pd
import requests


class DataProvider(Protocol):
    def prepare(self, codes: list[str]) -> None: ...
    def history(self, code: str, start: str, end: str) -> pd.DataFrame: ...
    def close(self) -> None: ...


OHLCV_COLUMNS = ["date", "open", "high", "low", "close", "volume"]


@dataclass
class CsvDirectoryProvider:
    directory: Path

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)

    def prepare(self, codes: list[str]) -> None:
        missing = [code for code in codes if not self._path(code).exists()]
        if missing:
            preview = ", ".join(missing[:5])
            raise FileNotFoundError(f"CSV 数据缺失 {len(missing)} 只（示例: {preview}）")

    def _path(self, code: str) -> Path:
        return self.directory / f"{code}.csv"

    def history(self, code: str, start: str, end: str) -> pd.DataFrame:
        data = pd.read_csv(self._path(code))
        if "time_key" in data.columns and "date" not in data.columns:
            data = data.rename(columns={"time_key": "date"})
        dates = pd.to_datetime(data["date"])
        return data.loc[(dates >= pd.Timestamp(start)) & (dates <= pd.Timestamp(end))].copy()

    def close(self) -> None:
        return None


class AStockDataProvider:
    """腾讯前复权日线适配器，遵循 a-stock-data skill 的 K 线口径。"""

    HOSTS = (
        "https://web.ifzq.gtimg.cn",
        "https://proxy.finance.qq.com/ifzqgtimg",
        "https://ifzq.gtimg.cn",
    )
    PATH = "/appstock/app/fqkline/get"
    HOST_COOLDOWN_SECONDS = 120.0
    MAX_CALENDAR_DAYS_PER_REQUEST = 700
    CODE_PATTERN = re.compile(r"^(SH|SZ)\.(\d{6})$")

    def __init__(
        self,
        interval: float = 0.08,
        timeout: tuple[float, float] = (8.0, 25.0),
        session: requests.Session | None = None,
    ):
        self.interval = max(0.0, interval)
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Referer": "https://gu.qq.com/",
                "Accept": "application/json,text/plain,*/*",
            }
        )
        self._down_until = {host: 0.0 for host in self.HOSTS}
        self._last_request_at = 0.0

    @classmethod
    def _symbol(cls, code: str) -> str:
        match = cls.CODE_PATTERN.fullmatch(str(code).strip().upper())
        if not match:
            raise ValueError(f"股票代码必须显式写成 SH.600519 或 SZ.000001: {code}")
        market, digits = match.groups()
        return f"{market.lower()}{digits}"

    @staticmethod
    def _parse_date(value: str) -> date:
        try:
            return date.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"日期必须为 YYYY-MM-DD: {value}") from exc

    def prepare(self, codes: list[str]) -> None:
        if not codes:
            raise ValueError("股票池为空")
        for code in codes:
            self._symbol(code)

    @staticmethod
    def _quote_number(values: list[str], index: int) -> float:
        value = values[index].strip() if index < len(values) else ""
        if not value:
            return 0.0
        try:
            number = float(value)
        except ValueError as exc:
            raise RuntimeError(f"腾讯实时行情数值字段无法解析（索引 {index}）: {value}") from exc
        if not math.isfinite(number):
            raise RuntimeError(f"腾讯实时行情包含非有限数值（索引 {index}）")
        return number

    @classmethod
    def _parse_quotes(cls, payload: str, key_to_code: dict[str, str]) -> dict[str, dict]:
        result = {}
        for line in payload.strip().split(";"):
            if not line.strip() or "=" not in line or '"' not in line:
                continue
            key = line.split("=", 1)[0].rsplit("_", 1)[-1].lower()
            if key not in key_to_code:
                continue
            values = line.split('"', 2)[1].split("~")
            if len(values) < 53:
                raise RuntimeError(f"腾讯实时行情字段数量异常: {key} 仅 {len(values)} 项")
            code = key_to_code[key]
            price = cls._quote_number(values, 3)
            previous_close = cls._quote_number(values, 4)
            amount_wan = cls._quote_number(values, 37)
            is_stale = amount_wan == 0 and price == previous_close and price > 0
            result[code] = {
                "quote_time": values[30].strip(),
                "realtime_name": values[1].strip(),
                "realtime_price": price,
                "realtime_previous_close": previous_close,
                "realtime_open": cls._quote_number(values, 5),
                "realtime_change_amount": cls._quote_number(values, 31),
                "realtime_change_pct": cls._quote_number(values, 32),
                "realtime_high": cls._quote_number(values, 33),
                "realtime_low": cls._quote_number(values, 34),
                "realtime_amount_wan": amount_wan,
                "realtime_turnover_pct": cls._quote_number(values, 38),
                "realtime_pe_ttm": cls._quote_number(values, 39),
                "realtime_amplitude_pct": cls._quote_number(values, 43),
                "realtime_float_mcap_yi": cls._quote_number(values, 44),
                "realtime_mcap_yi": cls._quote_number(values, 45),
                "realtime_pb": cls._quote_number(values, 46),
                "realtime_limit_up": cls._quote_number(values, 47),
                "realtime_limit_down": cls._quote_number(values, 48),
                "realtime_volume_ratio": cls._quote_number(values, 49),
                "realtime_is_stale": is_stale,
                "realtime_stale_reason": (
                    "成交额为 0（停牌、未开盘或废码），报价可能不是当日真实成交"
                    if is_stale else ""
                ),
            }
        return result

    def quotes(self, codes: list[str]) -> dict[str, dict]:
        """批量获取股票池实时价格，返回键保持 SH.XXXXXX/SZ.XXXXXX 格式。"""
        self.prepare(codes)
        result = {}
        for offset in range(0, len(codes), 50):
            batch = codes[offset:offset + 50]
            symbols = [self._symbol(code) for code in batch]
            key_to_code = dict(zip(symbols, batch))
            self._wait_for_rate_limit()
            try:
                response = self.session.get(
                    "https://qt.gtimg.cn/q=" + ",".join(symbols),
                    timeout=self.timeout,
                )
                self._last_request_at = time.monotonic()
                response.raise_for_status()
                payload = response.content.decode("gbk")
            except (requests.RequestException, UnicodeDecodeError) as exc:
                raise RuntimeError(f"腾讯实时行情请求失败: {exc}") from exc
            result.update(self._parse_quotes(payload, key_to_code))
        missing = [code for code in codes if code not in result]
        if missing:
            raise RuntimeError(f"腾讯实时行情缺失 {len(missing)} 只: {', '.join(missing[:5])}")
        return result

    def _wait_for_rate_limit(self) -> None:
        remaining = self.interval - (time.monotonic() - self._last_request_at)
        if remaining > 0:
            time.sleep(remaining)

    def _request(self, symbol: str, start: date, end: date) -> dict:
        parameter = f"{symbol},day,{start.isoformat()},{end.isoformat()},640,qfq"
        errors = []
        now = time.time()
        available = [host for host in self.HOSTS if self._down_until[host] <= now]
        if not available:
            available = [min(self.HOSTS, key=self._down_until.get)]

        for host in available:
            try:
                self._wait_for_rate_limit()
                response = self.session.get(
                    host + self.PATH,
                    params={"param": parameter},
                    timeout=self.timeout,
                )
                self._last_request_at = time.monotonic()
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
                    raise RuntimeError("响应不是预期 JSON 结构")
                if payload.get("code") not in (None, 0, "0"):
                    raise RuntimeError(f"接口状态异常: {payload.get('msg') or payload.get('code')}")
                if not payload["data"].get(symbol):
                    raise RuntimeError("接口返回空标的")
                return payload["data"]
            except (requests.RequestException, ValueError, RuntimeError) as exc:
                self._down_until[host] = time.time() + self.HOST_COOLDOWN_SECONDS
                errors.append(f"{host}: {exc}")
        raise RuntimeError(f"腾讯 K 线三个入口均失败（{symbol}）: {'; '.join(errors)}")

    @staticmethod
    def _rows_from_payload(data: dict, symbol: str, start: date, end: date) -> list[dict]:
        node = data.get(symbol)
        if not isinstance(node, dict):
            raise RuntimeError(f"腾讯 K 线缺少标的节点: {symbol}")
        if "qfqday" not in node:
            raise RuntimeError(f"腾讯 K 线缺少前复权日线字段: {symbol}")
        raw_rows = node["qfqday"]
        if not isinstance(raw_rows, list) or not raw_rows:
            raise RuntimeError(f"腾讯 K 线前复权数据为空: {symbol}")

        rows = []
        for raw in raw_rows:
            if not isinstance(raw, list) or len(raw) < 6:
                raise RuntimeError(f"腾讯 K 线字段结构变化: {symbol}")
            try:
                bar_date = date.fromisoformat(str(raw[0])[:10])
                open_, close, high, low, volume = map(float, raw[1:6])
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f"腾讯 K 线字段无法解析: {symbol} {raw}") from exc
            if not start <= bar_date <= end:
                raise RuntimeError(f"腾讯 K 线返回越界日期: {symbol} {bar_date}")
            values = (open_, high, low, close, volume)
            if not all(math.isfinite(value) for value in values):
                raise RuntimeError(f"腾讯 K 线包含非有限数值: {symbol} {bar_date}")
            if min(open_, high, low, close) <= 0 or volume < 0:
                raise RuntimeError(f"腾讯前复权价格非正或成交量为负: {symbol} {bar_date}")
            if high < max(open_, close, low) or low > min(open_, close, high):
                raise RuntimeError(f"腾讯 K 线 OHLC 关系异常: {symbol} {bar_date}")
            rows.append(
                {
                    "date": bar_date.isoformat(),
                    "open": open_,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": volume,
                }
            )
        return rows

    def history(self, code: str, start: str, end: str) -> pd.DataFrame:
        symbol = self._symbol(code)
        start_date, end_date = self._parse_date(start), self._parse_date(end)
        if start_date > end_date:
            raise ValueError(f"开始日期晚于结束日期: {start} > {end}")

        rows = []
        chunk_start = start_date
        while chunk_start <= end_date:
            chunk_end = min(end_date, chunk_start + timedelta(days=self.MAX_CALENDAR_DAYS_PER_REQUEST - 1))
            data = self._request(symbol, chunk_start, chunk_end)
            rows.extend(self._rows_from_payload(data, symbol, chunk_start, chunk_end))
            chunk_start = chunk_end + timedelta(days=1)

        result = pd.DataFrame(rows, columns=OHLCV_COLUMNS)
        if result.empty:
            return result
        result["date"] = pd.to_datetime(result["date"])
        result = result.sort_values("date").reset_index(drop=True)
        if result["date"].duplicated().any():
            duplicate = result.loc[result["date"].duplicated(), "date"].iloc[0].date()
            raise RuntimeError(f"腾讯 K 线日期重复: {code} {duplicate}")
        return result

    def close(self) -> None:
        self.session.close()
