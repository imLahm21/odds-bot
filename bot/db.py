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

-- 访客每日 /analyze 用量：按 (chat_id, 北京日期) 计数，持久化以防重启清零
CREATE TABLE IF NOT EXISTS analyze_usage (
    chat_id  INTEGER NOT NULL,
    day      TEXT NOT NULL,            -- 北京时间 YYYY-MM-DD
    used     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, day)
);

-- LLM 故障转移/熔断可调参数：由 TG /llm 面板实时改，llm_client 读取（免重启）。
-- 只存 9 个数值参数（非 secret）；端点密钥仍在 .env、不落库。key 白名单见
-- config.LLM_SETTING_SPECS，seed_config 灌默认值。
CREATE TABLE IF NOT EXISTS llm_settings (
    key        TEXT PRIMARY KEY,
    value      REAL NOT NULL,
    updated_at TEXT
);

-- 走地(滚球)快照：结构与盘前 odds_history 不同(分钟数/比分/封盘)，独立建表，
-- 避免污染赛前 SOP 的 CSV。只存 main:true 主盘口线(走地只看主盘)。
CREATE TABLE IF NOT EXISTS live_odds_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    fixture_id    INTEGER NOT NULL,
    snapshot_utc  TEXT NOT NULL,
    elapsed       INTEGER,                  -- 进行分钟数(走地节点语义)
    home_goals    INTEGER, away_goals INTEGER,  -- 抓取瞬间实时比分
    status_short  TEXT,                     -- fixture.status.short(1H/2H/ET/BT/P…)，判加时/点球阶段
    bookmaker_id  INTEGER, bookmaker TEXT,
    market        TEXT NOT NULL,            -- 'h2h' | 'ah' | 'ou'
    handicap      REAL,                     -- 主盘口线(h2h 为 NULL)
    home_water    REAL, away_water REAL,    -- ah:主/客水位 ou:大/小球水位 h2h:主/客胜赔率
    draw_odds     REAL,                     -- 仅 h2h 用(平局赔率)
    suspended     INTEGER DEFAULT 0
    -- 不设 fixtures 外键：进行中比赛未必在 fixtures 表(联赛未开启时无赛程)，
    -- 外键会让 insert_live_odds 失败。走地表本就独立。
);
CREATE INDEX IF NOT EXISTS idx_live_fix_time ON live_odds_history(fixture_id, snapshot_utc);
CREATE UNIQUE INDEX IF NOT EXISTS idx_live_dedup
    ON live_odds_history(fixture_id, snapshot_utc, bookmaker_id, market);

