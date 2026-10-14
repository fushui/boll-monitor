# -*- coding: utf-8 -*-
"""根据最近一次扫描结果生成可直接发布到 GitHub Pages 的静态页面。"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "output"
DOCS = ROOT / "docs"


def _number(value, digits=2):
    if pd.isna(value):
        return None
    return round(float(value), digits)


def _truth(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return bool(value) if not pd.isna(value) else False


def build_data() -> dict:
    scan = pd.read_csv(OUTPUT / "daily_scan.csv", encoding="utf-8-sig")
    candidates_path = OUTPUT / "daily_scan_candidates.csv"
    candidates = pd.read_csv(candidates_path, encoding="utf-8-sig")

    stocks = []
    for _, row in scan.iterrows():
        stocks.append({
            "code": row["code"],
            "name": row["name"],
            "price": _number(row.get("realtime_price")),
            "change_pct": _number(row.get("realtime_change_pct")) or 0.0,
            "change_amount": _number(row.get("realtime_change_amount")) or 0.0,
            "turnover": _number(row.get("realtime_turnover_pct")),
            "vol_ratio": _number(row.get("realtime_volume_ratio")),
            "pe": _number(row.get("realtime_pe_ttm")),
            "pb": _number(row.get("realtime_pb")),
            "high": _number(row.get("realtime_high")),
            "low": _number(row.get("realtime_low")),
            "amplitude": _number(row.get("realtime_amplitude_pct")),
            "mcap_yi": _number(row.get("realtime_mcap_yi"), 0),
        })
    stocks.sort(key=lambda item: item["change_pct"], reverse=True)

    candidate_rows = []
    for _, row in candidates.iterrows():
        candidate_rows.append({
            "code": row["code"],
            "name": row["name"],
            "score": _number(row["score"]),
            "date": str(row["date"]),
            "close": _number(row["close"]),
            "lower": _number(row["lower"]),
            "buy_limit": _number(row["buy_limit"]),
            "rsi": _number(row["rsi"]),
            "sell_target": _number(row["selected_sell_target"]),
            "realtime_price": _number(row.get("realtime_price")),
            "change_pct": _number(row.get("realtime_change_pct")) or 0.0,
            "below_lower": _truth(row.get("realtime_below_lower")),
            "at_or_below_buy": _truth(row.get("realtime_at_or_below_buy_limit")),
            "penetration": _number(float(row["lower_penetration"]) * 100),
        })

    near_source = scan.copy()
    near_source["dist_to_lower"] = (near_source["close"] - near_source["lower"]) / near_source["lower"] * 100
    near_source = near_source[(near_source["dist_to_lower"] > 0) & (near_source["dist_to_lower"] <= 1)].sort_values("dist_to_lower").head(10)
    near = [{
        "code": row["code"],
        "name": row["name"],
        "price": _number(row.get("realtime_price")),
        "dist": _number(row["dist_to_lower"]),
        "rsi": _number(row["rsi"], 1),
        "below": _truth(row.get("realtime_below_lower")),
        "change_pct": _number(row.get("realtime_change_pct")) or 0.0,
    } for _, row in near_source.iterrows()]

    baseline = json.loads((OUTPUT / "dashboard_data.json").read_text(encoding="utf-8"), parse_constant=lambda _: None)
    changes = [item["change_pct"] for item in stocks]
    return {
        "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M"),
        "quote": {
            "total": len(stocks),
            "up": sum(value > 0 for value in changes),
            "down": sum(value < 0 for value in changes),
            "flat": sum(value == 0 for value in changes),
            "avg_change": round(sum(changes) / len(changes), 2) if changes else 0.0,
            "stocks": stocks,
        },
        "candidates": candidate_rows,
        "near": near,
        "backtest": baseline["backtest"],
    }


def main() -> None:
    data = build_data()
    template = (ROOT / "_dashboard_template.html").read_text(encoding="utf-8")
    html = template.replace("__DASHBOARD_DATA__", json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    html = html.replace("__STATIC_MODE__", "true")
    DOCS.mkdir(exist_ok=True)
    (DOCS / "index.html").write_text(html, encoding="utf-8")
    (DOCS / ".nojekyll").write_text("", encoding="utf-8")
    print(f"GitHub Pages built: {DOCS / 'index.html'}")


if __name__ == "__main__":
    main()
