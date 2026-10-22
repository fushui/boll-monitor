from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class MomentumConfig:
    momentum_days: int = 20
    trend_days: int = 28
    fee_per_side: float = 0.0005

    def __post_init__(self):
        if self.momentum_days < 1 or self.trend_days < 1:
            raise ValueError("动量与均线窗口必须为正整数")
        if self.fee_per_side < 0:
            raise ValueError("交易成本不能为负数")


def analyze_latest(histories: dict[str, pd.DataFrame], assets: dict[str, dict], held_code: str | None = None,
                   config: MomentumConfig = MomentumConfig()) -> dict:
    """Rank by 20-session return, then require close strictly above 28-session SMA."""
    rows, errors = [], []
    for code, meta in assets.items():
        frame = histories.get(code)
        try:
            if frame is None or len(frame) < max(config.momentum_days + 1, config.trend_days):
                raise ValueError(f"有效日 K 不足 {max(config.momentum_days + 1, config.trend_days)} 根")
            data = frame.copy()
            data["date"] = pd.to_datetime(data["date"], errors="raise")
            data["close"] = pd.to_numeric(data["close"], errors="raise")
            data = data.dropna(subset=["date", "close"]).drop_duplicates("date").sort_values("date")
            close = data["close"].astype(float)
            last = float(close.iloc[-1])
            ma = float(close.rolling(config.trend_days).mean().iloc[-1])
            momentum = float(last / close.iloc[-config.momentum_days - 1] - 1)
            if not np.isfinite(last) or not np.isfinite(ma) or not np.isfinite(momentum) or last <= 0:
                raise ValueError("指标数据无效")
            rows.append({
                "code": code, "name": meta["name"], "category": meta["category"],
                "date": data["date"].iloc[-1].date().isoformat(), "close": round(last, 4),
                "ma28": round(ma, 4), "distance_to_ma_pct": round((last / ma - 1) * 100, 2),
                "momentum_20d_pct": round(momentum * 100, 2), "trend_ok": last > ma,
            })
        except Exception as exc:
            errors.append({"code": code, "name": meta["name"], "error": str(exc)})
    rows.sort(key=lambda row: (-row["momentum_20d_pct"], row["code"]))
    for rank, row in enumerate(rows, start=1):
        row["momentum_rank"] = rank
    eligible = [row for row in rows if row["trend_ok"]]
    target = eligible[0] if eligible else None
    held = next((row for row in rows if row["code"] == held_code), None)
    if not rows:
        action, target_code, reason = "UNAVAILABLE", None, "当前没有可用历史行情，无法判断仓位；这不是持币信号"
    elif target is None:
        action, target_code, reason = ("SELL_TO_CASH" if held_code else "CASH"), None, "没有收盘站上28日均线的标的，持币观望"
    elif held_code == target["code"]:
        action, target_code, reason = "HOLD", target["code"], "当前持仓仍是趋势合格标的中20日动量最高者"
    else:
        action, target_code = ("ROTATE" if held_code else "BUY"), target["code"]
        reason = "买入趋势合格且20日动量排名第一的标的；按下一交易日执行"
    return {
        "strategy": {"momentum_days": config.momentum_days, "trend_days": config.trend_days,
                     "rule": "先过滤收盘价高于28日均线，再按20日涨幅降序选第一名"},
        "signal_date": max((row["date"] for row in rows), default=None),
        "held_code": held_code, "held_trend_ok": held["trend_ok"] if held else None,
        "action": action, "target_code": target_code, "reason": reason,
        "ranked": rows, "eligible_count": len(eligible), "errors": errors,
    }


def backtest(histories: dict[str, pd.DataFrame], config: MomentumConfig = MomentumConfig()) -> dict:
    """Daily close signals apply to the following session; one-way fee charged on turnover."""
    closes = {}
    for code, frame in histories.items():
        if frame is None or frame.empty:
            continue
        data = frame.copy()
        data["date"] = pd.to_datetime(data["date"])
        data["close"] = pd.to_numeric(data["close"], errors="coerce")
        closes[code] = data.dropna(subset=["close"]).drop_duplicates("date").set_index("date")["close"].sort_index()
    if len(closes) < 2:
        return {"available": False, "error": "至少需要两只有效 ETF 的历史数据"}
    prices = pd.DataFrame(closes).sort_index()
    momentum = prices / prices.shift(config.momentum_days) - 1
    averages = prices.rolling(config.trend_days).mean()
    choices = []
    for day in prices.index:
        eligible = [c for c in prices.columns if pd.notna(prices.at[day, c]) and pd.notna(averages.at[day, c])
                    and pd.notna(momentum.at[day, c]) and prices.at[day, c] > averages.at[day, c]]
        choices.append(max(eligible, key=lambda c: momentum.at[day, c]) if eligible else None)
    targets = pd.Series(choices, index=prices.index).shift(1)
    asset_returns = prices.pct_change().fillna(0)
    gross = pd.Series(0.0, index=prices.index)
    for day, code in targets.items():
        if code is not None and code in asset_returns:
            gross.loc[day] = asset_returns.loc[day, code]
    changes = targets.ne(targets.shift(1)) & (targets.notna() | targets.shift(1).notna())
    costs = changes.astype(float) * config.fee_per_side
    if targets.notna().any():
        costs.loc[targets.notna() & targets.shift(1).notna() & targets.ne(targets.shift(1))] += config.fee_per_side
    net = gross - costs
    nav = (1 + net).cumprod()
    years = max((prices.index[-1] - prices.index[0]).days / 365.25, 1 / 252)
    total = float(nav.iloc[-1] - 1)
    running_peak = nav.cummax()
    drawdown = nav / running_peak - 1
    return {
        "available": True, "start": prices.index[0].date().isoformat(), "end": prices.index[-1].date().isoformat(),
        "total_return_pct": round(total * 100, 2), "annualized_return_pct": round(((nav.iloc[-1] ** (1 / years)) - 1) * 100, 2),
        "max_drawdown_pct": round(float(drawdown.min()) * 100, 2),
        "switches": int((targets.ne(targets.shift(1)) & targets.notna()).sum()),
        "nav": [{"date": d.date().isoformat(), "value": round(float(v), 5)} for d, v in nav.iloc[-500:].items()],
        "assumptions": "日收盘生成信号，下一交易日持仓生效；单边成本按5bp估算，未计滑点、溢折价与停牌影响。",
    }
