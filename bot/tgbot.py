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
  /add <关键词>|<id> <season>   搜索或按编号新增关注联赛
  /remove <id>        删除关注联赛
  /status             当前启用了哪些联赛/庄家
  /fixtures           过去 3 天 ~ 未来 3 天赛程
  /coverage <fixture_id>  某场数据采集进度（10 节点缺漏一览）
  /export <fixture_id>    导出某场全部盘口为 CSV
"""

import os
import time
import uuid
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import requests
from dotenv import load_dotenv

from . import config, db, api_client, analyzer, ghost_publish

load_dotenv()
log = logging.getLogger("odds_bot.tgbot")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

# 走地研判后台线程池：抓取循环(task_e)只管发盘口快报，LLM 研判丢这里异步跑，
# 研判完成再追发一条 💡 消息。这样 1min 一轮的抓取永远不被 LLM(最坏 30s)拖慢。
# daemon 线程，进程退出不阻塞；max_workers 限并发，避免研判堆积压垮网关。
_live_brief_pool = ThreadPoolExecutor(max_workers=3,
                                      thread_name_prefix="live-brief")


# ─── 多用户并发：每个 chat 一个单线程执行器 ─────────────────────────────────
# 轮询主循环只负责拉 update、分发、推进 offset，绝不亲自跑命令——否则一个用户
# 的 /analyze（LLM 最坏 1~3 分钟）会把整条轮询线程占死，其它用户的命令全部被
# 串行阻塞（这正是“多用户同时用就卡死”的根因）。
#
# 设计：按 chat_id 分桶，每桶一个 max_workers=1 的执行器。
#   - 跨用户并行：不同 chat 的命令在各自线程同时跑。
#   - 同一用户串行保序：同一 chat 的命令排队执行，保护 _pending_custom 两步流程
#     （选侧重→发文字依赖顺序），也避免单人连点触发多个并发精算。
# 执行器随用随建并缓存；数量受白名单人数天然限制。LLM 是网络 I/O，等待时释放
# GIL，故即便 1C1G 也能多人并行而不抢单核 CPU。
_chat_pools: dict[int, ThreadPoolExecutor] = {}
_chat_pools_lock = threading.Lock()


def _submit_for_chat(chat_id: int, fn, *args) -> None:
    """把某 chat 的一个 update 处理任务提交到它专属的单线程执行器。"""
    with _chat_pools_lock:
        pool = _chat_pools.get(chat_id)
        if pool is None:
            pool = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix=f"chat-{chat_id}")
            _chat_pools[chat_id] = pool

    def _run() -> None:
        try:
            fn(*args)
        except Exception:
            log.exception("处理 chat=%s 的 update 出错", chat_id)

    pool.submit(_run)


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
_ADMIN_ONLY_CMDS = {"leagues", "bookmakers", "add", "remove", "publish"}

API_BASE = f"{config.TELEGRAM_API}/bot{TOKEN}"

# 等待用户回复自定义侧重的会话状态：chat_id -> (fixture_id, effort)
# 用户点「✍️ 自定义侧重」并选完推理强度后置位，下一条非命令文本被当作侧重消费后清除。
_pending_custom: dict[int, tuple[int, str]] = {}

# 等待用户回复「场次号」的会话状态：chat_id -> 命令名（如 "review"/"analyze"）。
# 用户点菜单/直接发不带参数的命令后置位（force_reply 追问），下一条纯数字回复
# 被拼成 "<cmd> <fid>" 重新走命令分发后清除。免去手打命令+空格+号。
_pending_fixarg: dict[int, str] = {}
# 接受 force_reply 追问场次号的命令白名单（都吃单个 fixture_id 参数）
_FIXARG_CMDS = {"review", "analyze", "coverage", "export", "live", "unlive"}

# ─── /publish 发布到 Ghost 博客的会话状态（仅管理员）──────────────────────────
# 浏览态：chat_id -> {"date": 日期, "files": [文件名列表]}。/publish 选日期后置位，
# 供 pf:<idx> 把短索引映射回文件名（避免长文件名塞进 callback_data 64 字节）。
_publish_browse: dict[int, dict] = {}
# 待发布态：token -> {"path", "is_review", "title"}。选定报告后生成短 token，
# 后续 gt:/gv: 回调据 token 取回（避免路径塞进 callback_data）。
_publish_pending: dict[str, dict] = {}
# 等待管理员输入自定义标题：chat_id -> token。点「✍️ 自定义标题」后置位，
# 下一条文本被当作标题消费后清除。
_pending_pub_title: dict[int, str] = {}
_publish_lock = threading.Lock()

# 访客每日 /analyze 计数：持久化在 odds.db 的 analyze_usage 表（重启不清零，
# 跨北京日期自然分行）。管理员不计数、不受限。
def _today_cst() -> str:
    """北京时间的日期字符串（跨天判定用，与抓取时区一致）。"""
    from datetime import datetime, timezone, timedelta
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")


def _analyze_quota_left(chat_id: int) -> int:
    """返回该 chat 今日 /analyze 剩余次数。管理员或未设限时返回一个大数（视为无限）。"""
    limit = config.VISITOR_ANALYZE_DAILY_LIMIT
    if _is_admin(chat_id) or limit <= 0:
        return 1 << 30
    conn = db.get_conn()
    try:
        used = db.get_analyze_used(conn, chat_id, _today_cst())
    finally:
        conn.close()
    return max(0, limit - used)


def _analyze_consume(chat_id: int) -> None:
    """记一次 /analyze 使用（管理员/未设限时不计）。"""
    if _is_admin(chat_id) or config.VISITOR_ANALYZE_DAILY_LIMIT <= 0:
        return
    conn = db.get_conn()
    try:
        db.incr_analyze_used(conn, chat_id, _today_cst())
    finally:
        conn.close()


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


def edit_text(chat_id: int, message_id: int, text: str,
              reply_markup: dict | None = None) -> None:
    """编辑已发消息的文字（用于原地更新进度）。
    reply_markup 给定时一并替换内联键盘（用于多步流程的原地推进）。"""
    payload = {
        "chat_id": chat_id, "message_id": message_id,
        "text": text, "parse_mode": "HTML"}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    _post("editMessageText", payload)


def answer_callback(callback_id: str, text: str = "") -> None:
    _post("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})


# ─── 管理员告警（额度等运维信号）────────────────────────────────────────────
# 记录"已告警的日期"做去重，避免一天内反复推送同一类告警刷屏。
# key = 告警类别字符串, value = 已告警的北京日期（YYYY-MM-DD）
_alerted_on: dict[str, str] = {}


def alert_admins(text: str, dedup_key: str | None = None) -> None:
    """给所有管理员推一条运维告警。
    dedup_key 给定时，同一 key 当日只推一次（跨北京日期自然重置）。
    无管理员配置时退化为推给全体 ALLOWED，避免告警彻底没人收到。
    """
    if dedup_key is not None:
        today = _today_cst()
        if _alerted_on.get(dedup_key) == today:
            return
        _alerted_on[dedup_key] = today
    targets = ADMIN_CHAT_IDS or ALLOWED_CHAT_IDS
    for cid in targets:
        send(cid, text, plain=True)


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
        {"command": "export", "description": "导出某场全部盘口为 CSV"},
        {"command": "live", "description": "订阅某场走地实时播报：/live fixture_id"},
        {"command": "unlive", "description": "退订某场走地播报：/unlive fixture_id"},
        {"command": "lives", "description": "查看我当前订阅的走地比赛"},
        {"command": "leagues", "description": "联赛抓取开关面板"},
        {"command": "bookmakers", "description": "庄家抓取开关面板"},
        {"command": "status", "description": "当前启用了哪些联赛/庄家"},
        {"command": "add", "description": "加联赛：/add 关键词 搜了点 或 /add id season"},
        {"command": "remove", "description": "删除关注联赛"},
        {"command": "publish", "description": "把历史归档报告发布到 Ghost 博客（管理员）"},
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
LEAGUES_PER_PAGE = 10          # /leagues 每页联赛数（5 行 × 2）


def _leagues_keyboard(page: int = 0) -> dict:
    """联赛开关面板（分页）。每个按钮 callback=tl:<id>:<page>，翻转后停在本页；
    底部一行翻页按钮 lp:<page>。"""
    conn = db.get_conn()
    try:
        rows = db.list_leagues(conn)
    finally:
        conn.close()
    total = len(rows)
    pages = max(1, (total + LEAGUES_PER_PAGE - 1) // LEAGUES_PER_PAGE)
    page = max(0, min(page, pages - 1))
    chunk = rows[page * LEAGUES_PER_PAGE:(page + 1) * LEAGUES_PER_PAGE]

    buttons, line = [], []
    for lid, name, season, enabled in chunk:
        mark = "✅" if enabled else "⬜"
        line.append({"text": f"{mark} {name}",
                     "callback_data": f"tl:{lid}:{page}"})
        if len(line) >= config.TG_LEAGUES_PER_ROW:
            buttons.append(line)
            line = []
    if line:
        buttons.append(line)

    # 翻页行：‹ 上一页 | 页码 | 下一页 ›（首/末页对应按钮换成占位）
    nav = []
    nav.append({"text": "‹ 上一页", "callback_data": f"lp:{page - 1}"}
               if page > 0 else {"text": " ", "callback_data": "lp:noop"})
    nav.append({"text": f"{page + 1}/{pages}", "callback_data": "lp:noop"})
    nav.append({"text": "下一页 ›", "callback_data": f"lp:{page + 1}"}
               if page < pages - 1 else {"text": " ", "callback_data": "lp:noop"})
    buttons.append(nav)
    return {"inline_keyboard": buttons}


def _search_leagues_keyboard(results: list[tuple]) -> dict:
    """/add 关键词搜索结果键盘。results=[(league_id, 显示名, season)]，
    每个按钮 callback=ag:<id>:<season>，点击即添加并启用。"""
    buttons = []
    for lid, label, season in results:
        buttons.append([{"text": label,
                         "callback_data": f"ag:{lid}:{season}"}])
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
    "<b>赔率轮询 bot</b>\n"
    "👋 你已通过授权，直接在这个私聊里发命令即可使用（无需在群里 @ 我）。\n\n"
    "/fixtures — 过去 3 天 ~ 未来 3 天赛程（✅已开赛可复盘 / 🔵未来可精算）\n"
    "/coverage &lt;fixture_id&gt; — 看某场数据采集进度（10 节点抓了几个、缺哪些）\n"
    "/export &lt;fixture_id&gt; — 导出某场全部盘口为 CSV 文件\n"
    "/live &lt;fixture_id&gt; — 订阅走地实时播报（进行中有异动自动推送）\n"
    "/unlive &lt;fixture_id&gt; — 退订走地播报；/lives — 看我的订阅\n"
    "/analyze &lt;fixture_id&gt; — 先看基本面+盘口走势，再按钮选预设/自定义侧重跑SOP预测\n"
    "/review &lt;fixture_id&gt; — 对已结束的比赛做盘口复盘（盘口走势+实际比分）\n"
    "/status — 当前启用项\n"
)
# 管理员附加的配置类命令
_HELP_ADMIN_EXTRA = (
    "\n<b>管理员命令</b>\n"
    "/leagues — 联赛开关面板（分页，点 ✅/⬜ 切换，‹ › 翻页）\n"
    "/bookmakers — 庄家开关面板\n"
    "/add &lt;关键词&gt; — 搜联赛点按钮加（如 /add 瑞典）；或 /add &lt;id&gt; &lt;season&gt;\n"
    "/remove &lt;id&gt; — 删联赛\n"
    "/publish — 把 report/ 历史归档报告发布到 Ghost 博客（选日期→选报告→标题→可见性）\n"
)
HELP = _HELP_VISITOR + _HELP_ADMIN_EXTRA   # 兼容旧引用：完整版


def _help_for(chat_id: int) -> str:
    """按身份返回帮助：管理员看全部，访客只看查询/分析命令。"""
    return HELP if _is_admin(chat_id) else _HELP_VISITOR


def _cmd_status(chat_id: int) -> None:
    conn = db.get_conn()
    try:
        leagues = [config.league_label(lid, n)
                   for lid, n, _, e in db.list_leagues(conn) if e]
        bms = [n for _, n, e in db.list_bookmakers(conn) if e]
    finally:
        conn.close()
    send(chat_id,
         f"<b>启用联赛（{len(leagues)}）</b>\n" + "、".join(leagues) +
         f"\n\n<b>启用庄家（{len(bms)}）</b>\n" + "、".join(bms))


def _cmd_add(chat_id: int, args: list[str]) -> None:
    """三种用法：
      /add                      → 提示用法
      /add <关键词>             → 调 API 搜全球联赛，列按钮点选添加（推荐，不用记编号）
      /add <id> <season> [名称] → 已知编号时直接添加（旧用法保留）
    """
    if not args:
        send(chat_id, "用法：\n"
                      "① <b>/add 关键词</b> — 搜联赛点按钮添加（不用记编号）\n"
                      "　 例：/add 足协杯　/add 瑞典　/add China　/add FA Cup\n"
                      "　 （中文常用名会自动转英文搜；API 搜索本身只认英文）\n"
                      "② <b>/add &lt;id&gt; &lt;season&gt; [名称]</b> — 已知编号直接加\n"
                      "　 例：/add 207 2026 瑞超\n\n"
                      "（常用联赛也可直接 /leagues 翻页点开，无需 /add）")
        return

    # 旧用法：纯数字 id + season
    if args[0].isdigit() and len(args) >= 2 and args[1].isdigit():
        lid, season = int(args[0]), int(args[1])
        name = " ".join(args[2:]) if len(args) > 2 else None
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
        return

    # 关键词搜索：/add 瑞典 / /add sweden / /add 足协杯
    raw_kw = " ".join(args)
    # API search 只认英文：先查中文别名映射；命中则用英文词搜，并告知实际搜的词
    mapped = config.LEAGUE_SEARCH_ALIASES.get(raw_kw.strip())
    keyword = mapped or raw_kw
    note = f"（中文「{raw_kw}」→ 按英文「{keyword}」搜）\n" if mapped else ""
    send(chat_id, f"🔍 {note}正在搜索联赛「{keyword}」…")
    data = api_client.api_get("/leagues", {"search": keyword})
    resp = (data or {}).get("response", []) if data else []
    if not resp:
        has_cjk = any('一' <= c <= '鿿' for c in raw_kw)
        tip = ("\n⚠️ API 搜索只认<b>英文</b>，中文词常搜不到。"
               "试试国家英文名（China / Sweden / Japan）或联赛英文名（FA Cup）。"
               if has_cjk else
               "换个关键词试试（如 China / Sweden / FA Cup）。")
        send(chat_id, f"没搜到「{raw_kw}」。{tip}\n"
                      "若已知数字编号：/add &lt;id&gt; &lt;season&gt;")
        return

    # 整理结果：每项取 league_id、国家+名称、当前赛季（current=true，没有则取最大年份）
    results = []
    for item in resp[:24]:                      # 上限 24 个，避免按钮过多
        lg = item.get("league", {})
        country = item.get("country", {}).get("name", "")
        lid = lg.get("id")
        lname = lg.get("name", "")
        seasons = item.get("seasons", []) or []
        cur = next((s for s in seasons if s.get("current")), None)
        season = (cur or (seasons[-1] if seasons else {})).get("year")
        if lid and season:
            label = f"{lname}" + (f"（{country}）" if country else "")
            results.append((lid, label[:60], season))
    if not results:
        send(chat_id, f"搜到「{keyword}」但无可用赛季信息，请用 /add &lt;id&gt; &lt;season&gt; 手动添加。")
        return

    send(chat_id, f"搜到 {len(results)} 个联赛，点击即添加并启用：",
         _search_leagues_keyboard(results))


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


def _render_fixtures(view: str = "future", page: int = 0):
    """生成 /fixtures 的文本 + 内联键盘。view='future' 看未来可精算的比赛，
    view='past' 看已开赛可复盘的比赛。两个视图都按开球时间升序（最旧在上、
    最新在下）。比赛数超过 PAGE 时分页，page 为 0 起的页码。返回 (text, keyboard)。"""
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    # 过去 3 天 ~ 未来 3 天：过去的可 /review 复盘，未来的可 /analyze 精算
    start = (now - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = (now + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = db.get_conn()
    try:
        # 直接查，多带一列 league_name（不动 db.get_fixtures_between 的 4 列结构，
        # 那个被调度器解包复用）
        fixtures = conn.execute(
            "SELECT fixture_id, commence_utc, home_team, away_team, league_name, league_id "
            "FROM fixtures WHERE commence_utc BETWEEN ? AND ? "
            "ORDER BY commence_utc", (start, end)).fetchall()
    finally:
        conn.close()

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

    past = [f for f in fixtures if kicked_off(f[1])]
    future = [f for f in fixtures if not kicked_off(f[1])]

    # 切换按钮：高亮当前视图，点另一个切过去（切视图回到第 0 页）
    toggle_row = [
        {"text": ("🔵 未来(可精算)" if view == "future" else "🔵 未来"),
         "callback_data": "fx:future:0"},
        {"text": ("✅ 已开赛(可复盘)" if view == "past" else "✅ 已开赛"),
         "callback_data": "fx:past:0"},
    ]

    if not fixtures:
        return ("过去/未来 3 天暂无赛程（可能休赛期或赛程未拉取）",
                {"inline_keyboard": [toggle_row]})

    # 已开赛视图按开球时间降序：最新结束的在最前、落第 1 页（便于复盘刚结束的）。
    # 未来视图按开球时间升序：距现在最近的未来比赛在最前、落第 1 页（fixtures 查询本就升序）。
    if view == "past":
        all_rows = past[::-1]
        title = "已开赛（可 /review 复盘）"
    else:
        all_rows = future
        title = "未来（可 /analyze 精算）"

    # 不再硬性截断：超过 PAGE 场就分页
    PAGE = 20
    total = len(all_rows)
    pages = max(1, (total + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    rows = all_rows[page * PAGE:(page + 1) * PAGE]

    header = f"<b>{title}</b>　共 {total} 场"
    if pages > 1:
        header += f"（第 {page + 1}/{pages} 页）"

    lines = [header, "✅=已开赛可 /review 复盘　🔵=未来可 /analyze 精算",
             "👇 点下方按钮直接复盘/精算该场，无需输号"]
    keyboard = [toggle_row]
    if not rows:
        lines.append("（本视图暂无比赛，点下方按钮切换）")

    def _short(s: str, n: int = 12) -> str:
        s = s or "?"
        return s if len(s) <= n else s[:n - 1] + "…"

    for fid, commence, home, away, league, league_id in rows:
        kicked = kicked_off(commence)
        mark = "✅" if kicked else "🔵"
        label = config.league_label(league_id, league)
        lg = f"（{label}）" if label else ""
        lines.append(f"{mark} <code>{fid}</code> {to_cst(commence)}  "
                     f"{home} vs {away}{lg}")
        # 每场一行操作按钮：已开赛→复盘，未来→精算。带队名便于在按钮区分辨。
        vs = f"{_short(home)} vs {_short(away)}"
        if kicked:
            keyboard.append([{"text": f"🔬 复盘 {vs}",
                              "callback_data": f"fr:{fid}"}])
        else:
            keyboard.append([{"text": f"🎯 精算 {vs}",
                              "callback_data": f"fa:{fid}"}])

    if pages > 1:
        nav = []
        if page > 0:
            nav.append({"text": "⬅️ 上一页",
                        "callback_data": f"fx:{view}:{page - 1}"})
        if page < pages - 1:
            nav.append({"text": "下一页 ➡️",
                        "callback_data": f"fx:{view}:{page + 1}"})
        if nav:
            keyboard.append(nav)
    return "\n".join(lines), {"inline_keyboard": keyboard}


def _cmd_fixtures(chat_id: int) -> None:
    text, kb = _render_fixtures("future")
    send(chat_id, text, kb)


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


def _build_csv(fid: int):
    """查某场盘口快照，生成对齐旧 main.py 的 19 列 CSV 字符串。
    返回 (csv_str, meta)；无数据返回 (None, None)。meta 含 home/away/league/rows。

    ⚠️ 按 SOP「10 节点」去重：任务 C 每 15 分钟抓一次，同一节点会被重复采
    （曾见单场 3855 行 / 50 次快照），全量喂 LLM 会撑爆上下文窗（实测 prompt
    达 39 万 token → completion_tokens=0、finish_reason=stop 空内容）。故每个
    (节点 × 庄家 × market × 让球) 只取该节点最新一条，行数压到约 1/10，恰合 SOP 语义。
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
            "FROM ("
            "  SELECT *, ROW_NUMBER() OVER ("
            "    PARTITION BY node_label, bookmaker_id, market, handicap "
            "    ORDER BY snapshot_utc DESC) AS rn "
            "  FROM odds_history WHERE fixture_id=?"
            ") WHERE rn=1 "
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
    market_zh = {"h2h": "欧指", "ah": "亚盘", "ou": "大小球"}

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


