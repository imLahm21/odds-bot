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


# ─── 大小球 value 文本解析 ───────────────────────────────────────────────────
def parse_ou_value(value_str: str) -> tuple[str, float] | None:
    """
    解析大小球 value 文本 → (side, line)
      "Over 2.5"  → ("over", 2.5)
      "Under 2.75" → ("under", 2.75)
    side ∈ {"over", "under"}，line 为总进球盘口线。
    """
    parts = value_str.strip().split()
    if len(parts) != 2:
        return None
    side = parts[0].lower()
    num = _to_float(parts[1])
    if num is None or side not in ("over", "under"):
        return None
    return side, num


def extract_over_under(values: list[dict]) -> dict[float, dict]:
    """
    将一家庄的大小球 values 列表，按盘口线聚合大/小球水位。
    返回 {line: {"over_water":x, "under_water":y}}

    与亚盘同构：同一盘口线（如 2.5）API 给 "Over 2.5" 与 "Under 2.5" 两条，
    分别是大球/小球两侧水位。只保留两侧都齐的盘口线。
    """
    by_line: dict[float, dict] = {}
    for v in values:
        parsed = parse_ou_value(v.get("value", ""))
        if not parsed:
            continue
        side, line = parsed
        odd = _to_float(v.get("odd"))
        if odd is None:
            continue
        rec = by_line.setdefault(line, {})
        if side == "over":
            rec["over_water"] = odd
        else:
            rec["under_water"] = odd
    return {ln: r for ln, r in by_line.items()
            if "over_water" in r and "under_water" in r}


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
    # 大小球按盘口线聚合水位
    ou_all: dict[float, dict[str, list]] = {}

    parsed_per_bm: dict[int, dict] = {}  # 缓存每家解析结果，避免二次解析

    for bm in bookmakers:
        bid = bm.get("id")
        if bid not in pool_ids:
            continue
        h2h = None
        ah = None
        ou = None
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
            elif bet.get("id") == config.BET_OVER_UNDER:
                ou = extract_over_under(bet.get("values", []))
                for ln, rec in ou.items():
                    slot = ou_all.setdefault(ln, {"over": [], "under": []})
                    slot["over"].append(rec["over_water"])
                    slot["under"].append(rec["under_water"])
        parsed_per_bm[bid] = {"h2h": h2h, "ah": ah, "ou": ou}

    avg_h = {k: _avg(v) for k, v in h2h_all.items()}
    avg_ah = {hc: {"home": _avg(s["home"]), "away": _avg(s["away"])}
              for hc, s in ah_all.items()}
    avg_ou = {ln: {"over": _avg(s["over"]), "under": _avg(s["under"])}
              for ln, s in ou_all.items()}

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

        # 大小球行（每条盘口线一行）
        # 复用通用列：handicap=盘口线(如 2.5)，home_water=大球水位，away_water=小球水位，
        # kelly_h_water=大球凯利，kelly_a_water=小球凯利；market='ou' 区分语义。
        ou = pdata["ou"]
        if ou:
            for ln, rec in ou.items():
                mavg = avg_ou.get(ln, {})
                rows.append({
                    "fixture_id": fixture_id, "snapshot_utc": snapshot_utc,
                    "node_label": label, "bookmaker_id": bid, "bookmaker": bname,
                    "market": "ou",
                    "home_odds": None, "draw_odds": None, "away_odds": None,
                    "kelly_home": None, "kelly_draw": None, "kelly_away": None,
                    "handicap": ln,
                    "home_water": rec["over_water"],
                    "away_water": rec["under_water"],
                    "kelly_h_water": _kelly(rec["over_water"], mavg.get("over")),
                    "kelly_a_water": _kelly(rec["under_water"], mavg.get("under")),
                })

    return rows


