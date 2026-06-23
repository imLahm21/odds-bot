"""
调度层 —— 五档定时任务 A/B/C/D/E

  任务 A（每日 02:00 / 14:00）：拉关注联赛未来赛程，upsert fixtures
  任务 B（每 1 小时）：拉未来 N 天比赛的最新赔率，存 odds_history
  任务 C（每 15 分钟）：仅对"开球前 2h 内"的比赛高频抓取
  任务 D（每 5 分钟）：仅对"开球前 30min 内"的比赛冲刺抓取（知情资金窗口）
  任务 E（每 3 分钟）：走地实时播报——Bulk 抓进行中比赛，按订阅推送异动

任务函数都设计成可独立调用（便于手动测试与调度器注册）。
"""

import time
import logging
from datetime import datetime, timezone, timedelta

from . import config, db, api_client, parser

log = logging.getLogger("odds_bot.scheduler")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ─── 任务 A：赛程更新 ────────────────────────────────────────────────────────
def task_a_update_fixtures() -> int:
    """拉所有启用联赛未来 14 天赛程，写入 fixtures。返回写入场次。"""
    log.info("【任务A】开始更新赛程")
    conn = db.get_conn()
    total = 0
    try:
        today = _utc_now().strftime("%Y-%m-%d")
        end = (_utc_now() + timedelta(days=14)).strftime("%Y-%m-%d")
        leagues = db.get_enabled_leagues(conn)
        for league_id, (name, season) in leagues.items():
            fixtures = api_client.fetch_fixtures(league_id, season, today, end)
            rows = parser.parse_fixtures_response(fixtures, league_id, name, season)
            n = db.upsert_fixtures(conn, rows)
            total += n
            if n:
                log.info("  %s: %d 场", name, n)
            time.sleep(config.REQUEST_THROTTLE_SEC)
        # 清理结束很久的旧比赛，防表膨胀
        fix_n, odds_n = db.cleanup_old(conn, config.CLEANUP_DAYS)
        if fix_n or odds_n:
            log.info("  清理旧数据：删 %d 场 / %d 行快照", fix_n, odds_n)
        db.checkpoint_wal(conn)
    finally:
        conn.close()
    log.info("【任务A】完成，共 %d 场", total)
    return total


# ─── 共用：抓一批比赛的赔率 ──────────────────────────────────────────────────
def _fetch_odds_for(conn, fixtures: list[tuple], task: str = "B",
                    floor: int | None = None) -> int:
    """对给定 (fixture_id, commence_utc, home, away) 列表抓盘存库。返回新增行数。
    额度护栏：每场抓取前检查当日剩余额度，低于 floor 则提前中止本轮
    （不再无意义地撞 429），并给管理员推一条当日去重的 TG 告警。
    floor 默认取 config.ODDS_QUOTA_FLOOR；分级护栏让 B 先停、C/D 后停，
    优先保住临场高频抓取。task 仅用于日志/告警文案。
    """
    if floor is None:
        floor = config.ODDS_QUOTA_FLOOR
    snapshot = _iso(_utc_now())
    pool_ids = db.get_enabled_bookmaker_ids(conn)   # 动态庄家池
    total_rows = 0
    done = 0
    for fid, commence, home, away in fixtures:
        remaining = api_client.last_remaining()
        if remaining is not None and remaining < floor:
            log.warning("【任务%s】当日剩余额度 %d < 护栏 %d，本轮提前中止"
                        "（已抓 %d/%d 场）",
                        task, remaining, floor, done, len(fixtures))
            try:
                from . import tgbot
                tgbot.alert_admins(
                    f"⚠️ API 额度护栏触发：当日剩余 {remaining} < {floor}，"
                    f"赛前抓取(任务{task})本轮提前中止，已抓 {done}/{len(fixtures)} 场。"
                    f"高频临场/走地优先保留。",
                    dedup_key=f"odds_quota_floor_{task}")
            except Exception as e:  # 告警失败不能拖垮抓取
                log.warning("额度告警推送失败：%s", e)
            break
        resp = api_client.fetch_odds(fid)
        if resp:
            rows = parser.parse_odds_response(resp[0], snapshot, commence,
                                              pool_ids=pool_ids)
            n = db.insert_odds(conn, rows)
            total_rows += n
        done += 1
        time.sleep(config.REQUEST_THROTTLE_SEC)
    return total_rows


# ─── 任务 B：常规赔率 ────────────────────────────────────────────────────────
def task_b_regular_odds() -> int:
    """抓未来 N 天内全部关注比赛的当前赔率。"""
    log.info("【任务B】开始抓取常规赔率")
    conn = db.get_conn()
    try:
        now = _utc_now()
        start = _iso(now)
        end = _iso(now + timedelta(days=config.ODDS_DAYS_AHEAD))
        fixtures = db.get_fixtures_between(conn, start, end)
        log.info("  未来 %d 天共 %d 场待抓", config.ODDS_DAYS_AHEAD, len(fixtures))
        n = _fetch_odds_for(conn, fixtures, task="B")
        db.checkpoint_wal(conn)
    finally:
        conn.close()
    log.info("【任务B】完成，新增 %d 行快照", n)
    return n


