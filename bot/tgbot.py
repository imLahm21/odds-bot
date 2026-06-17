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

from . import config, db, api_client, parser

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
    start = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    end = (now + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = db.get_conn()
    try:
        fixtures = db.get_fixtures_between(conn, start, end)
    finally:
        conn.close()
    if not fixtures:
        send(chat_id, "未来 3 天暂无赛程（可能休赛期或赛程未拉取）")
        return
    lines = ["<b>未来 3 天赛程</b>"]
    for fid, commence, home, away in fixtures[:30]:
        cst = parser.node_label  # 仅借用模块；下面手动转时区
        t = commence.replace("T", " ")[:16]
        lines.append(f"<code>{fid}</code> {t}  {home} vs {away}")
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
    else:
        send(chat_id, "未知命令，发 /help 看用法")


def handle_callback(cb: dict) -> None:
    """处理内联按钮点击：tl:<league_id> / tb:<bookmaker_id>"""
    cb_id = cb.get("id")
    data = cb.get("data", "")
    msg = cb.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    message_id = msg.get("message_id")
    if not _authorized(chat_id):
        answer_callback(cb_id, "未授权")
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
