"""
基本面采集 —— 拉两队近况/交锋/赛程/积分榜/赛季统计/伤停/首发，拼成文本供 /analyze 使用

对应 CLAUDE.md SOP 步骤 1 第 6~8 条：
  1b 球队近况  /fixtures?team=&last=N+8 → 本地过滤「开球前已完场」再取前 N
  1c 历史交锋  /fixtures/headtohead?h2h=a-b&last=N+8 → 同上
  1d 未来赛程  /fixtures?team=&next=N → 只保留开球之后的比赛
  1d 积分榜    /standings?league=&season=（按球队 id 匹配；含主/客场分项、form、区位）
  1d 赛季统计  /teams/statistics?team=&league=&season=（两队各 1 次）
  1d 伤停      /injuries?fixture=
  1d 首发      /fixtures/lineups?fixture=（仅距开球 ≤ LINEUPS_WINDOW_MIN 分钟时请求）

全部以本场开球时间（fixtures.commence_utc）为截止：开球后再调用（/review、赛后重跑、
回测）也不会把本场及之后的比赛混进「近况」「交锋」（前视泄漏，F1）。

文本最前面是代码算好的【事实表】（胜平负、进失球、主客场分拆、form、休息天数、
未来 14 天场次、交锋场均进球、伤停人数、积分差），LLM 直接引用，不再自己数（F6）。
紧随标题的【基本面数据块】行是结构化标志，analyzer.fund_has 用它决定条件加载（F5）。

⚠️ API-Football 无澳客网的「99家平均终指/365终指」，故 SOP 的「终指质量加权」
   这一条在自动流程里跳过（CLAUDE.md 已加护栏：无终指字段则不执行、不编造）。
"""

import logging
import re
from datetime import datetime, timedelta, timezone

from . import config, api_client

log = logging.getLogger("odds_bot.fundamentals")


# 国家队赛事：基本面须切到 reference_national_team.md 口径（先分赛制/近况分层/实力锚等）。
#
# ⚠️ 以下 ID 均经 API-Football /leagues?type=cup 实调核对，非推测值。
#    新增前请照同样方式实调，不要凭名称猜 ID——名称与主体归属经常不一致。
_NATIONAL_LEAGUE_IDS = {
    1,    # World Cup
    4,    # Euro Championship
    5,    # UEFA Nations League
    6,    # Africa Cup of Nations
    7,    # Asian Cup
    9,    # Copa America
    10,   # Friendlies（注意：667 = Friendlies Clubs 是俱乐部，不在此列）
    19,   # African Nations Championship (CHAN)：国家队赛事，但只能派本土联赛
          # 注册球员，阵容含金量远低于 AFCON——实力锚需相应下调
    21,   # Confederations Cup
    22,   # CONCACAF Gold Cup
    23,   # EAFF E-1 Football Championship
    24,   # ASEAN Championship
    25,   # Gulf Cup of Nations
    28,   # SAFF Championship
    29, 30, 31, 32, 33, 34, 37,   # World Cup - Qualification 各洲 + 洲际附加赛
    35,   # Asian Cup - Qualification
    36,   # Africa Cup of Nations - Qualification
    536,  # CONCACAF Nations League
    808,  # CONCACAF Nations League - Qualification
    858,  # CONCACAF Gold Cup - Qualification
    860,  # Arab Cup
    913,  # CONMEBOL - UEFA Finalissima
    960,  # Euro Championship - Qualification
    1008, # CAFA Nations Cup
    1163, # African Nations Championship - Qualification（同 19，CHAN 预选）
}

# 已实调确认为【俱乐部】赛事、名称却含国家队特征词的陷阱 ID（命中即直接否决）：
#   3=UEFA Europa League、848=Europa Conference League、15=FIFA Club World Cup、
#   667=Friendlies Clubs、26=International Champions Cup、12=CAF Champions League、
#   13=CONMEBOL Libertadores、1043=African Football League、
#   1168=FIFA Intercontinental Cup
_CLUB_LEAGUE_IDS = {3, 12, 13, 15, 26, 667, 848, 1043, 1168}