# ─── 走地实时播报：订阅 / 退订 / 列表 / 推送 ─────────────────────────────────
_LIVE_MARKET_ZH = {"ah": "亚盘", "ou": "大小球", "h2h": "欧赔"}


def _cmd_live(chat_id: int, args: list[str]) -> None:
    """订阅某场走地实时播报。/live <fixture_id>"""
    if not args or not args[0].isdigit():
        send(chat_id, "用法：/live &lt;fixture_id&gt;（id 见 /fixtures）。"
                      "订阅后比赛进行中有显著异动会自动推送。")
        return
    fid = int(args[0])
    conn = db.get_conn()
    try:
        meta = db.get_fixture_meta(conn, fid)
        if not meta:
            send(chat_id, f"未找到 fixture {fid}，请先 /fixtures 查 id。")
            return
        # 限额校验（已订阅同一场不占新增名额）
        already = db.count_live_subs_for_chat(conn, chat_id)
        existing = {r[0] for r in db.list_live_subs_for_chat(conn, chat_id)}
        if fid not in existing and already >= config.LIVE_MAX_SUBS_PER_CHAT:
            send(chat_id, f"⚠️ 最多同时订阅 {config.LIVE_MAX_SUBS_PER_CHAT} 场走地，"
                          f"请先 /unlive 退订一些。")
            return
        home, away = meta[4], meta[5]
        db.add_live_sub(conn, chat_id, fid)
    finally:
        conn.close()
    send(chat_id, f"✅ 已订阅走地播报：{home} vs {away}\n"
                  f"比赛进行中每 {config.LIVE_MINUTES} 分钟检测一次，"
                  f"有进球/盘口线/水位/封盘异动才推送。结束自动退订。", plain=True)


