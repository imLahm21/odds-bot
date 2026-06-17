"""
SQLite 数据层 —— 建表、批量插入、连接管理

设计要点（ARM 1核1G 低内存）：
  - WAL 模式：读写不互斥，崩溃安全
  - executemany 批量插入，不经 pandas 中转
  - INSERT OR IGNORE + 唯一索引：重复轮询不会灌库
"""

import os
import sqlite3
from datetime import datetime, timezone

from . import config

# ─── 建表 SQL ────────────────────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS fixtures (
    fixture_id    INTEGER PRIMARY KEY,
    league_id     INTEGER NOT NULL,
    league_name   TEXT,
    season        INTEGER,
    home_team     TEXT NOT NULL,
    away_team     TEXT NOT NULL,
    home_team_id  INTEGER,
    away_team_id  INTEGER,
    commence_utc  TEXT NOT NULL,
    status        TEXT,
    updated_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_fix_commence ON fixtures(commence_utc);
CREATE INDEX IF NOT EXISTS idx_fix_league   ON fixtures(league_id);

CREATE TABLE IF NOT EXISTS odds_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    fixture_id    INTEGER NOT NULL,
    snapshot_utc  TEXT NOT NULL,
    node_label    TEXT,
    bookmaker_id  INTEGER NOT NULL,
    bookmaker     TEXT,
    market        TEXT NOT NULL,            -- 'h2h'(欧赔) | 'ah'(亚盘)
    home_odds     REAL, draw_odds REAL, away_odds REAL,
    kelly_home    REAL, kelly_draw REAL, kelly_away REAL,
    handicap      REAL,
    home_water    REAL, away_water REAL,
    kelly_h_water REAL, kelly_a_water REAL,
    FOREIGN KEY (fixture_id) REFERENCES fixtures(fixture_id)
);
CREATE INDEX IF NOT EXISTS idx_odds_fix_time ON odds_history(fixture_id, snapshot_utc);
CREATE UNIQUE INDEX IF NOT EXISTS idx_odds_dedup
    ON odds_history(fixture_id, snapshot_utc, bookmaker_id, market, handicap);