# 名称兜底：仅在 league_id 既不在国家队白名单、也不在俱乐部黑名单时使用。
# 实测（真实 API 联赛名 11 个俱乐部样本）：不加排除词时 7/11 误判。
_CLUB_EXCLUDE_KEYWORDS = (
    "club", "clubs", "俱乐部", "champions league", "europa", "conference",
    "libertadores", "sudamericana", "recopa", "super cup", "leagues cup",
    "youth", "u17", "u19", "u20", "u21", "u23", "青年",
    "women", "femenina", "女足",      # 女足赛事不套用本读法
    "euroleague", "de clubes", "intercontinental cup",
)
_NATIONAL_NAME_KEYWORDS = (
    "world cup", "世界杯", "nations league", "欧国联",
    "euro championship", "欧洲杯", "copa america", "美洲杯",
    "asian cup", "亚洲杯", "africa cup", "非洲杯", "afcon", "gold cup",
    "qualification", "qualifier", "预选赛", "世预赛",
    "friendlies", "友谊赛",
)


def _is_national_team_event(league_id: int | None, league_name: str) -> bool:
    """判断是否国家队赛事（世界杯/洲际杯/欧国联/世预赛/友谊赛）。

    判据优先级：俱乐部黑名单 → 国家队白名单 → 名称兜底（先排除俱乐部特征词）。

    ⚠️ 名称兜底不可用裸子串（实测误判）：
      'euro' → 命中 UEFA Europa League / Euroleague
      'world cup' → 命中 FIFA Club World Cup
      'friendl' → 命中 Friendlies Clubs
      'international' → 命中 International Champions Cup
    故关键词已收紧为 'euro championship' / 'friendlies'，并移除 'international'。
    女足赛事不套用本读法（排除词含 women/femenina/女足）。
    """
    if league_id is not None:
        if league_id in _CLUB_LEAGUE_IDS:
            return False
        if league_id in _NATIONAL_LEAGUE_IDS:
            return True
    name = (league_name or "").lower()
    if not name:
        return False
    if any(kw in name for kw in _CLUB_EXCLUDE_KEYWORDS):
        return False
    return any(kw in name for kw in _NATIONAL_NAME_KEYWORDS)


_NATIONAL_TEAM_HINT = (
    "⚠️ 本场为【国家队赛事】：基本面须切到 reference_national_team.md 口径，"
    "覆盖 CLAUDE.md 步骤 1 第 5~7 条的联赛读法——\n"
    "  ① 先分赛制：赛会制决赛圈=中立场（除东道主），禁用主场加成、废除主客场分拆；"
    "主客场制（世预赛/欧国联）=有地利但弱于俱乐部主场；友谊赛=地利近乎无意义。不可一刀切中立场；\n"
    "  ② 近 N 场按赛事性质分层：正式大赛/世预赛关键战=高权重，世预赛虐菜=进球注水，"
    "友谊赛=近乎无参考（连胜≠状态好），禁止等权数胜负；\n"
    "  ③ 无终指：改用 FIFA 排名/洲际强弱/世预赛含金量锚实力，与盘口方向并列记录（一致/背离），不据此推方向；\n"
    "  ④ 赛程改判集训磨合/休息天数/加时消耗/核心停赛/旅行气候，废除双线/分心/轮换；\n"
    "  ⑤ 小组赛须算末轮出线数学（已出线留力/默契球→利小球+冷平；生死战→强攻或崩盘）；\n"
    "  ⑥ 强弱深盘+高平赔时，冷平/受让方直接取胜是结构性尾部事件，文字结论不得因实力悬殊归零（平局概率以 p_市场 为准）；\n"
    "  ⑦ H2H 跨届换代/友谊赛≠大赛，仅作极弱背景参考，以本届状态+实力锚为准。"
)

# 赛事情境提示（所有赛事通用，注入后引导 LLM 先判阶段/赛制/赛程情境再进 SOP）。
_CONTEXT_HINT = (
    "ℹ️ 研判前先判【赛事情境】（依据下方实拉数据，见 reference_competition_context.md）——\n"
    "  · 赛事阶段：读积分榜结构（多张小表+最佳第三名=杯赛小组赛；无积分榜+杯赛=淘汰赛；"
    "单张长表=常规联赛），淘汰赛再分单场决胜 vs 两回合；\n"
    "  · 赛制：读积分榜已赛场次判单/双循环，样本不足时以近况为准、排名仅作参考；\n"
    "  · 俱乐部赛程情境：恢复天数与未来 14 天场次直接引用【事实表】（≤3 天记为体能风险，"
    "两队恢复不对称如实记录）；读未来赛程判下场对手压力/多线作战/是否留力放弃本场"
    "（无关键意义+下场紧邻高优先级赛事+多线 ≥2 项→记录留力风险，作为基本面事实）。\n"
    "  · 数据缺失（无积分榜/无赛程日期）则标注「情境无法判定」，不编造。"
)


