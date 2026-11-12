"""
第 8 节候选表由代码生成（WP-3.3）—— 枚举、取价、去抽水、五态 edge 全部在代码里算

· 节点：即时，缺则按 临场④ → 临场③ … 前推（CLAUDE.md 步骤 7.5 第 1 条）。
· 赔率列：首要分析庄该节点报价（market_facts.pick_bookmaker，缺则备选庄顺序）。
· p_市场：该节点全部有报价的庄「隐含概率跨庄平均 → 归一去抽水」。
· p_最终 = p_市场（过渡期不做校准平移）。
· edge：|让球| ≤ 0.5 用去抽水胜平负直接拆五态；|让球| ≥ 0.75 用 goals_model 比分矩阵
  （λ 由大小球反推、按胜平负反标定拆分）；大小球优先用相邻线市场价拆状态；
  胜平负 / 双进为二元式。两侧 edge 同时 < −15% 的线标 excluded（疑似串市场/非可执行副线）。
· 候选：让球主线 + 相邻次线 × 主/客、胜平负三项、大小球主线 + 相邻次线 × 大/小、双进 Yes/No。
  主线 = 挂出庄家数最多的线（并列取绝对值最小 / 最接近 λ_total）。

LLM 只负责照抄此表、填信号强度与数据质量、对可疑报价补充排除理由。
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median

from . import goals_model
from .market_facts import NODE_ORDER, Quote, consistent_lines, pick_bookmaker, rows_from_csv

_SUSPECT_EDGE = -0.15
_MIRROR = {"win": "lose", "half_win": "half_lose", "push": "push",
           "half_lose": "half_win", "lose": "win"}


@dataclass(frozen=True, slots=True)
class Candidate:
    play: str
    odds: float
    p_market: float
    edge: float | None
    eligible: bool
    note: str = ""


@dataclass
class CandidateTable:
    node: str
    bookmaker: str
    snapshot: str
    rows: list[Candidate]
    lam_total: float | None


def _avg_devig(pairs: list[tuple[float, ...]]) -> list[float] | None:
    pairs = [p for p in pairs if all(x and x > 1.0 for x in p)]
    if not pairs:
        return None
    n = len(pairs[0])
    inv = [sum(1 / p[i] for p in pairs) / len(pairs) for i in range(n)]
    total = sum(inv)
    return [x / total for x in inv]


def _pick_node(quotes: list[Quote]) -> str | None:
    present = {q.node for q in quotes}
    for node in reversed(NODE_ORDER):
        if node in present:
            return node
    return None


def _price(quotes: list[Quote], base_order: list[str]) -> Quote | None:
    by_book = {q.bookmaker.lower(): q for q in quotes}
    for book in base_order:
        if book.lower() in by_book:
            return by_book[book.lower()]
    return quotes[0] if quotes else None


def _lines_by_depth(groups: dict[float, list[Quote]], center: float | None) -> tuple[float, float | None] | None:
    """主线（庄家数最多；并列取最接近 center）+ 相邻次线（相差 0.25 或 0.5，庄家数最多）。"""
    if not groups:
        return None
    top = max(len(v) for v in groups.values())
    ref = center if center is not None else 0.0
    main = min((ln for ln, v in groups.items() if len(v) == top), key=lambda ln: (abs(ln - ref), abs(ln)))
    neighbours = [ln for ln in groups if abs(abs(ln - main) - 0.25) < 1e-9 or abs(abs(ln - main) - 0.5) < 1e-9]
    second = max(neighbours, key=lambda ln: (len(groups[ln]), -abs(ln - main), -abs(ln))) if neighbours else None
    return main, second


def _ah_states_1x2(line: float, ph: float, pd: float, pa: float) -> dict | None:
    """|让球| ≤ 0.5 的主队视角五态（line 为主队让球，负 = 主队让出）。"""
    table = {
        0.0: {"win": ph, "push": pd, "lose": pa},
        -0.25: {"win": ph, "half_lose": pd, "lose": pa},
        0.25: {"win": ph, "half_win": pd, "lose": pa},
        -0.5: {"win": ph, "lose": pd + pa},
        0.5: {"win": ph + pd, "lose": pa},
    }
    return table.get(round(line * 4) / 4)


def _mirror(states: dict) -> dict:
    return {_MIRROR[k]: v for k, v in states.items()}


def _fmt_line(x: float) -> str:
    return f"{x:+g}" if x else "0"


def build_table(csv_text: str, league: str = "", home: str = "主", away: str = "客") -> CandidateTable | None:
    quotes = rows_from_csv(csv_text)
    node = _pick_node([q for q in quotes if q.market in ("ah", "h2h", "ou")])
    if node is None:
        return None
    cur = [q for q in quotes if q.node == node]
    books = {q.bookmaker for q in cur}
    base = pick_bookmaker(league, books)
    if base is None:
        return None
    order = [base] + sorted(b for b in books if b != base)
    rows: list[Candidate] = []

    # 胜平负（去抽水，供 ±0.5 内让球拆五态 + λ 拆分）
    h2h = [q for q in cur if q.market == "h2h"]
    p1x2 = _avg_devig([(q.home_odds, q.draw_odds, q.away_odds) for q in h2h])
    snap = ""
    # 大小球 → λ 与比分矩阵
    ou_groups: dict[float, list[Quote]] = {}
    for q in cur:
        if q.market == "ou" and q.handicap is not None and q.home_water and q.away_water:
            ou_groups.setdefault(q.handicap, []).append(q)
    ou_rows = [(ln, median(q.home_water for q in v), median(q.away_water for q in v))
               for ln, v in sorted(ou_groups.items())]
    lam = goals_model.implied_total_goals(ou_rows) if ou_rows else None
    matrix = None
    if lam is not None and p1x2:
        lh, la, _res = goals_model.split_lambdas_from_1x2(lam, *p1x2)
        matrix = goals_model.score_matrix(lh, la)

    # 让球
    ah_groups: dict[float, list[Quote]] = {}
    per_book: dict[str, list[Quote]] = {}
    for q in cur:
        if q.market == "ah" and q.handicap is not None and q.home_water and q.away_water:
            per_book.setdefault(q.bookmaker, []).append(q)
    for book_quotes in per_book.values():          # 剔除各庄自相矛盾（疑似配对错位）的线
        for q in consistent_lines(book_quotes):
            ah_groups.setdefault(q.handicap, []).append(q)
    picked = _lines_by_depth(ah_groups, None)
    for ln in [x for x in (picked or ()) if x is not None]:
        group = ah_groups[ln]
        price = _price(group, order)
        snap = snap or price.snapshot
        pm = _avg_devig([(q.home_water, q.away_water) for q in group])
        if abs(ln) <= 0.5 + 1e-9 and p1x2:
            st_home = _ah_states_1x2(ln, *p1x2)
        elif matrix is not None:
            st_home = goals_model.ah_states(matrix, ln)
        else:
            st_home = None
        pair = []
        for side, odds, p, st in (
            (f"{home} {_fmt_line(ln)}", price.home_water, pm[0] if pm else None, st_home),
            (f"{away} {_fmt_line(-ln)}", price.away_water, pm[1] if pm else None,
             _mirror(st_home) if st_home else None),
        ):
            edge = goals_model.edge_from_states(st, odds) if st else None
            pair.append((side, odds, p, edge))
        suspect = all(e is not None and e < _SUSPECT_EDGE for _s, _o, _p, e in pair)
        for side, odds, p, edge in pair:
            rows.append(Candidate(
                f"让球 {side}", odds, p or 0.0, edge,
                eligible=not suspect and edge is not None,
                note="疑似串市场/非可执行副线（两侧 edge 均 < −15%）" if suspect else (
                    "无法估 λ（缺大小球）" if edge is None else "")))

    # 胜平负
    if h2h and p1x2:
        price = _price(h2h, order)
        snap = snap or price.snapshot
        for label, odds, p in ((f"{home} 胜", price.home_odds, p1x2[0]),
                               ("平局", price.draw_odds, p1x2[1]),
                               (f"{away} 胜", price.away_odds, p1x2[2])):
            if odds:
                rows.append(Candidate(f"胜平负 {label}", odds, p, p * odds - 1, True))

    # 全场大小球
    picked = _lines_by_depth(ou_groups, lam)
    for ln in [x for x in (picked or ()) if x is not None]:
        group = ou_groups[ln]
        price = _price(group, order)
        pm = _avg_devig([(q.home_water, q.away_water) for q in group])
        st_over = goals_model.ou_states_from_market(ou_rows, ln)
        if not st_over and matrix is not None:
            st_over = goals_model.ou_states(matrix, ln)
        pair = []
        for side, odds, p, st in (("大", price.home_water, pm[0] if pm else None, st_over),
                                  ("小", price.away_water, pm[1] if pm else None,
                                   _mirror(st_over) if st_over else None)):
            pair.append((side, odds, p, goals_model.edge_from_states(st, odds) if st else None))
        suspect = all(e is not None and e < _SUSPECT_EDGE for _s, _o, _p, e in pair)
        for side, odds, p, edge in pair:
            rows.append(Candidate(f"大小球 {side} {ln:g}", odds, p or 0.0, edge,
                                  eligible=not suspect and edge is not None,
                                  note="疑似串市场/非可执行副线（两侧 edge 均 < −15%）" if suspect else ""))

    # 双进
    btts = [q for q in cur if q.market == "btts" and q.home_water and q.away_water]
    pm = _avg_devig([(q.home_water, q.away_water) for q in btts])
    if btts and pm:
        price = _price(btts, order)
        for label, odds, p in (("Yes", price.home_water, pm[0]), ("No", price.away_water, pm[1])):
            rows.append(Candidate(f"双进 {label}", odds, p, p * odds - 1, True))

    return CandidateTable(node=node, bookmaker=base, snapshot=snap, rows=rows, lam_total=lam)


def format_table(table: CandidateTable | None) -> str:
    """注入 prompt 的【代码候选表】段落；列与 CLAUDE.md 输出格式第 8 节一致。"""
    if table is None or not table.rows:
        return ""
    out = ["### 代码候选表（第 8 节以此为准：数字照抄，不要重算）",
           f"- 节点：{table.node}；赔率列取首要分析庄 {table.bookmaker}（快照 {table.snapshot or '—'}）；"
           "p_市场 为该节点多庄平均去抽水；过渡期 p_最终 = p_市场",
           "| ID | 玩法 | 赔率 | 门槛=1/赔率 | p_市场 | p_最终 | edge | 资格 |",
           "|----|------|------|-------------|--------|--------|------|------|"]
    notes = []
    for i, c in enumerate(table.rows, start=1):
        edge = "无法估" if c.edge is None else f"{c.edge * 100:+.1f}%".replace("-", "−")
        out.append(f"| C{i:02d} | {c.play} | {c.odds:.2f} | {100 / c.odds:.1f}% | {c.p_market * 100:.1f}% "
                   f"| {c.p_market * 100:.1f}% | {edge} | {'eligible' if c.eligible else 'excluded'} |")
        if c.note:
            notes.append(f"C{i:02d}：{c.note}")
    if notes:
        out.append("- 排除 / 说明：" + "；".join(notes))
    out.append("- 可在表末追加「主队大小球 / 客队大小球」行（CSV 有数据时），ID 顺延；"
               "发现报价可疑可把资格改为 excluded 并写明理由，但不得改动数字")
    return "\n".join(out)


def candidates_block(csv_text: str, league: str = "", home: str = "主", away: str = "客") -> str:
    try:
        text = format_table(build_table(csv_text, league, home, away))
    except Exception:
        return ""
    return text + "\n\n" if text else ""