# ─── 走地(滚球)解析：单场 live odds → 行列表 ───────────────────────────────
def _live_main_line(values: list[dict], a_side: str, b_side: str) -> dict | None:
    """从 live 亚盘/大小球 values 里取 main:true 的主盘口线两侧。
    a_side/b_side 为两侧 value 文本(小写)，如 ('home','away') 或 ('over','under')。
    返回 {"handicap":线, "a_water":x, "b_water":y, "suspended":bool}；无 main 则 None。
    """
    rec: dict = {}
    for v in values:
        if not v.get("main"):
            continue
        side = str(v.get("value", "")).lower()
        odd = _to_float(v.get("odd"))
        if odd is None:
            continue
        hc = _to_float(v.get("handicap"))
        if side == a_side:
            rec["a_water"] = odd
            rec["handicap"] = hc
            rec["suspended"] = bool(v.get("suspended"))
        elif side == b_side:
            rec["b_water"] = odd
            rec.setdefault("handicap", hc)
            rec.setdefault("suspended", bool(v.get("suspended")))
    if "a_water" in rec and "b_water" in rec:
        return rec
    return None


def parse_live_response(entry: dict, snapshot_utc: str,
                        pool_ids: set[int] | None = None) -> list[dict]:
    """
    解析 /odds/live 的单场 entry → live_odds_history 行列表。
    只保留 main:true 的主盘口线(走地只看主盘，多线全存会爆量)。

    ⚠️ live 结构与盘前 /odds 完全不同(已用真实数据验证)：
      - entry['odds'] 是【盘口类型列表】(非庄家列表)：每元素 {id, name, values}，
        id 即 bet id。走地数据是聚合盘，【不分庄家】，无 bookmaker 层。
      - 盘口线在独立 'handicap' 字段，value 仅 Over/Under/Home/Away
      - 亚盘(33)/大小球(36) 每盘多条线，带 main:true 标主盘口
      - 欧赔(59 Fulltime Result) 无 main、无 handicap，三条 value 直取
      - 每条 value 带 suspended(进球/VAR 时临时封盘)
    走地无庄家维度，故 bookmaker_id 统一记为 0、bookmaker 记 'LIVE'。
    pool_ids 参数保留以兼容调用签名，走地不使用。
    """
    fixture_id = entry.get("fixture", {}).get("id")
    if not fixture_id:
        return []
    status = entry.get("fixture", {}).get("status", {})
    elapsed = status.get("elapsed")
    teams = entry.get("teams", {})
    home_goals = teams.get("home", {}).get("goals")
    away_goals = teams.get("away", {}).get("goals")

    base = {
        "fixture_id": fixture_id, "snapshot_utc": snapshot_utc,
        "elapsed": elapsed, "home_goals": home_goals, "away_goals": away_goals,
        "bookmaker_id": 0, "bookmaker": "LIVE",
    }
    rows = []
    for bet in entry.get("odds", []):
        betid = bet.get("id")
        values = bet.get("values", [])
        if betid == config.BET_LIVE_1X2:
            # 欧赔：无 main、无 handicap，三条 value 直取
            vals = {str(v.get("value", "")).lower(): v for v in values}
            h = _to_float(vals.get("home", {}).get("odd"))
            d = _to_float(vals.get("draw", {}).get("odd"))
            a = _to_float(vals.get("away", {}).get("odd"))
            if h and a:
                susp = bool(vals.get("home", {}).get("suspended"))
                rows.append({**base, "market": "h2h", "handicap": None,
                             "home_water": h, "away_water": a, "draw_odds": d,
                             "suspended": 1 if susp else 0})
        elif betid == config.BET_LIVE_ASIAN_HANDICAP:
            main = _live_main_line(values, "home", "away")
            if main:
                rows.append({**base, "market": "ah",
                             "handicap": main["handicap"],
                             "home_water": main["a_water"],
                             "away_water": main["b_water"], "draw_odds": None,
                             "suspended": 1 if main["suspended"] else 0})
        elif betid == config.BET_LIVE_OVER_UNDER:
            main = _live_main_line(values, "over", "under")
            if main:
                rows.append({**base, "market": "ou",
                             "handicap": main["handicap"],
                             "home_water": main["a_water"],
                             "away_water": main["b_water"], "draw_odds": None,
                             "suspended": 1 if main["suspended"] else 0})
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
            "home_team_id": teams.get("home", {}).get("id"),
            "away_team_id": teams.get("away", {}).get("id"),
            "commence_utc": fx.get("date", ""),
            "status": fx.get("status", {}).get("short", ""),
        })
    return rows