_FINISHED = {"FT", "AET", "PEN"}
_FETCH_PAD = 8                 # last=N+8：多取几场，本地过滤掉开球后/未完场的
LINEUPS_WINDOW_MIN = 75        # 距开球 ≤ 该分钟数才请求首发（API 约开球前 1 小时才有）
UPCOMING_WINDOW_DAYS = 14
_BLOCK_KEYS = (("has_form", "近况"), ("has_h2h", "交锋"), ("has_schedule", "赛程"),
               ("has_standings", "积分榜"), ("has_injuries", "伤停"))
_FLAG_PREFIX = "【基本面数据块】"
_FLAG_RE = re.compile(rf"^{_FLAG_PREFIX}(.*)$", re.M)
_TAIL_NOTE = "⚠️ 缺失的数据不要编造；统计数字以【事实表】为准，逐场列表仅供核对。"


# ─── 时间与比赛过滤 ───────────────────────────────────────────────────────────

def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _fx_dt(m: dict) -> datetime | None:
    return _parse_dt((m.get("fixture") or {}).get("date"))


def _status(m: dict) -> str:
    return ((m.get("fixture") or {}).get("status") or {}).get("short") or ""


def _resp(data) -> list:
    resp = (data or {}).get("response") if isinstance(data, dict) else None
    return resp if isinstance(resp, list) else []


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def completed_before(matches: list, before: datetime | None, n: int) -> list:
    """开球前已完场（FT/AET/PEN 且有比分）的比赛，按时间倒序取前 n（F1/F4）。"""
    out = []
    for m in matches or []:
        dt = _fx_dt(m)
        if before is not None and (dt is None or dt >= before):
            continue
        if _status(m) not in _FINISHED:
            continue
        goals = m.get("goals") or {}
        if goals.get("home") is None or goals.get("away") is None:
            continue
        out.append(m)
    out.sort(key=lambda m: _fx_dt(m) or _EPOCH, reverse=True)
    return out[:n]


def scheduled_after(matches: list, after: datetime | None, n: int) -> list:
    """开球之后的比赛，按时间正序取前 n。"""
    out = [m for m in matches or []
           if after is None or ((_fx_dt(m) or _EPOCH) > after)]
    out.sort(key=lambda m: _fx_dt(m) or _EPOCH)
    return out[:n]


# ─── 格式化 ──────────────────────────────────────────────────────────────────

_RESULT_LABELS = {"W": "胜", "D": "平", "L": "负"}


def _fmt_match(m: dict, focus_team_id: int | None = None) -> str:
    """格式化一场比赛：日期 [赛事] 主 X-Y 客（可标注 focus 队胜平负）。"""
    fx = m.get("fixture", {})
    date = (fx.get("date") or "")[:10]
    lg = m.get("league", {}).get("name", "")
    teams = m.get("teams", {})
    goals = m.get("goals", {})
    h = teams.get("home", {})
    a = teams.get("away", {})
    hg, ag = goals.get("home"), goals.get("away")
    score = f"{hg}-{ag}" if hg is not None and ag is not None else "未赛"
    line = f"{date} [{lg}] {h.get('name','')} {score} {a.get('name','')}"
    if focus_team_id and hg is not None and ag is not None:
        res = _RESULT_LABELS.get(_result_for(m, focus_team_id))
        if res:
            line += f"  ({res})"
    return line


def _result_for(m: dict, team_id: int) -> str:
    """该队在这场的结果 W/D/L；不是该队的比赛或无比分返回 ""。"""
    teams = m.get("teams") or {}
    goals = m.get("goals") or {}
    hg, ag = goals.get("home"), goals.get("away")
    if hg is None or ag is None:
        return ""
    if (teams.get("home") or {}).get("id") == team_id:
        mine, theirs = hg, ag
    elif (teams.get("away") or {}).get("id") == team_id:
        mine, theirs = ag, hg
    else:
        return ""
    return "W" if mine > theirs else ("D" if mine == theirs else "L")


