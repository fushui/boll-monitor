from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

import numpy as np
import pandas as pd


ExitLine = Literal["mid", "upper"]


@dataclass(frozen=True)
class StrategyConfig:
    boll_window: int = 20
    boll_std: float = 2.0
    rsi_window: int = 14
    max_rsi: float = 45.0
    min_volume_ratio: float = 0.3
    max_volume_ratio: float = 2.5
    max_daily_drop: float = -0.08
    min_close_location: float = 0.2
    buy_discount: float = 0.001
    exit_line: ExitLine = "mid"
    stop_loss: float = 0.07
    max_holding_days: int = 10
    round_trip_cost: float = 0.001

    def __post_init__(self) -> None:
        if self.boll_window < 2 or self.rsi_window < 2:
            raise ValueError("指标窗口必须至少为 2")
        if self.exit_line not in ("mid", "upper"):
            raise ValueError("exit_line 必须为 mid 或 upper")
        if not 0 <= self.buy_discount < 1:
            raise ValueError("buy_discount 必须位于 [0, 1)")

    def to_dict(self) -> dict:
        return asdict(self)


def _validate_ohlcv(frame: pd.DataFrame) -> pd.DataFrame:
    required = ["date", "open", "high", "low", "close", "volume"]
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise ValueError(f"K 线缺少列: {missing}")
    data = frame.copy()
    data["date"] = pd.to_datetime(data["date"], errors="raise")
    for column in required[1:]:
        data[column] = pd.to_numeric(data[column], errors="coerce")
    data = data.dropna(subset=required).sort_values("date")
    data = data.drop_duplicates("date", keep="last").reset_index(drop=True)
    if (data[["open", "high", "low", "close"]] <= 0).any().any():
        raise ValueError("OHLC 价格必须大于 0")
    return data


def add_indicators(frame: pd.DataFrame, config: StrategyConfig) -> pd.DataFrame:
    data = _validate_ohlcv(frame)
    close = data["close"]
    window = config.boll_window
    data["mid"] = close.rolling(window, min_periods=window).mean()
    # ddof=0 与常见行情软件/TA-Lib 的总体标准差口径一致。
    std = close.rolling(window, min_periods=window).std(ddof=0)
    data["upper"] = data["mid"] + config.boll_std * std
    data["lower"] = data["mid"] - config.boll_std * std
    data["bandwidth"] = (data["upper"] - data["lower"]) / data["mid"]

    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / config.rsi_window, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / config.rsi_window, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    data["rsi"] = (100 - 100 / (1 + rs)).fillna(
        pd.Series(np.where(gain.eq(0), 0.0, 100.0), index=data.index)
    )
    data["volume_ma"] = data["volume"].rolling(window, min_periods=window).mean()
    data["volume_ratio"] = data["volume"] / data["volume_ma"].replace(0, np.nan)
    data["daily_return"] = close.pct_change()
    candle_range = (data["high"] - data["low"]).replace(0, np.nan)
    data["close_location"] = ((close - data["low"]) / candle_range).fillna(0.5).clip(0, 1)
    data["lower_penetration"] = ((data["lower"] - close) / data["lower"]).clip(lower=0)
    data["mid_upside"] = (data["mid"] / close - 1).clip(lower=0)
    data["buy_limit"] = data["lower"] * (1 - config.buy_discount)
    return data


def _score_row(row: pd.Series) -> tuple[float, dict[str, float]]:
    # 分数只负责候选之间排序，所有候选仍须先通过严格的复合条件。
    breach = float(np.clip(row["lower_penetration"] / 0.05, 0, 1) * 30)
    oversold = float(np.clip((50 - row["rsi"]) / 30, 0, 1) * 25)
    rebound = float(np.clip(row["close_location"], 0, 1) * 15)
    volume = float(np.clip(1 - abs(row["volume_ratio"] - 1) / 1.5, 0, 1) * 10)
    stabilization = float(np.clip((row["daily_return"] + 0.04) / 0.06, 0, 1) * 10)
    upside = float(np.clip(row["mid_upside"] / 0.10, 0, 1) * 10)
    parts = {
        "score_breach": breach,
        "score_rsi": oversold,
        "score_rebound": rebound,
        "score_volume": volume,
        "score_stability": stabilization,
        "score_upside": upside,
    }
    return round(sum(parts.values()), 2), parts


