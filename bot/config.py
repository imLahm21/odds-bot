"""
配置中心 —— 联赛清单、关注庄家、轮询间隔、节点定义

所有 ID 均由阶段 0 探针（probe.py）实测确认，非猜测。
增删联赛只改这里，无需动其它代码（配置驱动）。
"""

# ─── API-Football 端点 ──────────────────────────────────────────────────────
BASE_URL = "https://v3.football.api-sports.io"
AUTH_HEADER = "x-apisports-key"     # 直连鉴权头（非 RapidAPI）

# ─── 关注的联赛（league_id 与 season 均已实测）──────────────────────────────
# 格式：league_id -> (中文名, season)
# season 注意：欧洲跨年联赛 2025-26 标记为 2025；北欧/世界杯为自然年
WATCH_LEAGUES: dict[int, tuple[str, int]] = {
    39:  ("英超 EPL",            2025),
    140: ("西甲 La Liga",        2025),
    78:  ("德甲 Bundesliga",     2025),
    135: ("意甲 Serie A",        2025),
    61:  ("法甲 Ligue 1",        2025),
    2:   ("欧冠 Champions Lg",   2025),
    3:   ("欧联 Europa Lg",      2025),
    848: ("欧协联 Conference",   2025),
    5:   ("欧国联 Nations Lg",   2026),
    1:   ("世界杯 World Cup",    2026),
    169: ("中超 CSL",            2026),
    98:  ("日职联 J1",           2027),
    292: ("韩K联 K League 1",    2026),
    307: ("沙特联 Pro League",   2025),
    164: ("冰岛超 Úrvalsdeild",  2026),
}

# ─── 关注的庄家（bookmaker_id 已实测）────────────────────────────────────────
# 全部 12 家都抓取并参与市场平均凯利计算；前两家是风控双锚。
BOOKMAKER_NAMES: dict[int, str] = {
    4:  "Pinnacle",       # 亚盘风控锚 <0.96 报警
    8:  "Bet365",         # 欧指基准锚 >1.03 报警
    7:  "William Hill",
    2:  "Marathonbet",
    3:  "Betfair",
    11: "1xBet",
    16: "Unibet",
    36: "BetVictor",
    1:  "10Bet",
    5:  "SBO",
    32: "Betano",
    9:  "Dafabet",
}
# 计算市场平均时纳入的庄家（全部）；如需收窄可改这里
KELLY_POOL_IDS = set(BOOKMAKER_NAMES.keys())

# ─── bet 类型 ID（已实测）────────────────────────────────────────────────────
BET_MATCH_WINNER = 1     # 欧赔 1X2，value: Home/Draw/Away
BET_ASIAN_HANDICAP = 4   # 亚盘，value: "Home +0.75" / "Away -1.25"（含完整 1/4 盘）

# ─── 轮询间隔与临场窗口 ──────────────────────────────────────────────────────
TASK_A_HOUR = 1          # 每日赛程更新：凌晨 1 点（本地时区）
TASK_A_MINUTE = 7        # 错峰，避开整点
TASK_B_HOURS = 2         # 常规赔率：每 2 小时
TASK_C_MINUTES = 15      # 临场高频：每 15 分钟
NEAR_KICKOFF_HOURS = 2.0 # 任务 C 的"临场"定义：开球前 2 小时内
ODDS_DAYS_AHEAD = 3      # 任务 B 只抓未来 N 天内的比赛，省额度

# 每次请求之间的节流（秒），保护每分钟 300 限速
REQUEST_THROTTLE_SEC = 0.3

# ─── 10 节点定义（距开球小时数 → 节点标签）──────────────────────────────────
# 轮询是定时的，抓到的散点按"距开球时长"对齐到最近的 SOP 节点语义。
# (小时阈值, 标签)：snapshot 距开球的小时数 >= 阈值时归入该节点
NODE_THRESHOLDS: list[tuple[float, str]] = [
    (72, "初盘①"),
    (48, "初盘②"),
    (36, "中盘①"),
    (24, "中盘②"),
    (12, "中盘③"),
    (6,  "临场①"),
    (3,  "临场②"),
    (1.5, "临场③"),
    (0.5, "临场④"),
    (0,  "即时"),
]

# ─── 数据库 ──────────────────────────────────────────────────────────────────
DB_PATH = "odds.db"