def team_record(matches: list, team_id: int, venue: str | None = None) -> dict:
    """战绩汇总。venue=None 全部；"home" 只算该队主场；"away" 只算客场。"""
    rec = {"played": 0, "w": 0, "d": 0, "l": 0, "gf": 0, "ga": 0}
    for m in matches:
        teams = m.get("teams") or {}
        goals = m.get("goals") or {}
        hg, ag = goals.get("home"), goals.get("away")
        if hg is None or ag is None:
            continue
        is_home = (teams.get("home") or {}).get("id") == team_id
        is_away = (teams.get("away") or {}).get("id") == team_id
        if not (is_home or is_away):
            continue
        if venue == "home" and not is_home or venue == "away" and not is_away:
            continue
        gf, ga = (hg, ag) if is_home else (ag, hg)
        rec["played"] += 1
        rec["gf"] += gf
        rec["ga"] += ga
        rec["w" if gf > ga else ("d" if gf == ga else "l")] += 1
    return rec


def _rec_text(rec: dict) -> str:
    return (f"{rec['played']}场 {rec['w']}胜{rec['d']}平{rec['l']}负 "
            f"进{rec['gf']}失{rec['ga']}")


def form_string(matches: list, team_id: int, n: int = 5) -> str:
    """近 n 场 form 串（左旧右新），如 WDWWL。"""
    recent = matches[:n]
    return "".join(_result_for(m, team_id) or "?" for m in reversed(recent))


# ─── 各块拉取 ────────────────────────────────────────────────────────────────

def _fetch_recent(team_id: int, before: datetime | None) -> list:
    data = api_client.api_get(
        "/fixtures", {"team": team_id, "last": config.FUND_RECENT_N + _FETCH_PAD})
    return completed_before(_resp(data), before, config.FUND_RECENT_N)


def _fetch_upcoming(team_id: int, after: datetime | None) -> list:
    data = api_client.api_get(
        "/fixtures", {"team": team_id, "next": config.FUND_UPCOMING_N})
    return scheduled_after(_resp(data), after, config.FUND_UPCOMING_N)


def _fetch_h2h(home_id: int, away_id: int, before: datetime | None) -> list:
    data = api_client.api_get(
        "/fixtures/headtohead",
        {"h2h": f"{home_id}-{away_id}", "last": config.FUND_H2H_N + _FETCH_PAD})
    return completed_before(_resp(data), before, config.FUND_H2H_N)


def _list_block(title: str, matches: list, focus_id: int | None, empty: str) -> str:
    if not matches:
        return empty
    lines = [title.format(n=len(matches))]
    lines += [f"  {_fmt_match(m, focus_id)}" for m in matches]
    return "\n".join(lines)


def _standings_tables(league_id: int, season: int) -> list | None:
    data = api_client.api_get("/standings", {"league": league_id, "season": season})
    resp = _resp(data)
    if not resp:
        return None
    try:
        tables = resp[0]["league"]["standings"]
    except (KeyError, IndexError, TypeError):
        raise ValueError("积分榜结构异常")
    return tables if isinstance(tables, list) else None


def _team_id(row: dict):
    return (row.get("team") or {}).get("id")


def _row_line(row: dict, focus_ids: set) -> str:
    team = row.get("team") or {}
    name = config.team_label(team.get("id"), team.get("name", ""))
    all_ = row.get("all") or {}
    home = row.get("home") or {}
    away = row.get("away") or {}

    def split(part: dict) -> str:
        goals = part.get("goals") or {}
        if not part:
            return "—"
        return (f"{part.get('win')}-{part.get('draw')}-{part.get('lose')}"
                f" 进{goals.get('for')}失{goals.get('against')}")

    mark = " ◀" if team.get("id") in focus_ids else ""
    extra = ""
    if team.get("id") in focus_ids:
        extra = (f"｜主场 {split(home)}｜客场 {split(away)}"
                 f"｜净胜 {row.get('goalsDiff')}｜form {row.get('form') or '—'}"
                 + (f"｜{row['description']}" if row.get("description") else ""))
    return (f"  {row.get('rank')}. {name} 积分{row.get('points')} "
            f"{all_.get('win')}-{all_.get('draw')}-{all_.get('lose')}{extra}{mark}")


def _played(row: dict) -> int:
    all_ = row.get("all") or {}
    p = all_.get("played")
    if p is not None:
        return p
    return (all_.get("win") or 0) + (all_.get("draw") or 0) + (all_.get("lose") or 0)


