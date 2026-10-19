from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "us_boll" / "data" / "latest.json"
TARGET = ROOT / "docs" / "us" / "index.html"
TEMPLATE = ROOT / "_dashboard_template.html"

def _summary(stocks: list[dict], line: str) -> dict:
    rows = [s.get("backtest", {}).get(line, {}) for s in stocks]
    trades = sum(int(x.get("trades", 0) or 0) for x in rows)
    if not trades:
        return {"trades": 0, "win_rate": 0, "avg": 0, "cum": 0, "profit_avg": 0, "loss_avg": 0, "pl_ratio": "—", "holding": 0, "max_win": 0, "max_loss": 0, "exit": {}}
    weighted = lambda key: round(sum(float(x.get(key, 0) or 0) * int(x.get("trades", 0) or 0) for x in rows) / trades, 2)
    wins = sum(round(int(x.get("trades", 0) or 0) * float(x.get("win_rate", 0) or 0) / 100) for x in rows)
    return {"trades": trades, "win_rate": round(wins / trades * 100, 1), "avg": weighted("average_return"), "cum": round(sum(float(x.get("total_return", 0) or 0) for x in rows), 2), "profit_avg": weighted("average_return"), "loss_avg": 0, "pl_ratio": "—", "holding": weighted("average_holding_days"), "max_win": max((float(x.get("average_return", 0) or 0) for x in rows), default=0), "max_loss": min((float(x.get("average_return", 0) or 0) for x in rows), default=0), "exit": {}}


def main() -> None:
    if not SOURCE.exists():
        raise SystemExit(f"US snapshot missing: {SOURCE}")
    raw = json.loads(SOURCE.read_text(encoding="utf-8"))
    stocks = raw.get("stocks", [])
    changes = [float(s.get("change_pct", 0) or 0) for s in stocks]
    quote_stocks = [{"code": s["symbol"], "name": s.get("name", s["symbol"]), "price": s.get("price"), "change_pct": s.get("change_pct", 0), "change_amount": 0, "turnover": None, "vol_ratio": s.get("volume_ratio"), "pe": None, "pb": None, "high": None, "low": None, "amplitude": None, "mcap_yi": None} for s in stocks]
    candidates = [{"code": s["symbol"], "name": s.get("name", s["symbol"]), "score": s.get("score", 0), "date": s.get("signal_date", ""), "close": s.get("close"), "lower": s.get("lower"), "buy_limit": s.get("buy_limit"), "rsi": s.get("rsi"), "sell_target": s.get("targets", {}).get("mid"), "realtime_price": s.get("price"), "change_pct": s.get("change_pct", 0), "below_lower": s.get("below_lower", False), "at_or_below_buy": s.get("at_or_below_buy", False), "penetration": s.get("lower_penetration_pct", 0)} for s in stocks if s.get("signal")]
    data = {"generated_at": raw.get("generated_at", ""), "quote": {"total": len(stocks), "up": sum(x > 0 for x in changes), "down": sum(x < 0 for x in changes), "flat": sum(x == 0 for x in changes), "avg_change": round(sum(changes) / len(changes), 2) if changes else 0, "stocks": quote_stocks}, "candidates": candidates, "near": [], "backtest": {"mid": _summary(stocks, "mid"), "upper": _summary(stocks, "upper"), "monthly": []}, "market": "us"}
    template = TEMPLATE.read_text(encoding="utf-8")
    page = template.replace("__DASHBOARD_DATA__", json.dumps(data, ensure_ascii=False, separators=(",", ":"))).replace("__STATIC_MODE__", "true").replace("__MARKET__", "us").replace("BOLL 策略监控台", "美股 BOLL 策略监控台").replace("A 股短线 · 布林下轨筛选与回测", "美股短线 · 布林下轨筛选与回测")
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(page, encoding="utf-8")
    (TARGET.parent / ".nojekyll").write_text("", encoding="utf-8")
    print(f"US Pages built: {TARGET}")


if __name__ == "__main__":
    main()
