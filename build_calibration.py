#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_calibration.py —— 从战绩表重算「置信度 → 真实胜率」校准表

用途：
    读本地 竞彩.xlsx 的下注战绩，按「置信度分桶」和「玩法分类」统计真实胜率/ROI，
    输出可直接粘进 rules/方法论/reference_staking_kelly.md 第二章的 Markdown 表。

    这是【低频重跑】产物：每新增约 30 笔或每周一次即可，不需要每天跑。
    每日「这场买什么/买多少」靠 LLM 实时算的 p_市场，不依赖本脚本。
    使用说明见 build_calibration.md。

只依赖本地 竞彩.xlsx + openpyxl，不碰服务器/赔率库，只读不写表格。

用法：
    python build_calibration.py                     # 默认 5 个 sheet
    python build_calibration.py --months 3          # 只取最近 3 个月（按日期滚动窗口）
    python build_calibration.py --sheets 世界杯 20260701-   # 指定 sheet
    python build_calibration.py --xlsx 竞彩.xlsx     # 指定表格路径
"""

import argparse
import io
import os
import re
import sys
from datetime import datetime, timedelta

try:
    import openpyxl
except ImportError:
    sys.exit("缺少 openpyxl，请先: pip install openpyxl")

# 默认统计的 sheet（用户选定的最近 3 个有效窗口）
# 更早的表因模型改动过多、判读尺子已漂移，剔除以免污染校准。
DEFAULT_SHEETS = [
    "20260520-20260630",
    "世界杯",
    "20260701-20260731",
]

# 置信度分桶边界（左闭右开）：与 reference_staking_kelly.md 第二章一致
CONF_BUCKETS = [(0, 60), (60, 64), (64, 66), (66, 70), (70, 74), (74, 200)]
CONF_LABELS = ["< 60", "60–64", "64–66", "66–70", "70–74", "74+"]

# 玩法分类顺序（输出用）
PLAY_CATS = ["让球", "大小球", "双进", "胜平负/其他", "波胆", "串关"]


def classify_play(play: str) -> str:
    """按投注玩法文本分类。规则与 plan 约定一致。"""
    p = str(play or "")
    if "&" in p or ("+" in p and "vs" in p):
        return "串关"
    if re.search(r"\d\s*:\s*\d", p):
        return "波胆"
    if "双进" in p:
        return "双进"
    if "大" in p or "小" in p:
        return "大小球"
    if re.search(r"[-+]\d", p) or "受" in p:
        return "让球"
    return "胜平负/其他"


def header_map(ws) -> dict:
    """表头名 → 列号（1-based）。取第一行；重名保留最左。"""
    h = {}
    for c in range(1, ws.max_column + 1):
        v = ws.cell(1, c).value
        if v is not None:
            h.setdefault(str(v).strip(), c)
    return h


def parse_date(v):
    """把 A 列日期解析成 datetime（兼容 datetime 对象与 '2026/7/3-1:00' 等字符串）。"""
    if isinstance(v, datetime):
        return v
    s = str(v or "").strip()
    if not s:
        return None
    # 取前面的日期部分（去掉 '-1:00' 之类的时间尾巴）
    s = re.split(r"[ \-T]", s)[0] if "/" in s else s.split(" ")[0]
    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def collect_rows(xlsx_path, sheets, months):
    """从指定 sheet 收集带置信度的决策行。返回 [(conf, odds, stake, net, result, play, sheet, date)]。"""
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    cutoff = None
    if months:
        cutoff = datetime.now() - timedelta(days=int(round(months * 30.4)))
    rows = []
    seen_sheets = []
    for ws in wb.worksheets:
        if ws.title not in sheets:
            continue
        seen_sheets.append(ws.title)
        h = header_map(ws)
        ci = h.get("置信度")
        oi = h.get("赔率")
        si = h.get("投入金额") or h.get("投入金额 ")
        ni = h.get("净盈亏")
        ri = h.get("结果")
        pi = h.get("投注玩法")
        di = h.get("日期")
        if not (ci and oi and ni):
            continue
        for r in range(2, ws.max_row + 1):
            conf = ws.cell(r, ci).value
            if not isinstance(conf, (int, float)):
                continue  # 只统计带数值置信度的「决策」行
            net = ws.cell(r, ni).value
            odds = ws.cell(r, oi).value
            if net is None or odds is None:
                continue
            date = parse_date(ws.cell(r, di).value) if di else None
            if cutoff and date and date < cutoff:
                continue
            stake = ws.cell(r, si).value if si else 0
            result = ws.cell(r, ri).value if ri else ""
            play = ws.cell(r, pi).value if pi else ""
            rows.append((float(conf), float(odds), float(stake or 0), float(net),
                         str(result or "").strip(), str(play or ""), ws.title, date))
    missing = [s for s in sheets if s not in seen_sheets]
    return rows, seen_sheets, missing


def stat(sel):
    """给一组行算 (n, 胜率%, ROI%, 净盈亏)。胜负判定：净盈亏 > 0 记为红（含赢一半）。"""
    n = len(sel)
    if n == 0:
        return (0, None, None, 0.0)
    win = sum(1 for x in sel if x[3] > 1e-4)
    stk = sum(x[2] for x in sel)
    net = sum(x[3] for x in sel)
    roi = (100 * net / stk) if stk else None
    return (n, 100 * win / n, roi, net)


def fmt_pct(v):
    return "—" if v is None else f"{v:.1f}%"


def fmt_roi(v):
    return "—" if v is None else f"{v:+.1f}%"


def build_markdown(rows):
    """产出可直接粘进文档第二章的 Markdown。"""
    out = []
    out.append("### 置信度校准表（本人实测，Kelly 的 p 从这里取）\n")
    out.append("| 自评置信度 | 笔数 | 实测胜率 | ROI | 净盈亏 | 判定 |")
    out.append("|-----------|------|---------|-----|--------|------|")
    for (lo, hi), label in zip(CONF_BUCKETS, CONF_LABELS):
        sel = [x for x in rows if lo <= x[0] < hi]
        n, wr, roi, net = stat(sel)
        verdict = "样本不足" if n < 15 else (
            "✅ 盈利" if (roi is not None and roi > 0) else "❌ 亏")
        out.append(f"| {label} | {n} | {fmt_pct(wr)} | {fmt_roi(roi)} "
                   f"| {net:+.2f} | {verdict} |")
    out.append("")
    out.append("### 玩法分类表现\n")
    out.append("| 玩法 | 笔数 | 命中 | ROI | 净盈亏 | 用法 |")
    out.append("|------|------|------|-----|--------|------|")
    for cat in PLAY_CATS:
        sel = [x for x in rows if classify_play(x[5]) == cat]
        n, wr, roi, net = stat(sel)
        if n == 0:
            continue
        if cat in ("波胆", "串关"):
            use = "❌ 拉黑"
        elif roi is not None and roi > 0:
            use = "✅ 优先" if n >= 10 else "✅ 优先（小样本）"
        else:
            use = "⚠️ 降级"
        out.append(f"| {cat} | {n} | {fmt_pct(wr)} | {fmt_roi(roi)} "
                   f"| {net:+.2f} | {use} |")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(
        description="从 竞彩.xlsx 重算置信度→真实胜率校准表（低频重跑，非每天）")
    ap.add_argument("--xlsx", default="竞彩.xlsx", help="战绩表路径（默认 竞彩.xlsx）")
    ap.add_argument("--sheets", nargs="+", default=None,
                    help="指定统计的 sheet 名（默认用户选定的 5 个）")
    ap.add_argument("--months", type=float, default=None,
                    help="只取最近 N 个月（按 A 列日期滚动窗口；缺日期的行仍计入）")
    ap.add_argument("--out", default=None,
                    help="把 Markdown 另存到文件（默认只打印到终端）")
    args = ap.parse_args()

    if not os.path.exists(args.xlsx):
        sys.exit(f"找不到战绩表: {args.xlsx}")
    sheets = args.sheets or DEFAULT_SHEETS

    rows, seen, missing = collect_rows(args.xlsx, sheets, args.months)

    # Windows 终端 GBK 常打不出部分字符，统一用 UTF-8 包一层 stdout
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    print(f"# 校准表（生成于 {datetime.now():%Y-%m-%d %H:%M}）\n")
    print(f"- 数据源：{args.xlsx}")
    print(f"- 统计 sheet：{', '.join(seen) if seen else '（无匹配）'}")
    if missing:
        print(f"- ⚠️ 未找到 sheet：{', '.join(missing)}")
    if args.months:
        print(f"- 滚动窗口：最近 {args.months} 个月")
    print(f"- 有效决策样本（带数值置信度）：{len(rows)} 笔\n")

    if not rows:
        print("（无样本，检查 sheet 名或 --months 是否过窄）")
        return

    md = build_markdown(rows)
    print(md)

    if args.out:
        with io.open(args.out, "w", encoding="utf-8") as f:
            f.write(f"# 校准表（生成于 {datetime.now():%Y-%m-%d %H:%M}）\n\n")
            f.write(f"- 有效决策样本：{len(rows)} 笔\n\n")
            f.write(md + "\n")
        print(f"\n已写入：{args.out}")


if __name__ == "__main__":
    main()
