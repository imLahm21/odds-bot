"""
假设登记表「后」样本统计（阶段 4，每周或每新增 30 场跑一次）

  python scripts/build_hypothesis_stats.py                 # 只出报告 + 状态建议
  python scripts/build_hypothesis_stats.py --write         # 另把「后」列写回 rules/假设登记表.md

· 样本 = 登记日期（登记表表头「部署时在此填写」）之后开球、已有比分的比赛；
  比分取 scripts/backtest_hypotheses.py scores 的缓存。
· 只统计可机械判定的假设（bot/hypotheses.py 已实现的那部分）。
· **状态不自动变更**：脚本只给出建议（观察中 / 已验证 / 已否决），由人工确认后改表并提交。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import backtest_hypotheses as bt  # noqa: E402
from bot import config, hypotheses, market_facts  # noqa: E402

REGISTRY = ROOT / config.HYPOTHESIS_REGISTRY


def registration_date(text: str) -> str | None:
    m = re.search(r"部署时在此填写：\s*(\d{4}-\d{2}-\d{2})", text)
    return m.group(1) if m else None


def suggest(t: "bt.Tally") -> str:
    ci = t.ci95()
    if t.n >= 60 and ci and ci[1] < 0.5:
        return "已否决"
    if (t.n >= 100 and t.roi is not None and t.roi > 0
            and t.halves_same_sign() and t.robust_sign()):
        return "已验证"
    if t.n >= 30:
        return "观察中"
    return "假设"


def _post_cell(t: "bt.Tally") -> str:
    hit = "—" if t.hit_rate is None else f"{t.hit_rate * 100:.1f}%"
    return f"{t.n} / {hit}"


def write_back(text: str, tallies: dict) -> str:
    """单编号行写「N / X%」；合并行（「H-R3a / H-R3b」「H-S1~S4」）按编号分段写
    「H-R3a N / X%；H-R3b N / X%」，只列有后样本的编号，全无则不动该格。"""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if not line.startswith("| H-"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 8:
            continue
        codes = hypotheses._expand_codes(cells[0])
        hits = [c for c in codes if c in tallies]
        if not hits:
            continue
        if len(codes) == 1:
            cells[6] = _post_cell(tallies[codes[0]])
        else:
            cells[6] = "；".join(f"{c} {_post_cell(tallies[c])}" for c in hits)
        lines[i] = "| " + " | ".join(cells) + " |"
    return "\n".join(lines)


NO_REGISTRATION_DATE = ("登记表表头尚未填写登记日期（「部署时在此填写：YYYY-MM-DD」），"
                        "无法区分登记前后样本。")


def build_report(paths, table: str = "odds_history") -> tuple[str, dict]:
    """统计登记后样本并生成报告 md（含状态建议）；返回 (md, {假设编号: Tally})。
    登记日期未填写时抛 ValueError(NO_REGISTRATION_DATE)。不写任何文件。"""
    text = REGISTRY.read_text(encoding="utf-8")
    since = registration_date(text)
    if not since:
        raise ValueError(NO_REGISTRATION_DATE)
    paths = [Path(p) for p in paths]
    if table == "odds_history":
        tallies, triggered, n_fix, n_scored = _live(paths, since)
    else:
        tallies, triggered, n_fix, n_scored = bt.evaluate_all(
            paths, table, bt.load_scores(), since=since)
    md, _ = bt.render(tallies, triggered, n_fix, n_scored, f"假设登记后样本（{since} 起）")
    status = {c: s for c, (s, _p) in hypotheses.registry_status().items()}
    md += "\n## 状态建议（须人工确认后改表）\n\n| 假设 | 现状态 | 建议 |\n|------|--------|------|\n"
    for code, t in sorted(tallies.items()):
        cur, new = status.get(code, "—"), suggest(t)
        if not cur.startswith(new):
            md += f"| {code} | {cur} | {new} |\n"
    return md, dict(tallies)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--db", action="append", help="默认线上库 odds.db（odds_history）")
    ap.add_argument("--table", default="odds_history")
    ap.add_argument("--write", action="store_true", help="把「后」列写回登记表（不改状态列）")
    args = ap.parse_args()
    try:
        md, tallies = build_report(args.db or [ROOT / config.DB_PATH], args.table)
    except ValueError as exc:
        sys.exit(str(exc))
    out = bt.OUT / "hypothesis_stats.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print(md)
    if args.write:
        text = REGISTRY.read_text(encoding="utf-8")
        REGISTRY.write_text(write_back(text, tallies), encoding="utf-8", newline="\n")
        print(f"已写回「后」列：{REGISTRY}")


def _live(paths, since):
    """线上库 odds_history：fixtures 表 + 原始快照（rows_from_db 内部按节点取最后一行）。"""
    from collections import defaultdict
    scores = bt.load_scores()
    tallies = defaultdict(bt.Tally)
    triggered = defaultdict(int)
    n_fix = n_scored = 0
    for path in paths:
        conn = bt.open_db(path)
        try:
            for fid, league, commence in conn.execute(
                    "SELECT fixture_id, league_name, commence_utc FROM fixtures WHERE commence_utc >= ?",
                    (since,)).fetchall():
                facts = market_facts.build_facts(market_facts.rows_from_db(conn, fid), league or "")
                if not facts.lines:
                    continue
                n_fix += 1
                triggers = hypotheses.evaluate(facts)
                for t in triggers:
                    triggered[t.code] += 1
                score = scores.get(int(fid))
                if score is None:
                    continue
                n_scored += 1
                for t in triggers:
                    settled = bt.settle_trigger(t, facts, score)
                    if settled is not None:
                        tallies[t.code].add(settled[0], settled[1], commence or "")
        finally:
            conn.close()
    return tallies, triggered, n_fix, n_scored


if __name__ == "__main__":
    main()
