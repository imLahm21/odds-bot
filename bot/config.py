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
#
# 两层结构：
#   DEFAULT_ENABLED_LEAGUES —— 开机即抓的核心联赛（enabled=1）
#   EXTRA_LEAGUES           —— 写入可选池但默认停用（enabled=0），TG bot 点开即用
# 调度器只抓数据库里 enabled=1 的，所以加进 EXTRA 不会平白消耗额度。

# 默认启用：五大联赛 + 五大杯赛 + 冰岛超/沙特联/中超 + 欧战 + 世界杯
DEFAULT_ENABLED_LEAGUES: dict[int, tuple[str, int]] = {
    39:  ("英超 EPL",            2025),
    140: ("西甲 La Liga",        2025),
    78:  ("德甲 Bundesliga",     2025),
    135: ("意甲 Serie A",        2025),
    61:  ("法甲 Ligue 1",        2025),
    45:  ("足总杯 FA Cup",       2025),
    143: ("西班牙国王杯 Copa del Rey", 2025),
    81:  ("德国杯 DFB Pokal",    2026),
    137: ("意大利杯 Coppa Italia", 2025),
    66:  ("法国杯 Coupe de France", 2025),
    164: ("冰岛超 Úrvalsdeild",  2026),
    307: ("沙特联 Pro League",   2025),
    169: ("中超 CSL",            2026),
    2:   ("欧冠 Champions Lg",   2025),
    3:   ("欧联 Europa Lg",      2025),
    848: ("欧协联 Conference",   2025),
    5:   ("欧国联 Nations Lg",   2026),
    1:   ("世界杯 World Cup",    2026),
}

# 扩充可选池：默认停用，需要时在 TG bot /leagues 面板点开
EXTRA_LEAGUES: dict[int, tuple[str, int]] = {
    # 英格兰次级
    40:  ("英冠 Championship",   2025),
    41:  ("英甲 League One",     2025),
    42:  ("英乙 League Two",     2025),
    # 其他五大次级
    141: ("西乙 Segunda",        2025),
    79:  ("德乙 2.Bundesliga",   2025),
    136: ("意乙 Serie B",        2025),
    62:  ("法乙 Ligue 2",        2026),
    # 其他欧洲顶级
    88:  ("荷甲 Eredivisie",     2026),
    94:  ("葡超 Primeira Liga",  2025),
    144: ("比甲 Jupiler Pro",    2025),
    203: ("土超 Süper Lig",      2025),
    179: ("苏超 Premiership",    2025),
    197: ("希超 Super League 1", 2025),
    235: ("俄超 Premier League", 2025),
    207: ("瑞士超 Super League", 2025),
    218: ("奥甲 Bundesliga",     2025),
    119: ("丹超 Superliga",      2026),
    113: ("瑞典超 Allsvenskan",  2026),
    103: ("挪超 Eliteserien",    2026),
    106: ("波兰 Ekstraklasa",    2026),
    333: ("乌超 Premier League", 2025),
    210: ("克甲 HNL",            2026),
    # 美洲
    71:  ("巴西甲 Serie A",      2026),
    128: ("阿甲 Liga Pro",       2026),
    253: ("美职联 MLS",          2026),
    262: ("墨超 Liga MX",        2026),
    239: ("哥伦比亚 Primera A",  2026),
    265: ("智利甲 Primera",      2026),
    # 亚洲/大洋洲
    98:  ("日职联 J1",           2027),
    292: ("韩K联 K League 1",    2026),
    188: ("澳超 A-League",       2025),
}

# 合并：种子灌库时用（值含 enabled 标记）
WATCH_LEAGUES: dict[int, tuple[str, int]] = {
    **DEFAULT_ENABLED_LEAGUES, **EXTRA_LEAGUES,
}

# ─── 关注的庄家（bookmaker_id 已用 /odds/bookmakers 实测，共 33 家）──────────
# 全部写入可选池；DEFAULT_ENABLED_BOOKMAKERS 默认启用，其余 TG bot 点开。
ALL_BOOKMAKERS: dict[int, str] = {
    1: "10Bet", 2: "Marathonbet", 3: "Betfair", 4: "Pinnacle", 5: "SBO",
    6: "Bwin", 7: "William Hill", 8: "Bet365", 9: "Dafabet", 10: "Ladbrokes",
    11: "1xBet", 12: "BetFred", 13: "188Bet", 15: "Interwetten", 16: "Unibet",
    17: "5Dimes", 18: "Intertops", 19: "Bovada", 20: "Betcris", 21: "888Sport",
    22: "Tipico", 23: "Sportingbet", 24: "Betway", 25: "Expekt", 26: "Betsson",
    27: "NordicBet", 28: "ComeOn", 30: "Netbet", 32: "Betano", 33: "Fonbet",
    34: "Superbet", 36: "BetVictor",
}