# ─── 任务 C：临场高频 ────────────────────────────────────────────────────────
def task_c_near_kickoff() -> int:
    """仅抓开球前 NEAR_KICKOFF_HOURS 小时内的比赛。"""
    conn = db.get_conn()
    try:
        now = _utc_now()
        start = _iso(now)
        end = _iso(now + timedelta(hours=config.NEAR_KICKOFF_HOURS))
        fixtures = db.get_fixtures_between(conn, start, end)
        if not fixtures:
            log.debug("【任务C】当前无临场比赛")
            return 0
        log.info("【任务C】%d 场临场比赛", len(fixtures))
        n = _fetch_odds_for(conn, fixtures, task="C",
                            floor=config.ODDS_QUOTA_FLOOR_NEAR)
        db.checkpoint_wal(conn)
    finally:
        conn.close()
    if n:
        log.info("【任务C】完成，新增 %d 行快照", n)
    return n


# ─── 任务 D：临场冲刺（开球前 30min 加密）──────────────────────────────────
def task_d_sprint() -> int:
    """开球前 SPRINT_KICKOFF_MINUTES 分钟内的比赛，每 TASK_D_MINUTES 分钟抓一次，
    专采临场④→即时的知情资金窗口。"""
    conn = db.get_conn()
    try:
        now = _utc_now()
        start = _iso(now)
        end = _iso(now + timedelta(minutes=config.SPRINT_KICKOFF_MINUTES))
        fixtures = db.get_fixtures_between(conn, start, end)
        if not fixtures:
            log.debug("【任务D】当前无冲刺窗口比赛")
            return 0
        log.info("【任务D】%d 场冲刺窗口比赛", len(fixtures))
        n = _fetch_odds_for(conn, fixtures, task="D",
                            floor=config.ODDS_QUOTA_FLOOR_NEAR)
        db.checkpoint_wal(conn)
    finally:
        conn.close()
    if n:
        log.info("【任务D】完成，新增 %d 行快照", n)
    return n


# ─── 任务 E：走地(滚球)实时播报 ──────────────────────────────────────────────
def _cur_from(rows: list[dict]) -> dict:
    """把 parse_live_response 的行列表转成按 market 索引的 dict，供异动对比。"""
    cur = {}
    for r in rows:
        cur[r["market"]] = r
    return cur


def _prev_from(snapshot_rows: list[tuple]) -> dict:
    """把 db.get_latest_live_snapshot 的行(元组)转成按 market 索引的 dict。
    元组列序: (market, handicap, home_water, away_water, draw_odds, suspended,
               elapsed, home_goals, away_goals)
    """
    prev = {}
    for (market, hc, hw, aw, draw, susp, el, hg, ag) in snapshot_rows:
        prev[market] = {
            "market": market, "handicap": hc, "home_water": hw, "away_water": aw,
            "draw_odds": draw, "suspended": susp,
            "elapsed": el, "home_goals": hg, "away_goals": ag,
        }
    return prev


def _detect_live_delta(prev: dict, cur: dict) -> list[str]:
    """对比上次与本次走地快照，返回显著异动描述列表(空=无异动，不推送)。
    触发条件(任一即推送)：
      - 进球(比分变化) —— 最重要的走地信号，必推
      - 主盘口线变化(大小球线 2.5→2.75、亚盘 -0.5→-0.75)
      - 主水位跳变 ≥ config.LIVE_WATER_DELTA
      - suspended 状态翻转(封盘/开盘)
    """
    deltas: list[str] = []
    if not prev:
        return deltas   # 首次抓取无对比基准，不推送(避免开局刷屏)

    # 比分变化(任一 market 的比分字段都行，取 cur 里第一条)
    any_cur = next(iter(cur.values()), None)
    any_prev = next(iter(prev.values()), None)
    if any_cur and any_prev:
        pc = (any_prev.get("home_goals"), any_prev.get("away_goals"))
        cc = (any_cur.get("home_goals"), any_cur.get("away_goals"))
        if None not in cc and None not in pc and cc != pc:
            deltas.append(f"⚽ 进球！比分 {pc[0]}-{pc[1]} → {cc[0]}-{cc[1]}")

    market_zh = {"ah": "亚盘", "ou": "大小球", "h2h": "欧赔"}
    for mk in ("ah", "ou", "h2h"):
        p, c = prev.get(mk), cur.get(mk)
        if not p or not c:
            continue
        zh = market_zh[mk]
        # 盘口线变化(h2h 无线)
        if mk in ("ah", "ou") and p.get("handicap") != c.get("handicap"):
            deltas.append(f"📊 {zh}盘口线 {p.get('handicap')} → {c.get('handicap')}")
        # 主水位跳变
        for side, label in (("home_water", "主/大"), ("away_water", "客/小")):
            pv, cv = p.get(side), c.get(side)
            if pv and cv and abs(cv - pv) >= config.LIVE_WATER_DELTA:
                deltas.append(f"💧 {zh}{label}水位 {pv} → {cv}")
        # 封盘状态翻转
        ps, cs = bool(p.get("suspended")), bool(c.get("suspended"))
        if ps != cs:
            deltas.append(f"🔒 {zh}{'封盘' if cs else '重新开盘'}")
    return deltas