def _standings_text(tables: list, home_id: int, away_id: int) -> tuple[str, dict]:
    """积分榜文本 + 两队行（按球队 id 匹配，F2）。"""
    focus = {home_id, away_id}
    rows = {}
    for table in tables:
        for row in table:
            if _team_id(row) in focus and _team_id(row) not in rows:
                rows[_team_id(row)] = row

    def has(table, ids) -> bool:
        return any(_team_id(r) in ids for r in table)

    # 分组赛制（世界杯/杯赛小组赛）：只列两队所在小组；48 队世界杯另有「最佳第三名」
    # 聚合表（>4 队），仅当已赛 ≥1 轮且两队有人排小组第 3 时附上。
    if len(tables) > 1:
        group_tables = [t for t in tables if len(t) <= 4]
        both = [t for t in group_tables if has(t, {home_id}) and has(t, {away_id})]
        if not both:
            for tid in (home_id, away_id):
                for t in group_tables:
                    if has(t, {tid}) and t not in both:
                        both.append(t)
                        break
        if both:
            lines = ["【积分榜（两队所在小组完整排名，仅组内可比）】"]
            for table in both:
                grp = (table[0].get("group") if table else None) or "本组"
                lines.append(f"〔{grp}〕")
                lines += [_row_line(r, focus) for r in table]
            rnd = max((_played(r) for t in both for r in t), default=0)
            focus_is_third = any(r.get("rank") == 3 and _team_id(r) in focus
                                 for t in both for r in t)
            if rnd >= 1 and focus_is_third:
                third = next((t for t in tables if len(t) > 4 and has(t, focus)), None)
                if third:
                    lines.append("〔最佳第三名排名（前8晋级淘汰赛）〕")
                    lines += [_row_line(r, focus) for r in third]
            return "\n".join(lines), rows

    lines = ["【积分榜（仅列两队及前4）】"]
    for table in tables:
        for row in table:
            rank = row.get("rank")
            if (rank and rank <= 4) or _team_id(row) in focus:
                lines.append(_row_line(row, focus))
    return "\n".join(lines), rows


def _fetch_injuries(fixture_id: int) -> list:
    return _resp(api_client.api_get("/injuries", {"fixture": fixture_id}))


_INJURY_TYPES = {"missing fixture": "缺阵", "questionable": "存疑"}


def _injuries_text(items: list, home_id: int, away_id: int,
                   home: str, away: str) -> tuple[str, dict]:
    by_team: dict = {home_id: [], away_id: []}
    for item in items:
        tid = (item.get("team") or {}).get("id")
        if tid in by_team:
            by_team[tid].append(item.get("player") or {})
    lines = ["【伤停】（/injuries，开球前名单）"]
    for tid, label in ((home_id, home), (away_id, away)):
        players = by_team[tid]
        if not players:
            lines.append(f"  {label}：无记录")
            continue
        parts = []
        for pl in players:
            kind = _INJURY_TYPES.get(str(pl.get("type") or "").lower(), pl.get("type") or "")
            reason = pl.get("reason") or ""
            detail = "·".join(x for x in (kind, reason) if x)
            parts.append(f"{pl.get('name', '?')}（{detail}）" if detail else str(pl.get("name", "?")))
        lines.append(f"  {label}（{len(players)} 人）：" + "、".join(parts))
    return "\n".join(lines), by_team


def _fetch_team_stats(team_id: int, league_id: int, season: int) -> dict | None:
    data = api_client.api_get(
        "/teams/statistics", {"team": team_id, "league": league_id, "season": season})
    resp = (data or {}).get("response") if isinstance(data, dict) else None
    return resp if isinstance(resp, dict) and resp else None


def _team_stats_text(stats: dict, label: str) -> str:
    fx = stats.get("fixtures") or {}
    goals = stats.get("goals") or {}

    def g(path: dict, side: str):
        return ((path.get("total") or {}).get(side)) if isinstance(path, dict) else None

    def side(s: str) -> str:
        played = (fx.get("played") or {}).get(s)
        return (f"{played}场 {(fx.get('wins') or {}).get(s)}胜{(fx.get('draws') or {}).get(s)}平"
                f"{(fx.get('loses') or {}).get(s)}负 进{g(goals.get('for') or {}, s)}"
                f"失{g(goals.get('against') or {}, s)}")

    cs = stats.get("clean_sheet") or {}
    fts = stats.get("failed_to_score") or {}
    form = stats.get("form") or "—"
    return (f"  {label}：form {form[-10:]}；主场 {side('home')}；客场 {side('away')}；"
            f"零封 {cs.get('total')}（主{cs.get('home')}/客{cs.get('away')}）；"
            f"未进球 {fts.get('total')}（主{fts.get('home')}/客{fts.get('away')}）")


