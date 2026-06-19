"""
Telegram bot —— 实时操控关注的联赛/庄家 + 查询数据

设计：
  - 裸 requests 做 long polling，无 asyncio，可与 apscheduler 后台线程共存
  - 安全：只响应 .env 白名单 TELEGRAM_ALLOWED_CHAT_IDS 里的 chat_id
  - 内联按钮：点击在 启用✅ / 停用⬜ 间切换，直接改数据库
  - 调度器每次抓取时读数据库的启用项，所以点完即时生效

命令：
  /start /help        帮助
  /leagues            联赛开关面板（内联按钮）
  /bookmakers         庄家开关面板（内联按钮）
  /add <id> <season> [名称]   按 league_id 新增关注联赛
  /remove <id>        删除关注联赛
  /status             当前启用了哪些联赛/庄家
  /fixtures           过去 3 天 ~ 未来 3 天赛程
  /coverage <fixture_id>  某场数据采集进度（10 节点缺漏一览）
  /odds <fixture_id>  某场最新盘口（Pinnacle/Bet365）
"""

import os
import time
import logging

import requests
from dotenv import load_dotenv

from . import config, db, api_client, analyzer

load_dotenv()
log = logging.getLogger("odds_bot.tgbot")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()


def _parse_ids(raw: str) -> set[int]:
    return {int(x) for x in raw.replace(" ", "").split(",")
            if x.lstrip("-").isdigit()}


# 两级权限：
#   ADMIN  —— 管理员，全部命令（含改联赛/庄家配置）。即 TELEGRAM_ADMIN_CHAT_IDS。
#   ALLOWED —— 能用 bot 的全体（管理员 + 访客）。访客只能查询/精算/复盘，
#             不能改配置。为向后兼容：管理员自动并入 ALLOWED，老配置不写
#             ADMIN 时退化为「ALLOWED 全员皆管理员」（与旧行为一致）。
ADMIN_CHAT_IDS = _parse_ids(os.getenv("TELEGRAM_ADMIN_CHAT_IDS", ""))
ALLOWED_CHAT_IDS = _parse_ids(os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "")) \
    | ADMIN_CHAT_IDS

# 仅管理员可用的配置类命令（访客调用会被拒绝）
_ADMIN_ONLY_CMDS = {"leagues", "bookmakers", "add", "remove"}

API_BASE = f"{config.TELEGRAM_API}/bot{TOKEN}"

# 等待用户回复自定义侧重的会话状态：chat_id -> fixture_id
# 用户点「✍️ 自定义侧重」后置位，下一条非命令文本被当作侧重消费后清除。
_pending_custom: dict[int, int] = {}


# ─── Telegram HTTP 封装 ──────────────────────────────────────────────────────
def _post(method: str, payload: dict) -> dict | None:
    try:
        r = requests.post(f"{API_BASE}/{method}", json=payload, timeout=60)
        data = r.json()
        # Telegram 业务错误（HTTP 200 但 ok=false，如 HTML 解析失败 400）以前被
        # 静默吞掉，导致“归档了但消息没发出”。这里记日志便于排查。
        if isinstance(data, dict) and not data.get("ok", True):
            log.warning("Telegram %s 失败: %s", method,
                        data.get("description", data))
        return data
    except requests.exceptions.RequestException as e:
        log.warning("Telegram %s 失败: %s", method, e)
        return None


def send(chat_id: int, text: str, reply_markup: dict | None = None,
         plain: bool = False) -> int | None:
    """发消息，返回新消息的 message_id（失败返回 None）。
    plain=True 时不带 parse_mode（纯文本），适合含 <>&| 的 LLM 报告，
    避免被当 HTML 解析导致 400 整条发送失败。
    """
    payload = {"chat_id": chat_id, "text": text}
    if not plain:
        payload["parse_mode"] = "HTML"
    if reply_markup:
        payload["reply_markup"] = reply_markup
    resp = _post("sendMessage", payload)
    try:
        return resp["result"]["message_id"]
    except (TypeError, KeyError):
        return None


def edit_markup(chat_id: int, message_id: int, reply_markup: dict) -> None:
    _post("editMessageReplyMarkup", {
        "chat_id": chat_id, "message_id": message_id,
        "reply_markup": reply_markup})


def edit_text(chat_id: int, message_id: int, text: str) -> None:
    """编辑已发消息的文字（用于原地更新进度）。"""
    _post("editMessageText", {
        "chat_id": chat_id, "message_id": message_id,
        "text": text, "parse_mode": "HTML"})


