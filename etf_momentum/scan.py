from __future__ import annotations

import argparse
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from boll_strategy.cli import _drop_incomplete_today
from boll_strategy.providers import AStockDataProvider
from boll_strategy.universe import load_universe

from .sina_fallback import SinaKlineFallback, get_history_with_fallback
from .strategy import MomentumConfig, analyze_latest, backtest

ROOT = Path(__file__).resolve().parents[1]
POOL = ROOT / "data" / "etf_pool.csv"
DEFAULT_OUTPUT = ROOT / "output" / "etf_momentum.json"


def run_scan(output: str | Path = DEFAULT_OUTPUT, history_years: int = 3, held_code: str | None = None) -> dict:
    pool = load_universe(POOL)
    # load_universe validates the shared SH/SZ code format; ETF metadata is read separately.
    import pandas as pd
    metadata = pd.read_csv(POOL, dtype=str).fillna("")
    assets = {row.code: {"name": row.name, "category": row.category}
              for row in metadata.itertuples(index=False)}
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    start = (today - timedelta(days=365 * history_years + 60)).isoformat()
    provider = AStockDataProvider(interval=0.12)
    fallback = SinaKlineFallback(session=provider.session)
    histories, errors = {}, []
    fallback_notes = []
    sources = {}
    try:
        provider.prepare(pool["code"].tolist())
        for code in pool["code"].tolist():
            try:
                history, source, primary_error = get_history_with_fallback(provider, fallback, code, start, today.isoformat())
                sources[code] = source
                if primary_error:
                    fallback_notes.append({"code": code, "source": source, "primary_error": primary_error})
                if datetime.now(ZoneInfo("Asia/Shanghai")).time() < time(15, 5):
                    history = _drop_incomplete_today(history)
                histories[code] = history
            except Exception as exc:
                errors.append({"code": code, "name": assets[code]["name"], "error": str(exc)})
    finally:
        provider.close()
    report = analyze_latest(histories, {code: assets[code] for code in histories}, held_code=held_code)
    report["errors"].extend(errors)
    report["generated_at"] = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
    report["data_sources"] = sources
    report["fallbacks"] = fallback_notes
    labels = {"tencent_qfq": "腾讯前复权", "sina_qfq_factor": "新浪日线+前复权因子"}
    report["data_source"] = "、".join(dict.fromkeys(labels.get(source, source) for source in sources.values())) or "暂无可用数据源"
    report["complete"] = len(histories) == len(assets) and not errors
    report["pool_count"] = len(assets)
    if not report["complete"]:
        report["action"] = "UNAVAILABLE"
        report["target_code"] = None
        report["reason"] = "股票池行情不完整，暂不生成轮动信号，避免用部分标的冒充全池排名"
        report["backtest"] = {"available": False, "error": "ETF历史数据不完整，无法生成完整股票池回测"}
    else:
        report["backtest"] = backtest(histories, MomentumConfig())
    report["ok"] = report["complete"]
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="ETF 动量 + 28 日均线轮动扫描")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--history-years", type=int, default=3)
    parser.add_argument("--held", help="当前持仓ETF代码（例如 SH.513100），用来计算持有/切换信号")
    args = parser.parse_args()
    report = run_scan(args.output, args.history_years, args.held)
    print(f"扫描日期: {report.get('signal_date') or '—'} · 建议: {report['action']} · 目标: {report['target_code'] or '现金'}")
    print(f"可用ETF: {len(report['ranked'])}/{len(load_universe(POOL))} · 数据失败: {len(report['errors'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
