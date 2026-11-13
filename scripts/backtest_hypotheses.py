"""
假设全量回测（WP-3.2）：导出精简库全部比赛 → 市场事实 → 假设触发 → 按赛果结算

用法：
  python scripts/backtest_hypotheses.py scores     # 按比赛日调 API-Football 取比分（缓存，约 1 次/比赛日）
  python scripts/backtest_hypotheses.py run        # 算触发、命中率、ROI，写 hypotheses_backtest.md/json

数据：data/odds_export/*.db(.gz)（表 fixtures / odds_node_last，见 docs/规则重新分层方案.md WP-0.1）；
      线上库 odds.db 的 odds_archive（WP-0.1b 归档）也可用 --db 指定。
结算：亚盘按「基准庄终盘主线」与上盘/下盘赔率结算五态；胜平负按基准庄终盘欧赔。
      「X 不出」类预测只算命中率，不算 ROI。结果只作「登记前样本」，不用于升级状态。
"""

from __future__ import annotations

import argparse
import gzip
import json
import shutil
import sqlite3
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import hypotheses, market_facts  # noqa: E402
from bot.hypothesis_settle import Tally, pnl, settle_home, settle_trigger  # noqa: E402,F401

OUT = ROOT / "scripts" / "output" / "hypotheses"
SCORES = OUT / "scores_by_date"
EXPORT = ROOT / "data" / "odds_export"


# ─── 数据源 ──────────────────────────────────────────────────────────────────

def open_db(path: Path) -> sqlite3.Connection:
    """.gz 先解到临时目录（只读打开）。"""
    if path.suffix == ".gz":
        target = Path(tempfile.gettempdir()) / "odds_hypotheses" / path.with_suffix("").name
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or target.stat().st_mtime < path.stat().st_mtime:
            with gzip.open(path, "rb") as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
        path = target
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def iter_fixtures(paths: list[Path], table: str):
    """去重后的 (fixture_id, league_name, home, away, commence_utc, quotes)。"""
    seen: set[int] = set()
    for path in paths:
        conn = open_db(path)
        try:
            fx_table = "fixtures" if table == "odds_node_last" else "fixtures_archive"
            fixtures = conn.execute(
                f"SELECT fixture_id, league_name, home_team, away_team, commence_utc FROM {fx_table}").fetchall()
            for fid, league, home, away, commence in fixtures:
                if fid in seen:
                    continue
                quotes = market_facts.rows_from_db(conn, fid, table)
                if not quotes:
                    continue
                seen.add(fid)
                yield fid, league or "", home, away, commence, quotes
        finally:
            conn.close()


def default_sources() -> list[Path]:
    return sorted(EXPORT.glob("odds_slim_*.db*"))


# ─── 比分 ────────────────────────────────────────────────────────────────────

# 已结束（含取消/延期/腰斩/判负）的比赛状态：缓存里出现其它状态说明取数时比赛还没踢完
FINAL_STATUSES = ("FT", "AET", "PEN", "CANC", "PST", "ABD", "AWD", "WO")
STALE_DAYS = 7


def _needs_fetch(day: str, today: str) -> bool:
    """某比赛日是否需要（重新）取比分：没有缓存 → 取；缓存里有未完场条目且该日
    距今不超过 STALE_DAYS 天 → 重取；更早的不再重取（避免对永远不会完场的条目反复请求）。"""
    path = SCORES / f"{day}.json"
    if not path.exists():
        return True
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    if all(r.get("status") in FINAL_STATUSES for r in rows):
        return False
    from datetime import date
    try:
        age = (date.fromisoformat(today) - date.fromisoformat(day)).days
    except ValueError:
        return False
    return 0 <= age <= STALE_DAYS


def fetch_scores(paths, table: str = "fixtures") -> int:
    """按比赛日调 API-Football 取比分写入缓存；返回本次新取的天数。
    paths：数据库路径列表；table：含 commence_utc 列的赛程表名。"""
    from bot import api_client
    SCORES.mkdir(parents=True, exist_ok=True)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    dates = set()
    for path in paths:
        conn = open_db(Path(path))
        try:
            for (commence,) in conn.execute(f"SELECT commence_utc FROM {table}"):
                if commence and commence < now:
                    dates.add(commence[:10])
        finally:
            conn.close()
    today = now[:10]
    todo = sorted(d for d in dates if _needs_fetch(d, today))
    print(f"比赛日 {len(dates)} 个，待取 {len(todo)} 个")
    for day in todo:
        data = api_client.api_get("/fixtures", {"date": day})
        rows = []
        for m in (data or {}).get("response", []):
            fx = m.get("fixture") or {}
            goals = m.get("goals") or {}
            rows.append({"id": fx.get("id"), "status": (fx.get("status") or {}).get("short"),
                         "home": goals.get("home"), "away": goals.get("away")})
        (SCORES / f"{day}.json").write_text(json.dumps(rows), encoding="utf-8")
        print(day, len(rows))
        time.sleep(0.3)
    return len(todo)


