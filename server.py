# -*- coding: utf-8 -*-
"""BOLL 策略实时查询后端服务。

启动（项目根目录）：
    C:\\Python314\\python.exe server.py
浏览器访问：
    http://127.0.0.1:8765

接口：
    GET /                          实时查询版 dashboard
    GET /api/quote                 实时抓取全池行情（约 10~15 秒）
    GET /api/scan?exit=mid|upper   完整 BOLL 筛选（约 20~30 秒，含实时行情）
"""
from __future__ import annotations

import json
import csv
import html as html_escape
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.parse import unquote

import requests

from boll_strategy.cli import _drop_incomplete_today, _merge_realtime
from boll_strategy.providers import AStockDataProvider
from boll_strategy.strategy import StrategyConfig, evaluate_latest
from boll_strategy.universe import load_universe

ROOT = Path(__file__).resolve().parent
DASHBOARD = ROOT / "output" / "dashboard.html"
ETF_DASHBOARD = ROOT / "output" / "etf" / "dashboard.html"
US_DASHBOARD = ROOT / "docs" / "us" / "index.html"
HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8765"))

_lock = threading.Lock()


def _resolve_stock_input(value: str) -> tuple[str, str]:
    """Resolve an A-share code or name through Tencent Smartbox."""
    value = value.strip()
    match = re.fullmatch(r"(?:(SH|SZ)\.)?(\d{6})", value.upper())
    if match:
        market, digits = match.groups()
        if not market:
            if digits.startswith(("6", "9")):
                market = "SH"
            elif digits.startswith(("0", "2", "3")):
                market = "SZ"
            else:
                raise ValueError("暂不支持该市场代码，请使用 SH.600519 或 SZ.000001 格式")
        code = f"{market}.{digits}"
        return code, _tencent_stock_name(code)
    response = requests.get("https://smartbox.gtimg.cn/s3/", params={"q": value, "t": "all"},
                            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}, timeout=12)
    response.raise_for_status()
    hint = re.search(r'v_hint="([^"]*)"', response.content.decode("gbk", errors="replace"))
    if not hint:
        raise ValueError(f"没有找到股票：{value}")
    records = []
    for item in json.loads('"' + hint.group(1) + '"').split("^"):
        fields = item.split("~")
        if len(fields) >= 5 and fields[0] in ("sh", "sz") and fields[1].isdigit() and len(fields[1]) == 6 and fields[4].startswith("GP"):
            records.append((fields[0].upper() + "." + fields[1], fields[2]))
    records = list(dict.fromkeys(records))
    exact = [row for row in records if row[1].casefold() == value.casefold()]
    matches = exact or records
    if len(matches) != 1:
        if not matches:
            raise ValueError(f"没有找到沪深 A 股：{value}")
        raise ValueError("名称匹配到多只股票，请输入代码：" + "、".join(f"{name}({code})" for code, name in matches[:5]))
    return matches[0]


def _tencent_stock_name(code: str) -> str:
    market, digits = code.lower().split(".", 1)
    response = requests.get(f"https://qt.gtimg.cn/q={market}{digits}",
                            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}, timeout=10)
    response.raise_for_status()
    match = re.search(r'="([^"]*)"', response.content.decode("gbk", errors="replace"))
    fields = match.group(1).split("~") if match else []
    if len(fields) < 4 or fields[3] in ("", "0"):
        raise ValueError(f"腾讯行情中找不到有效股票：{code}")
    return fields[1]


def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _build_stock(row: dict) -> dict:
    pe = row.get("realtime_pe_ttm")
    pb = row.get("realtime_pb")
    return {
        "code": row["code"],
        "name": row["name"],
        "price": round(_num(row.get("realtime_price")), 2),
        "change_pct": round(_num(row.get("realtime_change_pct")), 2),
        "change_amount": round(_num(row.get("realtime_change_amount")), 2),
        "turnover": round(_num(row.get("realtime_turnover_pct")), 2),
        "vol_ratio": round(_num(row.get("realtime_volume_ratio")), 2),
        "pe": round(pe, 2) if pe else None,
        "pb": round(pb, 2) if pb else None,
        "dividend_yield": round(_num(row.get("realtime_dividend_yield")), 2) if row.get("realtime_dividend_yield") is not None else None,
        "high": round(_num(row.get("realtime_high")), 2),
        "low": round(_num(row.get("realtime_low")), 2),
        "amplitude": round(_num(row.get("realtime_amplitude_pct")), 2),
        "mcap_yi": round(_num(row.get("realtime_mcap_yi")), 0),
    }


def _summarize(stocks: list[dict]) -> dict:
    up = sum(1 for s in stocks if s["change_pct"] > 0)
    down = sum(1 for s in stocks if s["change_pct"] < 0)
    flat = sum(1 for s in stocks if s["change_pct"] == 0)
    avg = round(sum(s["change_pct"] for s in stocks) / len(stocks), 2) if stocks else 0.0
    return {"total": len(stocks), "up": up, "down": down, "flat": flat, "avg_change": avg, "stocks": stocks}