def _fetch_lineups(fixture_id: int) -> list:
    return _resp(api_client.api_get("/fixtures/lineups", {"fixture": fixture_id}))


def _lineups_text(items: list, home_id: int, away_id: int,
                  home: str, away: str) -> str | None:
    labels = {home_id: home, away_id: away}
    lines = ["【首发】（/fixtures/lineups）"]
    for item in items:
        tid = (item.get("team") or {}).get("id")
        if tid not in labels:
            continue
        xi = [((p.get("player") or {}).get("name") or "?") for p in item.get("startXI") or []]
        lines.append(f"  {labels[tid]} 阵型 {item.get('formation') or '—'}：" + "、".join(xi))
    return "\n".join(lines) if len(lines) > 1 else None


# ─── 事实表 ──────────────────────────────────────────────────────────────────

def fact_table(*, home: str, away: str, home_id: int, away_id: int,
               kickoff: datetime | None,
               home_recent: list | None, away_recent: list | None,
               home_next: list | None, away_next: list | None,
               h2h: list | None, standings_rows: dict | None,
               injuries: dict | None, league_id: int | None = None) -> str:
    """代码计算的基本面事实表（F6）。各参数为 None 表示该块拉取失败。"""
    stamp = kickoff.strftime("%Y-%m-%d %H:%M UTC") if kickoff else "当前时间"
    lines = [f"【事实表】（代码计算，截至开球前 {stamp}；LLM 直接引用，不要自己数）"]

    def recent_line(label, matches, tid, venue, venue_label):
        if matches is None:
            return f"{label} 近况：拉取失败"
        if not matches:
            return f"{label} 近况：无开球前已完场比赛"
        all_rec = team_record(matches, tid)
        v_rec = team_record(matches, tid, venue)
        return (f"{label} 近{len(matches)}场 {all_rec['w']}胜{all_rec['d']}平{all_rec['l']}负 "
                f"进{all_rec['gf']}失{all_rec['ga']}；"
                f"{venue_label} {_rec_text(v_rec)}；近5场 form {form_string(matches, tid)}（左旧右新）")

    lines.append(recent_line(f"主队 {home}", home_recent, home_id, "home", "主场"))
    lines.append(recent_line(f"客队 {away}", away_recent, away_id, "away", "客场"))

    def rest(matches):
        if not matches or kickoff is None:
            return "—"
        last = _fx_dt(matches[0])
        return f"{(kickoff - last).days} 天" if last else "—"

    def next_count(matches):
        if matches is None:
            return "拉取失败"
        if kickoff is None:
            return f"{len(matches)} 场"
        window = [m for m in matches if (_fx_dt(m) or _EPOCH) <= kickoff + timedelta(days=UPCOMING_WINDOW_DAYS)]
        other = sum(1 for m in window
                    if league_id is not None and (m.get("league") or {}).get("id") != league_id)
        return f"{len(window)} 场" + (f"（其中其他赛事 {other} 场）" if other else "")

    lines.append(f"休息天数：主队 {rest(home_recent)} / 客队 {rest(away_recent)}；"
                 f"未来 {UPCOMING_WINDOW_DAYS} 天赛程：主队 {next_count(home_next)} / 客队 {next_count(away_next)}")
    if h2h is None:
        lines.append("交锋：拉取失败")
    elif not h2h:
        lines.append("交锋：无开球前已完场交锋")
    else:
        rec = team_record(h2h, home_id)
        avg = (rec["gf"] + rec["ga"]) / rec["played"] if rec["played"] else 0.0
        draws = rec["d"] / rec["played"] if rec["played"] else 0.0
        lines.append(f"交锋 近{rec['played']}场（主队视角）{rec['w']}胜{rec['d']}平{rec['l']}负，"
                     f"场均总进球 {avg:.1f}，平局率 {draws:.0%}")
    if injuries is None:
        lines.append("伤停：拉取失败")
    else:
        def inj(tid):
            players = injuries.get(tid) or []
            names = "、".join(str(p.get("name", "?")) for p in players[:3])
            return f"{len(players)} 人" + (f"（含 {names}）" if names else "")
        lines.append(f"伤停：主队 {inj(home_id)} / 客队 {inj(away_id)}")
    if standings_rows is None:
        lines.append("积分榜：无数据")
    else:
        hr, ar = standings_rows.get(home_id), standings_rows.get(away_id)
        if hr and ar:
            diff = (hr.get("points") or 0) - (ar.get("points") or 0)
            lines.append(f"积分榜：主队 第{hr.get('rank')} 积{hr.get('points')} / "
                         f"客队 第{ar.get('rank')} 积{ar.get('points')}，差 {diff:+d} 分")
        else:
            lines.append("积分榜：两队不在同一张积分榜（或未找到其行）")
    return "\n".join(lines)


