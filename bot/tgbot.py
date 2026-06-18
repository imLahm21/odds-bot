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
  /fixtures           未来 3 天赛程
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
_allowed_raw = os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "").strip()
ALLOWED_CHAT_IDS = {int(x) for x in _allowed_raw.replace(" ", "").split(",")
                    if x.lstrip("-").isdigit()}

API_BASE = f"{config.TELEGRAM_API}/bot{TOKEN}"


# ─── Telegram HTTP 封装 ──────────────────────────────────────────────────────
def _post(method: str, payload: dict) -> dict | None:
    try:
        r = requests.post(f"{API_BASE}/{method}", json=payload, timeout=60)
        return r.json()
    except requests.exceptions.RequestException as e:
        log.warning("Telegram %s 失败: %s", method, e)
        return None


def send(chat_id: int, text: str, reply_markup: dict | None = None) -> None:
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    _post("sendMessage", payload)


def edit_markup(chat_id: int, message_id: int, reply_markup: dict) -> None:
    _post("editMessageReplyMarkup", {
        "chat_id": chat_id, "message_id": message_id,
        "reply_markup": reply_markup})


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


def _authorized(chat_id: int) -> bool:
    """白名单校验：未配置白名单时拒绝所有（防误开放）。"""
    if not ALLOWED_CHAT_IDS:
        log.warning("未配置 TELEGRAM_ALLOWED_CHAT_IDS，拒绝 chat_id=%s", chat_id)
        return False
    return chat_id in ALLOWED_CHAT_IDS


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
HELP = (
    "<b>赔率轮询 bot</b>\n\n"
    "/leagues — 联赛开关面板\n"
    "/bookmakers — 庄家开关面板\n"
    "/add &lt;id&gt; &lt;season&gt; [名称] — 按 ID 加联赛\n"
    "/remove &lt;id&gt; — 删联赛\n"
    "/status — 当前启用项\n"
    "/fixtures — 未来 3 天赛程\n"
    "/odds &lt;fixture_id&gt; — 某场最新盘口\n"
    "/export &lt;fixture_id&gt; — 导出某场全部盘口为 CSV 文件\n"
    "/analyze &lt;fixture_id&gt; — 先看基本面+盘口走势，再按钮选是否跑SOP预测\n"
    "/review &lt;fixture_id&gt; — 对已结束的比赛做盘口复盘（盘口走势+实际比分）\n"
)


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


def _send_long(chat_id: int, text: str) -> None:
    """长文本按 TG_MSG_MAX 拆段发送，优先在换行处断开。"""
    limit = config.TG_MSG_MAX
    while text:
        if len(text) <= limit:
            send(chat_id, text)
            break
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        send(chat_id, text[:cut])
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

    # 盘口走势预览
    _send_long(chat_id, digest)
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
    _send_long(chat_id, "🧩 <b>基本面</b>\n" + funds)

    # 末条带「开始SOP精算」按钮
    if not analyzer.available():
        send(chat_id, "⚠️ 未配置 LLM（.env 缺 LLM_BASE_URL / LLM_API_KEY），"
                      "无法进行 SOP 精算预测，仅能查看上方数据。")
        return
    kb = {"inline_keyboard": [[
        {"text": "🎯 开始 SOP 精算预测", "callback_data": f"az:{fid}"}]]}
    send(chat_id, "以上为该场的基本面与盘口数据。是否用 SOP 跑结果预测？\n"
                  "（gpt-5.5 推理较慢，约 1~3 分钟）", kb)


def _run_sop(chat_id: int, fid: int) -> None:
    """第二步：真正跑 SOP 精算（由内联按钮 az:<fid> 触发）。"""
    from . import fundamentals
    if not analyzer.available():
        send(chat_id, "未配置 LLM（.env 缺 LLM_BASE_URL / LLM_API_KEY），无法精算。")
        return
    csv_str, meta = _build_csv(fid)
    if not csv_str:
        send(chat_id, f"fixture {fid} 暂无盘口数据，无法分析")
        return

    send(chat_id, f"⏳ 正在精算 {meta['home']} vs {meta['away']}，"
                  f"gpt-5.5 推理较慢，约 1~3 分钟，请稍候…")

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

    report = analyzer.analyze(csv_str, funds, meta["home"], meta["away"],
                              meta["league"])
    _send_long(chat_id, report)
    path = _archive_report(meta, report)
    if path:
        send(chat_id, f"📁 报告已归档：{path}")


def _cmd_review(chat_id: int, args: list[str]) -> None:
    """对已结束的比赛做事后复盘：实时拉最终比分 + 盘口走势 → LLM 归因 → 归档。"""
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

    send(chat_id, f"⏳ 正在复盘 {meta['home']} vs {meta['away']}\n"
                  f"{result_text.splitlines()[0]}\n"
                  f"gpt-5.5 推理较慢，约 1~3 分钟，请稍候…")

    report = analyzer.review(csv_str, result_text, meta["home"], meta["away"],
                             meta["league"])
    _send_long(chat_id, report)
    path = _archive_report(meta, report, suffix="review")
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

    parts = text.split()
    cmd = parts[0].lower().lstrip("/")
    args = parts[1:]

    if cmd in ("start", "help"):
        send(chat_id, HELP)
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
    """处理内联按钮点击：tl:<league_id> / tb:<bookmaker_id> / az:<fixture_id>"""
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
    offset = None
    while not stop_flag():
        try:
            params = {"timeout": config.TG_POLL_TIMEOUT}
            if offset is not None:
                params["offset"] = offset
            r = requests.get(f"{API_BASE}/getUpdates", params=params, timeout=70)
            updates = r.json().get("result", [])
            for u in updates:
                offset = u["update_id"] + 1
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
