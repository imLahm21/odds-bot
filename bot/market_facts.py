"""
市场层事实（纯函数，无 LLM）—— 假设登记表「一、统一机械定义」的代码实现

输入：某场比赛的盘口行（每节点 × 庄 × 市场 × 盘口线的最后一行）；
来源可以是 /export 的 CSV 文本（rows_from_csv）、线上库 odds_history（rows_from_db），
或回测用的导出精简库 odds_node_last（同 rows_from_db，表名可换）。

输出 MarketFacts：基准庄、各节点主线、初/终盘、变盘序列（含时段）、热门方、欧亚差、
水位档、单向升水、欧亚同步、凯利报警、返还率。bot/hypotheses.py 在此之上判定假设触发。

口径约定（与 CLAUDE.md「数据表字段说明」一致）：
  · 让球 handicap = 主队让球数，负 = 主队让出；水位为十进制赔率，净水位 = 水位 − 1。
  · 「上盘」= 初盘主线的让球方；平手初盘时取欧赔初赔概率更高的一方。
  · 「深度」= 上盘视角的让球数（上盘让得越多越大）；升盘 = 深度增加，降盘 = 深度减少。
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field

from . import config

NODE_ORDER = [label for _, label in config.NODE_THRESHOLDS]          # 初盘① … 即时
NODE_HOURS = {label: hours for hours, label in config.NODE_THRESHOLDS}  # 区间下界（小时）
_MARKET_ZH = {"欧指": "h2h", "亚盘": "ah", "大小球": "ou", "主队大小球": "ou_home",
              "客队大小球": "ou_away", "双进": "btts"}

# CLAUDE.md 步骤 1「首要分析庄」表：(联赛关键词, [首要, 备选...])，命中第一个即用
PRIMARY_BOOKMAKERS: list[tuple[tuple[str, ...], list[str]]] = [
    (("premier league", "fa cup", "英超", "足总杯", "scottish", "苏超"), ["Bet365", "William Hill", "BetVictor"]),
    (("world cup", "champions league", "europa", "conference league", "nations league",
      "世界杯", "欧冠", "欧联", "欧协联", "欧国联"), ["Pinnacle", "Bet365", "William Hill"]),
    (("serie a", "coppa italia", "意甲", "意大利杯"), ["Pinnacle", "Bet365", "SBO"]),
    (("primeira", "liga portugal", "葡超"), ["Betano", "Pinnacle", "Bet365"]),
    (("j1", "j. league", "k league", "super league", "chinese", "saudi", "a-league",
      "日职", "韩k", "中超", "沙特", "澳超"), ["SBO", "Dafabet", "Pinnacle"]),
    (("eliteserien", "allsvenskan", "veikkaus", "besta", "挪超", "瑞典超", "芬兰超", "冰岛超"),
     ["NordicBet", "Unibet", "Pinnacle"]),
    (("süper lig", "super lig", "土超"), ["1xBet", "Betano", "Pinnacle"]),
    (("virsliga", "latvia", "premier division", "爱尔兰", "拉脱"), ["1xBet", "Marathonbet", "Pinnacle"]),
    (("major league soccer", "mls", "美职联"), ["Pinnacle", "Bet365", "1xBet"]),
    (("la liga", "copa del rey", "bundesliga", "dfb", "ligue 1", "coupe de france",
      "eredivisie", "jupiler", "西甲", "国王杯", "德甲", "德国杯", "法甲", "法国杯", "荷甲", "比甲"),
     ["Pinnacle", "Bet365", "Betano"]),
]
FALLBACK_BOOKMAKERS = ["Pinnacle", "Bet365", "1xBet"]

# reference_asian_handicap 第十一章换算表：盘口深度 → 15 档净水位（0.75~1.10，步长 0.025）对应的胜赔
_CONVERSION = {
    0.0: [2.50, 2.55, 2.60, 2.65, 2.70, 2.75, 2.80, 2.85, 2.90, 2.95, 3.00, 3.05, 3.10, 3.15, 3.20],
    0.25: [2.00, 2.03, 2.07, 2.10, 2.13, 2.17, 2.20, 2.23, 2.27, 2.30, 2.33, 2.37, 2.40, 2.43, 2.46],
    0.5: [1.75, 1.78, 1.80, 1.83, 1.85, 1.88, 1.90, 1.93, 1.95, 1.98, 2.00, 2.03, 2.05, 2.08, 2.10],
    0.75: [1.50, 1.52, 1.53, 1.55, 1.57, 1.58, 1.60, 1.62, 1.63, 1.65, 1.67, 1.68, 1.70, 1.72, 1.73],
    1.0: [1.38, 1.39, 1.40, 1.41, 1.43, 1.44, 1.45, 1.46, 1.48, 1.49, 1.50, 1.51, 1.53, 1.54, 1.55],
    1.25: [1.30, 1.31, 1.32, 1.33, 1.34, 1.35, 1.36, 1.37, 1.38, 1.39, 1.40, 1.41, 1.42, 1.43, 1.44],
    1.5: [1.25, 1.26, 1.27, 1.28, 1.28, 1.29, 1.29, 1.30, 1.31, 1.32, 1.33, 1.34, 1.35, 1.36, 1.37],
    1.75: [1.21, 1.21, 1.22, 1.22, 1.24, 1.24, 1.25, 1.26, 1.27, 1.28, 1.29, 1.30, 1.30, 1.31, 1.31],
    2.0: [1.19, 1.19, 1.20, 1.20, 1.21, 1.21, 1.22, 1.22, 1.23, 1.24, 1.25, 1.26, 1.27, 1.28, 1.28],
    2.25: [1.17, 1.17, 1.18, 1.18, 1.19, 1.19, 1.20, 1.20, 1.21, 1.21, 1.22, 1.22, 1.23, 1.23, 1.24],
    2.5: [1.15, 1.15, 1.16, 1.16, 1.17, 1.17, 1.18, 1.18, 1.19, 1.19, 1.20, 1.20, 1.21, 1.21, 1.22],
    2.75: [1.14, 1.14, 1.15, 1.15, 1.16, 1.16, 1.17, 1.17, 1.18, 1.18, 1.19, 1.19, 1.20, 1.20, 1.20],
    3.0: [1.13, 1.13, 1.13, 1.14, 1.14, 1.14, 1.15, 1.15, 1.15, 1.16, 1.16, 1.16, 1.17, 1.17, 1.17],
}
_WATER_COLUMNS = [0.75 + 0.025 * i for i in range(15)]

HEAT_PP, BIG_HEAT_PP = 1.5, 3.0
GAP_STEP = 0.25
MONO_RISE_NODES, MONO_RISE_TOTAL = 3, 0.10


# ─── 输入行 ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class Quote:
    node: str
    bookmaker: str
    market: str                    # h2h / ah / ou / ou_home / ou_away / btts
    home_odds: float | None = None
    draw_odds: float | None = None
    away_odds: float | None = None
    kelly_home: float | None = None
    kelly_draw: float | None = None
    kelly_away: float | None = None
    handicap: float | None = None
    home_water: float | None = None
    away_water: float | None = None
    snapshot: str = ""


def _num(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def rows_from_csv(csv_text: str) -> list[Quote]:
    """/export CSV → Quote 列表。节点名取「快照时间(CST)」括注。"""
    out = []
    for r in csv.DictReader(io.StringIO((csv_text or "").lstrip("﻿"))):
        snap = r.get("快照时间(CST)") or ""
        m = re.search(r"（([^）]+)）", snap)
        node = m.group(1) if m else ""
        market = _MARKET_ZH.get((r.get("盘口类型") or "").strip())
        if not market or node not in NODE_HOURS:
            continue
        out.append(Quote(
            node=node, bookmaker=(r.get("博彩公司") or "").strip(), market=market,
            home_odds=_num(r.get("主胜赔率")), draw_odds=_num(r.get("平局赔率")),
            away_odds=_num(r.get("客胜赔率")), kelly_home=_num(r.get("凯利(主胜)")),
            kelly_draw=_num(r.get("凯利(平局)")), kelly_away=_num(r.get("凯利(客胜)")),
            handicap=_num(r.get("让球")), home_water=_num(r.get("主队水位")),
            away_water=_num(r.get("客队水位")), snapshot=snap))
    return out


def rows_from_db(conn, fixture_id: int, table: str = "odds_history") -> list[Quote]:
    """线上库 / 导出精简库 → Quote 列表（每节点 × 庄 × 市场 × 线取最后一行）。"""
    if not re.fullmatch(r"[A-Za-z_]+", table):
        raise ValueError("非法表名")
    from .db import ah_handicap_sql           # 修复前的亚盘行让球符号相反，读时翻回
    hc = ah_handicap_sql()
    cur = conn.execute(
        "SELECT node_label, bookmaker, market, home_odds, draw_odds, away_odds, "
        "kelly_home, kelly_draw, kelly_away, hc, home_water, away_water, snapshot_utc "
        f"FROM (SELECT *, {hc} AS hc, ROW_NUMBER() OVER (PARTITION BY node_label, bookmaker_id, "
        f"market, {hc} ORDER BY snapshot_utc DESC) AS rn FROM {table} WHERE fixture_id=?) "
        "WHERE rn=1", (fixture_id,))
    return [Quote(node=r[0] or "", bookmaker=r[1] or "", market=r[2], home_odds=r[3],
                  draw_odds=r[4], away_odds=r[5], kelly_home=r[6], kelly_draw=r[7],
                  kelly_away=r[8], handicap=r[9], home_water=r[10], away_water=r[11],
                  snapshot=r[12] or "")
            for r in cur.fetchall() if (r[0] or "") in NODE_HOURS]


# ─── 事实 ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class NodeLine:
    node: str
    handicap: float                # 主队视角
    depth: float                   # 上盘视角深度
    upper_water: float             # 上盘十进制水位
    lower_water: float


@dataclass(frozen=True, slots=True)
class Move:
    from_node: str
    to_node: str
    delta: float                   # 深度变化（+ 升盘 / − 降盘）
    phase: str                     # early / mid / late
    end: bool                      # 发生在末端（≤1.5h）
    upper_water_change: float      # 本次变盘前后上盘水位变化


@dataclass
class MarketFacts:
    base_bookmaker: str | None
    upper: str | None                         # "home" / "away"
    nodes: list[str] = field(default_factory=list)
    lines: list[NodeLine] = field(default_factory=list)
    moves: list[Move] = field(default_factory=list)
    initial_depth: float | None = None
    final_depth: float | None = None
    theory_depth: float | None = None
    gap: float | None = None
    gap_class: str = "unknown"                # deep / shallow / aligned / reversed / unknown
    prob_initial: dict = field(default_factory=dict)   # 基准庄欧赔去抽水 home/draw/away
    prob_final: dict = field(default_factory=dict)
    heat_side: str | None = None              # "home" / "away" / None
    heat_pp: float = 0.0
    big_heat: bool = False
    euro_initial: dict = field(default_factory=dict)
    euro_final: dict = field(default_factory=dict)
    upper_water_initial: float | None = None
    upper_water_final: float | None = None
    water_band_final: str | None = None
    mono_rise: bool = False
    consecutive_moves: int = 0                # 同向连续变盘的最长次数（带方向符号）
    return_initial: float | None = None
    return_final: float | None = None
    kelly_alarms: list[str] = field(default_factory=list)
    kelly_flags: dict = field(default_factory=dict)    # {"bet365_over": [...], "pinnacle_under": [...]}

    @property
    def upper_heat(self) -> bool:
        return self.heat_side is not None and self.heat_side == self.upper

    @property
    def lower_heat(self) -> bool:
        return self.heat_side is not None and self.heat_side != self.upper


def phase_of(node: str) -> str:
    hours = NODE_HOURS.get(node, 0)
    return "early" if hours >= 24 else ("mid" if hours >= 6 else "late")


def is_end(node: str) -> bool:
    return NODE_HOURS.get(node, 99) <= 1.5


def water_band(net: float | None) -> str | None:
    if net is None:
        return None
    if net < 0.80:
        return "超低"
    if net < 0.87:
        return "低"
    if net < 0.95:
        return "中"
    if net <= 1.05:
        return "高"
    return "超高"


def pick_bookmaker(league_name: str, available: set[str]) -> str | None:
    lg = (league_name or "").lower()
    order = FALLBACK_BOOKMAKERS
    for keys, books in PRIMARY_BOOKMAKERS:
        if any(k in lg for k in keys):
            order = books + [b for b in FALLBACK_BOOKMAKERS if b not in books]
            break
    lowered = {b.lower(): b for b in available}
    for book in order:
        if book.lower() in lowered:
            return lowered[book.lower()]
    return sorted(available)[0] if available else None


def _devig(*odds: float | None) -> list[float] | None:
    if any(o is None or o <= 1.0 for o in odds):
        return None
    inv = [1 / o for o in odds]
    total = sum(inv)
    return [x / total for x in inv]


def theory_depth(fav_odds: float, net_water: float | None = None) -> float:
    """欧赔（让球方胜赔）→ 理论让球深度（换算表最近的一行）。"""
    col = 6                                                    # 默认 0.90 列
    if net_water is not None:
        col = min(range(len(_WATER_COLUMNS)), key=lambda i: abs(_WATER_COLUMNS[i] - net_water))
    return min(_CONVERSION, key=lambda d: abs(_CONVERSION[d][col] - fav_odds))


def consistent_lines(quotes: list[Quote], tol: float = 0.02) -> list[Quote]:
    """剔除同一庄同一节点内概率顺序自相矛盾的让球线。

    主队受让越多（handicap 越大）主队赢盘概率只能越高。违反来源（2026-11 对原始 API 与导出库实测）：
    ① 2026-09-06 修复前入库的行让球符号相反，与修复后同节点的行混在一起
       （rows_from_db 已用 db.ah_handicap_sql() 翻回，此处兜底）；
    ② Marathonbet / 1xBet 的极端尾部线（赔率 1.00~1.08）报价噪声，占其节点组约四成，不影响主线；
    ③ 个别庄单条线报价滞后（Pinnacle 约 3%）。parser 本身的 Home/Away 配对经原始数据核对无误。
    保留按 handicap 排序后概率单调不减的最长子序列。"""
    rows = []
    for q in quotes:
        p = _devig(q.home_water, q.away_water)
        if q.handicap is not None and p:
            rows.append((q.handicap, p[0], q))
    rows.sort(key=lambda r: r[0])
    if len(rows) <= 1:
        return [r[2] for r in rows]
    def slope(chain: list[int]) -> float:
        # 并列时取「每多受让 1 球、主队概率上升最多」的那组：错位线会让斜率接近 0
        a, b = rows[chain[0]], rows[chain[-1]]
        return (b[1] - a[1]) / (b[0] - a[0]) if b[0] != a[0] else 0.0

    best: list[list] = []
    for i, (_hc, p, _q) in enumerate(rows):
        chain = [i]
        for j in range(i):
            if rows[j][1] > p + tol:
                continue
            cand = best[j] + [i]
            if len(cand) > len(chain) or (len(cand) == len(chain) and slope(cand) > slope(chain)):
                chain = cand
        best.append(chain)
    longest = max(len(c) for c in best)
    keep = max((c for c in best if len(c) == longest), key=slope)
    return [rows[i][2] for i in keep]


MAIN_LINE_MAX_GAP = 0.15        # 主线两侧去抽水概率须在 35%~65%，否则该节点视为无主线


def main_line(quotes: list[Quote]) -> Quote | None:
    """该节点亚盘里两侧去抽水概率最接近 50/50 的那条线（先剔除自相矛盾的线）。
    最接近的那条也偏离 50/50 超过 15 个百分点时（庄家该节点只挂了极端副线）返回 None。"""
    best, best_gap = None, 9.0
    for q in consistent_lines(quotes):
        p = _devig(q.home_water, q.away_water)
        if q.handicap is None or not p:
            continue
        gap = abs(p[0] - 0.5)
        if gap > MAIN_LINE_MAX_GAP:
            continue
        if gap < best_gap - 1e-12 or (abs(gap - best_gap) < 1e-12 and best is not None
                                      and abs(q.handicap) < abs(best.handicap)):
            best, best_gap = q, gap
    return best


def build_facts(quotes: list[Quote], league_name: str = "") -> MarketFacts:
    books = {q.bookmaker for q in quotes if q.market == "ah"}
    base = pick_bookmaker(league_name, books)
    facts = MarketFacts(base_bookmaker=base, upper=None)
    if base is None:
        return facts
    by_node: dict[str, list[Quote]] = {}
    euro: dict[str, Quote] = {}
    for q in quotes:
        if q.bookmaker != base:
            continue
        if q.market == "ah":
            by_node.setdefault(q.node, []).append(q)
        elif q.market == "h2h":
            euro[q.node] = q
    nodes = [n for n in NODE_ORDER if n in by_node]
    mains = {n: main_line(by_node[n]) for n in nodes}
    nodes = [n for n in nodes if mains[n] is not None]
    if not nodes:
        return facts
    facts.nodes = nodes
    first = mains[nodes[0]]
    euro_nodes = [n for n in NODE_ORDER if n in euro]
    e0 = euro[euro_nodes[0]] if euro_nodes else None
    e1 = euro[euro_nodes[-1]] if euro_nodes else None
    # 上盘：初盘主线让球方；平手时取欧赔初赔更低（概率更高）的一方
    if first.handicap < 0:
        upper = "home"
    elif first.handicap > 0:
        upper = "away"
    elif e0 and e0.home_odds and e0.away_odds:
        upper = "home" if e0.home_odds <= e0.away_odds else "away"
    else:
        upper = "home"
    facts.upper = upper
    sign = -1.0 if upper == "home" else 1.0
    for n in nodes:
        q = mains[n]
        uw, lw = (q.home_water, q.away_water) if upper == "home" else (q.away_water, q.home_water)
        facts.lines.append(NodeLine(n, q.handicap, sign * q.handicap, uw, lw))
    facts.initial_depth = facts.lines[0].depth
    facts.final_depth = facts.lines[-1].depth
    facts.upper_water_initial = facts.lines[0].upper_water
    facts.upper_water_final = facts.lines[-1].upper_water
    facts.water_band_final = water_band(
        facts.upper_water_final - 1 if facts.upper_water_final else None)

    run, best_run, last_dir = 0, 0, 0
    for prev, cur in zip(facts.lines, facts.lines[1:]):
        delta = cur.depth - prev.depth
        if abs(delta) >= GAP_STEP - 1e-9:
            facts.moves.append(Move(prev.node, cur.node, delta, phase_of(cur.node),
                                    is_end(cur.node), cur.upper_water - prev.upper_water))
            direction = 1 if delta > 0 else -1
            run = run + 1 if direction == last_dir else 1
            last_dir = direction
            if run > abs(best_run):
                best_run = run * direction
    facts.consecutive_moves = best_run       # 同向变盘（不要求相邻节点）的最长连续次数，带方向

    # 单向升水：主线不变的连续 ≥3 个节点里上盘净水位单调上升且累计 ≥0.10
    seg = [facts.lines[0]]
    for cur in facts.lines[1:]:
        if abs(cur.depth - seg[-1].depth) < 1e-9 and cur.upper_water >= seg[-1].upper_water:
            seg.append(cur)
        else:
            seg = [cur]
        if (len(seg) >= MONO_RISE_NODES
                and seg[-1].upper_water - seg[0].upper_water >= MONO_RISE_TOTAL - 1e-9):
            facts.mono_rise = True

    # 欧赔：去抽水概率、热门方、返还率
    if e0 and e1:
        p0 = _devig(e0.home_odds, e0.draw_odds, e0.away_odds)
        p1 = _devig(e1.home_odds, e1.draw_odds, e1.away_odds)
        if p0 and p1:
            facts.prob_initial = dict(zip(("home", "draw", "away"), p0))
            facts.prob_final = dict(zip(("home", "draw", "away"), p1))
            dh = (p1[0] - p0[0]) * 100
            da = (p1[2] - p0[2]) * 100
            side, pp = ("home", dh) if dh >= da else ("away", da)
            if pp >= HEAT_PP:
                facts.heat_side, facts.heat_pp, facts.big_heat = side, pp, pp >= BIG_HEAT_PP
        facts.euro_initial = {"home": e0.home_odds, "draw": e0.draw_odds, "away": e0.away_odds}
        facts.euro_final = {"home": e1.home_odds, "draw": e1.draw_odds, "away": e1.away_odds}
        for attr, q in (("return_initial", e0), ("return_final", e1)):
            if q.home_odds and q.draw_odds and q.away_odds:
                setattr(facts, attr, 1 / (1 / q.home_odds + 1 / q.draw_odds + 1 / q.away_odds))
        fav_odds = e0.home_odds if upper == "home" else e0.away_odds
        if fav_odds:
            net = facts.upper_water_initial - 1 if facts.upper_water_initial else None
            facts.theory_depth = theory_depth(fav_odds, net)
            if fav_odds > 2.5 + 1e-9:   # 欧赔看让球方不占优 → 理论盘口在平手之下
                facts.theory_depth = 0.0
            facts.gap = facts.initial_depth - facts.theory_depth
            if facts.initial_depth < -1e-9:
                facts.gap_class = "reversed"
            elif facts.gap >= GAP_STEP - 1e-9:
                facts.gap_class = "deep"
            elif facts.gap <= -GAP_STEP + 1e-9:
                facts.gap_class = "shallow"
            else:
                facts.gap_class = "aligned"

    # 凯利报警（终盘节点，全部庄）
    final_node = facts.nodes[-1]
    for q in quotes:
        if q.market != "h2h" or q.node != final_node:
            continue
        name = q.bookmaker.lower()
        for side, k in (("home", q.kelly_home), ("draw", q.kelly_draw), ("away", q.kelly_away)):
            if k is None:
                continue
            if name == "bet365" and k > 1.03:
                facts.kelly_flags.setdefault("bet365_over", []).append(side)
                facts.kelly_alarms.append(f"Bet365 {side} 凯利 {k:.3f} > 1.03")
            if name == "pinnacle" and k < 0.96:
                facts.kelly_flags.setdefault("pinnacle_under", []).append(side)
                facts.kelly_alarms.append(f"Pinnacle {side} 凯利 {k:.3f} < 0.96")
    return facts


_SIDE_ZH = {"home": "主队", "away": "客队", "draw": "平局"}
_PHASE_ZH = {"early": "早期", "mid": "中期", "late": "临场"}
_GAP_ZH = {"deep": "深于换算", "shallow": "浅于换算", "aligned": "欧亚一致",
           "reversed": "方向相反", "unknown": "无法计算（缺欧赔）"}


def format_facts(facts: MarketFacts) -> str:
    """注入 prompt 的【市场事实】段落。"""
    if not facts.lines:
        return "### 市场事实（代码计算）\n- 亚盘主线数据不足，无法计算"
    lines = ["### 市场事实（代码计算，步骤 2/3/5 以此为准）",
             f"- 基准庄：{facts.base_bookmaker}；上盘：{_SIDE_ZH.get(facts.upper, '—')}"]
    path = " → ".join(f"{l.node} {l.handicap:+g}@{l.upper_water:.2f}" for l in facts.lines)
    lines.append(f"- 主线路径（主队让球@上盘水位）：{path}")
    if facts.theory_depth is not None:
        lines.append(f"- 欧亚差：理论深度 {facts.theory_depth:g}，实际初盘深度 {facts.initial_depth:g}，"
                     f"差 {facts.gap:+g} → {_GAP_ZH[facts.gap_class]}")
    else:
        lines.append(f"- 欧亚差：{_GAP_ZH['unknown']}")
    if facts.moves:
        mv = "；".join(f"{m.from_node}→{m.to_node} {'升' if m.delta > 0 else '降'}{abs(m.delta):g}"
                       f"（{_PHASE_ZH[m.phase]}"
                       f"{'·末端' if m.end else ''}，上盘水 {m.upper_water_change:+.2f}）" for m in facts.moves)
        lines.append(f"- 变盘：{mv}")
    else:
        lines.append("- 变盘：全程不变盘")
    if facts.prob_initial:
        def fmt(p):
            return "/".join(f"{p[k] * 100:.1f}" for k in ("home", "draw", "away"))
        heat = (f"{_SIDE_ZH[facts.heat_side]}（+{facts.heat_pp:.1f}pp{'，大热' if facts.big_heat else ''}）"
                if facts.heat_side else "无明显热门")
        lines.append(f"- 基准庄欧赔去抽水 主/平/客：初 {fmt(facts.prob_initial)} → 终 {fmt(facts.prob_final)}；热门方：{heat}")
    if facts.upper_water_final:
        lines.append(f"- 上盘终盘净水位 {facts.upper_water_final - 1:.2f}（{facts.water_band_final}）"
                     f"；单向升水：{'是' if facts.mono_rise else '否'}")
    if facts.return_initial and facts.return_final:
        lines.append(f"- 返还率：{facts.return_initial:.1%} → {facts.return_final:.1%}")
    lines.append("- 凯利报警（终盘）：" + ("；".join(facts.kelly_alarms) if facts.kelly_alarms else "无"))
    return "\n".join(lines)
