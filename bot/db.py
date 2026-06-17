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
    """建表（幂等）。"""
    conn = get_conn(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def upsert_fixtures(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """插入/更新赛程。按 fixture_id 主键 upsert。返回写入行数。"""
    if not rows:
        return 0
    now = _now_utc_iso()
    payload = [
        (r["fixture_id"], r["league_id"], r.get("league_name"), r.get("season"),
         r["home_team"], r["away_team"], r["commence_utc"], r.get("status"), now)
        for r in rows
    ]
    conn.executemany(
        """INSERT INTO fixtures
           (fixture_id, league_id, league_name, season, home_team, away_team,
            commence_utc, status, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(fixture_id) DO UPDATE SET
             league_name=excluded.league_name,
             season=excluded.season,
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
