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

# 美股策略：收盘位置硬条件放宽到 10%（A 股 StrategyConfig 默认仍为 20%）。
MIN_CLOSE_LOCATION = 0.1


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


def _stats_from_trades(trades: pd.DataFrame) -> dict:
    empty = {"trades": 0, "win_rate": 0, "avg": 0, "cum": 0,
             "profit_avg": 0, "loss_avg": 0, "pl_ratio": None,
             "holding": 0, "max_win": 0, "max_loss": 0, "exit": {}}
    if trades.empty:
        return empty
    winners = trades.loc[trades["net_return"] > 0, "net_return"]
    losers = trades.loc[trades["net_return"] <= 0, "net_return"]
    win_mean = float(winners.mean()) if not winners.empty else 0.0
    loss_mean = float(losers.mean()) if not losers.empty else 0.0
    return {
        "trades": int(len(trades)),
        "win_rate": round(float((trades["net_return"] > 0).mean()) * 100, 1),
        "avg": round(float(trades["net_return"].mean()) * 100, 2),
        "cum": round(float(trades["net_return"].sum()) * 100, 2),
        "profit_avg": round(win_mean * 100, 2),
        "loss_avg": round(loss_mean * 100, 2),
        "pl_ratio": round(abs(win_mean / loss_mean), 2) if loss_mean else None,
        "holding": round(float(trades["holding_days"].mean()), 1),
        "max_win": round(float(trades["net_return"].max()) * 100, 2),
        "max_loss": round(float(trades["net_return"].min()) * 100, 2),
        "exit": {k: int(v) for k, v in trades["exit_reason"].value_counts().items()},
    }


def _build_monthly(mid_list: list[pd.DataFrame], up_list: list[pd.DataFrame]) -> list[dict]:
    empty_frame = pd.DataFrame(columns=["exit_date", "net_return"])

    def prepare(frames):
        frame = pd.concat(frames, ignore_index=True) if frames else empty_frame
        if frame.empty:
            return None
        data = frame.copy()
        data["exit_date"] = pd.to_datetime(data["exit_date"])
        data = data.sort_values("exit_date")
        data["cum"] = data["net_return"].cumsum() * 100
        return data.set_index("exit_date")

    mid = prepare(mid_list)
    up = prepare(up_list)
    mc = mid["cum"].resample("ME").last() if mid is not None else pd.Series(dtype=float)
    uc = up["cum"].resample("ME").last() if up is not None else pd.Series(dtype=float)
    mr = mid["net_return"].resample("ME").sum() * 100 if mid is not None else pd.Series(dtype=float)
    ur = up["net_return"].resample("ME").sum() * 100 if up is not None else pd.Series(dtype=float)

    index = sorted(set(mc.index) | set(uc.index))

    def _safe(series, stamp):
        if stamp not in series.index:
            return None
        try:
            value = float(series[stamp])
            return round(value, 2) if np.isfinite(value) else None
        except (TypeError, ValueError):
            return None

    monthly = []
    for stamp in index:
        monthly.append({
            "month": stamp.strftime("%Y-%m"),
            "cum_mid": _safe(mc, stamp),
            "cum_up": _safe(uc, stamp),
            "ret_mid": _safe(mr, stamp) if _safe(mr, stamp) is not None else 0.0,
            "ret_up": _safe(ur, stamp) if _safe(ur, stamp) is not None else 0.0,
        })
    return monthly


def scan(pool_path: Path = DEFAULT_POOL, output: Path = OUTPUT, range_: str = "3y") -> dict:
    rows = []
    errors = []
    all_mid_trades: list[pd.DataFrame] = []
    all_up_trades: list[pd.DataFrame] = []
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
            cfg = StrategyConfig(exit_line="mid", min_close_location=MIN_CLOSE_LOCATION)
            latest = evaluate_latest(history, cfg)
            indicators = add_indicators(history, cfg)
            candle = indicators.iloc[-1]
            raw_price = meta.get("regular_market_price") or float(candle["close"])
            pre_market = meta.get("pre_market_price")
            after_hours = meta.get("after_hours_price")
            pre_market = float(pre_market) if pre_market and np.isfinite(float(pre_market)) and float(pre_market) > 0 else None
            after_hours = float(after_hours) if after_hours and np.isfinite(float(after_hours)) and float(after_hours) > 0 else None
            prior_close = float(history.iloc[-2]["close"]) if len(history) > 1 else None
            # chartPreviousClose is the close preceding the requested chart range,
            # not necessarily yesterday's close; use the prior returned daily bar.
            change = (raw_price / prior_close - 1) * 100 if prior_close else float(candle["daily_return"]) * 100
            below_lower = raw_price < float(candle["lower"])
            mid_trades = backtest_one(history, StrategyConfig(exit_line="mid", min_close_location=MIN_CLOSE_LOCATION))
            up_trades = backtest_one(history, StrategyConfig(exit_line="upper", min_close_location=MIN_CLOSE_LOCATION))
            all_mid_trades.append(mid_trades)
            all_up_trades.append(up_trades)
            item_result = {
                "symbol": symbol,
                "name": item["name"],
                "currency": meta["currency"],
                "exchange": meta["exchange"],
                "signal_date": latest["date"],
                "price": _clean_number(raw_price),
                "pre_market_price": _clean_number(pre_market),
                "pre_market_pct": _clean_number(meta.get("pre_market_pct")),
                "after_hours_price": _clean_number(after_hours),
                "after_hours_pct": _clean_number(meta.get("after_hours_pct")),
                "session": meta.get("session"),
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
                    "mid": _stats_from_trades(mid_trades),
                    "upper": _stats_from_trades(up_trades),
                },
            }
            rows.append(item_result)
        except Exception as exc:
            errors.append({"symbol": symbol, "name": item["name"], "error": str(exc)[:300]})
        time.sleep(0.25)

    rows.sort(key=lambda row: (-int(row["signal"]), -float(row["score"]), row["symbol"]))

    near = []
    for row in rows:
        close = row.get("close")
        lower = row.get("lower")
        if close is None or not lower or float(lower) <= 0:
            continue
        dist = (float(close) - float(lower)) / float(lower) * 100
        if 0 < dist <= 1.0:
            near.append({
                "symbol": row["symbol"], "name": row["name"],
                "price": row.get("price"), "dist": round(dist, 2),
                "rsi": row.get("rsi"), "below": bool(row.get("below_lower")),
                "change_pct": row.get("change_pct"),
            })
    near.sort(key=lambda item: item["dist"])

    generated = datetime.now(timezone.utc).astimezone(NY).isoformat(timespec="minutes")
    result = {
        "generated_at": generated,
        "provider": "Tencent ifzq public quote/kline endpoint (unofficial)",
        "strategy": {"boll_window": 20, "boll_std": 2, "rsi_window": 14,
                     "max_rsi": 45, "entry": "daily close < BOLL lower; limit = lower × 0.999",
                     "exits": ["mid", "upper"], "stop_loss_pct": 7, "max_holding_days": 10},
        "total": len(rows),
        "candidate_count": sum(row["signal"] for row in rows),
        "candidates": [row for row in rows if row["signal"]],
        "near": near,
        "stocks": rows,
        "backtest": {
            "mid": _stats_from_trades(pd.concat(all_mid_trades, ignore_index=True) if all_mid_trades else pd.DataFrame()),
            "upper": _stats_from_trades(pd.concat(all_up_trades, ignore_index=True) if all_up_trades else pd.DataFrame()),
            "monthly": _build_monthly(all_mid_trades, all_up_trades),
        },
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
