"""
调度层 —— 三档定时任务 A/B/C

  任务 A（每日 01:07）：拉关注联赛未来赛程，upsert fixtures
  任务 B（每 2 小时）：拉未来 N 天比赛的最新赔率，存 odds_history
  任务 C（每 15 分钟）：仅对"开球前 2h 内"的比赛高频抓取

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
def _fetch_odds_for(conn, fixtures: list[tuple]) -> int:
    """对给定 (fixture_id, commence_utc, home, away) 列表抓盘存库。返回新增行数。"""
    snapshot = _iso(_utc_now())
    pool_ids = db.get_enabled_bookmaker_ids(conn)   # 动态庄家池
    total_rows = 0
    for fid, commence, home, away in fixtures:
        resp = api_client.fetch_odds(fid)
        if resp:
            rows = parser.parse_odds_response(resp[0], snapshot, commence,
                                              pool_ids=pool_ids)
            n = db.insert_odds(conn, rows)
            total_rows += n
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
        n = _fetch_odds_for(conn, fixtures)
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
        n = _fetch_odds_for(conn, fixtures)
        db.checkpoint_wal(conn)
    finally:
        conn.close()
    if n:
        log.info("【任务C】完成，新增 %d 行快照", n)
    return n


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
                  hour=config.TASK_A_HOUR, minute=config.TASK_A_MINUTE,
                  id="task_a", misfire_grace_time=3600)
    sched.add_job(task_b_regular_odds, "interval",
                  hours=config.TASK_B_HOURS, id="task_b",
                  misfire_grace_time=600)
    sched.add_job(task_c_near_kickoff, "interval",
                  minutes=config.TASK_C_MINUTES, id="task_c",
                  misfire_grace_time=300)
    return sched
