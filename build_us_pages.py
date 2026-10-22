from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "us_boll" / "data" / "latest.json"
TARGET = ROOT / "docs" / "us" / "index.html"
TEMPLATE = ROOT / "_dashboard_us_template.html"


def _num(value, digits=2):
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def main() -> None:
    if not SOURCE.exists():
        raise SystemExit(f"US snapshot missing: {SOURCE}")
    raw = json.loads(SOURCE.read_text(encoding="utf-8"))
    stocks = raw.get("stocks", [])

    quote_stocks = [{
        "code": s["symbol"],
        "name": s.get("name", s["symbol"]),
        "exchange": s.get("exchange", "NASDAQ"),
        "price": _num(s.get("price")),
        "pre_market_price": _num(s.get("pre_market_price")),
        "pre_market_pct": _num(s.get("pre_market_pct")),
        "after_hours_price": _num(s.get("after_hours_price")),
        "after_hours_pct": _num(s.get("after_hours_pct")),
        "change_pct": _num(s.get("change_pct")) or 0.0,
        "rsi": _num(s.get("rsi"), 1),
        "vol_ratio": _num(s.get("volume_ratio")),
        "lower": _num(s.get("lower")),
        "buy_limit": _num(s.get("buy_limit")),
        "score": _num(s.get("score"), 1),
        "signal": bool(s.get("signal")),
    } for s in stocks]

    candidates = [{
        "code": s["symbol"],
        "name": s.get("name", s["symbol"]),
        "score": _num(s.get("score"), 1) or 0.0,
        "date": s.get("signal_date", ""),
        "close": _num(s.get("close")),
        "lower": _num(s.get("lower")),
        "buy_limit": _num(s.get("buy_limit")),
        "rsi": _num(s.get("rsi"), 1),
        "sell_target": _num((s.get("targets") or {}).get("mid")),
        "realtime_price": _num(s.get("price")),
        "change_pct": _num(s.get("change_pct")) or 0.0,
        "below_lower": bool(s.get("below_lower")),
        "at_or_below_buy": bool(s.get("at_or_below_buy")),
        "penetration": _num(s.get("lower_penetration_pct")) or 0.0,
    } for s in stocks if s.get("signal")]

    near = [{
        "code": n.get("symbol"),
        "name": n.get("name", n.get("symbol")),
        "price": _num(n.get("price")),
        "dist": _num(n.get("dist")),
        "rsi": _num(n.get("rsi"), 1),
        "below": bool(n.get("below")),
        "change_pct": _num(n.get("change_pct")) or 0.0,
    } for n in raw.get("near", [])]

    changes = [float(s.get("change_pct", 0) or 0) for s in stocks]
    data = {
        "generated_at": raw.get("generated_at", ""),
        "quote": {
            "total": len(stocks),
            "up": sum(x > 0 for x in changes),
            "down": sum(x < 0 for x in changes),
            "flat": sum(x == 0 for x in changes),
            "avg_change": round(sum(changes) / len(changes), 2) if changes else 0.0,
            "stocks": quote_stocks,
        },
        "candidates": candidates,
        "near": near,
        "backtest": raw.get("backtest", {"mid": {}, "upper": {}, "monthly": []}),
        "market": "us",
    }

    template = TEMPLATE.read_text(encoding="utf-8")
    page = (template
            .replace("__DASHBOARD_DATA__", json.dumps(data, ensure_ascii=False, separators=(",", ":")))
            .replace("__STATIC_MODE__", "true")
            .replace("__MARKET__", "us"))
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(page, encoding="utf-8")
    (TARGET.parent / "latest.json").write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (TARGET.parent / ".nojekyll").write_text("", encoding="utf-8")
    print(f"US Pages built: {TARGET}")


if __name__ == "__main__":
    main()
