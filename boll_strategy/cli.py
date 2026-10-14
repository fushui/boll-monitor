from __future__ import annotations

import argparse
import json
import sys
from dataclasses import fields
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .providers import AStockDataProvider, CsvDirectoryProvider
from .strategy import StrategyConfig, backtest_one, evaluate_latest
from .universe import DEFAULT_POOL, load_universe


def load_config(path: str | None, exit_line: str | None = None) -> StrategyConfig:
    values = {}
    if path:
        values = json.loads(Path(path).read_text(encoding="utf-8"))
    allowed = {field.name for field in fields(StrategyConfig)}
    unknown = set(values) - allowed
    if unknown:
        raise ValueError(f"配置项未知: {sorted(unknown)}")
    if exit_line:
        values["exit_line"] = exit_line
    return StrategyConfig(**values)


def make_provider(args):
    if args.provider == "csv":
        if not args.data_dir:
            raise ValueError("provider=csv 时必须提供 --data-dir")
        return CsvDirectoryProvider(args.data_dir)
    return AStockDataProvider(interval=args.request_interval)


def _drop_incomplete_today(frame: pd.DataFrame) -> pd.DataFrame:
    """盘中实时查询时，不让尚未收盘的当日 K 线参与日线信号。"""
    if frame.empty:
        return frame
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    last_day = pd.Timestamp(frame.iloc[-1]["date"]).date()
    if now.weekday() < 5 and last_day == now.date() and now.time() < time(15, 5):
        return frame.iloc[:-1].copy()
    return frame


def _merge_realtime(row: dict, quote: dict | None) -> None:
    if not quote:
        return
    row.update(quote)
    price = quote["realtime_price"]
    valid = price > 0 and not quote["realtime_is_stale"]
    row["realtime_below_lower"] = bool(valid and price < row["lower"])
    row["realtime_at_or_below_buy_limit"] = bool(valid and price <= row["buy_limit"])
    row["realtime_entry_watch"] = bool(row["signal"] and valid and price < row["lower"])
    row["realtime_vs_lower_pct"] = price / row["lower"] - 1 if valid else None
    row["realtime_to_mid_pct"] = row["mid"] / price - 1 if valid else None
    row["realtime_to_upper_pct"] = row["upper"] / price - 1 if valid else None