def _cmd_unlive(chat_id: int, args: list[str]) -> None:
    """退订某场走地播报。/unlive <fixture_id>"""
    if not args or not args[0].isdigit():
        send(chat_id, "用法：/unlive &lt;fixture_id&gt;（看 /lives 查当前订阅）")
        return
    fid = int(args[0])
    conn = db.get_conn()
    try:
        ok = db.disable_live_sub(conn, chat_id, fid)
    finally:
        conn.close()
    send(chat_id, f"已退订 fixture {fid}。" if ok else f"你没有订阅 fixture {fid}。")


def _cmd_lives(chat_id: int) -> None:
    """列出本人当前订阅的走地比赛，每场带退订按钮。"""
    conn = db.get_conn()
    try:
        subs = db.list_live_subs_for_chat(conn, chat_id)
    finally:
        conn.close()
    if not subs:
        send(chat_id, "你当前没有订阅任何走地比赛。用 /live &lt;fixture_id&gt; 订阅。")
        return
    lines = ["你订阅的走地比赛（点按钮退订）："]
    buttons = []
    for (fid, home, away, league, commence) in subs:
        lg = f"（{league}）" if league else ""
        lines.append(f"<code>{fid}</code> {home or '?'} vs {away or '?'}{lg}")
        buttons.append([{"text": f"❌ 退订 {home or fid} vs {away or ''}".strip(),
                         "callback_data": f"ul:{fid}"}])
    send(chat_id, "\n".join(lines), {"inline_keyboard": buttons})


def _fmt_live_lines(rows: list[dict]) -> str:
    """把走地快照行格式化成简短盘口快报文本。"""
    out = []
    for r in rows:
        zh = _LIVE_MARKET_ZH.get(r["market"], r["market"])
        susp = "（封盘）" if r.get("suspended") else ""
        if r["market"] == "h2h":
            out.append(f"{zh}: 主 {r['home_water']} / 平 {r.get('draw_odds')} "
                       f"/ 客 {r['away_water']}{susp}")
        elif r["market"] == "ah":
            out.append(f"{zh}(让 {r['handicap']}): 主 {r['home_water']} "
                       f"/ 客 {r['away_water']}{susp}")
        else:  # ou
            out.append(f"{zh}({r['handicap']}): 大 {r['home_water']} "
                       f"/ 小 {r['away_water']}{susp}")
    return "\n".join(out)