def _maybe_autounsub(conn, fid: int) -> None:
    """订阅的比赛不在进行中列表里 → 查 fixture 状态，已结束则自动退订并通知。"""
    meta = db.get_fixture_meta(conn, fid)
    home, away = (meta[4], meta[5]) if meta else ("主队", "客队")
    result = api_client.fetch_fixture_result(fid)
    short = (result or {}).get("fixture", {}).get("status", {}).get("short", "")
    if short in ("FT", "AET", "PEN"):   # 已结束
        goals = (result or {}).get("goals", {})
        score = f"{goals.get('home', '?')}-{goals.get('away', '?')}"
        chats = db.disable_live_sub_all(conn, fid)
        if chats:
            from . import tgbot
            for cid in chats:
                tgbot.send(cid, f"🏁 {home} vs {away} 已结束(终场 {score})，"
                                f"走地播报自动退订。", plain=True)
        log.info("【任务E】fixture %d 已结束(%s)，自动退订 %d 人", fid, short, len(chats))


def task_e_live_broadcast() -> int:
    """每 LIVE_MINUTES 分钟：护栏检查 → Bulk 抓全部进行中比赛 → 按订阅 fid 过滤
    → 存库 → 检测异动 → 推送。返回处理的订阅场次数。"""
    conn = db.get_conn()
    try:
        subs = db.get_active_live_subs(conn)         # [(chat_id, fid), ...]
        if not subs:
            return 0                                  # 无人订阅，连请求都不发
        # ── 额度护栏 ──
        remaining = api_client.last_remaining()
        if remaining is not None and remaining < config.LIVE_QUOTA_FLOOR:
            log.warning("【任务E】当日剩余额度 %d < 护栏 %d，暂停走地抓取",
                        remaining, config.LIVE_QUOTA_FLOOR)
            return 0
        # ── Bulk 抓取：一次拿全部进行中比赛 ──
        all_live = api_client.fetch_live_odds()
        live_by_fid = {e["fixture"]["id"]: e for e in all_live
                       if e.get("fixture", {}).get("id")}
        subbed_fids = {fid for _, fid in subs}
        snapshot = _iso(_utc_now())
        log.info("【任务E】%d 场订阅，进行中 %d 场", len(subbed_fids), len(all_live))
        for fid in subbed_fids:
            entry = live_by_fid.get(fid)
            if entry is None:
                _maybe_autounsub(conn, fid)
                continue
            rows = parser.parse_live_response(entry, snapshot)
            if not rows:
                continue
            prev = _prev_from(db.get_latest_live_snapshot(
                conn, fid, config.LIVE_ANCHOR_BM))
            db.insert_live_odds(conn, rows)
            deltas = _detect_live_delta(prev, _cur_from(rows))
            if deltas:
                from . import tgbot
                for chat_id, sfid in subs:
                    if sfid == fid:
                        tgbot.push_live_update(chat_id, fid, entry, rows, deltas)
        db.checkpoint_wal(conn)
        return len(subbed_fids)
    finally:
        conn.close()


# ─── 注册到调度器 ────────────────────────────────────────────────────────────
def build_scheduler(blocking: bool = True):
    """构建并返回调度器（未启动）。
    blocking=True → BlockingScheduler（仅跑调度，无 bot）
    blocking=False → BackgroundScheduler（与 TG bot 主循环共存）
    """
    if blocking:
        from apscheduler.schedulers.blocking import BlockingScheduler
        sched = BlockingScheduler(timezone="Asia/Shanghai")
    else:
        from apscheduler.schedulers.background import BackgroundScheduler
        sched = BackgroundScheduler(timezone="Asia/Shanghai")
    sched.add_job(task_a_update_fixtures, "cron",
                  hour=config.TASK_A_HOURS, minute=config.TASK_A_MINUTE,
                  id="task_a", misfire_grace_time=3600)
    sched.add_job(task_b_regular_odds, "interval",
                  hours=config.TASK_B_HOURS, id="task_b",
                  misfire_grace_time=600)
    sched.add_job(task_c_near_kickoff, "interval",
                  minutes=config.TASK_C_MINUTES, id="task_c",
                  misfire_grace_time=300)
    sched.add_job(task_d_sprint, "interval",
                  minutes=config.TASK_D_MINUTES, id="task_d",
                  misfire_grace_time=120)
    sched.add_job(task_e_live_broadcast, "interval",
                  minutes=config.LIVE_MINUTES, id="task_e",
                  misfire_grace_time=60)
    return sched