-- 动态配置表：由 TG bot 实时增删/开关，调度器每次抓取时读取
CREATE TABLE IF NOT EXISTS watched_leagues (
    league_id   INTEGER PRIMARY KEY,
    league_name TEXT,
    season      INTEGER,
    enabled     INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS watched_bookmakers (
    bookmaker_id INTEGER PRIMARY KEY,
    name         TEXT,
    enabled      INTEGER NOT NULL DEFAULT 1
);
"""

# odds_history 批量插入用的列顺序（与 parser 产出的行字典对齐）
ODDS_COLS = [
    "fixture_id", "snapshot_utc", "node_label", "bookmaker_id", "bookmaker",
    "market", "home_odds", "draw_odds", "away_odds",
    "kelly_home", "kelly_draw", "kelly_away",
    "handicap", "home_water", "away_water", "kelly_h_water", "kelly_a_water",
]


def get_conn(db_path: str | None = None) -> sqlite3.Connection:
    """打开连接并启用 WAL 与外键。"""
    path = db_path or config.DB_PATH
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db(db_path: str | None = None) -> None:
    """建表（幂等）+ 轻量迁移。"""
    conn = get_conn(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
        _migrate(conn)
        seed_config(conn)
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """对已存在的旧库补加新列（CREATE TABLE IF NOT EXISTS 不会改已有表）。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(fixtures)")}
    for col in ("home_team_id", "away_team_id"):
        if col not in cols:
            conn.execute(f"ALTER TABLE fixtures ADD COLUMN {col} INTEGER")
    conn.commit()


def seed_config(conn: sqlite3.Connection) -> None:
    """首次启动把 config 的联赛/公司灌入配置表。
    INSERT OR IGNORE：已存在的行保留用户在 bot 里改过的开关，不覆盖。
    enabled 初值按 config 的「默认启用」集合决定（核心联赛/12家庄=1，扩充池=0）。
    """
    default_lg = set(config.DEFAULT_ENABLED_LEAGUES)
    league_rows = [
        (lid, name, season, 1 if lid in default_lg else 0)
        for lid, (name, season) in config.WATCH_LEAGUES.items()
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO watched_leagues (league_id, league_name, season, enabled) "
        "VALUES (?,?,?,?)", league_rows)

    default_bm = config.DEFAULT_ENABLED_BOOKMAKERS
    bm_rows = [(bid, name, 1 if bid in default_bm else 0)
               for bid, name in config.ALL_BOOKMAKERS.items()]
    conn.executemany(
        "INSERT OR IGNORE INTO watched_bookmakers (bookmaker_id, name, enabled) "
        "VALUES (?,?,?)", bm_rows)
    conn.commit()


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def upsert_fixtures(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """插入/更新赛程。按 fixture_id 主键 upsert。返回写入行数。"""
    if not rows:
        return 0
    now = _now_utc_iso()
    payload = [
        (r["fixture_id"], r["league_id"], r.get("league_name"), r.get("season"),
         r["home_team"], r["away_team"], r.get("home_team_id"),
         r.get("away_team_id"), r["commence_utc"], r.get("status"), now)
        for r in rows
    ]
    conn.executemany(
        """INSERT INTO fixtures
           (fixture_id, league_id, league_name, season, home_team, away_team,
            home_team_id, away_team_id, commence_utc, status, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(fixture_id) DO UPDATE SET
             league_name=excluded.league_name,
             season=excluded.season,
             home_team_id=excluded.home_team_id,
             away_team_id=excluded.away_team_id,
             commence_utc=excluded.commence_utc,
             status=excluded.status,
             updated_at=excluded.updated_at""",
        payload,
    )
    conn.commit()
    return len(payload)


def insert_odds(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """批量插入盘口快照。重复（唯一索引命中）自动忽略。返回实际新增行数。"""
    if not rows:
        return 0
    placeholders = ",".join("?" * len(ODDS_COLS))
    payload = [tuple(r.get(c) for c in ODDS_COLS) for r in rows]
    before = conn.total_changes
    conn.executemany(
        f"INSERT OR IGNORE INTO odds_history ({','.join(ODDS_COLS)}) "
        f"VALUES ({placeholders})",
        payload,
    )
    conn.commit()
    return conn.total_changes - before


def get_fixtures_between(conn: sqlite3.Connection, start_utc: str,
                         end_utc: str) -> list[tuple]:
    """取开球时间在 [start, end] 区间的比赛，用于任务 B/C 选场。"""
    cur = conn.execute(
        "SELECT fixture_id, commence_utc, home_team, away_team "
        "FROM fixtures WHERE commence_utc BETWEEN ? AND ? "
        "ORDER BY commence_utc",
        (start_utc, end_utc),
    )
    return cur.fetchall()


def checkpoint_wal(conn: sqlite3.Connection) -> None:
    """截断 WAL，避免长跑时文件膨胀。"""
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")


def get_fixture_meta(conn: sqlite3.Connection, fixture_id: int):
    """取单场比赛元信息（供基本面/精算用）。返回行或 None。"""
    return conn.execute(
        "SELECT fixture_id, league_id, league_name, season, home_team, away_team, "
        "home_team_id, away_team_id, commence_utc FROM fixtures WHERE fixture_id=?",
        (fixture_id,)).fetchone()


def cleanup_old(conn: sqlite3.Connection, days: int = 30) -> tuple[int, int]:
    """删除开球时间早于 days 天前的比赛及其盘口快照。返回 (删赛程数, 删快照数)。"""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    od = conn.execute(
        "DELETE FROM odds_history WHERE fixture_id IN "
        "(SELECT fixture_id FROM fixtures WHERE commence_utc < ?)", (cutoff,))
    odds_n = od.rowcount
    fx = conn.execute("DELETE FROM fixtures WHERE commence_utc < ?", (cutoff,))
    fix_n = fx.rowcount
    conn.commit()
    return fix_n, odds_n


# ─── 动态配置读写（供调度器读、TG bot 写）──────────────────────────────────
def get_enabled_leagues(conn: sqlite3.Connection) -> dict[int, tuple[str, int]]:
    """返回启用的联赛 {league_id: (name, season)}，供调度器抓取。"""
    cur = conn.execute(
        "SELECT league_id, league_name, season FROM watched_leagues "
        "WHERE enabled=1")
    return {r[0]: (r[1], r[2]) for r in cur.fetchall()}


def get_enabled_bookmaker_ids(conn: sqlite3.Connection) -> set[int]:
    """返回启用的庄家 ID 集合，供解析时过滤与凯利池。"""
    cur = conn.execute(
        "SELECT bookmaker_id FROM watched_bookmakers WHERE enabled=1")
    return {r[0] for r in cur.fetchall()}


def list_leagues(conn: sqlite3.Connection) -> list[tuple]:
    """全部联赛 (league_id, name, season, enabled)，供 bot 展示。"""
    return conn.execute(
        "SELECT league_id, league_name, season, enabled FROM watched_leagues "
        "ORDER BY league_id").fetchall()


def list_bookmakers(conn: sqlite3.Connection) -> list[tuple]:
    """全部庄家 (bookmaker_id, name, enabled)，供 bot 展示。"""
    return conn.execute(
        "SELECT bookmaker_id, name, enabled FROM watched_bookmakers "
        "ORDER BY bookmaker_id").fetchall()


def toggle_league(conn: sqlite3.Connection, league_id: int) -> int | None:
    """翻转某联赛启用状态，返回新状态（1/0）；不存在返回 None。"""
    row = conn.execute("SELECT enabled FROM watched_leagues WHERE league_id=?",
                       (league_id,)).fetchone()
    if row is None:
        return None
    new = 0 if row[0] else 1
    conn.execute("UPDATE watched_leagues SET enabled=? WHERE league_id=?",
                 (new, league_id))
    conn.commit()
    return new


def toggle_bookmaker(conn: sqlite3.Connection, bookmaker_id: int) -> int | None:
    """翻转某庄家启用状态，返回新状态；不存在返回 None。"""
    row = conn.execute(
        "SELECT enabled FROM watched_bookmakers WHERE bookmaker_id=?",
        (bookmaker_id,)).fetchone()
    if row is None:
        return None
    new = 0 if row[0] else 1
    conn.execute("UPDATE watched_bookmakers SET enabled=? WHERE bookmaker_id=?",
                 (new, bookmaker_id))
    conn.commit()
    return new


def add_league(conn: sqlite3.Connection, league_id: int, name: str,
               season: int) -> None:
    """新增/更新一个关注联赛（默认启用）。"""
    conn.execute(
        "INSERT INTO watched_leagues (league_id, league_name, season, enabled) "
        "VALUES (?,?,?,1) ON CONFLICT(league_id) DO UPDATE SET "
        "league_name=excluded.league_name, season=excluded.season, enabled=1",
        (league_id, name, season))
    conn.commit()


def remove_league(conn: sqlite3.Connection, league_id: int) -> bool:
    """删除一个关注联赛。返回是否删除了行。"""
    cur = conn.execute("DELETE FROM watched_leagues WHERE league_id=?",
                       (league_id,))
    conn.commit()
    return cur.rowcount > 0