def push_live_update(chat_id: int, fid: int, entry: dict,
                     rows: list[dict], deltas: list[str]) -> None:
    """走地异动推送：立即发【盘口快报】，LLM 一句话研判丢后台线程异步追发。
    供 scheduler.task_e_live_broadcast 后台调用。

    解耦要点：盘口快报(进球/水位/封盘)是确定性数据，必须秒级到达；LLM 研判最坏
    要 30s，绝不能卡住 1min 一轮的抓取循环。故本函数同步只做发快报，研判提交到
    _live_brief_pool 后立即返回；研判线程跑完再单独 send 一条 💡 消息。"""
    status = entry.get("fixture", {}).get("status", {})
    elapsed = status.get("elapsed")
    short = status.get("short") or ""
    teams = entry.get("teams", {})
    hg = teams.get("home", {}).get("goals")
    ag = teams.get("away", {}).get("goals")
    conn = db.get_conn()
    try:
        meta = db.get_fixture_meta(conn, fid)
    finally:
        conn.close()
    home, away = (meta[4], meta[5]) if meta else ("主队", "客队")
    score = f"{hg}-{ag}"

    # 阶段标注：加时/点球阶段明确写出，不再只有「第 N′」（点球阶段 elapsed 常为空）
    phase = config.LIVE_PHASE_ZH.get(short, "")
    if short == "P":
        time_line = f"🥅 点球大战  比分 {score}"
    elif short in ("ET", "BT"):
        mins = f"第 {elapsed}′" if elapsed is not None else ""
        time_line = f"⏱ {phase} {mins}  比分 {score}".replace("  ", " ").strip()
    else:
        mins = f"第 {elapsed}′" if elapsed is not None else (phase or "进行中")
        time_line = f"{mins}  比分 {score}"

    live_lines = _fmt_live_lines(rows)
    parts = [f"🔴 走地 | {home} vs {away}",
             time_line, "",
             "异动："]
    parts += [f"  {d}" for d in deltas]
    parts.append("")
    parts.append(live_lines)
    # ① 盘口快报：立即发，不等 LLM
    send(chat_id, "\n".join(parts), plain=True)

    # ② LLM 研判：丢后台线程，跑完再追发 💡。绝不阻塞抓取循环。
    _live_brief_pool.submit(_async_live_brief, chat_id, fid,
                            live_lines, deltas, home, away, elapsed, score)


def _async_live_brief(chat_id: int, fid: int, live_lines: str,
                       deltas: list[str], home: str, away: str,
                       elapsed, score: str) -> None:
    """后台线程：跑走地 LLM 研判，成功则单独追发一条 💡 消息。
    失败/未配置/超时静默跳过——盘口快报已先行送达，不影响主信息。"""
    try:
        brief = analyzer.live_brief(live_lines, deltas, home, away,
                                    elapsed, score)
    except Exception as e:
        log.warning("走地研判失败 fixture %d: %s", fid, e)
        return
    if brief and not brief.startswith(("LLM", "未配置")):
        send(chat_id, f"💡 {home} vs {away} 第 {elapsed}′：{brief}", plain=True)


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


def _md_to_tg(text: str) -> str:
    """把 Markdown 报告转成 TG 易读的纯文本（仅用于 TG 显示，不影响归档/发布）。
    - 去掉标题井号：'### 7. 最终精算结论' → '7. 最终精算结论'
    - 去掉加粗/斜体标记：'**亚盘判定**' → '亚盘判定'
    - 列表符号 '- ' / '* ' → '• '（保留缩进层级）
    - 去掉引用符号 '> '
    表格行（含 |）保持原样不动。
    """
    import re
    out = []
    for line in text.split("\n"):
        # 标题：行首 1~6 个 # + 空格 → 去掉
        line = re.sub(r"^\s{0,3}#{1,6}\s*", "", line)
        # 引用：行首 > → 去掉
        line = re.sub(r"^(\s*)>\s?", r"\1", line)
        # 列表符号：行首（可带缩进）- / * / + + 空格 → •
        m = re.match(r"^(\s*)[-*+]\s+(.*)$", line)
        if m and "|" not in line:
            line = f"{m.group(1)}• {m.group(2)}"
        # 加粗 **x** / __x__ → x
        line = re.sub(r"\*\*(.+?)\*\*", r"\1", line)
        line = re.sub(r"__(.+?)__", r"\1", line)
        # 斜体 *x* / _x_ → x（避开已处理的列表符；表格行不动）
        if "|" not in line:
            line = re.sub(r"(?<!\*)\*(?!\*)([^*\n]+?)\*(?!\*)", r"\1", line)
        # 残留的成对 ** 清掉
        line = line.replace("**", "")
        out.append(line)
    return "\n".join(out)


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


def _archive_report(meta: dict, report: str, suffix: str = "report",
                    chat_id: int | None = None) -> str | None:
    """把报告存一份 md。
    - 管理员（或 chat_id 缺省）：report/<开球日期>/<主队>_vs_<客队>_<suffix>.md
    - 访客：report/visitors/<chat_id>/<开球日期>/<...>.md
      每个访客有独立专用文件夹（用其 chat_id 命名），互不混淆、便于审计。
    suffix: 'report'（精算）或 'review'（复盘）。
    """
    import os
    try:
        date = (meta.get("kick_cst") or "")[:10] or "未知日期"
        teams = f"{meta['home']}_vs_{meta['away']}".replace(" ", "_")
        if chat_id is not None and not _is_admin(chat_id):
            out_dir = os.path.join("report", "visitors", str(chat_id), date)
        else:
            out_dir = os.path.join("report", date)
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"{teams}_{suffix}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(report)
        return path
    except Exception as e:
        log.warning("报告归档失败: %s", e)
        return None


def _publish_date_keyboard() -> dict | None:
    """扫描 report/ 下日期子目录，构造日期选择键盘（倒序，含当日报告数）。
    无任何报告时返回 None。回调 pd:<date>。"""
    import os
    base = "report"
    if not os.path.isdir(base):
        return None
    dates = []
    for name in os.listdir(base):
        d = os.path.join(base, name)
        # 仅取形如 2026-06-12 的日期目录（排除 visitors 等）
        if os.path.isdir(d) and len(name) == 10 and name[4] == "-":
            n = len([f for f in os.listdir(d) if f.endswith(".md")])
            if n:
                dates.append((name, n))
    if not dates:
        return None
    dates.sort(reverse=True)   # 最新日期在前
    rows = [[{"text": f"{date}（{n}）", "callback_data": f"pd:{date}"}]
            for date, n in dates]
    return {"inline_keyboard": rows}


def _publish_report_keyboard(chat_id: int, date: str) -> dict | None:
    """列某日期目录下所有 *.md，构造报告选择键盘。回调 pf:<idx>。
    把文件名列表暂存 _publish_browse[chat_id]，idx 映射回文件名。"""
    import os
    d = os.path.join("report", date)
    if not os.path.isdir(d):
        return None
    files = sorted(f for f in os.listdir(d) if f.endswith(".md"))
    if not files:
        return None
    with _publish_lock:
        _publish_browse[chat_id] = {"date": date, "files": files}
    rows = []
    for idx, fname in enumerate(files):
        # Mexico_vs_South_Africa_report.md → Mexico vs South Africa（复盘）
        is_review = fname.endswith("_review.md")
        stem = fname[:-len("_review.md")] if is_review else fname[:-len("_report.md")]
        label = stem.replace("_", " ")
        if is_review:
            label += "（复盘）"
        rows.append([{"text": label, "callback_data": f"pf:{idx}"}])
    return {"inline_keyboard": rows}


def _publish_title_keyboard(token: str) -> dict:
    """标题模式选择键盘。回调 gt:<token>:preset|custom|cancel。"""
    return {"inline_keyboard": [[
        {"text": "📋 预设标题", "callback_data": f"gt:{token}:preset"},
        {"text": "✍️ 自定义", "callback_data": f"gt:{token}:custom"},
        {"text": "❌ 取消", "callback_data": f"gt:{token}:cancel"},
    ]]}


