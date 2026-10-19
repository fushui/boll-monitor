from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, time as dt_time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from boll_strategy.strategy import StrategyConfig, add_indicators, backtest_one, evaluate_latest
from .provider import fetch_with_retry


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POOL = ROOT / "data" / "us_stock_pool.csv"
OUTPUT = ROOT / "us_boll" / "data" / "latest.json"
NY = ZoneInfo("America/New_York")


def load_pool(path: Path) -> list[dict[str, str]]:
    frame = pd.read_csv(path, dtype=str)
    if not {"symbol", "name"}.issubset(frame.columns):
        raise ValueError("美股池 CSV 必须包含 symbol,name 列")
    frame["symbol"] = frame["symbol"].str.strip().str.upper()
    frame["name"] = frame["name"].str.strip()
    if frame["symbol"].eq("").any() or frame["symbol"].duplicated().any():
        raise ValueError("美股代码不能为空或重复")
    if not frame["symbol"].str.fullmatch(r"[A-Z0-9.^-]{1,15}").all():
        raise ValueError("美股代码含有不支持的字符")
    return frame.to_dict("records")


def _clean_number(value, digits=2):
    if value is None or pd.isna(value) or not np.isfinite(float(value)):
        return None
    return round(float(value), digits)


def _backtest_stats(frame: pd.DataFrame, exit_line: str) -> dict:
    trades = backtest_one(frame, StrategyConfig(exit_line=exit_line))
    if trades.empty:
        return {"trades": 0, "win_rate": 0, "average_return": 0, "total_return": 0,
                "profit_factor": None, "average_holding_days": 0}
    winners = trades.loc[trades["net_return"] > 0, "net_return"]
    losers = trades.loc[trades["net_return"] <= 0, "net_return"]
    gross_profit = float(winners.sum())
    gross_loss = abs(float(losers.sum()))
    return {
        "trades": int(len(trades)),
        "win_rate": round(float((trades["net_return"] > 0).mean()) * 100, 1),
        "average_return": round(float(trades["net_return"].mean()) * 100, 2),
        "total_return": round(float(trades["net_return"].sum()) * 100, 2),
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss else None,
        "average_holding_days": round(float(trades["holding_days"].mean()), 1),
    }


