from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import pandas as pd


# 腾讯美股日线接口（公开、免 key）。Yahoo Finance 已对中国大陆屏蔽，
# 腾讯 ifzq 接口同样提供前复权日线 + 实时/盘前盘后行情，一次请求拿全。
KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
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

# range -> 日线数量（腾讯接口按“最近 N 根”返回）
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
    """下载日线、实时价与盘前盘后行情（腾讯公开接口，非官方、无可用性保证）。"""
    tcode = _tencent_code(symbol)
    count = _RANGE_COUNT.get(range_, 1000)
    query = urllib.parse.urlencode({"param": f"{tcode},day,,,{count},qfq"})
    request = urllib.request.Request(
        f"{KLINE_URL}?{query}",
        headers={"User-Agent": USER_AGENT, "Referer": "https://gu.qq.com/"},
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        payload = json.loads(response.read().decode("utf-8"))

    data = (payload.get("data") or {}).get(tcode)
    if not data or not data.get("day"):
        raise ValueError(f"{symbol}: 腾讯返回空日线（代码 {tcode}）")

    rows = []
    for item in data["day"]:
        # 腾讯字段顺序：[date, open, close, high, low, volume]
        if len(item) < 6:
            continue
        rows.append({
            "date": item[0],
            "open": _to_float(item[1]),
            "close": _to_float(item[2]),
            "high": _to_float(item[3]),
            "low": _to_float(item[4]),
            "volume": _to_float(item[5]),
        })
    frame = pd.DataFrame(rows).dropna(subset=["open", "high", "low", "close", "volume"])
    if frame.empty:
        raise ValueError(f"{symbol}: 无有效日线")

    # 实时/盘前盘后行情来自同一响应的 qt / pandata 字段（qt 是 {code: [...]} 嵌套结构）
    qt_node = data.get("qt") or {}
    qt = qt_node.get(tcode) if isinstance(qt_node, dict) else qt_node
    if not isinstance(qt, list):
        qt = []
    regular_price = _to_float(qt[3]) if len(qt) > 3 else None
    previous_close = _to_float(qt[4]) if len(qt) > 4 else None
    after_hours = _to_float(qt[67]) if len(qt) > 67 else None
    quote_time = qt[30] if len(qt) > 30 else ""
    pandata = data.get("pandata") or {}

    meta_out = {
        "symbol": symbol.upper(),
        "currency": "USD",
        "exchange": _KNOWN_EXCHANGE.get(symbol.upper(), "NASDAQ"),
        "regular_market_price": regular_price,
        "previous_close": previous_close,
        "post_market_price": after_hours if after_hours and after_hours > 0 else None,
        "pre_market_price": _to_float(pandata.get("last")),
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