def _publish_visibility_keyboard(token: str) -> dict:
    """可见性选择键盘。回调 gv:<token>:public|members|cancel。
    无 Stripe 方案：付费内容用 members 级（手动白名单），不提供 paid（paid 需 Stripe）。"""
    return {"inline_keyboard": [[
        {"text": "🌐 公开（全文免费）", "callback_data": f"gv:{token}:public"},
        {"text": "🔒 会员（付费解锁）", "callback_data": f"gv:{token}:members"},
    ], [
        {"text": "❌ 取消", "callback_data": f"gv:{token}:cancel"},
    ]]}


def _cmd_publish(chat_id: int, args: list[str]) -> None:
    """从 report/ 历史归档里选一份报告发布到 Ghost 博客（仅管理员，权限已在分发处校验）。"""
    if not ghost_publish.available():
        send(chat_id, "未配置 Ghost 发布（.env 缺 GHOST_ADMIN_API_KEY / "
                      "GHOST_ADMIN_API_URL）。配好后重启 bot 即可用 /publish。")
        return
    kb = _publish_date_keyboard()
    if kb is None:
        send(chat_id, "report/ 下暂无归档报告。先用 /analyze 或 /review 生成。")
        return
    send(chat_id, "📤 发布到博客 —— 选择报告日期：", kb)


def _publish_do(chat_id: int, message_id: int, token: str, visibility: str) -> None:
    """取缓存 → 转换 → 调 Ghost 发文 → 回链接/错误。消费后清缓存。"""
    with _publish_lock:
        info = _publish_pending.get(token)
    if not info:
        edit_text(chat_id, message_id, "⏳ 会话已过期，请重新 /publish。")
        return
    import os
    try:
        with open(info["path"], encoding="utf-8") as f:
            md = f.read()
    except OSError as e:
        edit_text(chat_id, message_id, f"❌ 读取报告失败：{e}")
        with _publish_lock:
            _publish_pending.pop(token, None)
        return

    edit_text(chat_id, message_id, "⏳ 正在发布到 Ghost…")
    try:
        title, html, excerpt, slug, meta_title, meta_description, seo_err = \
            ghost_publish.report_to_post(
                md, title=info.get("title"), is_review=info["is_review"])
        # LLM 概括 SEO 文案失败：先告知原因（已自动回退到固定模板，发布不中断）
        if seo_err:
            send(chat_id, f"⚠️ SEO 文案 LLM 概括失败，已回退默认模板：\n{seo_err}",
                 plain=True)
        post = ghost_publish.create_post(
            title, html, status="published", visibility=visibility,
            custom_excerpt=excerpt, slug=slug,
            meta_title=meta_title, meta_description=meta_description)
    except ghost_publish.GhostError as e:
        edit_text(chat_id, message_id, f"❌ 发布失败：{e}")
        return
    finally:
        with _publish_lock:
            _publish_pending.pop(token, None)

    url = post.get("url", "")
    vis_label = {"public": "公开全文", "members": "会员付费解锁"}.get(
        visibility, visibility)
    tip = ""
    if visibility == "members":
        tip = "\n\n💡 付费读者付款后，到 Ghost 后台 Members → New member 加他邮箱即可解锁。"
    edit_text(chat_id, message_id,
              f"✅ 已发布（{vis_label}）：{title}\n{url}{tip}")

    # 发布成功后：若配置了广播目标（群/频道），弹勾选键盘让管理员决定是否通知、通知谁。
    targets = config.TELEGRAM_BROADCAST_TARGETS
    if targets and url:
        bc_token = uuid.uuid4().hex[:12]
        with _publish_lock:
            _broadcast_pending[bc_token] = {
                "title": title, "url": url, "vis_label": vis_label,
                "selected": set(),
            }
        send(chat_id,
             "📢 是否把这篇通知到群聊/频道？勾选目标后点「发送通知」：",
             _broadcast_keyboard(bc_token))


# ─── /publish 成功后广播到群/频道 ───────────────────────────────────────────
# 待广播态：token -> {"title","url","vis_label","selected": set(目标索引)}。
# 选定目标的勾选状态存在 selected 里，bx: 回调切换、发送时据此推送。
_broadcast_pending: dict[str, dict] = {}


def _broadcast_keyboard(token: str) -> dict:
    """构造广播目标勾选键盘。每个目标一行可切换（✅/⬜），底部发送/取消。
    回调：bx:<token>:<idx> 切换某目标；bx:<token>:send 发送；bx:<token>:cancel 取消。"""
    with _publish_lock:
        info = _broadcast_pending.get(token)
    selected = info["selected"] if info else set()
    rows = []
    for idx, (label, _cid) in enumerate(config.TELEGRAM_BROADCAST_TARGETS):
        mark = "✅" if idx in selected else "⬜"
        rows.append([{"text": f"{mark} {label}",
                      "callback_data": f"bx:{token}:{idx}"}])
    rows.append([
        {"text": "📤 发送通知", "callback_data": f"bx:{token}:send"},
        {"text": "🚫 不通知", "callback_data": f"bx:{token}:cancel"},
    ])
    return {"inline_keyboard": rows}


def _broadcast_do(chat_id: int, message_id: int, token: str) -> None:
    """按勾选的目标推送文章链接。汇总成功/失败回报给管理员。"""
    with _publish_lock:
        info = _broadcast_pending.pop(token, None)
    if not info:
        edit_text(chat_id, message_id, "⏳ 广播会话已过期。")
        return
    selected = info["selected"]
    if not selected:
        edit_text(chat_id, message_id, "（未勾选任何目标，已取消通知。）")
        return
    text = (f"📰 新文章发布\n<b>{info['title']}</b>\n{info['url']}")
    ok, fail = [], []
    for idx in sorted(selected):
        if idx >= len(config.TELEGRAM_BROADCAST_TARGETS):
            continue
        label, cid = config.TELEGRAM_BROADCAST_TARGETS[idx]
        mid = send(cid, text)
        (ok if mid is not None else fail).append(label)
    summary = f"✅ 已通知：{'、'.join(ok)}" if ok else ""
    if fail:
        summary += (("\n" if summary else "")
                    + f"❌ 失败：{'、'.join(fail)}（确认机器人已加入该群/频道并有发言权）")
    edit_text(chat_id, message_id, summary or "未发送。")


def _effort_keyboard(chat_id: int, mode: str, fid: int) -> dict:
    """构造推理强度选择键盘。mode='p'(预设)/'c'(自定义)。
    管理员显示全部 4 档；访客仅 config.LLM_EFFORT_VISITOR_ALLOWED（低/中）。
    回调格式 ae:<mode>:<fid>:<effort>。
    """
    admin = _is_admin(chat_id)
    row = []
    for eff, label in config.LLM_EFFORT_LABELS.items():
        if not admin and eff not in config.LLM_EFFORT_VISITOR_ALLOWED:
            continue
        row.append({"text": label, "callback_data": f"ae:{mode}:{fid}:{eff}"})
    return {"inline_keyboard": [row]}


def _cmd_analyze(chat_id: int, args: list[str]) -> None:
    """第一步：展示某场基本面 + 盘口走势预览，附【开始SOP精算】按钮。

    拆分为两步——本命令只读库/拉基本面做展示，不跑 LLM；
    用户点按钮（callback az:<fid>）才真正跑 SOP（见 _run_sop）。
    """
    from . import fundamentals
    if not args or not args[0].isdigit():
        send(chat_id, "用法：/analyze &lt;fixture_id&gt;（id 见 /fixtures）")
        return
    # 访客每日 /analyze 限额（管理员不限）。无额度则提前拒绝，不浪费 API 拉基本面。
    if _analyze_quota_left(chat_id) <= 0:
        send(chat_id, f"⛔ 你今日的精算次数已用完（每日上限 "
                      f"{config.VISITOR_ANALYZE_DAILY_LIMIT} 次），请明天再试。")
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
                  "选完后再选推理强度（低/中/高/超高）。\n"
                  "（gpt-5.5 推理较慢，约 1~3 分钟；强度越高越慢）", kb)


