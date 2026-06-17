"""
解析层 —— API-Football odds JSON → odds_history 行 + 凯利计算

核心改造（相对旧 main.py:582-709）：
  - 数据源层级变了：response[0].bookmakers[].bets[].values[]
  - 欧赔：bet id=1 (Match Winner)，value=Home/Draw/Away
  - 亚盘：bet id=4 (Asian Handicap)，value="Home +0.75"/"Away -1.25"（文本，需解析）
  - 凯利逻辑保留：某公司赔率 ÷ 全市场平均（移植 _avg/_kelly）
  - odd 字段是字符串，需转 float
"""

import logging
from datetime import datetime, timezone

from . import config

log = logging.getLogger("odds_bot.parser")


# ─── 凯利计算（移植 main.py:616-624）─────────────────────────────────────────
def _avg(lst: list[float]):
    return sum(lst) / len(lst) if lst else None


def _kelly(odds, market_avg):
    if odds and market_avg and market_avg > 0:
        return round(odds / market_avg, 3)
    return None


def _to_float(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


# ─── 节点标签推算 ────────────────────────────────────────────────────────────
def node_label(commence_utc: str, snapshot_utc: str) -> str:
    """根据快照距开球的小时数，对齐到 SOP 的 10 节点语义。"""
    try:
        kick = datetime.fromisoformat(commence_utc.replace("Z", "+00:00"))
        snap = datetime.fromisoformat(snapshot_utc.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return ""
    hours = (kick - snap).total_seconds() / 3600
    if hours < 0:
        return "赛后"
    for threshold, label in config.NODE_THRESHOLDS:
        if hours >= threshold:
            return label
    return "即时"


# ─── 亚盘 value 文本解析 ─────────────────────────────────────────────────────
def parse_ah_value(value_str: str) -> tuple[str, float] | None:
    """
    解析亚盘 value 文本 → (side, handicap_from_home_view)
      "Home +0.75" → ("home", -0.75)   主队 +0.75 表示主队受让，主队视角让球 = -0.75
      "Home -1"    → ("home", +1.0)    主队 -1 表示主队让 1，主队视角让球 = +1.0
      "Away +1.25" → ("away", -1.25)   客队受让 1.25 → 等价主队让 1.25 → 主队视角 +1.25?

    统一约定：handicap 字段存"主队视角让球数"，负=主队受让，正=主队让出。
      Home ±X  : 主队视角 = -(±X)  （Home +0.75 = 主队受让 0.75 = -0.75）
      Away ±X  : 该盘是从客队角度报的，主队视角 = +(±X 的符号反向再反向)
                 客队 +X = 客队受让 X = 主队让出 X = 主队视角 +X
                 但 API 对同一盘口会同时给 Home 和 Away 两条，取 Home 那条即可定盘。
    实际只需用 Home 那条确定盘口；Away 条用于取客队水位。
    """
    parts = value_str.strip().split()
    if len(parts) != 2:
        return None
    side = parts[0].lower()
    num = _to_float(parts[1])
    if num is None or side not in ("home", "away"):
        return None
    return side, num


def extract_asian_handicap(values: list[dict]) -> dict[float, dict]:
    """
    将一家庄的亚盘 values 列表，按"主队视角让球数"聚合主客水位。
    返回 {handicap: {"home_water":x, "away_water":y}}

    关键（已用真实数据验证）：API-Football 对同一盘口同时给两条 value，
    数字和符号相同、仅 Home/Away 前缀不同，二者是同一盘口的两侧水位：
      "Home +0.75"(4.25) 与 "Away +0.75"(1.20) → 去水后主23%/客77%，正好互补。
    因此二者的"主队视角让球数"相同 = -num：
      Home/Away +0.75 → 主队受让 0.75 → handicap = -0.75
      Home/Away -1    → 主队让 1      → handicap = +1.0
    side 仅决定这条赔率存进 home_water 还是 away_water。
    """
    by_line: dict[float, dict] = {}
    for v in values:
        parsed = parse_ah_value(v.get("value", ""))
        if not parsed:
            continue
        side, num = parsed
        odd = _to_float(v.get("odd"))
        if odd is None:
            continue
        home_view = -num   # Home/Away 同号同数字 → 同一盘口，主队视角 = -num
        rec = by_line.setdefault(home_view, {})
        if side == "home":
            rec["home_water"] = odd
        else:
            rec["away_water"] = odd
    # 只保留主客水位都齐的盘口
    return {hc: r for hc, r in by_line.items()
            if "home_water" in r and "away_water" in r}


# ─── 主解析：单场 odds → 行列表 ──────────────────────────────────────────────
def parse_odds_response(entry: dict, snapshot_utc: str,
                        commence_utc: str,
                        pool_ids: set[int] | None = None) -> list[dict]:
    """
    解析 /odds 的 response[0] → odds_history 行列表（含凯利）。
    两轮：先用启用的庄算市场平均，再为每家庄生成行。
    pool_ids: 参与抓取/凯利计算的庄家 ID 集合；None 时回退到 config 全集。
    """
    if pool_ids is None:
        pool_ids = config.KELLY_POOL_IDS
    fixture_id = entry.get("fixture", {}).get("id")
    if not fixture_id:
        return []
    bookmakers = entry.get("bookmakers", [])
    label = node_label(commence_utc, snapshot_utc)

    # ── 第一轮：收集全市场赔率算平均（凯利池）──
    h2h_all = {"home": [], "draw": [], "away": []}
    # 亚盘按盘口线聚合水位
    ah_all: dict[float, dict[str, list]] = {}

    parsed_per_bm: dict[int, dict] = {}  # 缓存每家解析结果，避免二次解析

    for bm in bookmakers:
        bid = bm.get("id")
        if bid not in pool_ids:
            continue
        h2h = None
        ah = None
        for bet in bm.get("bets", []):
            if bet.get("id") == config.BET_MATCH_WINNER:
                vals = {v.get("value", "").lower(): _to_float(v.get("odd"))
                        for v in bet.get("values", [])}
                h2h = {"home": vals.get("home"),
                       "draw": vals.get("draw"),
                       "away": vals.get("away")}
                for k in ("home", "draw", "away"):
                    if h2h[k]:
                        h2h_all[k].append(h2h[k])
            elif bet.get("id") == config.BET_ASIAN_HANDICAP:
                ah = extract_asian_handicap(bet.get("values", []))
                for hc, rec in ah.items():
                    slot = ah_all.setdefault(hc, {"home": [], "away": []})
                    slot["home"].append(rec["home_water"])
                    slot["away"].append(rec["away_water"])
        parsed_per_bm[bid] = {"h2h": h2h, "ah": ah}

    avg_h = {k: _avg(v) for k, v in h2h_all.items()}
    avg_ah = {hc: {"home": _avg(s["home"]), "away": _avg(s["away"])}
              for hc, s in ah_all.items()}

    # ── 第二轮：生成行 ──
    rows = []
    for bm in bookmakers:
        bid = bm.get("id")
        if bid not in parsed_per_bm:
            continue
        bname = config.BOOKMAKER_NAMES.get(bid, bm.get("name", str(bid)))
        pdata = parsed_per_bm[bid]

        # 欧赔行
        h2h = pdata["h2h"]
        if h2h and h2h["home"] and h2h["away"]:
            rows.append({
                "fixture_id": fixture_id, "snapshot_utc": snapshot_utc,
                "node_label": label, "bookmaker_id": bid, "bookmaker": bname,
                "market": "h2h",
                "home_odds": h2h["home"], "draw_odds": h2h["draw"],
                "away_odds": h2h["away"],
                "kelly_home": _kelly(h2h["home"], avg_h["home"]),
                "kelly_draw": _kelly(h2h["draw"], avg_h["draw"]),
                "kelly_away": _kelly(h2h["away"], avg_h["away"]),
                "handicap": None, "home_water": None, "away_water": None,
                "kelly_h_water": None, "kelly_a_water": None,
            })

        # 亚盘行（每条盘口线一行）
        ah = pdata["ah"]
        if ah:
            for hc, rec in ah.items():
                mavg = avg_ah.get(hc, {})
                rows.append({
                    "fixture_id": fixture_id, "snapshot_utc": snapshot_utc,
                    "node_label": label, "bookmaker_id": bid, "bookmaker": bname,
                    "market": "ah",
                    "home_odds": None, "draw_odds": None, "away_odds": None,
                    "kelly_home": None, "kelly_draw": None, "kelly_away": None,
                    "handicap": hc,
                    "home_water": rec["home_water"],
                    "away_water": rec["away_water"],
                    "kelly_h_water": _kelly(rec["home_water"], mavg.get("home")),
                    "kelly_a_water": _kelly(rec["away_water"], mavg.get("away")),
                })

    return rows


def parse_fixtures_response(fixtures: list[dict], league_id: int,
                            league_name: str, season: int) -> list[dict]:
    """解析 /fixtures → fixtures 表行列表。"""
    rows = []
    for f in fixtures:
        fx = f.get("fixture", {})
        teams = f.get("teams", {})
        fid = fx.get("id")
        if not fid:
            continue
        rows.append({
            "fixture_id": fid,
            "league_id": league_id,
            "league_name": league_name,
            "season": season,
            "home_team": teams.get("home", {}).get("name", ""),
            "away_team": teams.get("away", {}).get("name", ""),
            "commence_utc": fx.get("date", ""),
            "status": fx.get("status", {}).get("short", ""),
        })
    return rows