def _run_scan(exit_line: str) -> dict:
    config = StrategyConfig(exit_line=exit_line)
    pool = load_universe(None)
    codes = pool["code"].tolist()
    end = date.today().isoformat()
    start = (date.today() - timedelta(days=180)).isoformat()

    t0 = time.time()
    provider = AStockDataProvider(interval=0.08)
    results: list[dict] = []
    errors: list[dict] = []
    realtime_quotes: dict[str, dict] = {}
    try:
        provider.prepare(codes)
        try:
            realtime_quotes = provider.quotes(codes)
        except Exception as exc:
            errors.append({"code": "REALTIME", "name": "全池实时行情", "error": str(exc)})
        for item in pool.itertuples(index=False):
            try:
                history = provider.history(item.code, start, end)
                history = _drop_incomplete_today(history)
                row = evaluate_latest(history, config)
                row.update(code=item.code, name=item.name)
                _merge_realtime(row, realtime_quotes.get(item.code))
                results.append(row)
            except Exception as exc:
                errors.append({"code": item.code, "name": item.name, "error": str(exc)})
    finally:
        provider.close()
    elapsed = round(time.time() - t0, 1)

    signal_date = results[0]["date"] if results else date.today().isoformat()
    candidates = []
    for r in results:
        if not r.get("signal"):
            continue
        candidates.append({
            "code": r["code"],
            "name": r["name"],
            "score": round(r["score"], 2),
            "date": r["date"],
            "close": round(_num(r.get("close")), 2),
            "lower": round(_num(r.get("lower")), 2),
            "buy_limit": round(_num(r.get("buy_limit")), 2),
            "rsi": round(_num(r.get("rsi")), 2),
            "sell_target": round(_num(r.get("selected_sell_target")), 2),
            "realtime_price": round(_num(r.get("realtime_price")), 2),
            "change_pct": round(_num(r.get("realtime_change_pct")), 2),
            "dividend_yield": round(_num(r.get("realtime_dividend_yield")), 2) if r.get("realtime_dividend_yield") is not None else None,
            "below_lower": bool(r.get("realtime_below_lower")),
            "at_or_below_buy": bool(r.get("realtime_at_or_below_buy_limit")),
            "penetration": round(_num(r.get("lower_penetration")) * 100, 2),
        })
    candidates.sort(key=lambda x: -x["score"])

    near_list = []
    for r in results:
        try:
            close = _num(r.get("close"))
            lower = _num(r.get("lower"))
            if lower > 0:
                dist = (close - lower) / lower * 100
                if 0 < dist <= 1.0:
                    near_list.append({
                        "code": r["code"],
                        "name": r["name"],
                        "price": round(_num(r.get("realtime_price")), 2),
                        "dist": round(dist, 2),
                        "rsi": round(_num(r.get("rsi")), 1),
                        "below": bool(r.get("realtime_below_lower")),
                        "change_pct": round(_num(r.get("realtime_change_pct")), 2),
                    })
        except (TypeError, ValueError):
            continue
    near_list.sort(key=lambda x: x["dist"])
    near_list = near_list[:10]

    stocks = [_build_stock(r) for r in results]
    stocks.sort(key=lambda x: -x["change_pct"])

    return {
        "ok": True,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "signal_date": signal_date,
        "elapsed": elapsed,
        "exit_line": exit_line,
        "errors": len(errors),
        "quote": _summarize(stocks),
        "candidates": candidates,
        "near": near_list,
    }


