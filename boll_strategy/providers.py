from __future__ import annotations

import json
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
        # 东财分红明细用于计算近12个月股息率；行情主源不可用时不影响实时行情返回。
        try:
            yields = self._dividend_yields(codes, result)
            for code, value in yields.items():
                result[code]["realtime_dividend_yield"] = value
        except Exception:
            pass
        return result

    def _dividend_yields(self, codes: list[str], quotes: dict[str, dict]) -> dict[str, float | None]:
        """东财分红明细：近12个月税前现金分红/当前价，返回百分比。"""
        raw_codes = [re.search(r"(\d{6})$", str(c)).group(1) for c in codes]
        q = chr(34)
        code_filter = "(SECURITY_CODE in (" + ",".join(q + c + q for c in raw_codes) + "))"
        params = {"reportName": "RPT_SHAREBONUS_DET", "columns": "SECURITY_CODE,PRETAX_BONUS_RMB,EX_DIVIDEND_DATE", "filter": code_filter, "pageNumber": "1", "pageSize": "500", "sortColumns": "EX_DIVIDEND_DATE", "sortTypes": "-1", "source": "WEB", "client": "WEB"}
        response = self.session.get("https://datacenter.eastmoney.com/securities/api/data/v1/get", params=params, timeout=self.timeout)
        response.raise_for_status()
        rows = (response.json().get("result") or {}).get("data") or []
        cutoff = date.today() - timedelta(days=366)
        totals = {c: 0.0 for c in raw_codes}
        for row in rows:
            try:
                code = str(row.get("SECURITY_CODE", ""))
                ex_date = date.fromisoformat(str(row.get("EX_DIVIDEND_DATE", ""))[:10])
                if code in totals and ex_date >= cutoff:
                    # 东财该字段按“每10股派息”披露。
                    totals[code] += float(row.get("PRETAX_BONUS_RMB") or 0) / 10.0
            except (TypeError, ValueError):
                continue
        out = {}
        for code in codes:
            raw = re.search(r"(\d{6})$", str(code)).group(1)
            price = float(quotes[code].get("realtime_price") or 0)
            out[code] = round(totals[raw] / price * 100, 2) if price > 0 and totals[raw] > 0 else None
        return out

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

        # 主源：腾讯前复权日线；被 WAF 风控（501）时回退到新浪 K 线。
        try:
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
        except Exception:
            return self._history_sina(symbol, start_date, end_date)

        result["date"] = pd.to_datetime(result["date"])
        result = result.sort_values("date").reset_index(drop=True)
        if result["date"].duplicated().any():
            duplicate = result.loc[result["date"].duplicated(), "date"].iloc[0].date()
            raise RuntimeError(f"腾讯 K 线日期重复: {code} {duplicate}")
        return result

    def _history_sina(self, symbol: str, start_date: date, end_date: date) -> pd.DataFrame:
        """新浪 K 线备用源（腾讯 ifzq 被 WAF 风控时使用，scale=240 为日线）。"""
        days = (end_date - start_date).days
        datalen = min(1023, max(60, int(days * 0.75) + 30))
        url = "https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_k=/CN_MarketDataService.getKLineData"
        try:
            response = self.session.get(
                url,
                params={"symbol": symbol, "scale": "240", "ma": "no", "datalen": datalen},
                timeout=self.timeout,
            )
            response.raise_for_status()
            text = re.sub(r"/\*.*?\*/", "", response.text, flags=re.S)
            # 新浪 JSONP 实际为 `var _k=([{...}])`，等号后先有括号再是数组；
            # 数组是响应中唯一的 `[...]`，直接整体提取即可。
            match = re.search(r"\[.*\]", text, re.S)
            if not match:
                raise RuntimeError("新浪 K 线响应无法解析")
            payload = json.loads(match.group(0))
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            raise RuntimeError(f"新浪 K 线请求失败（{symbol}）: {exc}") from exc

        rows = []
        for item in payload:
            try:
                bar_date = date.fromisoformat(str(item.get("day", ""))[:10])
            except ValueError:
                continue
            if not (start_date <= bar_date <= end_date):
                continue
            try:
                open_, close, high, low, volume = map(
                    float, (item["open"], item["close"], item["high"], item["low"], item["volume"])
                )
            except (KeyError, TypeError, ValueError):
                continue
            if not all(math.isfinite(v) for v in (open_, high, low, close, volume)):
                continue
            if min(open_, high, low, close) <= 0 or volume < 0:
                continue
            rows.append({
                "date": bar_date.isoformat(),
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
            })

        frame = pd.DataFrame(rows, columns=OHLCV_COLUMNS)
        if frame.empty:
            raise RuntimeError(f"新浪 K 线无有效数据: {symbol}")
        frame["date"] = pd.to_datetime(frame["date"])
        frame = frame.sort_values("date").drop_duplicates("date").reset_index(drop=True)
        return frame

    def close(self) -> None:
        self.session.close()
