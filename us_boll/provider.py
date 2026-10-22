from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone

import pandas as pd
import requests


# Follow global-stock-data: Sina daily bars, Tencent live snapshot.
SINA_KLINE_URL = "https://stock.finance.sina.com.cn/usstock/api/jsonp.php/var/US_MinKService.getDailyK"
TENCENT_QUOTE_URL = "https://qt.gtimg.cn"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36"

# 腾讯美股代码后缀：NASDAQ=.OQ / NYSE=.N / AMEX=.A
_EXCHANGE_SUFFIX = {
    "NASDAQ": ".OQ",
    "NYSE": ".N",
    "AMEX": ".A",
}
# 常见股票所属交易所（未收录的默认按 NASDAQ 处理）。当前股票池 9 只均为 NASDAQ。
_KNOWN_EXCHANGE = {
    "TSLA": "NASDAQ", "AAPL": "NASDAQ", "NVDA": "NASDAQ", "META": "NASDAQ",
    "AMZN": "NASDAQ", "MSFT": "NASDAQ", "GOOGL": "NASDAQ", "GOOG": "NASDAQ",
    "SNDK": "NASDAQ", "MU": "NASDAQ", "NFLX": "NASDAQ", "AVGO": "NASDAQ",
    "BRK.B": "NYSE", "JPM": "NYSE", "V": "NYSE", "WMT": "NYSE", "XOM": "NYSE",
}

# range -> approximate daily bar count
_RANGE_COUNT = {"1y": 320, "2y": 640, "3y": 1000, "5y": 1600}


def _tencent_code(symbol: str) -> str:
    sym = symbol.strip().upper()
    exchange = _KNOWN_EXCHANGE.get(sym, "NASDAQ")
    return f"us{sym}{_EXCHANGE_SUFFIX.get(exchange, '.OQ')}"


def _to_float(value) -> float | None:
    try:
        number = float(value)
        return number if number == number else None  # NaN 检查
    except (TypeError, ValueError):
        return None


def fetch_history(symbol: str, range_: str = "3y") -> tuple[pd.DataFrame, dict]:
    """Fetch Sina daily OHLCV and a Tencent live quote (public, unofficial sources)."""
    sym = symbol.strip().upper()
    count = _RANGE_COUNT.get(range_, 1000)
    response = requests.get(
        SINA_KLINE_URL,
        params={"symbol": sym, "num": count},
        headers={"User-Agent": USER_AGENT, "Referer": "https://finance.sina.com.cn/"},
        timeout=20,
    )
    response.raise_for_status()
    match = re.search(r"\((\[.*\])\)", response.text, flags=re.S)
    if not match:
        raise ValueError(f"{sym}: 新浪返回空日线或格式无法识别")
    items = json.loads(match.group(1))
    rows = [{
        "date": item.get("d"),
        "open": _to_float(item.get("o")),
        "high": _to_float(item.get("h")),
        "low": _to_float(item.get("l")),
        "close": _to_float(item.get("c")),
        "volume": _to_float(item.get("v")),
    } for item in items]
    frame = pd.DataFrame(rows).dropna(subset=["open", "high", "low", "close", "volume"])
    if frame.empty:
        raise ValueError(f"{sym}: 新浪无有效日线")
    # Sina may return the full available history even when num is supplied.
    # Enforce the selected period locally so backtests honor the requested range.
    frame = frame.tail(count).reset_index(drop=True)

    # Skill's Tencent US quote endpoint returns JSON when fmt=json is set.
    quote_response = requests.get(
        TENCENT_QUOTE_URL,
        params={"q": f"us{sym}", "fmt": "json"},
        headers={"User-Agent": USER_AGENT, "Referer": "https://gu.qq.com/"},
        timeout=12,
    )
    quote_response.raise_for_status()
    quote_payload = quote_response.json()
    qt = quote_payload.get(f"us{sym}", [])
    if not isinstance(qt, list):
        qt = []
    regular_price = _to_float(qt[3]) if len(qt) > 3 else None
    previous_close = _to_float(qt[4]) if len(qt) > 4 else None
    quote_time = qt[30] if len(qt) > 30 else ""

    meta_out = {
        "symbol": sym,
        "currency": "USD",
        "exchange": _KNOWN_EXCHANGE.get(sym, "NASDAQ"),
        "regular_market_price": regular_price,
        "previous_close": previous_close,
        "pre_market_price": None,
        "pre_market_pct": None,
        "pre_market_time": None,
        "after_hours_price": None,
        "after_hours_pct": None,
        "after_hours_time": None,
        "session": "",
        "quote_timestamp": quote_time,
        "market_timezone": "America/New_York",
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    return frame, meta_out


def fetch_with_retry(symbol: str, range_: str = "3y", attempts: int = 3) -> tuple[pd.DataFrame, dict]:
    last_error = None
    for attempt in range(attempts):
        try:
            return fetch_history(symbol, range_)
        except Exception as exc:  # provider/network errors are reported per ticker
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{symbol}: {last_error}") from last_error