def _run_sop(chat_id: int, fid: int, extra_instruction: str = "",
             effort: str = "") -> None:
    """第二步：真正跑 SOP 精算。
    extra_instruction 非空时为用户自定义侧重（由 ✍️ 自定义触发）。
    effort 为推理强度（low/medium/high/xhigh），透传给 analyzer。
    """
    from . import fundamentals
    if not analyzer.available():
        send(chat_id, "未配置 LLM（.env 缺 LLM_BASE_URL / LLM_API_KEY），无法精算。")
        return
    csv_str, meta = _build_csv(fid)
    if not csv_str:
        send(chat_id, f"fixture {fid} 暂无盘口数据，无法分析")
        return

    # 访客每日限额权威卡点：确认有数据、真正发起 LLM 前才检查并计数。
    if _analyze_quota_left(chat_id) <= 0:
        send(chat_id, f"⛔ 你今日的精算次数已用完（每日上限 "
                      f"{config.VISITOR_ANALYZE_DAILY_LIMIT} 次），请明天再试。")
        return
    _analyze_consume(chat_id)
    left = _analyze_quota_left(chat_id)
    if not _is_admin(chat_id) and config.VISITOR_ANALYZE_DAILY_LIMIT > 0:
        send(chat_id, f"（本次精算已计入，今日剩余 {left} 次）")

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

    # 两阶段：先用轻量模型把原始基本面分析成研判，再喂主 SOP（失败/超时回退原始）。
    # 仅当基本面确有数据时才预处理；上游拉取已失败则跳过。
    if funds and not funds.startswith("（基本面"):
        send(chat_id, "🧠 正在分析基本面（轻量模型预处理）…")
        fund_brief, ok = analyzer.analyze_fundamentals(
            funds, meta["home"], meta["away"], meta["league"])
        if ok:
            funds = ("（以下为基本面方法论研判：原始数据已由轻量模型按"
                     "国家队/赛事情境/大小球规则预处理）\n\n" + fund_brief)
        else:
            funds = ("⚠️ 基本面预处理失败，已回退原始数据。\n\n" + funds)

    # 流式精算 + 原地进度播报
    tag = "✍️自定义" if extra_instruction.strip() else "🎯预设"
    eff_label = config.LLM_EFFORT_LABELS.get(effort, "")
    if eff_label:
        tag += f"·{eff_label}"
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
                                      extra_instruction, effort):
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
    _send_long(chat_id, _md_to_tg(report))   # TG 显示纯文本；归档仍用原始 report
    path = _archive_report(meta, report, chat_id=chat_id)
    if path:
        send(chat_id, f"📁 报告已归档：{path}")


def _cmd_review(chat_id: int, args: list[str]) -> None:
    """复盘第一步：校验 + 弹推理强度选择键盘（ae:r:<fid>:<effort>）。
    选完强度才真正开跑（见 _run_review）。强度同时用于盲推与对照两遍。"""
    if not args or not args[0].isdigit():
        send(chat_id, "用法：/review &lt;fixture_id&gt;（对已结束的比赛复盘）")
        return
    if not analyzer.available():
        send(chat_id, "未配置 LLM（.env 缺 LLM_BASE_URL / LLM_API_KEY），无法复盘。")
        return
    fid = int(args[0])
    # 盘口数据校验（读库，无 API 成本）；是否结束的校验留给 _run_review（它要拉结果）
    csv_str, _ = _build_csv(fid)
    if not csv_str:
        send(chat_id, f"fixture {fid} 暂无盘口数据，无法复盘")
        return
    send(chat_id, "🔬 复盘（盲推 + 对照两遍）：请选择推理强度\n"
                  "低/中=快、省额度；高/超高=更慢更深。该强度同时用于两遍。",
         _effort_keyboard(chat_id, "r", fid))


def _run_review(chat_id: int, fid: int, effort: str = "") -> None:
    """对已结束的比赛做【正向盲推 + 对照】复盘：
    第一遍只喂盘口走势(不给比分)正向跑 SOP 得预判 → 第二遍揭晓真实比分做对照归因。
    两遍各自实时播报进度，最后两份报告都发回并归档。
    effort 为推理强度，同时用于盲推与对照两遍。"""
    from . import api_client
    if not analyzer.available():
        send(chat_id, "未配置 LLM（.env 缺 LLM_BASE_URL / LLM_API_KEY），无法复盘。")
        return
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
                                           meta["away"], meta["league"],
                                           effort):
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
    _send_long(chat_id, _md_to_tg("🔮 第一步·盲推预判\n\n" + forecast))

    # ── 基本面（仅供第二遍对照归因，盲推刻意不看）──
    # 两阶段：拉原始基本面 → 轻量模型预处理成研判 → 传给对照复盘做归因（失败回退原始/空）。
    from . import fundamentals
    fund_brief = ""
    try:
        conn = db.get_conn()
        try:
            raw_funds = fundamentals.build_fundamentals(conn, fid)
        finally:
            conn.close()
    except Exception as e:
        log.warning("复盘基本面拉取失败: %s", e)
        raw_funds = ""
    if raw_funds and not raw_funds.startswith("（基本面"):
        send(chat_id, "🧠 正在分析基本面（供对照归因）…")
        brief, ok = analyzer.analyze_fundamentals(
            raw_funds, meta["home"], meta["away"], meta["league"])
        # 成功用研判；失败回退原始数据（仍可供归因，只是未经方法论提炼）
        fund_brief = brief if ok else raw_funds

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
                                     meta["home"], meta["away"], meta["league"],
                                     effort, fund_brief):
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
    _send_long(chat_id, _md_to_tg(report))   # TG 纯文本；归档 full 仍用原始 md
    # 归档：盲推预判 + 对照复盘合并存一份
    full = ("# 第一步·盲推预判（不看比分）\n\n" + forecast
            + "\n\n---\n\n# 第二步·对照复盘\n\n" + report)
    path = _archive_report(meta, full, suffix="review", chat_id=chat_id)
    if path:
        send(chat_id, f"📁 复盘已归档：{path}")


def _prompt_fixarg(chat_id: int, cmd: str) -> None:
    """命令未带场次号时，用 force_reply 追问，并置位 _pending_fixarg。
    用户随后只需回一条纯数字（场次号），免去手打命令+空格+号。"""
    _pending_fixarg[chat_id] = cmd
    label = {
        "review": "复盘", "analyze": "精算", "coverage": "查采集进度",
        "export": "导出CSV", "live": "订阅走地", "unlive": "退订走地",
    }.get(cmd, cmd)
    send(chat_id,
         f"🔢 请回复要{label}的场次号（fixture_id，如 1562344）。\n"
         f"不知道号？发 /fixtures 看赛程列表。（发 /cancel 取消）",
         {"force_reply": True, "input_field_placeholder": "输入场次号…"})