def _run_quote() -> dict:
    pool = load_universe(None)
    codes = pool["code"].tolist()
    name_map = dict(zip(pool["code"], pool["name"]))
    t0 = time.time()
    provider = AStockDataProvider(interval=0.08)
    try:
        quotes = provider.quotes(codes)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "elapsed": round(time.time() - t0, 1)}
    finally:
        provider.close()
    rows = [{"code": c, "name": name_map.get(c, c), **quotes.get(c, {})} for c in codes]
    stocks = [_build_stock(r) for r in rows]
    stocks.sort(key=lambda x: -x["change_pct"])
    return {
        "ok": True,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed": round(time.time() - t0, 1),
        "quote": _summarize(stocks),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "BOLLServer/1.0"

    def log_message(self, fmt, *args):  # 精简日志
        pass

    def _send_json(self, obj: dict, status: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, path: Path):
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _acquire(self) -> bool:
        return _lock.acquire(blocking=False)

    def _authorized_cron(self) -> bool:
        expected = os.environ.get("CRON_SECRET", "")
        supplied = self.headers.get("X-Cron-Secret", "")
        return bool(expected) and secrets.compare_digest(expected, supplied)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/pool":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                action = body.get("action")
                value = str(body.get("input", body.get("code", ""))).strip()
                code = value.upper()
                name = str(body.get("name", "")).strip() or code
                pool_path = ROOT / "data" / "stock_pool.csv"
                pool = load_universe(pool_path)
                if action == "add":
                    if not value:
                        raise ValueError("请输入股票名称或代码")
                    code, name = _resolve_stock_input(value)
                    if code not in set(pool["code"]):
                        pool = __import__("pandas").concat([pool, __import__("pandas").DataFrame([{"code": code, "name": name}])], ignore_index=True)
                    else:
                        raise ValueError(f"{name}（{code}）已在股票池中")
                elif action == "remove":
                    pool = pool[pool["code"] != code].copy()
                    if pool.empty: raise ValueError("股票池不能全部删除")
                else:
                    raise ValueError("action 必须是 add 或 remove")
                pool.to_csv(pool_path, index=False, encoding="utf-8-sig")
                self._send_json({"ok": True, "action": action, "total": len(pool), "stocks": pool.to_dict("records")})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
            return
        if path != "/api/scheduled-push":
            self._send_json({"ok": False, "error": "未知路径 " + path}, status=404)
            return
        if not self._authorized_cron():
            self._send_json({"ok": False, "error": "未授权"}, status=401)
            return
        if not self._acquire():
            self._send_json({"ok": False, "error": "已有查询进行中，请稍候重试"}, status=429)
            return
        try:
            result = subprocess.run(
                [sys.executable, str(ROOT / "push_report.py")],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=300,
            )
            output = (result.stdout or result.stderr or "").strip()[-1000:]
            self._send_json(
                {"ok": result.returncode == 0, "exit_code": result.returncode, "output": output},
                status=200 if result.returncode == 0 else 500,
            )
        except subprocess.TimeoutExpired:
            self._send_json({"ok": False, "error": "筛选运行超时"}, status=504)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=500)
        finally:
            _lock.release()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/":
            if DASHBOARD.exists():
                self._send_html(DASHBOARD)
            else:
                self._send_json({"ok": False, "error": "dashboard.html 不存在，请先运行生成脚本"})
            return

        if path in ("/us", "/us/"):
            if US_DASHBOARD.exists():
                self._send_html(US_DASHBOARD)
            else:
                self._send_json({"ok": False, "error": "美股 dashboard 不存在，请先运行美股页面构建脚本"}, status=404)
            return

        if path in ("/etf", "/etf/"):
            if ETF_DASHBOARD.exists():
                self._send_html(ETF_DASHBOARD)
            else:
                template = (ROOT / "_etf_momentum_template.html").read_text(encoding="utf-8")
                empty = {"action": "UNAVAILABLE", "reason": "尚无行情数据，点击重新查询扫描股票池", "ranked": [], "errors": [], "backtest": {"available": False}}
                with (ROOT / "data" / "etf_pool.csv").open(encoding="utf-8-sig", newline="") as file:
                    options = "".join(
                        f'<option value="{html_escape.escape(row["code"], quote=True)}">{html_escape.escape(row["name"])} {html_escape.escape(row["code"])}</option>'
                        for row in csv.DictReader(file)
                    )
                template = template.replace("__ETF_OPTIONS__", options)
                page = template.replace("__ETF_DATA__", json.dumps(empty, ensure_ascii=False)).replace("__STATIC_MODE__", "false")
                body = page.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            return

        if path == "/api/etf":
            if not self._acquire():
                self._send_json({"ok": False, "error": "已有查询进行中，请稍候重试"}, status=429)
                return
            try:
                from etf_momentum.scan import run_scan
                held_code = (query.get("held", [""])[0] or "").strip().upper() or None
                self._send_json(run_scan(held_code=held_code))
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=502)
            finally:
                _lock.release()
            return

        if path == "/api/scan":
            if not self._acquire():
                self._send_json({"ok": False, "error": "已有查询进行中，请稍候重试"}, status=429)
                return
            try:
                exit_line = query.get("exit", ["mid"])[0]
                if exit_line not in ("mid", "upper"):
                    exit_line = "mid"
                self._send_json(_run_scan(exit_line))
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)})
            finally:
                _lock.release()
            return

        if path == "/api/quote":
            if not self._acquire():
                self._send_json({"ok": False, "error": "已有查询进行中，请稍候重试"}, status=429)
                return
            try:
                self._send_json(_run_quote())
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)})
            finally:
                _lock.release()
            return

        self._send_json({"ok": False, "error": "未知路径 " + path}, status=404)


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"BOLL 实时查询服务已启动： http://{HOST}:{PORT}")
    print("接口： /api/quote （实时行情） · /api/scan?exit=mid|upper （完整筛选）")
    print("按 Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