def cmd_scores(args) -> None:
    fetch_scores(args.db or default_sources())


def load_scores() -> dict[int, tuple[int, int]]:
    out = {}
    for f in SCORES.glob("*.json"):
        for r in json.loads(f.read_text(encoding="utf-8")):
            if r.get("status") in ("FT", "AET", "PEN") and r.get("home") is not None and r.get("away") is not None:
                out[int(r["id"])] = (int(r["home"]), int(r["away"]))
    return out


# ─── 结算 ────────────────────────────────────────────────────────────────────
# settle_home / pnl / Tally / settle_trigger 已移入 bot/hypothesis_settle.py（复盘共用）；
# 这里按原名重新导出，build_hypothesis_stats.py 的 bt.Tally / bt.settle_trigger 照常可用。

def evaluate_all(paths, table, scores, since: str | None = None):
    tallies: dict[str, Tally] = defaultdict(Tally)
    triggered: dict[str, int] = defaultdict(int)
    n_fix = n_scored = 0
    for fid, league, _home, _away, commence, quotes in iter_fixtures(paths, table):
        if since and (commence or "") < since:
            continue
        facts = market_facts.build_facts(quotes, league)
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
            settled = settle_trigger(t, facts, score)
            if settled is not None:
                tallies[t.code].add(settled[0], settled[1], commence or "")
    return tallies, triggered, n_fix, n_scored


def render(tallies, triggered, n_fix, n_scored, title: str) -> tuple[str, dict]:
    def pct(v):
        return "—" if v is None else f"{v * 100:.1f}%"
    md = [f"# {title}", "",
          f"- 有亚盘主线的比赛 {n_fix} 场；配到完场比分 {n_scored} 场",
          "- 命中率 = 赢（含半赢）/（赢 + 输），走盘不计；ROI 按基准庄终盘赔率、每注 1 单位",
          "- 95% 区间为 Wilson 区间；「前后半同号」「剔最大单笔同号」对应登记表「已验证」门槛", "",
          "| 假设 | 触发 | 结算 | 命中率 | 95% 区间 | ROI | 前后半同号 | 剔最大单笔同号 |",
          "|------|------|------|--------|---------|-----|-----------|---------------|"]
    data = {}
    for code in sorted(set(triggered) | set(tallies)):
        t = tallies.get(code, Tally())
        ci = t.ci95()
        md.append(f"| {code} | {triggered.get(code, 0)} | {t.n} | {pct(t.hit_rate)} | "
                  f"{'—' if not ci else f'{ci[0] * 100:.0f}~{ci[1] * 100:.0f}%'} | {pct(t.roi)} | "
                  f"{'—' if t.halves_same_sign() is None else ('是' if t.halves_same_sign() else '否')} | "
                  f"{'—' if t.robust_sign() is None else ('是' if t.robust_sign() else '否')} |")
        data[code] = {"triggered": triggered.get(code, 0), "settled": t.n, "hit_rate": t.hit_rate,
                      "ci95": ci, "roi": t.roi, "halves_same_sign": t.halves_same_sign(),
                      "robust_sign": t.robust_sign()}
    return "\n".join(md) + "\n", data


def cmd_run(args) -> None:
    paths = [Path(p) for p in (args.db or default_sources())]
    tallies, triggered, n_fix, n_scored = evaluate_all(paths, args.table, load_scores())
    md, data = render(tallies, triggered, n_fix, n_scored, "假设全量回测（登记前样本）")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "hypotheses_backtest.md").write_text(md, encoding="utf-8")
    (OUT / "hypotheses_backtest.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(md)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("cmd", choices=["scores", "run"])
    ap.add_argument("--db", action="append", help="数据库路径（可多次）；默认 data/odds_export/odds_slim_*.db(.gz)")
    ap.add_argument("--table", default="odds_node_last", choices=["odds_node_last", "odds_archive"],
                    help="odds_node_last（导出精简库）或 odds_archive（线上库归档）")
    args = ap.parse_args()
    {"scores": cmd_scores, "run": cmd_run}[args.cmd](args)


if __name__ == "__main__":
    main()