# ─── 结构化标志（F5） ─────────────────────────────────────────────────────────

def _flag_line(flags: dict) -> str:
    parts = [f"{label}={'有' if flags.get(key) else '无'}" for key, label in _BLOCK_KEYS]
    return _FLAG_PREFIX + " ".join(parts)


def parse_flags(text: str) -> dict | None:
    """读 build_fundamentals 写入的【基本面数据块】标志行；无此行（旧文本/手动粘贴）返回 None。"""
    match = _FLAG_RE.search(text or "")
    if not match:
        return None
    pairs = dict(re.findall(r"(\S+?)=(有|无)", match.group(1)))
    flags = {key: pairs.get(label) == "有" for key, label in _BLOCK_KEYS}
    flags["missing"] = [label for key, label in _BLOCK_KEYS if not flags[key]]
    return flags


def strip_fixed_hints(text: str) -> str:
    """去掉固定提示语，供关键词判据使用（提示语里恒含「交锋」「近 10 场」等词）。"""
    out = text or ""
    for hint in (_NATIONAL_TEAM_HINT, _CONTEXT_HINT, _TAIL_NOTE):
        out = out.replace(hint, "")
    return out


# ─── 组装 ────────────────────────────────────────────────────────────────────

def _safe(label: str, fn, *args):
    """单块拉取：异常只影响本块（F3）。返回 (结果, 错误文本或 None)。"""
    try:
        return fn(*args), None
    except Exception as e:                 # 基本面失败不应阻断精算
        log.warning("基本面【%s】拉取失败: %s", label, e)
        return None, f"【{label}】拉取失败（{type(e).__name__}）"


