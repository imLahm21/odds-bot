"""
基本面采集 —— 拉两队近况/交锋/积分榜，拼成文本供 /analyze 使用

对应 CLAUDE.md SOP 步骤：
  1b 球队近况  /fixtures?team=&last=N
  1c 历史交锋  /fixtures/headtohead?h2h=a-b&last=N
  1d 未来赛程  /fixtures?team=&next=N（赛程密度/双线/轮换风险）
  1d 积分榜    /standings?league=&season=

⚠️ API-Football 无澳客网的「99家平均终指/365终指」，故 SOP 的「终指质量加权」
   这一条在自动流程里跳过（CLAUDE.md 已加护栏：无终指字段则不执行、不编造），
   LLM 按战绩/比分/排名/交锋综合加权即可。
"""

import logging

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
    "  ③ 无终指：改用 FIFA 排名/洲际强弱/世预赛含金量锚实力，盘口与实力锚一致才可提置信度；\n"
    "  ④ 赛程改判集训磨合/休息天数/加时消耗/核心停赛/旅行气候，废除双线/分心/轮换；\n"
    "  ⑤ 小组赛须算末轮出线数学（已出线留力/默契球→利小球+冷平；生死战→强攻或崩盘）；\n"
    "  ⑥ 强弱深盘+高平赔时，冷平/受让方直接取胜须保留独立高权重，不得因实力悬殊归零；\n"
    "  ⑦ H2H 跨届换代/友谊赛≠大赛，仅作极弱背景参考，本届状态+实力锚权重 ≫ 历史交锋。"
)

# 赛事情境提示（所有赛事通用，注入后引导 LLM 先判阶段/赛制/赛程情境再进 SOP）。
_CONTEXT_HINT = (
    "ℹ️ 研判前先判【赛事情境】（依据下方实拉数据，见 reference_competition_context.md）——\n"
    "  · 赛事阶段：读积分榜结构（多张小表+最佳第三名=杯赛小组赛；无积分榜+杯赛=淘汰赛；"
    "单张长表=常规联赛），淘汰赛再分单场决胜 vs 两回合；\n"
    "  · 赛制：读积分榜已赛场次判单/双循环，样本不足则近况权重>排名；\n"
    "  · 俱乐部赛程情境：读两队近 10 场最近日期算「上场→本场恢复天数」（≤3 天下调体能，"
    "两队恢复不对称=爆冷信号）；读未来 5 场赛程判下场对手压力/多线作战/是否留力放弃本场"
    "（无关键意义+下场紧邻高优先级赛事+多线 ≥2 项→标注留力风险，下调战力、上调对手与小球/冷平权重）。\n"
    "  · 数据缺失（无积分榜/无赛程日期）则标注「情境无法判定」，不编造。"
)



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
    score = f"{hg}-{ag}" if hg is not None else "未赛"
    line = f"{date} [{lg}] {h.get('name','')} {score} {a.get('name','')}"
    # 标注 focus 队结果
    if focus_team_id and hg is not None:
        if h.get("id") == focus_team_id:
            res = "胜" if hg > ag else ("平" if hg == ag else "负")
        elif a.get("id") == focus_team_id:
            res = "胜" if ag > hg else ("平" if hg == ag else "负")
        else:
            res = ""
        if res:
            line += f"  ({res})"
    return line


def _recent(team_id: int, team_name: str) -> str:
    data = api_client.api_get("/fixtures",
                              {"team": team_id, "last": config.FUND_RECENT_N})
    matches = (data or {}).get("response", []) if data else []
    if not matches:
        return f"【{team_name} 近况】无数据"
    lines = [f"【{team_name} 近 {len(matches)} 场】"]
    lines += [f"  {_fmt_match(m, team_id)}" for m in matches]
    return "\n".join(lines)


def _upcoming(team_id: int, team_name: str) -> str:
    """该队未来 N 场赛程（判赛程密度/双线作战/临近强敌/轮换风险）。
    /fixtures?team=&next=N 返回未开赛比赛，_fmt_match 对其显示「未赛」。"""
    data = api_client.api_get("/fixtures",
                              {"team": team_id, "next": config.FUND_UPCOMING_N})
    matches = (data or {}).get("response", []) if data else []
    if not matches:
        return f"【{team_name} 未来赛程】无数据"
    lines = [f"【{team_name} 未来 {len(matches)} 场赛程】"]
    lines += [f"  {_fmt_match(m, team_id)}" for m in matches]
    return "\n".join(lines)


def _h2h(home_id: int, away_id: int, home_name: str, away_name: str) -> str:
    data = api_client.api_get(
        "/fixtures/headtohead",
        {"h2h": f"{home_id}-{away_id}", "last": config.FUND_H2H_N})
    matches = (data or {}).get("response", []) if data else []
    if not matches:
        return f"【{home_name} vs {away_name} 历史交锋】无数据"
    lines = [f"【历史交锋 近 {len(matches)} 场】"]
    lines += [f"  {_fmt_match(m, home_id)}" for m in matches]
    return "\n".join(lines)


