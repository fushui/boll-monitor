# -*- coding: utf-8 -*-
import pandas as pd
import json
import datetime


def load(p):
    return pd.read_csv(p, encoding="utf-8-sig")


# ---------- 1. 实时行情 ----------
q = load("output/realtime_quotes_2026-09-21.csv").sort_values(
    "realtime_change_pct", ascending=False
)
up_count = int((q["realtime_change_pct"] > 0).sum())
down_count = int((q["realtime_change_pct"] < 0).sum())
flat_count = int((q["realtime_change_pct"] == 0).sum())

stocks = []
for _, r in q.iterrows():
    stocks.append({
        "code": r["code"],
        "name": r["name"],
        "price": round(float(r["realtime_price"]), 2),
        "change_pct": round(float(r["realtime_change_pct"]), 2),
        "change_amount": round(float(r["realtime_change_amount"]), 2),
        "turnover": round(float(r["realtime_turnover_pct"]), 2),
        "vol_ratio": round(float(r["realtime_volume_ratio"]), 2),
        "pe": round(float(r["realtime_pe_ttm"]), 2) if pd.notna(r["realtime_pe_ttm"]) else None,
        "pb": round(float(r["realtime_pb"]), 2) if pd.notna(r["realtime_pb"]) else None,
        "high": round(float(r["realtime_high"]), 2),
        "low": round(float(r["realtime_low"]), 2),
        "amplitude": round(float(r["realtime_amplitude_pct"]), 2),
        "mcap_yi": round(float(r["realtime_mcap_yi"]), 0),
    })

# ---------- 2. BOLL 候选 ----------
c = load("output/daily_scan_candidates.csv")
cands = []
for _, r in c.iterrows():
    cands.append({
        "code": r["code"],
        "name": r["name"],
        "score": round(float(r["score"]), 2),
        "date": str(r["date"]),
        "close": round(float(r["close"]), 2),
        "lower": round(float(r["lower"]), 2),
        "buy_limit": round(float(r["buy_limit"]), 2),
        "rsi": round(float(r["rsi"]), 2),
        "sell_target": round(float(r["selected_sell_target"]), 2),
        "realtime_price": round(float(r["realtime_price"]), 2),
        "change_pct": round(float(r["realtime_change_pct"]), 2),
        "below_lower": bool(r["realtime_below_lower"]),
        "at_or_below_buy": bool(r["realtime_at_or_below_buy_limit"]),
        "penetration": round(float(r["lower_penetration"]) * 100, 2),
    })

# ---------- 2.5 即将符合 ----------
scan_full = load("output/daily_scan.csv")
scan_full["dist_to_lower"] = (scan_full["close"] - scan_full["lower"]) / scan_full["lower"] * 100
near_df = scan_full[(scan_full["dist_to_lower"] > 0) & (scan_full["dist_to_lower"] <= 1.0)].sort_values("dist_to_lower").head(10)
near_list = []
for _, r in near_df.iterrows():
    near_list.append({
        "code": r["code"],
        "name": r["name"],
        "price": round(float(r["realtime_price"]), 2),
        "dist": round(float(r["dist_to_lower"]), 2),
        "rsi": round(float(r["rsi"]), 1),
        "below": bool(r["realtime_below_lower"]),
        "change_pct": round(float(r["realtime_change_pct"]), 2),
    })

# ---------- 3. 回测统计 ----------
def bt_stats(path):
    df = load(path)
    n = len(df)
    win = df[df["net_return"] > 0]
    loss = df[df["net_return"] <= 0]
    ex = df["exit_reason"].value_counts().to_dict()
    return {
        "trades": int(n),
        "win_rate": round(len(win) / n * 100, 1),
        "avg": round(df["net_return"].mean() * 100, 2),
        "cum": round(df["net_return"].sum() * 100, 2),
        "profit_avg": round(win["net_return"].mean() * 100, 2),
        "loss_avg": round(loss["net_return"].mean() * 100, 2),
        "pl_ratio": round(abs(win["net_return"].mean() / loss["net_return"].mean()), 2),
        "holding": round(df["holding_days"].mean(), 1),
        "max_win": round(df["net_return"].max() * 100, 2),
        "max_loss": round(df["net_return"].min() * 100, 2),
        "exit": {k: int(v) for k, v in ex.items()},
    }


mid = load("output/backtest_trades.csv")
up = load("output/backtest_trades_upper.csv")
mid["exit_date"] = pd.to_datetime(mid["exit_date"])
up["exit_date"] = pd.to_datetime(up["exit_date"])
mid = mid.sort_values("exit_date")
up = up.sort_values("exit_date")
mid["cum"] = mid["net_return"].cumsum() * 100
up["cum"] = up["net_return"].cumsum() * 100


def monthly_cum(df):
    d = df.set_index("exit_date")
    return d["cum"].resample("ME").last()


def monthly_ret(df):
    d = df.set_index("exit_date")
    return d["net_return"].resample("ME").sum() * 100


mc = monthly_cum(mid)
uc = monthly_cum(up)
mr = monthly_ret(mid)
ur = monthly_ret(up)
idx = sorted(set(mc.index) | set(uc.index))
monthly = []
for t in idx:
    monthly.append({
        "month": t.strftime("%Y-%m"),
        "cum_mid": round(float(mc[t]), 2) if t in mc.index else None,
        "cum_up": round(float(uc[t]), 2) if t in uc.index else None,
        "ret_mid": round(float(mr[t]), 2) if t in mr.index else 0.0,
        "ret_up": round(float(ur[t]), 2) if t in ur.index else 0.0,
    })

data = {
    "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
    "quote": {
        "total": int(len(q)),
        "up": up_count,
        "down": down_count,
        "flat": flat_count,
        "avg_change": round(float(q["realtime_change_pct"].mean()), 2),
        "stocks": stocks,
    },
    "candidates": cands,
    "near": near_list,
    "backtest": {
        "mid": bt_stats("output/backtest_trades.csv"),
        "upper": bt_stats("output/backtest_trades_upper.csv"),
        "monthly": monthly,
    },
}

with open("output/dashboard_data.json", "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False)

print("done | stocks:", len(stocks), "| candidates:", len(cands), "| months:", len(monthly))