def answer_callback(callback_id: str, text: str = "") -> None:
    _post("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})


def send_document(chat_id: int, filename: str, content: bytes,
                  caption: str = "") -> None:
    """以文件形式发送（multipart 上传）。content 为文件字节。"""
    try:
        requests.post(
            f"{API_BASE}/sendDocument",
            data={"chat_id": chat_id, "caption": caption},
            files={"document": (filename, content, "text/csv")},
            timeout=60)
    except requests.exceptions.RequestException as e:
        log.warning("sendDocument 失败: %s", e)


def setup_commands() -> None:
    """注册命令菜单（setMyCommands）：用户在输入框打 / 会弹出命令列表，
    左下角菜单按钮也会展示这些命令。启动时调一次即可，Telegram 端持久保存。
    command 必须是全小写、不含 /；description 是右侧灰字说明。
    """
    commands = [
        {"command": "help", "description": "查看全部命令用法"},
        {"command": "fixtures", "description": "过去3天~未来3天赛程（含 fixture_id）"},
        {"command": "coverage", "description": "看某场数据采集进度（10节点缺漏）"},
        {"command": "analyze", "description": "对某场跑 SOP 精算预测"},
        {"command": "review", "description": "对已结束的比赛做盘口复盘"},
        {"command": "odds", "description": "查某场最新盘口"},
        {"command": "export", "description": "导出某场全部盘口为 CSV"},
        {"command": "leagues", "description": "联赛抓取开关面板"},
        {"command": "bookmakers", "description": "庄家抓取开关面板"},
        {"command": "status", "description": "当前启用了哪些联赛/庄家"},
        {"command": "add", "description": "按 league_id 新增关注联赛"},
        {"command": "remove", "description": "删除关注联赛"},
    ]
    resp = _post("setMyCommands", {"commands": commands})
    if resp and resp.get("ok"):
        log.info("命令菜单已注册（%d 条）", len(commands))
    else:
        log.warning("命令菜单注册失败: %s", resp)


def _authorized(chat_id: int) -> bool:
    """白名单校验：未配置白名单时拒绝所有（防误开放）。"""
    if not ALLOWED_CHAT_IDS:
        log.warning("未配置 TELEGRAM_ALLOWED_CHAT_IDS，拒绝 chat_id=%s", chat_id)
        return False
    return chat_id in ALLOWED_CHAT_IDS


def _is_admin(chat_id: int) -> bool:
    """管理员判定。未单独配 TELEGRAM_ADMIN_CHAT_IDS 时，退化为
    「ALLOWED 全员皆管理员」——与未引入访客概念前的旧行为一致，
    避免老部署升级后突然没人能改配置。"""
    if not ADMIN_CHAT_IDS:
        return chat_id in ALLOWED_CHAT_IDS
    return chat_id in ADMIN_CHAT_IDS


# ─── 内联键盘构建 ────────────────────────────────────────────────────────────
def _leagues_keyboard() -> dict:
    conn = db.get_conn()
    try:
        rows = db.list_leagues(conn)
    finally:
        conn.close()
    buttons, line = [], []
    for lid, name, season, enabled in rows:
        mark = "✅" if enabled else "⬜"
        line.append({"text": f"{mark} {name}",
                     "callback_data": f"tl:{lid}"})
        if len(line) >= config.TG_LEAGUES_PER_ROW:
            buttons.append(line)
            line = []
    if line:
        buttons.append(line)
    return {"inline_keyboard": buttons}


def _bookmakers_keyboard() -> dict:
    conn = db.get_conn()
    try:
        rows = db.list_bookmakers(conn)
    finally:
        conn.close()
    buttons, line = [], []
    for bid, name, enabled in rows:
        mark = "✅" if enabled else "⬜"
        line.append({"text": f"{mark} {name}",
                     "callback_data": f"tb:{bid}"})
        if len(line) >= config.TG_BOOKMAKERS_PER_ROW:
            buttons.append(line)
            line = []
    if line:
        buttons.append(line)
    return {"inline_keyboard": buttons}


# ─── 命令处理 ────────────────────────────────────────────────────────────────
# 访客可见命令（查询 + 精算/复盘）
_HELP_VISITOR = (
    "<b>赔率轮询 bot</b>\n\n"
    "/fixtures — 过去 3 天 ~ 未来 3 天赛程（✅已开赛可复盘 / 🔵未来可精算）\n"
    "/coverage &lt;fixture_id&gt; — 看某场数据采集进度（10 节点抓了几个、缺哪些）\n"
    "/odds &lt;fixture_id&gt; — 某场最新盘口\n"
    "/export &lt;fixture_id&gt; — 导出某场全部盘口为 CSV 文件\n"
    "/analyze &lt;fixture_id&gt; — 先看基本面+盘口走势，再按钮选预设/自定义侧重跑SOP预测\n"
    "/review &lt;fixture_id&gt; — 对已结束的比赛做盘口复盘（盘口走势+实际比分）\n"
    "/status — 当前启用项\n"
)
# 管理员附加的配置类命令
_HELP_ADMIN_EXTRA = (
    "\n<b>管理员命令</b>\n"
    "/leagues — 联赛开关面板\n"
    "/bookmakers — 庄家开关面板\n"
    "/add &lt;id&gt; &lt;season&gt; [名称] — 按 ID 加联赛\n"
    "/remove &lt;id&gt; — 删联赛\n"
)
HELP = _HELP_VISITOR + _HELP_ADMIN_EXTRA   # 兼容旧引用：完整版


def _help_for(chat_id: int) -> str:
    """按身份返回帮助：管理员看全部，访客只看查询/分析命令。"""
    return HELP if _is_admin(chat_id) else _HELP_VISITOR


def _cmd_status(chat_id: int) -> None:
    conn = db.get_conn()
    try:
        leagues = [n for _, n, _, e in db.list_leagues(conn) if e]
        bms = [n for _, n, e in db.list_bookmakers(conn) if e]
    finally:
        conn.close()
    send(chat_id,
         f"<b>启用联赛（{len(leagues)}）</b>\n" + "、".join(leagues) +
         f"\n\n<b>启用庄家（{len(bms)}）</b>\n" + "、".join(bms))


def _cmd_add(chat_id: int, args: list[str]) -> None:
    if len(args) < 2 or not args[0].isdigit() or not args[1].isdigit():
        send(chat_id, "用法：/add &lt;league_id&gt; &lt;season&gt; [名称]\n"
                      "例：/add 207 2026 瑞超")
        return
    lid, season = int(args[0]), int(args[1])
    name = " ".join(args[2:]) if len(args) > 2 else None
    # 没给名称时，调 API 查真实联赛名
    if not name:
        data = api_client.api_get("/leagues", {"id": lid})
        resp = (data or {}).get("response", [])
        name = resp[0]["league"]["name"] if resp else f"League {lid}"
    conn = db.get_conn()
    try:
        db.add_league(conn, lid, name, season)
    finally:
        conn.close()
    send(chat_id, f"已添加并启用：<b>{name}</b> (id={lid}, season={season})\n"
                  f"下次抓取生效。")


def _cmd_remove(chat_id: int, args: list[str]) -> None:
    if not args or not args[0].lstrip("-").isdigit():
        send(chat_id, "用法：/remove &lt;league_id&gt;")
        return
    lid = int(args[0])
    conn = db.get_conn()
    try:
        ok = db.remove_league(conn, lid)
    finally:
        conn.close()
    send(chat_id, f"已删除联赛 id={lid}" if ok else f"未找到 id={lid}")


def _cmd_fixtures(chat_id: int) -> None:
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    # 过去 3 天 ~ 未来 3 天：过去的可 /review 复盘，未来的可 /analyze 精算
    start = (now - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = (now + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = db.get_conn()
    try:
        fixtures = db.get_fixtures_between(conn, start, end)
    finally:
        conn.close()
    if not fixtures:
        send(chat_id, "过去/未来 3 天暂无赛程（可能休赛期或赛程未拉取）")
        return

    tz_cst = timezone(timedelta(hours=8))

    def to_cst(iso_str):
        try:
            dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
            return dt.astimezone(tz_cst).strftime("%m-%d %H:%M")
        except (ValueError, AttributeError):
            return iso_str.replace("T", " ")[:16]

    def kicked_off(iso_str):
        try:
            return datetime.fromisoformat(iso_str.replace("Z", "+00:00")) <= now
        except (ValueError, AttributeError):
            return False

    lines = ["<b>赛程（过去 3 天 ~ 未来 3 天）</b>",
             "✅=已开赛可 /review 复盘　🔵=未来可 /analyze 精算"]
    for fid, commence, home, away in fixtures[:40]:
        mark = "✅" if kicked_off(commence) else "🔵"
        lines.append(f"{mark} <code>{fid}</code> {to_cst(commence)}  {home} vs {away}")
    send(chat_id, "\n".join(lines))


def _cmd_coverage(chat_id: int, args: list[str]) -> None:
    """查某场比赛的数据采集进度：10 节点哪些已抓/缺失、各节点快照数与庄家数、
    距开球时长。用于跑 SOP 前判断数据是否够用。"""
    from datetime import datetime, timezone, timedelta
    if not args or not args[0].isdigit():
        send(chat_id, "用法：/coverage &lt;fixture_id&gt;（看某场数据采集到哪了）")
        return
    fid = int(args[0])
    conn = db.get_conn()
    try:
        fx = conn.execute(
            "SELECT home_team, away_team, league_name, commence_utc "
            "FROM fixtures WHERE fixture_id=?", (fid,)).fetchone()
        # 各节点：快照次数、去重庄家数、最近一次抓取时间
        rows = conn.execute(
            "SELECT node_label, COUNT(DISTINCT snapshot_utc) AS snaps, "
            "COUNT(DISTINCT bookmaker_id) AS bms, MAX(snapshot_utc) AS last_snap "
            "FROM odds_history WHERE fixture_id=? GROUP BY node_label",
            (fid,)).fetchall()
    finally:
        conn.close()
    if not fx:
        send(chat_id, f"未找到 fixture {fid}（先 /fixtures 看可用 id）")
        return

    home, away, league, commence = fx[0], fx[1], fx[2], fx[3]
    now = datetime.now(timezone.utc)
    tz_cst = timezone(timedelta(hours=8))

    def to_cst(iso_str):
        if not iso_str:
            return ""
        try:
            dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
            return dt.astimezone(tz_cst).strftime("%m-%d %H:%M")
        except (ValueError, AttributeError):
            return iso_str

    # 距开球时长（正=未开赛剩余，负=已开赛）
    kick_line = to_cst(commence)
    try:
        kt = datetime.fromisoformat(commence.replace("Z", "+00:00"))
        delta_h = (kt - now).total_seconds() / 3600
        if delta_h >= 0:
            till = f"距开球 {delta_h:.1f}h"
        else:
            till = f"已开赛 {-delta_h:.1f}h"
    except (ValueError, AttributeError):
        delta_h, till = None, "开球时间未知"

    by_node = {r[0]: (r[1], r[2], r[3]) for r in rows if r[0]}
    got = sum(1 for _, lbl in config.NODE_THRESHOLDS if lbl in by_node)
    total = len(config.NODE_THRESHOLDS)

    lines = [f"📡 <b>{home} vs {away}</b> 数据采集进度",
             f"{league}  开球 {kick_line}（{till}）",
             f"已采集 <b>{got}/{total}</b> 个节点：",
             "<pre>节点    状态  快照×庄家  最近抓取"]
    # 按 SOP 时间线顺序（初盘→即时）逐节点列出
    for thresh, lbl in config.NODE_THRESHOLDS:
        if lbl in by_node:
            snaps, bms, last_snap = by_node[lbl]
            lines.append(f"{lbl:<6} ✅    {snaps}×{bms:<6} {to_cst(last_snap)}")
        else:
            # 该节点是否「本该已过」：开球前 thresh 小时这个时点已到却没抓到
            missed = delta_h is not None and delta_h < thresh
            mark = "❌" if missed else "⬜"
            lines.append(f"{lbl:<6} {mark}")
    lines.append("</pre>")
    lines.append("✅=已抓　❌=该时点已过却缺数据　⬜=尚未到该节点")
    if got < total:
        lines.append("\n数据越全 SOP 定性越准；缺初盘段会影响开盘深浅判断。")
    send(chat_id, "\n".join(lines))


def _cmd_odds(chat_id: int, args: list[str]) -> None:
    if not args or not args[0].isdigit():
        send(chat_id, "用法：/odds &lt;fixture_id&gt;（id 见 /fixtures）")
        return
    fid = int(args[0])
    conn = db.get_conn()
    try:
        latest = conn.execute(
            "SELECT bookmaker, market, home_odds, draw_odds, away_odds, "
            "handicap, home_water, away_water, snapshot_utc "
            "FROM odds_history WHERE fixture_id=? AND bookmaker_id IN (4,8) "
            "AND snapshot_utc=(SELECT MAX(snapshot_utc) FROM odds_history WHERE fixture_id=?) "
            "ORDER BY bookmaker, market, handicap", (fid, fid)).fetchall()
    finally:
        conn.close()
    if not latest:
        send(chat_id, f"fixture {fid} 暂无盘口数据")
        return
    lines = [f"<b>fixture {fid} 最新盘口（Pinnacle/Bet365）</b>"]
    for bm, market, ho, do, ao, hc, hw, aw, snap in latest:
        if market == "h2h":
            lines.append(f"{bm} 欧赔: 主{ho} 平{do} 客{ao}")
        else:
            lines.append(f"{bm} 亚盘{hc:+}: 主水{hw} 客水{aw}")
    send(chat_id, "\n".join(lines[:40]))


def _build_csv(fid: int):
    """查某场盘口快照，生成对齐旧 main.py 的 19 列 CSV 字符串。
    返回 (csv_str, meta)；无数据返回 (None, None)。meta 含 home/away/league/rows。
    """
    import csv
    import io
    from datetime import datetime, timezone, timedelta
    conn = db.get_conn()
    try:
        fx = conn.execute(
            "SELECT home_team, away_team, league_name, commence_utc FROM fixtures "
            "WHERE fixture_id=?", (fid,)).fetchone()
        rows = conn.execute(
            "SELECT snapshot_utc, node_label, bookmaker, market, "
            "home_odds, draw_odds, away_odds, kelly_home, kelly_draw, kelly_away, "
            "handicap, home_water, away_water, kelly_h_water, kelly_a_water "
            "FROM odds_history WHERE fixture_id=? "
            "ORDER BY snapshot_utc, bookmaker, market, handicap", (fid,)).fetchall()
    finally:
        conn.close()
    if not rows:
        return None, None

    tz_cst = timezone(timedelta(hours=8))

    def to_cst(iso_str: str) -> str:
        if not iso_str:
            return ""
        try:
            dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
            return dt.astimezone(tz_cst).strftime("%Y-%m-%d %H:%M")
        except (ValueError, AttributeError):
            return iso_str

    home = fx[0] if fx else ""
    away = fx[1] if fx else ""
    league = fx[2] if fx else ""
    kick_cst = to_cst(fx[3]) if fx else ""
    market_zh = {"h2h": "欧指", "ah": "亚盘"}

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["快照时间(CST)", "联赛", "开球时间(CST)", "主队", "客队",
                "博彩公司", "盘口类型", "主胜赔率", "平局赔率", "客胜赔率",
                "凯利(主胜)", "凯利(平局)", "凯利(客胜)",
                "让球", "主队水位", "客队水位", "凯利(主)", "凯利(客)",
                "数据更新(CST)"])
    for (snap, node, bm, market, ho, do, ao, kh, kd, ka,
         hc, hw, aw, khw, kaw) in rows:
        snap_cst = to_cst(snap)
        snap_label = f"{snap_cst}（{node}）" if node else snap_cst
        is_h2h = market == "h2h"
        w.writerow([
            snap_label, league, kick_cst, home, away, bm,
            market_zh.get(market, market),
            ho if is_h2h else "", do if is_h2h else "", ao if is_h2h else "",
            kh if is_h2h else "", kd if is_h2h else "", ka if is_h2h else "",
            "" if is_h2h else hc, "" if is_h2h else hw, "" if is_h2h else aw,
            "" if is_h2h else khw, "" if is_h2h else kaw,
            snap_cst,
        ])
    meta = {"home": home, "away": away, "league": league,
            "kick_cst": kick_cst, "rows": len(rows)}
    return buf.getvalue(), meta


def _cmd_export(chat_id: int, args: list[str]) -> None:
    """导出某场全部盘口快照为 CSV，对齐旧 main.py 格式（可直接喂精算 SOP）。"""
    if not args or not args[0].isdigit():
        send(chat_id, "用法：/export &lt;fixture_id&gt;（id 见 /fixtures）")
        return
    fid = int(args[0])
    csv_str, meta = _build_csv(fid)
    if not csv_str:
        send(chat_id, f"fixture {fid} 暂无盘口数据")
        return
    content = ("﻿" + csv_str).encode("utf-8")
    teams = f"{meta['home']}_vs_{meta['away']}".replace(" ", "_")
    caption = f"{meta['league']} {meta['home']} vs {meta['away']}\n共 {meta['rows']} 行快照"
    send_document(chat_id, f"{teams}_stages.csv", content, caption)


# 复盘主盘口取盘优先级：Pinnacle > Bet365（与凯利锚一致）
_DIGEST_BM_IDS = (4, 8)


def _odds_digest(fid: int) -> tuple[str, dict] | tuple[None, None]:
    """读库生成盘口走势预览（给人看的概览，非完整 CSV）。

    取 Pinnacle（缺则 Bet365）各节点的欧赔 + 主盘口亚盘水位，按节点时间线
    列成走势表。返回 (digest_text, meta)；无数据返回 (None, None)。
    """
    conn = db.get_conn()
    try:
        fx = conn.execute(
            "SELECT home_team, away_team, league_name, commence_utc FROM fixtures "
            "WHERE fixture_id=?", (fid,)).fetchone()
        rows = conn.execute(
            "SELECT snapshot_utc, node_label, bookmaker_id, market, "
            "home_odds, draw_odds, away_odds, handicap, home_water, away_water "
            "FROM odds_history WHERE fixture_id=? AND bookmaker_id IN (4,8) "
            "ORDER BY snapshot_utc", (fid,)).fetchall()
    finally:
        conn.close()
    if not rows:
        return None, None

    from datetime import datetime, timezone, timedelta
    tz_cst = timezone(timedelta(hours=8))

    def to_cst(iso_str):
        if not iso_str:
            return ""
        try:
            dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
            return dt.astimezone(tz_cst).strftime("%Y-%m-%d %H:%M")
        except (ValueError, AttributeError):
            return iso_str

    home = fx[0] if fx else ""
    away = fx[1] if fx else ""
    league = fx[2] if fx else ""
    kick_cst = to_cst(fx[3]) if fx else ""

    # 按 (snapshot, node) 聚合：选优先级最高的庄家的欧赔 + 主盘口亚盘
    # 主盘口 = 该快照下 |让球| 最小的那条线（最接近平手的主流盘）
    snaps: dict[tuple, dict] = {}
    for (snap, node, bid, market, ho, do, ao, hc, hw, aw) in rows:
        key = (snap, node)
        slot = snaps.setdefault(key, {"h2h": None, "h2h_bid": None,
                                      "ah": None, "ah_bid": None})
        if market == "h2h" and ho:
            # Pinnacle(4) 优先于 Bet365(8)
            if slot["h2h_bid"] is None or bid < slot["h2h_bid"]:
                slot["h2h"] = (ho, do, ao)
                slot["h2h_bid"] = bid
        elif market == "ah" and hc is not None and hw and aw:
            better_bm = slot["ah_bid"] is None or bid < slot["ah_bid"]
            closer = (slot["ah"] is not None
                      and abs(hc) < abs(slot["ah"][0]))
            if better_bm or (bid == slot["ah_bid"] and closer):
                if better_bm:
                    slot["ah"] = (hc, hw, aw)
                    slot["ah_bid"] = bid
                elif closer:
                    slot["ah"] = (hc, hw, aw)

    header = f"📊 <b>{home} vs {away}</b>\n{league}  开球 {kick_cst}\n" \
             f"盘口走势（主锚 Pinnacle，缺则 Bet365）："
    table = ["节点     欧赔H/D/A         让球  主水/客水"]
    n_nodes = 0
    for (snap, node) in sorted(snaps.keys()):
        slot = snaps[(snap, node)]
        if not slot["h2h"] and not slot["ah"]:
            continue
        n_nodes += 1
        label = (node or to_cst(snap)[-5:]).ljust(7)
        if slot["h2h"]:
            ho, do, ao = slot["h2h"]
            eu = f"{ho:.2f}/{do:.2f}/{ao:.2f}"
        else:
            eu = "—"
        if slot["ah"]:
            hc, hw, aw = slot["ah"]
            ah = f"{hc:+g}  {hw:.2f}/{aw:.2f}"
        else:
            ah = "—"
        table.append(f"{label} {eu:<16} {ah}")

    digest = header + "\n<pre>" + "\n".join(table) + "</pre>"
    meta = {"home": home, "away": away, "league": league,
            "kick_cst": kick_cst, "nodes": n_nodes}
    return digest, meta


def _fmt_result(entry: dict) -> tuple[str, str] | tuple[None, None]:
    """把 /fixtures?id= 的 response[0] 格式化为结果文本。
    返回 (result_text, status_short)；无法解析返回 (None, None)。
    未结束的比赛 status_short 非 FT/AET/PEN，调用方据此拒绝复盘。
    """
    fx = entry.get("fixture", {})
    status = fx.get("status", {})
    short = status.get("short", "")
    teams = entry.get("teams", {})
    goals = entry.get("goals", {})
    score = entry.get("score", {})
    home = teams.get("home", {}).get("name", "")
    away = teams.get("away", {}).get("name", "")
    hg, ag = goals.get("home"), goals.get("away")
    if hg is None or ag is None:
        return None, short

    ht = score.get("halftime", {}) or {}
    et = score.get("extratime", {}) or {}
    pen = score.get("penalty", {}) or {}
    res = "主胜" if hg > ag else ("平局" if hg == ag else "客胜")
    parts = [f"{home} {hg}-{ag} {away}（{res}）",
             f"全场比分：{hg}-{ag}",
             f"总进球：{hg + ag}"]
    if ht.get("home") is not None:
        parts.append(f"半场：{ht.get('home')}-{ht.get('away')}")
    if et.get("home") is not None:
        parts.append(f"加时（含）：{et.get('home')}-{et.get('away')}")
    if pen.get("home") is not None:
        parts.append(f"点球：{pen.get('home')}-{pen.get('away')}")
    parts.append(f"赛事状态：{status.get('long', short)}")
    return "\n".join(parts), short


def _send_long(chat_id: int, text: str, plain: bool = True) -> None:
    """长文本按 TG_MSG_MAX 拆段发送，优先在换行处断开。
    默认 plain=True（纯文本）：LLM 报告/基本面含 <>&| 等字符，HTML 模式会
    400 整条失败。需要 HTML 渲染（如含 <pre> 表格的盘口走势）时传 plain=False。
    """
    limit = config.TG_MSG_MAX
    while text:
        if len(text) <= limit:
            send(chat_id, text, plain=plain)
            break
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        send(chat_id, text[:cut], plain=plain)
        text = text[cut:].lstrip("\n")


def _archive_report(meta: dict, report: str, suffix: str = "report") -> str | None:
    """把报告存一份 md 到 report/<开球日期>/<主队>_vs_<客队>_<suffix>.md。
    suffix: 'report'（精算）或 'review'（复盘）。
    """
    import os
    try:
        date = (meta.get("kick_cst") or "")[:10] or "未知日期"
        teams = f"{meta['home']}_vs_{meta['away']}".replace(" ", "_")
        out_dir = os.path.join("report", date)
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"{teams}_{suffix}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(report)
        return path
    except Exception as e:
        log.warning("报告归档失败: %s", e)
        return None


def _cmd_analyze(chat_id: int, args: list[str]) -> None:
    """第一步：展示某场基本面 + 盘口走势预览，附【开始SOP精算】按钮。

    拆分为两步——本命令只读库/拉基本面做展示，不跑 LLM；
    用户点按钮（callback az:<fid>）才真正跑 SOP（见 _run_sop）。
    """
    from . import fundamentals
    if not args or not args[0].isdigit():
        send(chat_id, "用法：/analyze &lt;fixture_id&gt;（id 见 /fixtures）")
        return
    fid = int(args[0])
    digest, meta = _odds_digest(fid)
    if not digest:
        send(chat_id, f"fixture {fid} 暂无盘口数据")
        return

    # 盘口走势预览（含 <pre> 表格，需 HTML 渲染）
    _send_long(chat_id, digest, plain=False)
    send(chat_id, f"已获取 {meta['nodes']} 个节点的盘口走势。正在拉取基本面…")

    # 基本面（读库 + 调 API，失败不阻断）
    try:
        conn = db.get_conn()
        try:
            funds = fundamentals.build_fundamentals(conn, fid)
        finally:
            conn.close()
    except Exception as e:
        log.warning("基本面拉取失败: %s", e)
        funds = "（基本面拉取失败）"
    _send_long(chat_id, "🧩 基本面\n" + funds)

    # 末条带「预设精算 / 自定义侧重」两个按钮
    if not analyzer.available():
        send(chat_id, "⚠️ 未配置 LLM（.env 缺 LLM_BASE_URL / LLM_API_KEY），"
                      "无法进行 SOP 精算预测，仅能查看上方数据。")
        return
    kb = {"inline_keyboard": [[
        {"text": "🎯 预设精算", "callback_data": f"az:{fid}"},
        {"text": "✍️ 自定义侧重", "callback_data": f"ac:{fid}"},
    ]]}
    send(chat_id, "以上为该场的基本面与盘口数据。是否用 SOP 跑结果预测？\n"
                  "🎯 预设精算 = 直接按标准 SOP 跑；\n"
                  "✍️ 自定义侧重 = 在 SOP 基础上加你的一句侧重要求（如「重点看临场异动」「忽略基本面只看盘口」）。\n"
                  "（gpt-5.5 推理较慢，约 1~3 分钟）", kb)


def _run_sop(chat_id: int, fid: int, extra_instruction: str = "") -> None:
    """第二步：真正跑 SOP 精算。
    extra_instruction 非空时为用户自定义侧重（由 ✍️ 自定义触发）。
    """
    from . import fundamentals
    if not analyzer.available():
        send(chat_id, "未配置 LLM（.env 缺 LLM_BASE_URL / LLM_API_KEY），无法精算。")
        return
    csv_str, meta = _build_csv(fid)
    if not csv_str:
        send(chat_id, f"fixture {fid} 暂无盘口数据，无法分析")
        return

    # 重新拉基本面（与展示步独立，确保用最新数据）
    try:
        conn = db.get_conn()
        try:
            funds = fundamentals.build_fundamentals(conn, fid)
        finally:
            conn.close()
    except Exception as e:
        log.warning("基本面拉取失败: %s", e)
        funds = "（基本面拉取失败）"

    # 流式精算 + 原地进度播报
    tag = "✍️自定义" if extra_instruction.strip() else "🎯预设"
    title = (f"⏳ 正在精算 {meta['home']} vs {meta['away']}"
             f"（{tag}，gpt-5.5，约 1~3 分钟）")
    if extra_instruction.strip():
        title += f"\n侧重：{extra_instruction.strip()[:80]}"
    total = analyzer._TOTAL_STAGES
    done_stages: list[str] = []

    def progress_text(cur_n: int | None, cur_name: str | None) -> str:
        lines = [title, ""]
        for n in range(1, total + 1):
            name = analyzer._STAGE_NAMES[n]
            if n < (cur_n or 0) or name in done_stages:
                lines.append(f"✅ {n}. {name}")
            elif n == cur_n:
                lines.append(f"🔄 {n}. {name} …")
            else:
                lines.append(f"⬜ {n}. {name}")
        return "\n".join(lines)

    msg_id = send(chat_id, progress_text(1, "数据提取"))
    report = None
    for ev in analyzer.analyze_stream(csv_str, funds, meta["home"],
                                      meta["away"], meta["league"],
                                      extra_instruction):
        if ev[0] == "stage":
            _, n, name = ev
            done_stages = [analyzer._STAGE_NAMES[i] for i in range(1, n)]
            if msg_id:
                edit_text(chat_id, msg_id, progress_text(n, name))
        elif ev[0] == "done":
            report = ev[1]
        elif ev[0] == "error":
            if msg_id:
                edit_text(chat_id, msg_id, f"❌ 精算失败：{ev[1]}")
            else:
                send(chat_id, f"❌ 精算失败：{ev[1]}")
            return

    if not report:
        send(chat_id, "❌ 精算未产出报告，请稍后重试。")
        return
    if msg_id:
        edit_text(chat_id, msg_id, title + "\n\n✅ 全部 7 步完成，报告如下：")
    _send_long(chat_id, report)
    path = _archive_report(meta, report)
    if path:
        send(chat_id, f"📁 报告已归档：{path}")


def _cmd_review(chat_id: int, args: list[str]) -> None:
    """对已结束的比赛做【正向盲推 + 对照】复盘：
    第一遍只喂盘口走势(不给比分)正向跑 SOP 得预判 → 第二遍揭晓真实比分做对照归因。
    两遍各自实时播报进度，最后两份报告都发回并归档。"""
    from . import api_client
    if not args or not args[0].isdigit():
        send(chat_id, "用法：/review &lt;fixture_id&gt;（对已结束的比赛复盘）")
        return
    if not analyzer.available():
        send(chat_id, "未配置 LLM（.env 缺 LLM_BASE_URL / LLM_API_KEY），无法复盘。")
        return
    fid = int(args[0])
    csv_str, meta = _build_csv(fid)
    if not csv_str:
        send(chat_id, f"fixture {fid} 暂无盘口数据，无法复盘")
        return

    # 实时拉最终结果（不入库，复盘一次性使用）
    entry = api_client.fetch_fixture_result(fid)
    if not entry:
        send(chat_id, f"无法拉取 fixture {fid} 的结果（API 无返回）")
        return
    result_text, short = _fmt_result(entry)
    finished = {"FT", "AET", "PEN"}
    if result_text is None or short not in finished:
        send(chat_id, f"⚠️ fixture {fid} 尚未结束（状态：{short or '未知'}），"
                      f"无法复盘。请在比赛结束后再试。")
        return

    # ── 第一遍：盲推（只喂盘口，不给比分），正向跑 SOP 7 步 ──
    send(chat_id, f"🔬 复盘分两步。第一步【盲推】：不看比分，仅凭盘口正向跑 SOP "
                  f"得赛前预判（gpt-5.5，约 1~3 分钟）…")
    blind_title = f"⏳ 第一步·盲推 {meta['home']} vs {meta['away']}（不看结果）"
    total_a = analyzer._TOTAL_STAGES

    def blind_progress(cur_n: int | None, cur_name: str | None) -> str:
        lines = [blind_title, ""]
        for n in range(1, total_a + 1):
            name = analyzer._STAGE_NAMES[n]
            if cur_n and n < cur_n:
                lines.append(f"✅ {n}. {name}")
            elif n == cur_n:
                lines.append(f"🔄 {n}. {name} …")
            else:
                lines.append(f"⬜ {n}. {name}")
        return "\n".join(lines)

    msg_a = send(chat_id, blind_progress(1, analyzer._STAGE_NAMES[1]))
    forecast = None
    for ev in analyzer.review_blind_stream(csv_str, meta["home"],
                                           meta["away"], meta["league"]):
        if ev[0] == "stage":
            if msg_a:
                edit_text(chat_id, msg_a, blind_progress(ev[1], ev[2]))
        elif ev[0] == "done":
            forecast = ev[1]
        elif ev[0] == "error":
            if msg_a:
                edit_text(chat_id, msg_a, f"❌ 盲推失败：{ev[1]}")
            else:
                send(chat_id, f"❌ 盲推失败：{ev[1]}")
            return
    if not forecast:
        send(chat_id, "❌ 盲推未产出预判，复盘中止。")
        return
    if msg_a:
        edit_text(chat_id, msg_a, blind_title + "\n\n✅ 盲推完成，预判如下：")
    _send_long(chat_id, "🔮 第一步·盲推预判\n\n" + forecast)

    # ── 第二遍：揭晓比分，对照归因（6 步）──
    send(chat_id, "🎬 第二步【对照】：揭晓真实比分，对照盲推预判做归因复盘…")
    title = (f"⏳ 第二步·对照复盘 {meta['home']} vs {meta['away']}\n"
             f"{result_text.splitlines()[0]}\n（gpt-5.5，约 1~3 分钟）")
    total = analyzer._REVIEW_TOTAL_STAGES

    def progress_text(cur_n: int | None) -> str:
        lines = [title, ""]
        for n in range(1, total + 1):
            name = analyzer._REVIEW_STAGE_NAMES[n]
            if n < (cur_n or 0):
                lines.append(f"✅ {n}. {name}")
            elif n == cur_n:
                lines.append(f"🔄 {n}. {name} …")
            else:
                lines.append(f"⬜ {n}. {name}")
        return "\n".join(lines)

    msg_id = send(chat_id, progress_text(1))
    report = None
    for ev in analyzer.review_stream(csv_str, forecast, result_text,
                                     meta["home"], meta["away"], meta["league"]):
        if ev[0] == "stage":
            if msg_id:
                edit_text(chat_id, msg_id, progress_text(ev[1]))
        elif ev[0] == "done":
            report = ev[1]
        elif ev[0] == "error":
            if msg_id:
                edit_text(chat_id, msg_id, f"❌ 对照复盘失败：{ev[1]}")
            else:
                send(chat_id, f"❌ 对照复盘失败：{ev[1]}")
            return

    if not report:
        send(chat_id, "❌ 对照复盘未产出报告，请稍后重试。")
        return
    if msg_id:
        edit_text(chat_id, msg_id, title + "\n\n✅ 全部 6 步完成，复盘如下：")
    _send_long(chat_id, report)
    # 归档：盲推预判 + 对照复盘合并存一份
    full = ("# 第一步·盲推预判（不看比分）\n\n" + forecast
            + "\n\n---\n\n# 第二步·对照复盘\n\n" + report)
    path = _archive_report(meta, full, suffix="review")
    if path:
        send(chat_id, f"📁 复盘已归档：{path}")


def handle_message(msg: dict) -> None:
    chat_id = msg.get("chat", {}).get("id")
    text = msg.get("text", "").strip()
    if chat_id is None or not text:
        return
    if not _authorized(chat_id):
        send(chat_id, f"⛔ 未授权。你的 chat_id 是 <code>{chat_id}</code>，"
                      f"把它加入服务器 .env 的 TELEGRAM_ALLOWED_CHAT_IDS 即可。")
        return

    # 若该 chat 正在等待「自定义侧重」输入，优先消费这条消息
    if chat_id in _pending_custom:
        fid = _pending_custom.pop(chat_id)
        if text.lstrip("/").lower() in ("cancel", "取消"):
            send(chat_id, "已取消自定义精算。")
            return
        if text.startswith("/"):
            # 用户改发了别的命令，放弃自定义、按正常命令处理
            send(chat_id, "（已取消上一条自定义精算输入，改为执行新命令）")
        else:
            send(chat_id, f"收到自定义侧重，开始精算 fixture {fid} …")
            try:
                _run_sop(chat_id, fid, extra_instruction=text)
            except Exception:
                log.exception("自定义 SOP 精算执行出错")
                send(chat_id, "精算执行出错，请查看服务器日志。")
            return

    parts = text.split()
    cmd = parts[0].lower().lstrip("/")
    args = parts[1:]

    # 配置类命令仅管理员可用；访客只能查询/精算/复盘
    if cmd in _ADMIN_ONLY_CMDS and not _is_admin(chat_id):
        send(chat_id, "⛔ 该命令仅管理员可用。你可以用 /fixtures 选比赛，"
                      "再用 /analyze 或 /review 分析。发 /help 看可用命令。")
        return

    if cmd in ("start", "help"):
        send(chat_id, _help_for(chat_id))
    elif cmd == "leagues":
        send(chat_id, "点击切换联赛抓取开关（✅启用 / ⬜停用）：",
             _leagues_keyboard())
    elif cmd == "bookmakers":
        send(chat_id, "点击切换庄家抓取开关：", _bookmakers_keyboard())
    elif cmd == "add":
        _cmd_add(chat_id, args)
    elif cmd == "remove":
        _cmd_remove(chat_id, args)
    elif cmd == "status":
        _cmd_status(chat_id)
    elif cmd == "fixtures":
        _cmd_fixtures(chat_id)
    elif cmd == "coverage":
        _cmd_coverage(chat_id, args)
    elif cmd == "odds":
        _cmd_odds(chat_id, args)
    elif cmd == "export":
        _cmd_export(chat_id, args)
    elif cmd == "analyze":
        _cmd_analyze(chat_id, args)
    elif cmd == "review":
        _cmd_review(chat_id, args)
    else:
        send(chat_id, "未知命令，发 /help 看用法")


def handle_callback(cb: dict) -> None:
    """处理内联按钮点击：
    tl:<league_id> / tb:<bookmaker_id> / az:<fixture_id>(预设精算) /
    ac:<fixture_id>(自定义侧重，引导用户回复后再跑)
    """
    cb_id = cb.get("id")
    data = cb.get("data", "")
    msg = cb.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    message_id = msg.get("message_id")
    if not _authorized(chat_id):
        answer_callback(cb_id, "未授权")
        return

    # 精算按钮：先应答消除转圈，再同步跑 SOP（耗时 1~3 分钟）
    if data.startswith("az:"):
        answer_callback(cb_id, "已开始精算，请稍候…")
        # 点完即移除按钮，避免重复触发
        edit_markup(chat_id, message_id, {"inline_keyboard": []})
        try:
            _run_sop(chat_id, int(data[3:]))
        except Exception:
            log.exception("SOP 精算执行出错")
            send(chat_id, "精算执行出错，请查看服务器日志。")
        return

    # 自定义侧重按钮：置位待输入状态，引导用户回复一条侧重要求
    if data.startswith("ac:"):
        fid = int(data[3:])
        answer_callback(cb_id, "请回复你的侧重要求")
        edit_markup(chat_id, message_id, {"inline_keyboard": []})
        _pending_custom[chat_id] = fid
        send(chat_id,
             f"✍️ 请发一条消息，描述对 fixture {fid} 的精算侧重要求\n"
             "例：「重点分析临场④异动」「忽略基本面只看盘口资金流」「给保守口径」。\n"
             "（直接发文字即可；发 /cancel 取消）",
             {"force_reply": True, "input_field_placeholder": "输入精算侧重…"})
        return

    # 配置类按钮（联赛/庄家开关）仅管理员可点
    if data.startswith(("tl:", "tb:")) and not _is_admin(chat_id):
        answer_callback(cb_id, "仅管理员可改配置")
        return

    conn = db.get_conn()
    try:
        if data.startswith("tl:"):
            lid = int(data[3:])
            new = db.toggle_league(conn, lid)
            answer_callback(cb_id, "已启用" if new else "已停用")
            edit_markup(chat_id, message_id, _leagues_keyboard())
        elif data.startswith("tb:"):
            bid = int(data[3:])
            new = db.toggle_bookmaker(conn, bid)
            answer_callback(cb_id, "已启用" if new else "已停用")
            edit_markup(chat_id, message_id, _bookmakers_keyboard())
        else:
            answer_callback(cb_id)
    finally:
        conn.close()


# ─── long polling 主循环 ─────────────────────────────────────────────────────
def run_polling(stop_flag=lambda: False) -> None:
    """阻塞式 long polling。stop_flag() 返回 True 时退出。"""
    if not TOKEN:
        log.error("未配置 TELEGRAM_BOT_TOKEN，bot 不启动")
        return
    log.info("Telegram bot 启动，白名单 %d 人", len(ALLOWED_CHAT_IDS))
    setup_commands()   # 注册 / 命令菜单（输入框打 / 弹出）
    offset = None
    last_processed = -1   # 已处理的最大 update_id，去重水位线（防同一 update 被重复消费）

    # 启动时丢弃积压 update：重启后 offset=None 会拉回 Telegram 保留(最长24h)的
    # 所有未确认旧消息，导致历史命令被重新执行。offset=-1 仅取最后一条并据此
    # 确认掉之前全部积压，避免重启重放。
    try:
        r0 = requests.get(f"{API_BASE}/getUpdates",
                          params={"offset": -1, "timeout": 0}, timeout=15)
        backlog = r0.json().get("result", [])
        if backlog:
            last_uid = backlog[-1]["update_id"]
            offset = last_uid + 1
            last_processed = last_uid   # 这些积压全部视为已处理，不再执行
            log.info("启动丢弃积压 update（截至 update_id=%s）", last_uid)
    except requests.exceptions.RequestException as e:
        log.warning("启动清积压失败（忽略，继续）: %s", e)

    while not stop_flag():
        try:
            params = {"timeout": config.TG_POLL_TIMEOUT}
            if offset is not None:
                params["offset"] = offset
            r = requests.get(f"{API_BASE}/getUpdates", params=params, timeout=70)
            updates = r.json().get("result", [])
            if updates:
                log.info("收到 %d 个 update: %s", len(updates),
                         [(u.get("update_id"),
                           ("msg:" + u["message"].get("text", "")[:20])
                           if "message" in u else
                           ("cb:" + u.get("callback_query", {}).get("data", "")))
                          for u in updates])
            for u in updates:
                uid = u["update_id"]
                offset = uid + 1            # 始终前移 offset 确认，避免重拉
                if uid <= last_processed:   # 已处理过的 update，跳过（去重）
                    log.warning("跳过重复 update_id=%s", uid)
                    continue
                last_processed = uid
                if "message" in u:
                    handle_message(u["message"])
                elif "callback_query" in u:
                    handle_callback(u["callback_query"])
        except requests.exceptions.RequestException as e:
            log.warning("getUpdates 异常，5s 后重试: %s", e)
            time.sleep(5)
        except Exception:
            log.exception("处理 update 出错，继续")
            time.sleep(1)
