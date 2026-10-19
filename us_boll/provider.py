from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import pandas as pd


BASE_URL = "https://query1.finance.yahoo.com/v8/finance/chart/"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36"


def fetch_history(symbol: str, range_: str = "3y") -> tuple[pd.DataFrame, dict]:
    """Download daily candles and metadata from Yahoo Finance's public chart endpoint.

    This is an undocumented endpoint. Callers should pace requests and treat
    temporary 401/429/5xx responses as provider failures, not empty histories.
    """
    encoded = urllib.parse.quote(symbol.strip().upper(), safe=".-^")
    query = urllib.parse.urlencode({"range": range_, "interval": "1d", "events": "div,splits"})
    request = urllib.request.Request(
        f"{BASE_URL}{encoded}?{query}",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        payload = json.loads(response.read().decode("utf-8"))

    chart = payload.get("chart") or {}
    if chart.get("error"):
        raise RuntimeError(f"{symbol}: {chart['error'].get('description', 'provider error')}")
    result = (chart.get("result") or [None])[0]
    if result is None:
        raise ValueError(f"{symbol}: provider returned no history")
    timestamps = result.get("timestamp") or []
    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    rows = []
    fields = ("open", "high", "low", "close", "volume")
    for index, stamp in enumerate(timestamps):
        row = {field: quote.get(field, [None] * len(timestamps))[index] for field in fields}
        close = row["close"]
        rows.append({
            "date": datetime.fromtimestamp(stamp, timezone.utc).date(),
            **row,
        })
    frame = pd.DataFrame(rows).dropna(subset=["open", "high", "low", "close", "volume"])
    if frame.empty:
        raise ValueError(f"{symbol}: no valid daily candles")
    meta = result.get("meta") or {}
    meta_out = {
        "symbol": meta.get("symbol", symbol.upper()),
        "currency": meta.get("currency", "USD"),
        "exchange": meta.get("fullExchangeName") or meta.get("exchangeName", ""),
        "regular_market_price": meta.get("regularMarketPrice"),
        "quote_timestamp": meta.get("regularMarketTime"),
        "market_timezone": meta.get("exchangeTimezoneName", "America/New_York"),
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