def _standings(league_id: int, season: int,
               home_name: str, away_name: str) -> str:
    data = api_client.api_get("/standings",
                              {"league": league_id, "season": season})
    resp = (data or {}).get("response", []) if data else []
    if not resp:
        return "【积分榜】无数据（国家队赛事/杯赛通常无积分榜）"
    try:
        tables = resp[0]["league"]["standings"]
    except (KeyError, IndexError, TypeError):
        return "【积分榜】结构异常，跳过"

    focus = {home_name, away_name}

    def _row_line(row: dict) -> str:
        name = row.get("team", {}).get("name", "")
        all_ = row.get("all", {})
        mark = " ◀" if name in focus else ""
        return (f"  {row.get('rank')}. {name} 积分{row.get('points')} "
                f"{all_.get('win')}-{all_.get('draw')}-{all_.get('lose')}{mark}")

    def _has(t, names) -> bool:
        if isinstance(names, str):
            names = {names}
        return any(r.get("team", {}).get("name") in names for r in t)

    def _played(row: dict) -> int:
        all_ = row.get("all", {})
        p = all_.get("played")
        if p is not None:
            return p
        return ((all_.get("win") or 0) + (all_.get("draw") or 0)
                + (all_.get("lose") or 0))

    # 分组赛制（世界杯/杯赛小组赛）：standings 是多张表。
    #   · 真小组表恒为 4 队 → 只列两队所在那张（组内比积分才有意义）。
    #   · 48 队世界杯还会多返回一张「最佳第三名」聚合表（>4 队，各组第 3 横向排）。
    #     规则：各组前 2 + 8 个最好的小组第 3 共 32 队晋级。故这张表【有条件】才显示——
    #     仅当已打到第 2/3 轮（两队已赛≥1 场、出线形势明朗）且两队中有人正排小组第 3
    #     （出线生死线）时才附上并标注「前8晋级」；否则（第1轮/都在前二或垫底）不列，
    #     免得跨组数据干扰单场研判。
    if len(tables) > 1:
        group_tables = [t for t in tables if len(t) <= 4]   # 排除聚合表
        both = [t for t in group_tables
                if _has(t, home_name) and _has(t, away_name)]
        if not both:                       # 淘汰赛两队不同组：各列其首张组表
            for nm in (home_name, away_name):
                for t in group_tables:
                    if _has(t, nm) and t not in both:
                        both.append(t)
                        break
        if both:
            lines = ["【积分榜（两队所在小组完整排名，仅组内可比）】"]
            for table in both:
                grp = (table[0].get("group") if table else None) or "本组"
                lines.append(f"〔{grp}〕")
                lines += [_row_line(r) for r in table]
            # 轮次 = 组内各队已赛场次最大值（0=第1轮前,1=第2轮,2=第3轮）
            rnd = max((_played(r) for t in both for r in t), default=0)
            focus_is_third = any(
                r.get("rank") == 3 and r.get("team", {}).get("name") in focus
                for t in both for r in t)
            if rnd >= 1 and focus_is_third:
                third = next((t for t in tables
                              if len(t) > 4 and _has(t, focus)), None)
                if third:
                    lines.append("〔最佳第三名排名（前8晋级淘汰赛）〕")
                    lines += [_row_line(r) for r in third]
            return "\n".join(lines)
        # group_tables 找不到两队（数据异常）→ 落到下方单表逻辑

    # 单表联赛（非分组）：列两队 + 前4
    lines = ["【积分榜（仅列两队及前4）】"]
    for table in tables:
        for row in table:
            name = row.get("team", {}).get("name", "")
            rank = row.get("rank")
            if (rank and rank <= 4) or name in focus:
                lines.append(_row_line(row))
    return "\n".join(lines)


def build_fundamentals(conn, fixture_id: int) -> str:
    """组装某场比赛的两队基本面文本。需要 fixtures 表已存 team id。"""
    from . import db
    meta = db.get_fixture_meta(conn, fixture_id)
    if not meta:
        return "（无此比赛的基本面）"
    (_fid, league_id, league_name, season, home, away,
     home_id, away_id, _commence) = meta
    # 队名统一中文（中超/足协杯按 team_id 映射；非中国队回退英文），与其它流程一致
    home = config.team_label(home_id, home)
    away = config.team_label(away_id, away)

    parts = [f"=== 基本面：{home} vs {away}（{league_name}）==="]
    if _is_national_team_event(league_id, league_name):
        parts.append(_NATIONAL_TEAM_HINT)
    if not home_id or not away_id:
        parts.append("⚠️ 该比赛缺少球队 ID（旧数据未刷新），基本面暂不可用。"
                     "等任务A刷新后重试。")
        return "\n".join(parts)

    try:
        parts.append(_recent(home_id, home))
        parts.append(_recent(away_id, away))
        parts.append(_h2h(home_id, away_id, home, away))
        parts.append(_upcoming(home_id, home))
        parts.append(_upcoming(away_id, away))
        parts.append(_standings(league_id, season, home, away))
    except Exception as e:               # 基本面失败不应阻断精算
        log.warning("基本面拉取部分失败: %s", e)
        parts.append(f"（基本面拉取出错：{e}）")

    parts.append(_CONTEXT_HINT)
    parts.append("⚠️ 缺失的数据不要编造，请按以上战绩/比分/排名/交锋综合判断。")
    return "\n".join(parts)