-- 走地订阅：谁订阅了哪场比赛的实时播报
CREATE TABLE IF NOT EXISTS live_subscriptions (
    chat_id       INTEGER NOT NULL,
    fixture_id    INTEGER NOT NULL,
    enabled       INTEGER NOT NULL DEFAULT 1,
    last_push_utc TEXT,                     -- 上次推送时间(节流用)
    created_utc   TEXT,
    PRIMARY KEY (chat_id, fixture_id)
);
"""

# odds_history 批量插入用的列顺序（与 parser 产出的行字典对齐）
ODDS_COLS = [
    "fixture_id", "snapshot_utc", "node_label", "bookmaker_id", "bookmaker",
    "market", "home_odds", "draw_odds", "away_odds",
    "kelly_home", "kelly_draw", "kelly_away",
    "handicap", "home_water", "away_water", "kelly_h_water", "kelly_a_water",
]

# live_odds_history 批量插入列顺序（与 parser.parse_live_response 产出对齐）
LIVE_ODDS_COLS = [
    "fixture_id", "snapshot_utc", "elapsed", "home_goals", "away_goals",
    "status_short",
    "bookmaker_id", "bookmaker", "market",
    "handicap", "home_water", "away_water", "draw_odds", "suspended",
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
    # 走地表补 status_short（旧库无此列；新增走地阶段标注/终局判定用）
    live_cols = {r[1] for r in conn.execute("PRAGMA table_info(live_odds_history)")}
    if "status_short" not in live_cols:
        conn.execute("ALTER TABLE live_odds_history ADD COLUMN status_short TEXT")
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

    # LLM 熔断/故障转移参数默认值（INSERT OR IGNORE：用户在 /llm 改过的不覆盖）
    now = _now_utc_iso()
    llm_rows = [(k, float(spec["default"]), now)
                for k, spec in config.LLM_SETTING_SPECS.items()]
    conn.executemany(
        "INSERT OR IGNORE INTO llm_settings (key, value, updated_at) "
        "VALUES (?,?,?)", llm_rows)
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


def update_fixture_status(conn: sqlite3.Connection, fixture_id: int,
                          status: str) -> None:
    """只更新单场比赛的 status（供 /fixtures 实时校正用）。
    赛程 upsert(任务A)一天仅两次，两次之间开球并结束的比赛 status 会滞留旧值
    (如 NS)，导致已结束的比赛错留在「未来可精算」档。这里按 fixture_id 就地改状态。"""
    conn.execute(
        "UPDATE fixtures SET status=?, updated_at=? WHERE fixture_id=?",
        (status, _now_utc_iso(), fixture_id))
    conn.commit()


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


# ─── 访客 /analyze 每日用量（持久化，防重启清零）────────────────────────────
def get_analyze_used(conn: sqlite3.Connection, chat_id: int, day: str) -> int:
    """取某 chat 在某北京日期已用的 /analyze 次数（无记录返回 0）。"""
    row = conn.execute(
        "SELECT used FROM analyze_usage WHERE chat_id=? AND day=?",
        (chat_id, day)).fetchone()
    return row[0] if row else 0


def incr_analyze_used(conn: sqlite3.Connection, chat_id: int, day: str) -> int:
    """该 chat 当日用量 +1，返回自增后的值。UPSERT，跨天自然分行。"""
    conn.execute(
        "INSERT INTO analyze_usage (chat_id, day, used) VALUES (?,?,1) "
        "ON CONFLICT(chat_id, day) DO UPDATE SET used = used + 1",
        (chat_id, day))
    conn.commit()
    return get_analyze_used(conn, chat_id, day)


# ─── LLM 熔断/故障转移参数（TG /llm 面板读写，llm_client 读取）──────────────
def get_llm_settings(conn: sqlite3.Connection) -> dict[str, float]:
    """读全部 LLM 参数为 {key: value}。以 config.LLM_SETTING_SPECS 为准补齐缺失键
    （旧库首次加表、或新增参数时未 seed 到的键，回退该键的默认值），确保调用方
    永远拿得到 9 个键的完整字典。"""
    rows = dict(conn.execute("SELECT key, value FROM llm_settings").fetchall())
    return {k: float(rows.get(k, spec["default"]))
            for k, spec in config.LLM_SETTING_SPECS.items()}


def set_llm_setting(conn: sqlite3.Connection, key: str, value: float) -> bool:
    """写单个 LLM 参数（UPSERT）。仅接受 LLM_SETTING_SPECS 白名单里的 key，
    非法 key 返回 False（防伪造回调写入任意键）。范围校验由调用方(tgbot)据
    spec 的 min/max 负责，这里只做键白名单守卫。"""
    if key not in config.LLM_SETTING_SPECS:
        return False
    conn.execute(
        "INSERT INTO llm_settings (key, value, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
        "updated_at=excluded.updated_at",
        (key, float(value), _now_utc_iso()))
    conn.commit()
    return True


def reset_llm_settings(conn: sqlite3.Connection) -> None:
    """把全部 LLM 参数恢复为 config.LLM_SETTING_SPECS 的默认值。"""
    now = _now_utc_iso()
    rows = [(k, float(spec["default"]), now)
            for k, spec in config.LLM_SETTING_SPECS.items()]
    conn.executemany(
        "INSERT INTO llm_settings (key, value, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
        "updated_at=excluded.updated_at", rows)
    conn.commit()


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


# ─── 走地(滚球)快照与订阅 ──────────────────────────────────────────────────
def insert_live_odds(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """批量插入走地快照。重复（唯一索引命中）自动忽略。返回实际新增行数。"""
    if not rows:
        return 0
    placeholders = ",".join("?" * len(LIVE_ODDS_COLS))
    payload = [tuple(r.get(c) for c in LIVE_ODDS_COLS) for r in rows]
    before = conn.total_changes
    conn.executemany(
        f"INSERT OR IGNORE INTO live_odds_history ({','.join(LIVE_ODDS_COLS)}) "
        f"VALUES ({placeholders})",
        payload,
    )
    conn.commit()
    return conn.total_changes - before


def get_latest_live_snapshot(conn: sqlite3.Connection, fixture_id: int,
                             bookmaker_id: int) -> list[tuple]:
    """取某场某庄最近一次走地快照的全部 market 行（异动对比用）。
    返回 [(market, handicap, home_water, away_water, draw_odds, suspended,
           elapsed, home_goals, away_goals, status_short), ...]；无则空列表。"""
    row = conn.execute(
        "SELECT snapshot_utc FROM live_odds_history "
        "WHERE fixture_id=? AND bookmaker_id=? "
        "ORDER BY snapshot_utc DESC LIMIT 1", (fixture_id, bookmaker_id)).fetchone()
    if not row:
        return []
    return conn.execute(
        "SELECT market, handicap, home_water, away_water, draw_odds, suspended, "
        "elapsed, home_goals, away_goals, status_short FROM live_odds_history "
        "WHERE fixture_id=? AND bookmaker_id=? AND snapshot_utc=?",
        (fixture_id, bookmaker_id, row[0])).fetchall()


def add_live_sub(conn: sqlite3.Connection, chat_id: int, fixture_id: int) -> None:
    """新增/重新启用一条走地订阅。"""
    now = _now_utc_iso()
    conn.execute(
        "INSERT INTO live_subscriptions (chat_id, fixture_id, enabled, created_utc) "
        "VALUES (?,?,1,?) ON CONFLICT(chat_id, fixture_id) DO UPDATE SET enabled=1",
        (chat_id, fixture_id, now))
    conn.commit()


def disable_live_sub(conn: sqlite3.Connection, chat_id: int,
                     fixture_id: int) -> bool:
    """停用一条走地订阅。返回是否有匹配行。"""
    cur = conn.execute(
        "UPDATE live_subscriptions SET enabled=0 "
        "WHERE chat_id=? AND fixture_id=? AND enabled=1", (chat_id, fixture_id))
    conn.commit()
    return cur.rowcount > 0


def disable_live_sub_all(conn: sqlite3.Connection, fixture_id: int) -> list[int]:
    """停用某场比赛的全部订阅（比赛结束自动退订用）。返回受影响的 chat_id 列表。"""
    chats = [r[0] for r in conn.execute(
        "SELECT chat_id FROM live_subscriptions "
        "WHERE fixture_id=? AND enabled=1", (fixture_id,)).fetchall()]
    if chats:
        conn.execute("UPDATE live_subscriptions SET enabled=0 WHERE fixture_id=?",
                     (fixture_id,))
        conn.commit()
    return chats


def get_active_live_subs(conn: sqlite3.Connection) -> list[tuple]:
    """取全部启用的走地订阅，返回 [(chat_id, fixture_id), ...]。"""
    return conn.execute(
        "SELECT chat_id, fixture_id FROM live_subscriptions WHERE enabled=1"
    ).fetchall()


def list_live_subs_for_chat(conn: sqlite3.Connection, chat_id: int) -> list[tuple]:
    """列出某 chat 的启用订阅，含比赛信息。供 /lives 展示。
    返回 [(fixture_id, home_team, away_team, league_name, commence_utc), ...]。"""
    return conn.execute(
        "SELECT s.fixture_id, f.home_team, f.away_team, f.league_name, f.commence_utc "
        "FROM live_subscriptions s LEFT JOIN fixtures f ON s.fixture_id=f.fixture_id "
        "WHERE s.chat_id=? AND s.enabled=1 ORDER BY f.commence_utc",
        (chat_id,)).fetchall()


def count_live_subs_for_chat(conn: sqlite3.Connection, chat_id: int) -> int:
    """某 chat 当前启用的订阅数（防滥用上限校验用）。"""
    return conn.execute(
        "SELECT COUNT(*) FROM live_subscriptions WHERE chat_id=? AND enabled=1",
        (chat_id,)).fetchone()[0]