def handle_message(msg: dict) -> None:
    chat_id = msg.get("chat", {}).get("id")
    text = msg.get("text", "").strip()
    if chat_id is None or not text:
        return
    if not _authorized(chat_id):
        send(chat_id, f"⛔ 未授权。你的 chat_id 是 <code>{chat_id}</code>，"
                      f"把它加入服务器 .env 的 TELEGRAM_ALLOWED_CHAT_IDS 即可。")
        return

    # 若该 chat 正在等待「自定义博客标题」输入，优先消费这条消息
    if chat_id in _pending_pub_title:
        token = _pending_pub_title.pop(chat_id)
        if text.lstrip("/").lower() in ("cancel", "取消") or text.startswith("/"):
            with _publish_lock:
                _publish_pending.pop(token, None)
            send(chat_id, "已取消发布。")
            if text.startswith("/"):
                pass  # 落到下方按新命令处理
            else:
                return
        else:
            with _publish_lock:
                info = _publish_pending.get(token)
            if not info:
                send(chat_id, "⏳ 会话已过期，请重新 /publish。")
                return
            info["title"] = text.strip()
            send(chat_id, f"标题已设：{text.strip()}\n选择可见性以发布：",
                 _publish_visibility_keyboard(token))
            return

    # 若该 chat 正在等待「自定义侧重」输入，优先消费这条消息
    if chat_id in _pending_custom:
        fid, eff = _pending_custom.pop(chat_id)
        if text.lstrip("/").lower() in ("cancel", "取消"):
            send(chat_id, "已取消自定义精算。")
            return
        if text.startswith("/"):
            # 用户改发了别的命令，放弃自定义、按正常命令处理
            send(chat_id, "（已取消上一条自定义精算输入，改为执行新命令）")
        else:
            send(chat_id, f"收到自定义侧重，开始精算 fixture {fid} …")
            try:
                _run_sop(chat_id, fid, extra_instruction=text, effort=eff)
            except Exception:
                log.exception("自定义 SOP 精算执行出错")
                send(chat_id, "精算执行出错，请查看服务器日志。")
            return

    # 若该 chat 正在等待「场次号」输入（点了不带参数的命令后），优先消费这条
    if chat_id in _pending_fixarg:
        pend_cmd = _pending_fixarg.pop(chat_id)
        if text.lstrip("/").lower() in ("cancel", "取消"):
            send(chat_id, "已取消。")
            return
        if text.startswith("/"):
            # 用户改发了别的命令，放弃本次追问、按新命令正常处理（落到下方分发）
            pass
        else:
            fid_str = text.strip().split()[0] if text.strip() else ""
            if not fid_str.isdigit():
                send(chat_id, f"「{text.strip()}」不是有效的场次号（应为纯数字，如 1562344）。"
                              f"请重发命令再试。")
                return
            # 拼成「<cmd> <fid>」重走分发，复用各命令既有逻辑（含权限/限额校验）
            text = f"/{pend_cmd} {fid_str}"

    parts = text.split()
    cmd = parts[0].lower().lstrip("/")
    args = parts[1:]

    # 配置类命令仅管理员可用；访客只能查询/精算/复盘
    if cmd in _ADMIN_ONLY_CMDS and not _is_admin(chat_id):
        send(chat_id, "⛔ 该命令仅管理员可用。你可以用 /fixtures 选比赛，"
                      "再用 /analyze 或 /review 分析。发 /help 看可用命令。")
        return

    # 吃场次号的命令若未带参数：用 force_reply 追问，免手打号（点菜单即用）
    if cmd in _FIXARG_CMDS and not args:
        _prompt_fixarg(chat_id, cmd)
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
    elif cmd == "export":
        _cmd_export(chat_id, args)
    elif cmd == "live":
        _cmd_live(chat_id, args)
    elif cmd == "unlive":
        _cmd_unlive(chat_id, args)
    elif cmd == "lives":
        _cmd_lives(chat_id)
    elif cmd == "analyze":
        _cmd_analyze(chat_id, args)
    elif cmd == "review":
        _cmd_review(chat_id, args)
    elif cmd == "publish":
        _cmd_publish(chat_id, args)
    else:
        send(chat_id, "未知命令，发 /help 看用法")


def _handle_publish_callback(cb_id: str, data: str, chat_id: int,
                             message_id: int) -> None:
    """处理 /publish 系列回调：pd: 选日期 / pf: 选报告 / gt: 选标题模式 / gv: 选可见性。"""
    import os

    # 立即先确认回调，清掉 TG 客户端的加载圈（服务器到 Telegram 跨境延迟高，
    # 若等扫目录/调 Ghost 完再确认，客户端会超时显示"点了没反应"，需点第二次）。
    # 同一 cb_id 再次 answer 是无害空操作，故后续分支保留带文案的 answer 不冲突。
    answer_callback(cb_id)

    # pd:<date> —— 列该日期的报告
    if data.startswith("pd:"):
        date = data[3:]
        kb = _publish_report_keyboard(chat_id, date)
        answer_callback(cb_id)
        if kb is None:
            edit_text(chat_id, message_id, f"{date} 下无报告。")
        else:
            edit_text(chat_id, message_id, f"📅 {date} —— 选择报告：", kb)
        return

    # pf:<idx> —— 选定报告，弹标题模式
    if data.startswith("pf:"):
        try:
            idx = int(data[3:])
        except ValueError:
            answer_callback(cb_id, "参数错误")
            return
        with _publish_lock:
            browse = _publish_browse.get(chat_id)
        if not browse or idx >= len(browse["files"]):
            answer_callback(cb_id, "会话已过期，请重新 /publish")
            edit_text(chat_id, message_id, "⏳ 会话已过期，请重新 /publish。")
            return
        fname = browse["files"][idx]
        path = os.path.join("report", browse["date"], fname)
        token = uuid.uuid4().hex[:12]
        with _publish_lock:
            _publish_pending[token] = {
                "path": path,
                "is_review": fname.endswith("_review.md"),
                "title": None,
            }
        answer_callback(cb_id)
        label = fname.replace("_", " ").rsplit(".", 1)[0]
        edit_text(chat_id, message_id,
                  f"已选：{label}\n选择标题方式：",
                  _publish_title_keyboard(token))
        return

    # gt:<token>:preset|custom|cancel
    if data.startswith("gt:"):
        try:
            _, token, mode = data.split(":")
        except ValueError:
            answer_callback(cb_id, "参数错误")
            return
        with _publish_lock:
            exists = token in _publish_pending
        if not exists:
            answer_callback(cb_id, "会话已过期")
            edit_text(chat_id, message_id, "⏳ 会话已过期，请重新 /publish。")
            return
        if mode == "cancel":
            with _publish_lock:
                _publish_pending.pop(token, None)
            answer_callback(cb_id, "已取消")
            edit_text(chat_id, message_id, "❌ 已取消发布。")
            return
        if mode == "custom":
            _pending_pub_title[chat_id] = token
            answer_callback(cb_id)
            edit_text(chat_id, message_id,
                      "✍️ 请发一条消息作为文章标题（发 /取消 放弃）。")
            return
        # preset
        answer_callback(cb_id)
        edit_text(chat_id, message_id, "选择可见性以发布：",
                  _publish_visibility_keyboard(token))
        return

    # gv:<token>:public|members|cancel
    if data.startswith("gv:"):
        try:
            _, token, vis = data.split(":")
        except ValueError:
            answer_callback(cb_id, "参数错误")
            return
        if vis == "cancel":
            with _publish_lock:
                _publish_pending.pop(token, None)
            answer_callback(cb_id, "已取消")
            edit_text(chat_id, message_id, "❌ 已取消发布。")
            return
        answer_callback(cb_id, "发布中…")
        _publish_do(chat_id, message_id, token, vis)
        return


