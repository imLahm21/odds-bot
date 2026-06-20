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

    parts = [f"=== 基本面：{home} vs {away}（{league_name}）==="]
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

    parts.append("⚠️ 缺失的数据不要编造，请按以上战绩/比分/排名/交锋综合判断。")
    return "\n".join(parts)
