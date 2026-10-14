# -*- coding: utf-8 -*-
"""BOLL 每日筛选 + pushplus 微信推送。

用法：
    C:\\Python314\\python.exe push_report.py [--no-scan]

默认先跑 scan 抓最新行情并筛选，再生成报告推送到微信。
--no-scan 表示跳过扫描、直接用 output 下已有的 CSV（用于快速重推）。
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
import urllib.request
from zoneinfo import ZoneInfo

import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(BASE, ".pushplus_token")
PUSH_URL = "https://www.pushplus.plus/send"

# 2026 年 A 股非周末休市日（依据沪深交易所 2026 年部分节假日休市安排，上证公告〔2025〕45号）
HOLIDAYS = {
    "2026-01-01", "2026-01-02",
    "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20", "2026-02-23",
    "2026-04-06",
    "2026-05-01", "2026-05-04", "2026-05-05",
    "2026-06-19",
    "2026-09-25",
    "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07",
}

EXIT_LINE = "mid"
CHINA_TZ = ZoneInfo("Asia/Shanghai")


def load_token():
    """从环境变量或本机私密文件读取 PushPlus token。"""
    token = os.getenv("PUSHPLUS_TOKEN", "").strip()
    if not token and os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "r", encoding="utf-8") as stream:
            token = stream.read().strip()
    if not token:
        raise RuntimeError("未配置 PushPlus token：请设置 PUSHPLUS_TOKEN 或 .pushplus_token")
    return token


def run_scan():
    r = subprocess.run(
        [sys.executable, "-m", "boll_strategy", "scan",
         "--provider", "astock", "--exit-line", EXIT_LINE],
        cwd=BASE, capture_output=True, text=True, timeout=180,
    )
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip()[-300:]
        raise RuntimeError("scan 失败：" + tail)


def build_report(now):
    scan_path = os.path.join(BASE, "output", "daily_scan.csv")
    cand_path = os.path.join(BASE, "output", "daily_scan_candidates.csv")
    scan = pd.read_csv(scan_path, encoding="utf-8-sig")
    cands = pd.read_csv(cand_path, encoding="utf-8-sig") if os.path.exists(cand_path) else pd.DataFrame()

    up = int((scan["realtime_change_pct"] > 0).sum())
    down = int((scan["realtime_change_pct"] < 0).sum())
    flat = int((scan["realtime_change_pct"] == 0).sum())
    avg = float(scan["realtime_change_pct"].mean())

    lines = []
    lines.append(f"## BOLL 下轨候选（{len(cands)} 只）")
    lines.append("")
    if len(cands):
        lines.append("| 名称 | 现价 | 今日涨跌 | 下轨 | 买入限价 | 卖出目标 | 评分 | 状态 |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for _, c in cands.iterrows():
            status = "下轨下方" if bool(c["realtime_below_lower"]) else "已反弹"
            lines.append(
                f"| {c['name']} | {c['realtime_price']:.2f} | {c['realtime_change_pct']:+.2f}% "
                f"| {c['lower']:.2f} | {c['buy_limit']:.2f} | {c['selected_sell_target']:.2f} "
                f"| {c['score']:.1f} | {status} |"
            )
    else:
        lines.append("今日无符合条件的候选（无收盘价严格跌破 BOLL 下轨）。")

    # 即将符合：收盘价距下轨 1% 以内且尚未跌破
    scan_tmp = scan.copy()
    scan_tmp["dist_to_lower"] = (scan_tmp["close"] - scan_tmp["lower"]) / scan_tmp["lower"] * 100
    near = scan_tmp[(scan_tmp["dist_to_lower"] > 0) & (scan_tmp["dist_to_lower"] <= 1.0)]
    near = near.sort_values("dist_to_lower").head(10)
    lines.append("")
    lines.append(f"## 即将符合（距下轨 <1%，{len(near)} 只）")
    lines.append("")
    if len(near):
        lines.append("| 名称 | 现价 | 距下轨 | RSI | 状态 |")
        lines.append("|---|---|---|---|---|")
        for _, r in near.iterrows():
            st = "已破下轨" if bool(r["realtime_below_lower"]) else "待跌破"
            lines.append(
                f"| {r['name']} | {r['realtime_price']:.2f} | {r['dist_to_lower']:.2f}% "
                f"| {r['rsi']:.1f} | {st} |"
            )
    else:
        lines.append("暂无")

    lines.append("")
    lines.append("## 市场概况")
    lines.append(f"- 涨 **{up}** / 跌 **{down}** / 平 {flat}，平均 **{avg:+.2f}%**")
    top = scan.nlargest(3, "realtime_change_pct")
    bottom = scan.nsmallest(3, "realtime_change_pct")
    lines.append("- 领涨：" + "、".join(
        f"{r['name']} {r['realtime_change_pct']:+.2f}%" for _, r in top.iterrows()))
    lines.append("- 领跌：" + "、".join(
        f"{r['name']} {r['realtime_change_pct']:+.2f}%" for _, r in bottom.iterrows()))

    lines.append("")
    lines.append(f"<sub>{now.strftime('%Y-%m-%d %H:%M')} · 数据仅供研究，非收益承诺</sub>")
    return "\n".join(lines)


def push(title, content):
    payload = json.dumps({
        "token": load_token(), "title": title, "content": content, "template": "markdown",
    }).encode("utf-8")
    req = urllib.request.Request(PUSH_URL, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-scan", action="store_true", help="跳过扫描，直接用已有 CSV")
    args = ap.parse_args()

    now = datetime.datetime.now(CHINA_TZ)
    today = now.strftime("%Y-%m-%d")
    if now.weekday() >= 5:  # 周六=5 / 周日=6
        print(f"周末休市（{today}），跳过推送")
        return
    if today in HOLIDAYS:
        print(f"法定节假日休市（{today}），跳过推送")
        return
    try:
        if not args.no_scan:
            run_scan()
        content = build_report(now)
    except Exception as e:
        # 查询失败也推送，避免静默失败
        content = "## BOLL 筛选失败\n\n" + str(e) + f"\n\n<sub>{now.strftime('%Y-%m-%d %H:%M')}</sub>"
        title = f"【BOLL】{now.strftime('%m-%d %H:%M')} 查询失败"
        try:
            res = push(title, content)
            print("失败通知已推送:", res)
        except Exception as pe:
            print("推送失败通知也失败:", pe)
        sys.exit(1)

    cands = pd.read_csv(os.path.join(BASE, "output", "daily_scan_candidates.csv"), encoding="utf-8-sig")
    title = f"【BOLL】{now.strftime('%m-%d %H:%M')} 候选 {len(cands)} 只"
    res = push(title, content)
    print("推送结果:", res)
    if res.get("code") == 200:
        print("推送成功")
    else:
        print("推送失败:", res)
        sys.exit(1)


if __name__ == "__main__":
    main()