def evaluate_latest(frame: pd.DataFrame, config: StrategyConfig) -> dict:
    data = add_indicators(frame, config)
    minimum = max(config.boll_window, config.rsi_window) + 1
    if len(data) < minimum or pd.isna(data.iloc[-1]["lower"]):
        raise ValueError(f"有效 K 线不足，至少需要 {minimum} 根")
    row = data.iloc[-1]
    checks = {
        # 核心硬约束：收盘价必须严格低于下轨；建议委托价也严格低于下轨。
        "below_lower": bool(row["close"] < row["lower"]),
        "rsi_ok": bool(row["rsi"] <= config.max_rsi),
        "volume_ok": bool(config.min_volume_ratio <= row["volume_ratio"] <= config.max_volume_ratio),
        "drop_ok": bool(row["daily_return"] >= config.max_daily_drop),
        "candle_ok": bool(row["close_location"] >= config.min_close_location),
    }
    score, score_parts = _score_row(row)
    signal = all(checks.values())
    failed = [name for name, passed in checks.items() if not passed]
    result = {
        "date": row["date"].date().isoformat(),
        "close": float(row["close"]),
        "mid": float(row["mid"]),
        "upper": float(row["upper"]),
        "lower": float(row["lower"]),
        "buy_limit": float(row["buy_limit"]),
        "rsi": float(row["rsi"]),
        "volume_ratio": float(row["volume_ratio"]),
        "daily_return": float(row["daily_return"]),
        "close_location": float(row["close_location"]),
        "lower_penetration": float(row["lower_penetration"]),
        "signal": signal,
        "score": score if signal else 0.0,
        "failed_checks": ",".join(failed),
        "sell_target_mid": float(row["mid"]),
        "sell_target_upper": float(row["upper"]),
        "selected_sell_target": float(row[config.exit_line]),
    }
    result.update(checks)
    result.update(score_parts)
    return result


def backtest_one(frame: pd.DataFrame, config: StrategyConfig) -> pd.DataFrame:
    """Long-only EOD backtest; signals execute as next-day below-lower limit orders."""
    data = add_indicators(frame, config)
    trades: list[dict] = []
    position: dict | None = None
    pending: dict | None = None

    for index, row in data.iterrows():
        if pending is not None and position is None:
            limit_price = pending["limit_price"]
            if row["low"] <= limit_price:
                fill = min(float(row["open"]), limit_price)
                position = {
                    "signal_date": pending["signal_date"],
                    "entry_date": row["date"],
                    "entry_price": fill,
                    "entry_lower": pending["entry_lower"],
                    "score": pending["score"],
                    "entry_index": index,
                }
            pending = None

        if position is not None:
            holding_days = index - position["entry_index"] + 1
            stop = position["entry_price"] * (1 - config.stop_loss)
            target = float(row[config.exit_line]) if pd.notna(row[config.exit_line]) else np.inf
            reason = None
            exit_price = None
            # 日 K 无法知道同日触发顺序，按保守口径先判止损。
            if row["low"] <= stop:
                reason, exit_price = "stop_loss", stop
            elif row["high"] >= target:
                reason, exit_price = config.exit_line, target
            elif holding_days >= config.max_holding_days:
                reason, exit_price = "time_exit", float(row["close"])
            if reason:
                gross = exit_price / position["entry_price"] - 1
                trades.append(
                    {
                        **position,
                        "exit_date": row["date"],
                        "exit_price": exit_price,
                        "exit_reason": reason,
                        "holding_days": holding_days,
                        "gross_return": gross,
                        "net_return": gross - config.round_trip_cost,
                    }
                )
                position = None

        if position is None and pending is None and index + 1 < len(data) and pd.notna(row["lower"]):
            checks = (
                row["close"] < row["lower"]
                and row["rsi"] <= config.max_rsi
                and config.min_volume_ratio <= row["volume_ratio"] <= config.max_volume_ratio
                and row["daily_return"] >= config.max_daily_drop
                and row["close_location"] >= config.min_close_location
            )
            if checks:
                score, _ = _score_row(row)
                pending = {
                    "signal_date": row["date"],
                    "limit_price": float(row["buy_limit"]),
                    "entry_lower": float(row["lower"]),
                    "score": score,
                }

    columns = [
        "signal_date", "entry_date", "entry_price", "entry_lower", "score",
        "exit_date", "exit_price", "exit_reason", "holding_days", "gross_return", "net_return",
    ]
    return pd.DataFrame(trades, columns=columns)