def scan(args) -> int:
    config = load_config(args.config, args.exit_line)
    pool = load_universe(args.pool)
    end = args.end or date.today().isoformat()
    start = args.start or (date.fromisoformat(end) - timedelta(days=180)).isoformat()
    provider = make_provider(args)
    results, errors = [], []
    try:
        provider.prepare(pool["code"].tolist())
        realtime_quotes = {}
        if args.realtime and isinstance(provider, AStockDataProvider):
            try:
                realtime_quotes = provider.quotes(pool["code"].tolist())
            except Exception as exc:
                errors.append({"code": "REALTIME", "name": "全池实时行情", "error": str(exc)})
        for item in pool.itertuples(index=False):
            try:
                history = provider.history(item.code, start, end)
                if args.realtime:
                    history = _drop_incomplete_today(history)
                row = evaluate_latest(history, config)
                row.update(code=item.code, name=item.name)
                _merge_realtime(row, realtime_quotes.get(item.code))
                results.append(row)
            except Exception as exc:  # 单只失败不应丢掉全池结果。
                errors.append({"code": item.code, "name": item.name, "error": str(exc)})
    finally:
        provider.close()

    result = pd.DataFrame(results)
    if not result.empty:
        result = result.sort_values(["signal", "score"], ascending=[False, False])
    candidates = result.loc[result["signal"]].copy() if not result.empty else result
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(output, index=False, encoding="utf-8-sig")
    candidates_path = output.with_name(f"{output.stem}_candidates{output.suffix}")
    candidates.to_csv(candidates_path, index=False, encoding="utf-8-sig")
    if errors:
        output.with_name(f"{output.stem}_errors.json").write_text(
            json.dumps(errors, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    print(f"扫描完成：{len(result)} 只成功，{len(errors)} 只失败，{len(candidates)} 只候选")
    print(f"完整结果: {output.resolve()}")
    print(f"候选排名: {candidates_path.resolve()}")
    if not candidates.empty:
        columns = ["code", "name", "score", "close", "lower", "buy_limit", "rsi", "selected_sell_target"]
        if "realtime_price" in candidates:
            columns[3:3] = ["realtime_price", "realtime_change_pct", "realtime_below_lower"]
        display = candidates[columns]
        print(display.head(args.top).to_string(index=False))
    return 0 if not errors else 2


def quote(args) -> int:
    pool = load_universe(args.pool)
    provider = AStockDataProvider(interval=args.request_interval)
    try:
        quotes = provider.quotes(pool["code"].tolist())
    finally:
        provider.close()
    rows = [{"code": item.code, "name": item.name, **quotes[item.code]} for item in pool.itertuples(index=False)]
    result = pd.DataFrame(rows).sort_values("realtime_change_pct", ascending=False)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(output, index=False, encoding="utf-8-sig")
    print(f"实时行情完成：{len(result)} 只。结果: {output.resolve()}")
    display = result[["code", "name", "realtime_price", "realtime_change_pct", "realtime_high", "realtime_low", "quote_time", "realtime_is_stale"]]
    print(display.head(args.top).to_string(index=False))
    return 0


def backtest(args) -> int:
    config = load_config(args.config, args.exit_line)
    pool = load_universe(args.pool)
    provider = make_provider(args)
    all_trades, errors = [], []
    try:
        provider.prepare(pool["code"].tolist())
        for item in pool.itertuples(index=False):
            try:
                trades = backtest_one(provider.history(item.code, args.start, args.end), config)
                if not trades.empty:
                    trades.insert(0, "name", item.name)
                    trades.insert(0, "code", item.code)
                    all_trades.append(trades)
            except Exception as exc:
                errors.append({"code": item.code, "name": item.name, "error": str(exc)})
    finally:
        provider.close()
    trades = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    trades.to_csv(output, index=False, encoding="utf-8-sig")
    if trades.empty:
        print(f"回测完成：无成交；错误 {len(errors)} 只。结果: {output.resolve()}")
    else:
        win_rate = (trades["net_return"] > 0).mean()
        print(
            f"回测完成：{len(trades)} 笔，胜率 {win_rate:.1%}，"
            f"单笔平均 {trades['net_return'].mean():.2%}，错误 {len(errors)} 只"
        )
        print(f"交易明细: {output.resolve()}")
    return 0 if not errors else 2


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="A 股短线 BOLL 下轨策略")
    sub = root.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--pool", default=str(DEFAULT_POOL), help="股票池 CSV")
    common.add_argument("--config", help="策略 JSON 配置")
    common.add_argument("--provider", choices=["astock", "csv"], default="astock")
    common.add_argument("--data-dir", help="离线 CSV 目录，文件名如 SH.600519.csv")
    common.add_argument("--request-interval", type=float, default=0.08, help="腾讯请求最小间隔秒数")
    common.add_argument("--exit-line", choices=["mid", "upper"], help="覆盖配置中的卖出线")

    scan_parser = sub.add_parser("scan", parents=[common], help="执行每日筛选和评分")
    scan_parser.add_argument("--start", help="数据开始日 YYYY-MM-DD；默认向前 180 天")
    scan_parser.add_argument("--end", help="数据截止日 YYYY-MM-DD；默认今天")
    scan_parser.add_argument("--output", default="output/daily_scan.csv")
    scan_parser.add_argument("--top", type=int, default=20)
    scan_parser.add_argument("--no-realtime", dest="realtime", action="store_false", help="不合并当天实时行情")
    scan_parser.set_defaults(realtime=True)
    scan_parser.set_defaults(func=scan)

    backtest_parser = sub.add_parser("backtest", parents=[common], help="历史回测")
    backtest_parser.add_argument("--start", required=True)
    backtest_parser.add_argument("--end", required=True)
    backtest_parser.add_argument("--output", default="output/backtest_trades.csv")
    backtest_parser.set_defaults(func=backtest)

    quote_parser = sub.add_parser("quote", help="查询股票池当天实时行情")
    quote_parser.add_argument("--pool", default=str(DEFAULT_POOL), help="股票池 CSV")
    quote_parser.add_argument("--request-interval", type=float, default=0.08, help="腾讯请求最小间隔秒数")
    quote_parser.add_argument("--output", default="output/realtime_quotes.csv")
    quote_parser.add_argument("--top", type=int, default=20)
    quote_parser.set_defaults(func=quote)
    return root


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        return args.func(args)
    except (ValueError, RuntimeError, FileNotFoundError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