def handle_callback(cb: dict) -> None:
    """处理内联按钮点击：
    tl:<league_id>[:<page>] 联赛开关 / lp:<page> 联赛翻页 /
    ag:<league_id>:<season> 搜索结果添加 / tb:<bookmaker_id> 庄家开关 /
    az:<fixture_id> 预设精算 / ac:<fixture_id> 自定义侧重 /
    ae:<mode>:<fixture_id>:<effort> 选推理强度后执行（mode=p预设/c自定义/r复盘）
    """
    cb_id = cb.get("id")
    data = cb.get("data", "")
    msg = cb.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    message_id = msg.get("message_id")
    if not _authorized(chat_id):
        answer_callback(cb_id, "未授权")
        return

    # ── /fixtures 每场的「复盘/精算」直达按钮（访客可点，复用既有命令逻辑）──
    # fr:<fid> 复盘  fa:<fid> 精算。直接喂 fid 给 _cmd_review/_cmd_analyze，
    # 走它们原有的校验→弹强度/预览键盘流程，省去手输场次号。
    if data.startswith(("fr:", "fa:")):
        fid_str = data[3:]
        if not fid_str.isdigit():
            answer_callback(cb_id, "场次号错误")
            return
        answer_callback(cb_id)
        if data.startswith("fr:"):
            _cmd_review(chat_id, [fid_str])
        else:
            _cmd_analyze(chat_id, [fid_str])
        return

    # ── /publish 发布到博客的回调（pd:/pf:/gt:/gv:，仅管理员）──
    if data.startswith(("pd:", "pf:", "gt:", "gv:")):
        if not _is_admin(chat_id):
            answer_callback(cb_id, "仅管理员可发布")
            return
        _handle_publish_callback(cb_id, data, chat_id, message_id)
        return

    # ── 发布成功后广播到群/频道（bx:，仅管理员）──
    # 格式 bx:<token>:<idx> 切换目标勾选 / bx:<token>:send 发送 / bx:<token>:cancel 取消
    if data.startswith("bx:"):
        if not _is_admin(chat_id):
            answer_callback(cb_id, "仅管理员可操作")
            return
        try:
            _, token, action = data.split(":")
        except ValueError:
            answer_callback(cb_id, "参数错误")
            return
        if action == "cancel":
            with _publish_lock:
                _broadcast_pending.pop(token, None)
            answer_callback(cb_id, "已跳过通知")
            edit_text(chat_id, message_id, "🚫 未通知群聊/频道。")
            return
        if action == "send":
            answer_callback(cb_id, "发送中…")
            _broadcast_do(chat_id, message_id, token)
            return
        # 切换某目标的勾选状态
        with _publish_lock:
            info = _broadcast_pending.get(token)
            if info is None:
                answer_callback(cb_id, "会话已过期")
                edit_text(chat_id, message_id, "⏳ 广播会话已过期。")
                return
            if action.isdigit():
                idx = int(action)
                if idx in info["selected"]:
                    info["selected"].discard(idx)
                else:
                    info["selected"].add(idx)
        answer_callback(cb_id)
        edit_markup(chat_id, message_id, _broadcast_keyboard(token))
        return

    # /fixtures 过去/未来切换 + 翻页按钮（访客可点）。格式 fx:<view>[:<page>]
    if data.startswith("fx:"):
        parts = data.split(":")
        view = parts[1] if len(parts) > 1 and parts[1] in ("past", "future") else "future"
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        text, kb = _render_fixtures(view, page)
        answer_callback(cb_id)
        edit_text(chat_id, message_id, text, kb)
        return

    # 走地退订按钮（访客可点，退自己的订阅）
    if data.startswith("ul:"):
        fid = int(data[3:])
        conn = db.get_conn()
        try:
            ok = db.disable_live_sub(conn, chat_id, fid)
        finally:
            conn.close()
        answer_callback(cb_id, "已退订" if ok else "未找到该订阅")
        # 刷新列表（点完即更新这条消息）
        conn = db.get_conn()
        try:
            subs = db.list_live_subs_for_chat(conn, chat_id)
        finally:
            conn.close()
        if subs:
            lines = ["你订阅的走地比赛（点按钮退订）："]
            buttons = []
            for (sfid, home, away, league, commence) in subs:
                lg = f"（{league}）" if league else ""
                lines.append(f"<code>{sfid}</code> {home or '?'} vs {away or '?'}{lg}")
                buttons.append([{"text": f"❌ 退订 {home or sfid} vs {away or ''}".strip(),
                                 "callback_data": f"ul:{sfid}"}])
            edit_text(chat_id, message_id, "\n".join(lines))
            edit_markup(chat_id, message_id, {"inline_keyboard": buttons})
        else:
            edit_text(chat_id, message_id, "已全部退订，当前无走地订阅。")
            edit_markup(chat_id, message_id, {"inline_keyboard": []})
        return

    # 预设精算按钮：不直接跑，先弹推理强度选择（ae:p:<fid>:<effort>）
    if data.startswith("az:"):
        fid = int(data[3:])
        answer_callback(cb_id, "请选择推理强度")
        edit_markup(chat_id, message_id, {"inline_keyboard": []})
        send(chat_id, "🎯 预设精算：请选择推理强度\n"
                      "低/中=快、省额度；高/超高=更慢更深（超高约数分钟）。",
             _effort_keyboard(chat_id, "p", fid))
        return

    # 自定义侧重按钮：也先选推理强度（ae:c:<fid>:<effort>），选完再要侧重文字
    if data.startswith("ac:"):
        fid = int(data[3:])
        answer_callback(cb_id, "请选择推理强度")
        edit_markup(chat_id, message_id, {"inline_keyboard": []})
        send(chat_id, "✍️ 自定义侧重：先选推理强度，下一步再发你的侧重要求。",
             _effort_keyboard(chat_id, "c", fid))
        return

    # 推理强度选定：ae:<mode>:<fid>:<effort>
    #   mode=p 预设精算→直接跑；c 自定义→置位待输入侧重文字；r 复盘→直接跑两遍
    if data.startswith("ae:"):
        try:
            _, mode, sfid, eff = data.split(":")
            fid = int(sfid)
        except ValueError:
            answer_callback(cb_id, "参数错误")
            return
        # 权限校验：访客不能选超出白名单的高强度（防伪造回调）
        if eff not in config.LLM_EFFORT_LABELS or (
                not _is_admin(chat_id)
                and eff not in config.LLM_EFFORT_VISITOR_ALLOWED):
            answer_callback(cb_id, "该强度不可用")
            return
        edit_markup(chat_id, message_id, {"inline_keyboard": []})
        eff_label = config.LLM_EFFORT_LABELS[eff]
        if mode == "c":
            answer_callback(cb_id, f"强度：{eff_label}，请回复侧重要求")
            _pending_custom[chat_id] = (fid, eff)
            send(chat_id,
                 f"✍️ 强度已设【{eff_label}】。请发一条消息，描述对 fixture {fid} "
                 "的精算侧重要求\n"
                 "例：「重点分析临场④异动」「忽略基本面只看盘口资金流」「给保守口径」。\n"
                 "（直接发文字即可；发 /cancel 取消）",
                 {"force_reply": True, "input_field_placeholder": "输入精算侧重…"})
        elif mode == "r":
            answer_callback(cb_id, f"强度：{eff_label}，已开始复盘…")
            try:
                _run_review(chat_id, fid, effort=eff)
            except Exception:
                log.exception("复盘执行出错")
                send(chat_id, "复盘执行出错，请查看服务器日志。")
        else:
            answer_callback(cb_id, f"强度：{eff_label}，已开始精算…")
            try:
                _run_sop(chat_id, fid, effort=eff)
            except Exception:
                log.exception("SOP 精算执行出错")
                send(chat_id, "精算执行出错，请查看服务器日志。")
        return

    # 配置类按钮（联赛/庄家开关、翻页、搜索添加）仅管理员可点
    if data.startswith(("tl:", "tb:", "lp:", "ag:")) and not _is_admin(chat_id):
        answer_callback(cb_id, "仅管理员可改配置")
        return

    conn = db.get_conn()
    try:
        if data.startswith("tl:"):
            # tl:<id> 或 tl:<id>:<page>（带页码则翻转后停在该页）
            rest = data[3:].split(":")
            lid = int(rest[0])
            page = int(rest[1]) if len(rest) > 1 else 0
            new = db.toggle_league(conn, lid)
            answer_callback(cb_id, "已启用" if new else "已停用")
            edit_markup(chat_id, message_id, _leagues_keyboard(page))
        elif data.startswith("lp:"):
            arg = data[3:]
            if arg == "noop":
                answer_callback(cb_id)
            else:
                answer_callback(cb_id)
                edit_markup(chat_id, message_id, _leagues_keyboard(int(arg)))
        elif data.startswith("ag:"):
            # ag:<league_id>:<season> 搜索结果点选 → 添加并启用
            _, sid, sseason = data.split(":")
            lid, season = int(sid), int(sseason)
            ld = api_client.api_get("/leagues", {"id": lid})
            lresp = (ld or {}).get("response", [])
            name = lresp[0]["league"]["name"] if lresp else f"League {lid}"
            db.add_league(conn, lid, name, season)
            answer_callback(cb_id, f"已添加 {name}")
            send(chat_id, f"✅ 已添加并启用：<b>{name}</b> "
                          f"(id={lid}, season={season})，下次抓取生效。")
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
    # 启动时打印实际读到的广播目标，便于排查 /publish 不弹通知按钮：
    # 若这里是 0 个，说明 .env 的 TELEGRAM_BROADCAST_TARGETS 没被读到（未配/格式错/未重启）。
    bt = config.TELEGRAM_BROADCAST_TARGETS
    log.info("广播目标 %d 个：%s", len(bt),
             "、".join(f"{lbl}({cid})" for lbl, cid in bt) or "（无，/publish 不弹通知按钮）")
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
                # 不在轮询线程里直接处理——丢给该 chat 的专属执行器，使轮询线程
                # 立刻回到 getUpdates，不被任何用户的耗时命令（LLM 精算）阻塞。
                if "message" in u:
                    m = u["message"]
                    cid = m.get("chat", {}).get("id")
                    if cid is not None:
                        _submit_for_chat(cid, handle_message, m)
                elif "callback_query" in u:
                    cb = u["callback_query"]
                    cid = cb.get("message", {}).get("chat", {}).get("id")
                    if cid is not None:
                        _submit_for_chat(cid, handle_callback, cb)
        except requests.exceptions.RequestException as e:
            log.warning("getUpdates 异常，5s 后重试: %s", e)
            time.sleep(5)
        except Exception:
            log.exception("处理 update 出错，继续")
            time.sleep(1)