def scan(pool_path: Path = DEFAULT_POOL, output: Path = OUTPUT, range_: str = "3y") -> dict:
    rows = []
    errors = []
    for item in load_pool(pool_path):
        symbol = item["symbol"]
        try:
            history, meta = fetch_with_retry(symbol, range_)
            market_now = datetime.now(NY)
            if (market_now.weekday() < 5 and dt_time(9, 30) <= market_now.time() < dt_time(16, 0)
                    and history.iloc[-1]["date"] == market_now.date()):
                # Do not let an unfinished daily candle set the BOLL bands.
                history = history.iloc[:-1].copy()
            if len(history) < 40:
                raise ValueError("有效日线少于 40 根")
            cfg = StrategyConfig(exit_line="mid")
            latest = evaluate_latest(history, cfg)
            indicators = add_indicators(history, cfg)
            candle = indicators.iloc[-1]
            raw_price = meta.get("regular_market_price") or float(candle["close"])
            prior_close = float(history.iloc[-2]["close"]) if len(history) > 1 else None
            # chartPreviousClose is the close preceding the requested chart range,
            # not necessarily yesterday's close; use the prior returned daily bar.
            change = (raw_price / prior_close - 1) * 100 if prior_close else float(candle["daily_return"]) * 100
            below_lower = raw_price < float(candle["lower"])
            item_result = {
                "symbol": symbol,
                "name": item["name"],
                "currency": meta["currency"],
                "exchange": meta["exchange"],
                "signal_date": latest["date"],
                "price": _clean_number(raw_price),
                "change_pct": _clean_number(change),
                "close": _clean_number(latest["close"]),
                "lower": _clean_number(latest["lower"]),
                "mid": _clean_number(latest["mid"]),
                "upper": _clean_number(latest["upper"]),
                "buy_limit": _clean_number(latest["buy_limit"]),
                "rsi": _clean_number(latest["rsi"]),
                "volume_ratio": _clean_number(latest["volume_ratio"]),
                "score": _clean_number(latest["score"]),
                "signal": bool(latest["signal"]),
                "below_lower": below_lower,
                "at_or_below_buy": raw_price <= float(latest["buy_limit"]),
                "lower_penetration_pct": _clean_number(latest["lower_penetration"] * 100),
                "targets": {"mid": _clean_number(latest["mid"]), "upper": _clean_number(latest["upper"])},
                "quote_timestamp": meta["quote_timestamp"],
                "fetched_at": meta["fetched_at"],
                "history": [
                    {"date": row["date"].date().isoformat(),
                     "close": _clean_number(row["close"]), "lower": _clean_number(row["lower"]),
                     "mid": _clean_number(row["mid"]), "upper": _clean_number(row["upper"])}
                    for _, row in indicators.tail(100).iterrows() if pd.notna(row["lower"])
                ],
                "backtest": {
                    "mid": _backtest_stats(history, "mid"),
                    "upper": _backtest_stats(history, "upper"),
                },
            }
            rows.append(item_result)
        except Exception as exc:
            errors.append({"symbol": symbol, "name": item["name"], "error": str(exc)[:300]})
        time.sleep(0.25)

    rows.sort(key=lambda row: (-int(row["signal"]), -float(row["score"]), row["symbol"]))
    generated = datetime.now(timezone.utc).astimezone(NY).isoformat(timespec="minutes")
    result = {
        "generated_at": generated,
        "provider": "Yahoo Finance public chart endpoint (unofficial)",
        "strategy": {"boll_window": 20, "boll_std": 2, "rsi_window": 14,
                     "max_rsi": 45, "entry": "daily close < BOLL lower; limit = lower × 0.999",
                     "exits": ["mid", "upper"], "stop_loss_pct": 7, "max_holding_days": 10},
        "total": len(rows),
        "candidate_count": sum(row["signal"] for row in rows),
        "candidates": [row for row in rows if row["signal"]],
        "stocks": rows,
        "errors": errors,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False), encoding="utf-8")
    print(f"US scan complete: {len(rows)} stocks, {result['candidate_count']} candidates, {len(errors)} errors")
    print(f"Snapshot: {output.resolve()}")
    if errors:
        print(json.dumps(errors, ensure_ascii=False, indent=2))
    return result


def pushplus(result: dict) -> None:
    token = os.environ.get("PUSHPLUS_TOKEN", "").strip()
    if not token:
        raise RuntimeError("PUSHPLUS_TOKEN secret is missing")
    lines = [f"## 美股 BOLL 候选（{result['candidate_count']} 只）", ""]
    candidates = result["candidates"]
    if candidates:
        lines.extend(["| Ticker | 名称 | 现价 USD | BOLL下轨 | 买入限价 | RSI | 评分 |", "|---|---|---:|---:|---:|---:|---:|"])
        for row in candidates[:15]:
            lines.append(f"| {row['symbol']} | {row['name']} | {row['price']:.2f} | {row['lower']:.2f} | {row['buy_limit']:.2f} | {row['rsi']:.1f} | {row['score']:.1f} |")
    else:
        lines.append("本次没有满足严格下轨条件的股票。")
    lines.extend(["", f"扫描时间：{result['generated_at']}（纽约时间）", "策略仅供研究，不构成投资建议。"])
    payload = json.dumps({"token": token, "title": f"【美股BOLL】候选 {result['candidate_count']} 只", "content": "\n".join(lines), "template": "markdown"}).encode()
    request = urllib.request.Request("https://www.pushplus.plus/send", data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=25) as response:
        response_data = json.loads(response.read().decode("utf-8"))
    if response_data.get("code") != 200:
        raise RuntimeError(f"PushPlus failed: {response_data.get('msg', 'unknown error')}")
    print("PushPlus notification sent")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="美股 BOLL 下轨筛选与回测")
    parser.add_argument("--pool", type=Path, default=DEFAULT_POOL)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--range", default="3y", choices=["1y", "2y", "3y", "5y"])
    parser.add_argument("--push", action="store_true", help="扫描后发送 PushPlus")
    args = parser.parse_args(argv)
    try:
        result = scan(args.pool, args.output, args.range)
        if args.push:
            pushplus(result)
        return 0 if result["stocks"] else 1
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