def build_fundamentals_ex(conn, fixture_id: int,
                          now: datetime | None = None) -> tuple[str, dict]:
    """组装某场比赛的两队基本面文本 + 结构化标志 {"has_form": bool, ...}。"""
    from . import db
    empty = {key: False for key, _ in _BLOCK_KEYS}
    meta = db.get_fixture_meta(conn, fixture_id)
    if not meta:
        return "（无此比赛的基本面）", {**empty, "missing": [l for _, l in _BLOCK_KEYS]}
    (_fid, league_id, league_name, season, home, away,
     home_id, away_id, commence) = meta
    home_raw, away_raw = home, away
    # 队名统一中文（中超/足协杯按 team_id 映射；非中国队回退英文），与其它流程一致
    home = config.team_label(home_id, home_raw)
    away = config.team_label(away_id, away_raw)
    now = now or datetime.now(timezone.utc)
    kickoff = _parse_dt(commence)
    cutoff = kickoff or now

    parts = [f"=== 基本面：{home} vs {away}（{league_name}）==="]
    if not home_id or not away_id:
        flags = {**empty, "missing": [l for _, l in _BLOCK_KEYS]}
        parts.insert(1, _flag_line(flags))
        if _is_national_team_event(league_id, league_name):
            parts.append(_NATIONAL_TEAM_HINT)
        parts.append("⚠️ 该比赛缺少球队 ID（旧数据未刷新），基本面暂不可用。"
                     "等任务A刷新后重试。")
        return "\n".join(parts), flags

    errors: list[str] = []

    def grab(label, fn, *args):
        value, err = _safe(label, fn, *args)
        if err:
            errors.append(err)
        return value

    home_recent = grab(f"{home} 近况", _fetch_recent, home_id, cutoff)
    away_recent = grab(f"{away} 近况", _fetch_recent, away_id, cutoff)
    h2h = grab("历史交锋", _fetch_h2h, home_id, away_id, cutoff)
    home_next = grab(f"{home} 未来赛程", _fetch_upcoming, home_id, cutoff)
    away_next = grab(f"{away} 未来赛程", _fetch_upcoming, away_id, cutoff)
    tables = grab("积分榜", _standings_tables, league_id, season)
    injuries_raw = grab("伤停", _fetch_injuries, fixture_id)
    home_stats = grab(f"{home} 赛季统计", _fetch_team_stats, home_id, league_id, season)
    away_stats = grab(f"{away} 赛季统计", _fetch_team_stats, away_id, league_id, season)
    lineups_raw = None
    if kickoff is not None and (kickoff - now).total_seconds() <= LINEUPS_WINDOW_MIN * 60:
        lineups_raw = grab("首发", _fetch_lineups, fixture_id)

    standings_block, standings_rows = None, None
    if tables:
        standings_block, standings_rows = _standings_text(tables, home_id, away_id)
    injuries_block, injuries_by_team = None, None
    if injuries_raw is not None:
        injuries_block, injuries_by_team = _injuries_text(
            injuries_raw, home_id, away_id, home, away)

    flags = {
        "has_form": bool(home_recent) or bool(away_recent),
        "has_h2h": bool(h2h),
        "has_schedule": home_next is not None or away_next is not None,
        "has_standings": bool(standings_rows),
        "has_injuries": injuries_raw is not None,
    }
    flags["missing"] = [label for key, label in _BLOCK_KEYS if not flags[key]]
    parts.append(_flag_line(flags))
    if flags["missing"]:
        parts.append("基本面缺块：" + "、".join(flags["missing"])
                     + "（数据质量按每块 3 分扣，上限 15）")
    if _is_national_team_event(league_id, league_name):
        parts.append(_NATIONAL_TEAM_HINT)

    parts.append(fact_table(
        home=home, away=away, home_id=home_id, away_id=away_id, kickoff=kickoff,
        home_recent=home_recent, away_recent=away_recent,
        home_next=home_next, away_next=away_next, h2h=h2h,
        standings_rows=standings_rows, injuries=injuries_by_team,
        league_id=league_id))

    if home_recent is not None:
        parts.append(_list_block(f"【{home} 近 {{n}} 场】（开球前已完场）", home_recent,
                                 home_id, f"【{home} 近况】无开球前已完场比赛"))
    if away_recent is not None:
        parts.append(_list_block(f"【{away} 近 {{n}} 场】（开球前已完场）", away_recent,
                                 away_id, f"【{away} 近况】无开球前已完场比赛"))
    if h2h is not None:
        parts.append(_list_block("【历史交锋 近 {n} 场】（开球前已完场，括注为主队结果）", h2h,
                                 home_id, f"【{home} vs {away} 历史交锋】无数据"))
    if home_next is not None:
        parts.append(_list_block(f"【{home} 未来 {{n}} 场赛程】", home_next, home_id,
                                 f"【{home} 未来赛程】无数据"))
    if away_next is not None:
        parts.append(_list_block(f"【{away} 未来 {{n}} 场赛程】", away_next, away_id,
                                 f"【{away} 未来赛程】无数据"))
    if standings_block:
        parts.append(standings_block)
    elif tables is not None or "积分榜" not in " ".join(errors):
        parts.append("【积分榜】无数据（国家队赛事/杯赛通常无积分榜）")
    stats_lines = [
        _team_stats_text(stats, label)
        for stats, label in ((home_stats, home), (away_stats, away)) if stats]
    if stats_lines:
        parts.append(f"【赛季统计（{league_name} {season}）】\n" + "\n".join(stats_lines))
    if injuries_block:
        parts.append(injuries_block)
    if lineups_raw:
        lineup_block = _lineups_text(lineups_raw, home_id, away_id, home, away)
        if lineup_block:
            parts.append(lineup_block)
    parts.extend(errors)

    parts.append(_CONTEXT_HINT)
    parts.append(_TAIL_NOTE)
    return "\n".join(parts), flags


def build_fundamentals(conn, fixture_id: int) -> str:
    """组装某场比赛的两队基本面文本（6 处调用点沿用此签名）。"""
    return build_fundamentals_ex(conn, fixture_id)[0]