# 默认启用的 12 家主流（前两家是风控双锚）
DEFAULT_ENABLED_BOOKMAKERS: set[int] = {
    4,   # Pinnacle  亚盘风控锚 <0.96 报警
    8,   # Bet365    欧指基准锚 >1.03 报警
    7,   # William Hill
    2,   # Marathonbet
    3,   # Betfair
    11,  # 1xBet
    16,  # Unibet
    36,  # BetVictor
    1,   # 10Bet
    5,   # SBO
    32,  # Betano
    9,   # Dafabet
}

# 兼容旧引用：种子用全集
BOOKMAKER_NAMES: dict[int, str] = ALL_BOOKMAKERS
# 凯利池 = 数据库里启用的庄家（运行时由 db.get_enabled_bookmaker_ids 提供）；
# 这里作为离线/兜底默认值
KELLY_POOL_IDS = set(DEFAULT_ENABLED_BOOKMAKERS)

# ─── bet 类型 ID（已实测）────────────────────────────────────────────────────
BET_MATCH_WINNER = 1     # 欧赔 1X2，value: Home/Draw/Away
BET_ASIAN_HANDICAP = 4   # 亚盘，value: "Home +0.75" / "Away -1.25"（含完整 1/4 盘）

# ─── 轮询间隔与临场窗口 ──────────────────────────────────────────────────────
TASK_A_HOUR = 1          # 每日赛程更新：凌晨 1 点（本地时区）
TASK_A_MINUTE = 7        # 错峰，避开整点
TASK_B_HOURS = 1         # 常规赔率：每 1 小时（早节点初盘/中盘间隔大，
                         # 2h 一轮易漏抓；1h 让 -72/-48/-36/-24h 稳定落袋。
                         # Pro 额度 7500/日，每轮约 30 场，远未触顶）
TASK_C_MINUTES = 15      # 临场高频：每 15 分钟
NEAR_KICKOFF_HOURS = 2.0 # 任务 C 的"临场"定义：开球前 2 小时内
ODDS_DAYS_AHEAD = 4      # 任务 B 抓未来 N 天内的比赛。设 4(=96h)而非 3:
                         # SOP 初盘①在 -72h，若窗口恰为 72h 该点落在边缘易抓漏；
                         # 放宽到 96h 让 -72h 稳定落在窗口内，确保初盘①采到。

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

# ─── Telegram bot ────────────────────────────────────────────────────────────
# token 与白名单从 .env 读取，不写死在代码里：
#   TELEGRAM_BOT_TOKEN=123456:ABC...
#   TELEGRAM_ALLOWED_CHAT_IDS=11111111,22222222   （逗号分隔，只有这些人能操控）
TELEGRAM_API = "https://api.telegram.org"
TG_POLL_TIMEOUT = 50          # long polling 超时（秒）
TG_LEAGUES_PER_ROW = 2        # 联赛按钮每行个数
TG_BOOKMAKERS_PER_ROW = 3     # 庄家按钮每行个数
TG_MSG_MAX = 4000             # 单条消息最大字符（Telegram 上限 4096，留余量）

# ─── 旧数据清理 ──────────────────────────────────────────────────────────────
CLEANUP_DAYS = 30             # 删除开球早于 N 天前的比赛及其快照

# ─── LLM 精算 (/analyze) ─────────────────────────────────────────────────────
# IKuncode（OpenAI 兼容），从 .env 读：
#   LLM_BASE_URL=https://api.ikuncode.cc/v1
#   LLM_API_KEY=sk-xxx
LLM_MODEL = "gpt-5.5"
LLM_TIMEOUT = 300             # 秒，gpt-5.5 high 推理 + 长报告，给足超时
LLM_MAX_TOKENS = 8000         # 报告输出上限
# 全量规则文件（相对项目根），按顺序拼接成 system prompt
ANALYZE_RULE_FILES = [
    "CLAUDE.md",
    "rules/方法论/reference_asian_handicap.md",
    "rules/方法论/reference_dynamic_analysis.md",
    "rules/风控验证/reference_kelly_index.md",
    "rules/实战教训/reference_case_lessons.md",
    "rules/实战教训/reference_cases.md",
]
# 基本面拉取参数
FUND_RECENT_N = 10            # 各队近 N 场
FUND_H2H_N = 10               # 交锋近 N 场
FUND_UPCOMING_N = 5           # 各队未来 N 场赛程（判赛程密度/双线/轮换风险）
