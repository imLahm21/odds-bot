"""
配置中心 —— 联赛清单、关注庄家、轮询间隔、节点定义

所有 ID 均由阶段 0 探针（probe.py）实测确认，非猜测。
增删联赛只改这里，无需动其它代码（配置驱动）。
"""

import os

from dotenv import load_dotenv

# config 在导入链最前端被加载（daemon→db→config），早于 tgbot 的 load_dotenv()。
# 这里必须自己先加载 .env，否则模块体里 os.getenv(...) 读到的全是空
# （TELEGRAM_BROADCAST_TARGETS 曾因此读不到、/publish 后不弹通知按钮）。
load_dotenv()

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
BET_OVER_UNDER = 5       # 大小球（总进球数），value: "Over 2.5" / "Under 2.5"

# ─── 走地(滚球/in-play) bet 类型 ID ─────────────────────────────────────────
# ⚠️ /odds/live 的 bet id 体系与盘前 /odds 完全不同（已用 probe_live.py 实测）。
# 走地结构：盘口线在独立 handicap 字段，value 仅 Over/Under/Home/Away；
# 亚盘/大小球每盘带 main:true 标主盘口线；欧赔(59)无 main、直接取三条 value。
BET_LIVE_1X2 = 59             # 走地欧赔 Fulltime Result（无 main，三条 value 直取）
BET_LIVE_ASIAN_HANDICAP = 33  # 走地亚盘（取 main:true 主盘口线）
BET_LIVE_OVER_UNDER = 36      # 走地大小球 Over/Under Line（取 main:true 主盘口线）

# ─── 轮询间隔与临场窗口 ──────────────────────────────────────────────────────
TASK_A_HOURS = "2,14"    # 每日赛程更新：北京时间 02:00 和 14:00 各一次
TASK_A_MINUTE = 0        # 整点触发
TASK_B_HOURS = 1         # 常规赔率：每 1 小时（早节点初盘/中盘间隔大，
                         # 2h 一轮易漏抓；1h 让 -72/-48/-36/-24h 稳定落袋。
                         # Pro 额度 7500/日，每轮约 30 场，远未触顶）
TASK_C_MINUTES = 15      # 临场高频：每 15 分钟
NEAR_KICKOFF_HOURS = 2.0 # 任务 C 的"临场"定义：开球前 2 小时内
# 任务 D：临场冲刺档。开球前 SPRINT_KICKOFF_MINUTES 分钟内再加密到每
# TASK_D_MINUTES 分钟一次，专抓临场④(-0.5h)→即时这段"知情资金窗口"。
# 此窗口变盘最猛，15min 粒度偏粗，5min 把封盘前的最后异动采全。
# 临场比赛通常仅数场，每场每 5min 一次，Pro 额度(7500/日)绰绰有余。
TASK_D_MINUTES = 5
SPRINT_KICKOFF_MINUTES = 30  # 任务 D 的"冲刺"定义：开球前 30 分钟内

# ─── 任务 E：走地(滚球)实时播报 ─────────────────────────────────────────────
# 用户 /live 订阅一场进行中比赛，后台每 LIVE_MINUTES 分钟 Bulk 抓一次全部
# 进行中比赛(/odds/live 不带 fixture，一次拿全量)，按订阅 fid 过滤、检测异动、
# 有显著异动才推送。Bulk 方式成本与订阅数解耦，封顶 480 请求/天。
LIVE_MINUTES = 1              # 走地抓取/检测间隔（分钟）。1min：进球后封盘/重开/
                             # 水位剧调常在数十秒内完成，3min 粒度会漏掉关键窗口，
                             # 故加密到 1min（Bulk 一次拿全量，约 1440 请求/天，
                             # 由 LIVE_QUOTA_FLOOR=500 护栏在额度见底时自动暂停）。
LIVE_WATER_DELTA = 0.08      # 主水位跳变推送门槛（超过即视为显著异动）
LIVE_MAX_ELAPSED = 130       # 进行分钟数超此值仍在订阅中，视为已结束，自动退订
LIVE_QUOTA_FLOOR = 500       # 额度护栏：当日剩余额度 < 此值时暂停走地抓取
LIVE_MAX_SUBS_PER_CHAT = 3   # 每人最多同时订阅的走地场数（防滥用）
LIVE_ANCHOR_BM = 0           # 走地为聚合盘、不分庄家，快照 bookmaker_id 统一记 0

# API-Football fixture.status.short 状态码语义（走地阶段标注与终局判定用）。
# 进行中各阶段：1H上半 / HT中场 / 2H下半 / ET加时 / BT加时中场休息 / P点球大战 / LIVE通用进行中
# 终局：FT常规结束 / AET加时后结束 / PEN点球后结束
# 异常未开打/中断：TBD待定 / NS未开 / SUSP中断 / INT中断 / PST延期 / CANC取消 / ABD放弃 / AWD判负 / WO弃权
LIVE_STATUS_IN_PROGRESS = {"1H", "HT", "2H", "ET", "BT", "P", "LIVE"}
LIVE_STATUS_FINISHED = {"FT", "AET", "PEN"}          # 正常结束（有比分）
LIVE_STATUS_ABNORMAL_END = {"SUSP", "INT", "PST", "CANC", "ABD", "AWD", "WO"}  # 异常终止
# 走地阶段中文标签（推送里显示，让用户一眼看出是否进入加时/点球）
LIVE_PHASE_ZH = {
    "1H": "上半场", "HT": "中场", "2H": "下半场",
    "ET": "加时赛", "BT": "加时中场", "P": "点球大战",
    "LIVE": "进行中", "AET": "加时结束", "PEN": "点球结束", "FT": "完场",
}

# ─── 赛前抓取(任务 B/C/D)额度护栏 ────────────────────────────────────────────
# 走地(E)早有 LIVE_QUOTA_FLOOR 护栏，但占额度大头的赛前 B/C/D 一直裸奔——
# 只会被动撞 429 再切 key，两个 key 都耗尽就静默 None，且管理员无感知。
# 这里给 B/C/D 加额度刹车 + 管理员 TG 告警：每场抓取前检查 last_remaining()，
# 低于对应 floor 则本轮提前中止，并给管理员推一条当日去重告警。
#
# 分级护栏（让低优先的先停，保住要紧的高频抓取）：
#   任务 B(常规、每场每日抓 24 次、最吃额度) → 高 floor，最先停
#   任务 C/D(临场/冲刺、采的是最关键的封盘前异动) → 低 floor，最后停
#   任务 E(走地) → LIVE_QUOTA_FLOOR=500，最低，最后才停
# floor 越高越早触发；优先级 E ≈ C/D > B。
ODDS_QUOTA_FLOOR = 800        # 任务 B 默认护栏（最先停）
ODDS_QUOTA_FLOOR_NEAR = 550   # 任务 C/D 临场护栏（比 B 低，更晚停，略高于走地 500）
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
# /leagues 面板「改赛季」可选年份。watched_leagues.league_id 是主键，每个联赛只
# 持有一个 season；点某年即把该联赛的抓取赛季切到该年（覆盖旧值）。跨年联赛
# （2025-26）在 API-Football 里标记为起始年 2025，故列 2025~2030 覆盖近几季。
LEAGUE_SEASON_CHOICES = [2025, 2026, 2027, 2028, 2029, 2030]
PUBLISH_DATES_PER_PAGE = 8    # /publish 日期列表每页条数（随天数累积，需翻页）
TG_MSG_MAX = 4000             # 单条消息最大字符（Telegram 上限 4096，留余量）
# 访客每日 /analyze 次数上限（管理员不受限）。防额度被刷；0 或负数=不限制。
VISITOR_ANALYZE_DAILY_LIMIT = 10

# ─── /parlay 3串1串关分析（Beta）────────────────────────────────────────────
# 一次串关现场跑 3 场重档单场精算再串。配额「共享双扣」：
#   - 每腿各扣 1 次 VISITOR_ANALYZE_DAILY_LIMIT（共享桶，一次串关扣 3）；
#   - 另设串关专属日上限 VISITOR_PARLAY_DAILY_LIMIT（跑满 2 次 = 6 场）。
# 准入需同时满足：analyze 剩余 ≥ PARLAY_LEGS 且 parlay 剩余 ≥ 1。管理员不受限。
VISITOR_PARLAY_DAILY_LIMIT = 2   # 访客每日串关次数上限（0 或负数=不限制）
PARLAY_LEGS = 3                  # 只做 3串1（2串1 无 ×1.15 增益，见 reference_staking_kelly §5.2）
PARLAY_BCG_MULTIPLIER = 1.15     # BCG 平台 3串1 总赔率增益（抵消抽水连乘）
PARLAY_STAKE_BANKROLL = 100.0    # 凯利注额本金基准（与单场一致）
PARLAY_STAKE_CAP = 12.0          # 单注上限（$）
# 证据强度 → 凯利分数 k（同 reference_staking_kelly：强 1/2、中 1/4、弱 1/8、无 0）。
# 串关取三腿最弱腿的档位定 k（木桶效应）。
PARLAY_EVIDENCE_K = {"strong": 0.5, "medium": 0.25, "weak": 0.125, "none": 0.0}
PARLAY_MIN_EVIDENCE = "medium"   # 准入门槛：每腿证据须 ≥ 此档（§5.2 准入①）
# Beta 提示：/parlay 仍在打磨，各处引用此常量，日后一键去 Beta。
PARLAY_BETA_NOTICE = ("⚠️ /parlay 为 Beta 实验功能，串关判定仍在打磨，"
                      "结果仅供参考，请自行复核。")

# /publish 发布成功后可选广播的目标（群聊/频道）。从 .env 读：
#   TELEGRAM_BROADCAST_TARGETS=群聊|-1001111111,频道|-1002222222
# 每项 "标签|chat_id"，逗号分隔。标签是按钮上显示的中文名，chat_id 是群/频道的数字 id
# （群/超级群/频道一般是 -100 开头的负数）。发布完成后机器人按你勾选的目标推送文章链接。
# 注意：机器人必须已在该群里（频道则需设为频道管理员）才能发出，否则推送会失败。
def _parse_broadcast_targets(raw: str) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for item in raw.split(","):
        item = item.strip()
        if not item or "|" not in item:
            continue
        label, _, cid = item.partition("|")
        label, cid = label.strip(), cid.strip()
        if label and cid.lstrip("-").isdigit():
            out.append((label, int(cid)))
    return out


TELEGRAM_BROADCAST_TARGETS = _parse_broadcast_targets(
    os.getenv("TELEGRAM_BROADCAST_TARGETS", ""))

# ─── 旧数据清理 ──────────────────────────────────────────────────────────────
CLEANUP_DAYS = 30             # 删除开球早于 N 天前的比赛及其快照

# ─── LLM 精算 (/analyze) ─────────────────────────────────────────────────────
# IKuncode（OpenAI 兼容），从 .env 读：
#   LLM_BASE_URL=https://api.ikuncode.cc/v1
#   LLM_API_KEY=sk-xxx
# LLM_MODEL 是【重档默认模型】的兼容常量：等于 LLM_TIER_MODELS["heavy"]["default"]。
# 运行时实际用哪个重档模型由 db.llm_runtime_state 决定（/llm 面板可切 sol/5.5），
# llm_client 按【档位 tier】而非模型名路由（见 _resolve_model）。此常量供 probe_llm 等直读兜底。
LLM_MODEL = "gpt-5.6-sol"
LLM_TIMEOUT = 300             # 秒，gpt-5.5 high 推理 + 长报告，给足超时
LLM_MAX_TOKENS = 32000        # 报告输出上限。gpt-5.5 是推理模型，先消耗大量
                              # reasoning token 再写正文；规则 system prompt 约
                              # 7万字符，上限太低（曾设8000）会在推理阶段就被吃光、
                              # 正文为空 → "LLM 返回空内容"。放宽到 32000 留足空间。
# 推理强度（reasoning_effort）档位：/analyze 选完预设/自定义后再选一档。
# key = 传给 LLM 的 reasoning_effort 值（OpenAI 兼容字段），value = TG 按钮中文标签。
# gpt-5.5/Codex 系支持 xhigh（超高）扩展档；IKuncode 网关透传。
LLM_EFFORT_LABELS: dict[str, str] = {
    "low":    "低",
    "medium": "普通",
    "high":   "高",
    "xhigh":  "极高",
    "max":    "最高",
    "ultra":  "超高",
}
# 访客可选档位（管理员不受限，可用全部 LLM_EFFORT_LABELS）。
# 新增的极高/最高/超高为实验档，仅管理员可用（新档不稳定时只影响管理员自己）。
LLM_EFFORT_VISITOR_ALLOWED: set[str] = {"low", "medium", "high"}
# 默认强度：未显式选择时用（如旧入口直接调 analyze 不带 effort 则传空=不附带字段）
LLM_EFFORT_DEFAULT = "high"

# ─── 三档模型（重/平衡/轻）运行时可选（TG /llm 面板切换，落 db.llm_runtime_state）──
# 新出的 gpt-5.6 系不稳定，用户要能在新旧模型间随时切换免重启。这里是三档候选的
# 【唯一真相源】：db seed 读 default 灌初值、TG 面板展示 choices 供轮换、set 时校验 val∈choices。
#   heavy    —— 主 SOP 精算（/analyze /review）
#   balanced —— 基本面预分析 + SEO/科普段
#   light    —— 走地实时研判
# visitor=True 的档访客可用（访客不能用 heavy）。
# 每档存两份运行时选定：管理员自己用的（default/choices）+ 访客用的（visitor_default/visitor_choices）。
# 访客不碰重模型：重档的访客候选 = 平衡/轻模型（terra/mini），默认 terra；
# 平衡/轻档访客候选同管理员。管理员可在 /llm 面板把两份各自再调。
#   choices          —— 管理员该档可选模型
#   visitor_choices  —— 访客该档可选模型（省略则同 choices）
#   default          —— 管理员该档初值
#   visitor_default  —— 访客该档初值（省略则同 default）
LLM_TIER_MODELS: dict[str, dict] = {
    "heavy":    {"label": "重档·主精算", "choices": ["gpt-5.6-sol", "gpt-5.5"],
                 "default": "gpt-5.6-sol",
                 "visitor_choices": ["gpt-5.6-terra", "gpt-5.4-mini"],
                 "visitor_default": "gpt-5.6-terra"},
    "balanced": {"label": "平衡·基本面/SEO", "choices": ["gpt-5.6-terra", "gpt-5.4-mini"],
                 "default": "gpt-5.6-terra",
                 "visitor_choices": ["gpt-5.6-terra", "gpt-5.4-mini"],
                 "visitor_default": "gpt-5.6-terra"},
    "light":    {"label": "轻档·走地", "choices": ["gpt-5.6-luna", "gpt-5.4-mini"],
                 "default": "gpt-5.6-luna",
                 "visitor_choices": ["gpt-5.6-luna", "gpt-5.4-mini"],
                 "visitor_default": "gpt-5.6-luna"},
}


def llm_tier_choices(tier: str, visitor: bool = False) -> list[str]:
    """取某档某角色的可选模型列表。访客用 visitor_choices（缺则回退 choices）。"""
    spec = LLM_TIER_MODELS.get(tier, {})
    if visitor:
        return spec.get("visitor_choices", spec.get("choices", []))
    return spec.get("choices", [])


def llm_tier_default(tier: str, visitor: bool = False) -> str:
    """取某档某角色的默认模型。访客用 visitor_default（缺则回退 default）。"""
    spec = LLM_TIER_MODELS.get(tier, {})
    if visitor:
        return spec.get("visitor_default", spec.get("default", ""))
    return spec.get("default", "")
# 一键回退：新 5.6 三档都出问题时，管理员在 /llm 点「↩️ 回退旧模型」一次性切回升级前方案
# ——主 SOP 用 gpt-5.5，走地/基本面/SEO 全用 gpt-5.4-mini（本功能引入前的确切行为）。
# 回退值即便不在上面 choices 里也允许写入（回退优先级最高，见 llm_client.apply_legacy_models）。
LLM_LEGACY_TIER_MODELS: dict[str, str] = {
    "heavy": "gpt-5.5", "balanced": "gpt-5.4-mini", "light": "gpt-5.4-mini",
}

# ─── LLM 故障转移 + 熔断器 可调参数（TG /llm 面板实时改，落 db.llm_settings）───
# 这里是这 9 个参数的【唯一真相源】：db seed 读它灌默认值、TG 面板展示/校验读它、
# llm_client 无 DB 值时兜底也读它。修改默认/范围只改这一处。
#   key   —— 存 db.llm_settings 的主键，也是 TG 回调 ls:<key> 的标识
#   default/min/max —— 默认值与合法闭区间（TG 改值越界即拒）
#   label —— TG 面板中文名
#   help  —— TG 面板补充说明（越界提示也用它）
# 说明：主 SOP 精算走【流式】，用 stream_first_byte_timeout + stream_idle_timeout；
# non_stream_timeout 只作用于未显式传超时的阻塞调用（走地/基本面/SEO 各有自己的短超时，
# 优先级更高、不受此值影响）。故调这些默认值不会回归已调好的走地/基本面时效。
LLM_SETTING_SPECS: dict[str, dict] = {
    "max_retries": {
        "default": 2, "min": 0, "max": 10,
        "label": "最大重试次数", "help": "单端点请求失败时的重试次数（0-10）"},
    "degrade_threshold": {
        "default": 1, "min": 1, "max": 10,
        "label": "降级阈值", "help": "连续失败多少次进入降级预警（仍在用但选路降优先，须≤失败阈值）"},
    "failure_threshold": {
        "default": 3, "min": 3, "max": 10,
        "label": "失败阈值", "help": "连续失败多少次后打开熔断器（3-10）"},
    "stream_first_byte_timeout": {
        "default": 60, "min": 1, "max": 120,
        "label": "流式首字节超时", "help": "等待首个数据块的最大秒数（1-120，默认60）"},
    "stream_idle_timeout": {
        "default": 90, "min": 0, "max": 600,
        "label": "流式静默超时", "help": "数据块之间最大间隔秒数（60-600，填0禁用）"},
    "non_stream_timeout": {
        "default": 180, "min": 60, "max": 1200,
        "label": "非流式超时", "help": "非流式请求总超时秒数（60-1200，默认180）"},
    "recovery_success_threshold": {
        "default": 3, "min": 1, "max": 20,
        "label": "恢复成功阈值", "help": "半开状态下成功多少次后关闭熔断器"},
    "recovery_wait_seconds": {
        "default": 90, "min": 30, "max": 120,
        "label": "恢复等待时间", "help": "熔断打开后等待多久尝试恢复（30-120秒）"},
    "error_rate_threshold_pct": {
        "default": 70, "min": 1, "max": 100,
        "label": "错误率阈值%", "help": "滚动错误率超此值打开熔断器（1-100）"},
    "min_requests": {
        "default": 15, "min": 1, "max": 200,
        "label": "最小请求数", "help": "计算错误率前的最小请求数"},
}

# ─── 走地(滚球)实时研判专用 LLM ──────────────────────────────────────────────
# 走地 live_brief 跑在 1min 一轮的广播循环里、是同步阻塞调用，要的是【秒级】反应。
# gpt-5.5(推理模型)先烧大量 reasoning token 再出正文，单次可能几十秒~1min+，会拖住
# 下一轮抓取。故走地单独用轻量模型 + 最低推理档 + 短超时 + 小输出（研判仅 3~5 句）。
# 赛前 7 步精算继续用上面的 LLM_MODEL=gpt-5.5 high 不变。
# LLM_LIVE_MODEL 是【轻档默认模型】兼容常量（=LLM_TIER_MODELS["light"]["default"]）；走地用。
# 运行时实际模型由 db.llm_runtime_state 决定（/llm 可切 luna/mini），按档位路由。
LLM_LIVE_MODEL = "gpt-5.6-luna"   # 走地轻量模型（轻档默认）
LLM_LIVE_EFFORT = "low"           # 走地推理强度（最低档，求快）
LLM_LIVE_TIMEOUT = 30             # 走地超时（秒）：超过即跳过研判，不阻塞盘口快报
LLM_LIVE_MAX_TOKENS = 1200        # 走地输出上限：留足 mini 的少量推理 + 3~5 句正文
# ─── 基本面分析（/analyze 精算前的两阶段预处理）─────────────────────────────
# 用轻量模型先把 build_fundamentals 的原始数据（近况/交锋/赛程/积分榜）依据
# 国家队/赛事情境/大小球方法论规则，分析成一份「基本面研判」，再喂给主 SOP 精算。
# 好处：mini 专注读数据出研判，gpt-5.5 专注盘口精算，职责分离。
FUND_ANALYZE_MODEL = "gpt-5.6-terra"  # 【平衡档默认模型】兼容常量；基本面/SEO 用（运行时可切）
FUND_ANALYZE_EFFORT = "medium"        # 基本面研判要点判断，比走地 low 略高
FUND_ANALYZE_TIMEOUT = 90             # 秒，比走地 30 长（研判内容多），比主精算 300 短
FUND_ANALYZE_MAX_TOKENS = 4000        # 研判输出上限（走地 1200 太小，主精算 32000 太大）
# 基本面分析专用规则（只加载基本面相关方法论，不加载全套 SOP，减负提速）
FUND_ANALYZE_RULE_FILES = [
    "rules/方法论/reference_national_team.md",
    "rules/方法论/reference_competition_context.md",
    "rules/方法论/reference_over_under.md",
]
# 全量规则文件（相对项目根），按顺序拼接成 system prompt
ANALYZE_RULE_FILES = [
    "CLAUDE.md",
    "rules/方法论/reference_asian_handicap.md",
    "rules/方法论/reference_dynamic_analysis.md",
    "rules/方法论/reference_over_under.md",
    "rules/方法论/reference_national_team.md",
    "rules/方法论/reference_competition_context.md",
    "rules/风控验证/reference_kelly_index.md",
    "rules/风控验证/reference_staking_kelly.md",
    "rules/实战教训/reference_case_lessons.md",
    "rules/实战教训/reference_cases.md",
]
# 走地(滚球)研判规则：独立加载，不混进赛前 SOP 规则缓存
LIVE_RULE_FILES = [
    "rules/方法论/reference_live.md",
]
# 基本面拉取参数
FUND_RECENT_N = 10            # 各队近 N 场
FUND_H2H_N = 10               # 交锋近 N 场
FUND_UPCOMING_N = 5           # 各队未来 N 场赛程（判赛程密度/双线/轮换风险）

# ─── /add 联赛搜索：中文→英文关键词映射 ──────────────────────────────────────
# API-Football 的 /leagues?search= 只认英文，中文搜不到（如「足协杯」返回空）。
# 这里把常用中文名/简称映射到 API 库里的英文关键词；命中则用英文去搜。
# 搜国家名（如 China）会返回该国全部联赛+杯赛，足够覆盖。
LEAGUE_SEARCH_ALIASES: dict[str, str] = {
    # 杯赛（API 多叫 "FA Cup"/"Cup"，搭配国家名搜更准 → 直接映射到国家名）
    "足协杯": "China", "中国足协杯": "China", "中国杯": "China",
    "英格兰足总杯": "England", "足总杯": "England",
    "国王杯": "Spain", "西班牙国王杯": "Spain",
    "德国杯": "Germany", "意大利杯": "Italy", "法国杯": "France",
    # 国家（搜国家名会列出该国所有赛事）
    "中国": "China", "英格兰": "England", "英国": "England",
    "西班牙": "Spain", "德国": "Germany", "意大利": "Italy",
    "法国": "France", "荷兰": "Netherlands", "葡萄牙": "Portugal",
    "瑞典": "Sweden", "挪威": "Norway", "丹麦": "Denmark",
    "日本": "Japan", "韩国": "South-Korea", "美国": "USA",
    "巴西": "Brazil", "阿根廷": "Argentina", "墨西哥": "Mexico",
    "沙特": "Saudi-Arabia", "土耳其": "Turkey", "俄罗斯": "Russia",
    "比利时": "Belgium", "苏格兰": "Scotland", "瑞士": "Switzerland",
    "奥地利": "Austria", "希腊": "Greece", "波兰": "Poland",
    "冰岛": "Iceland", "澳大利亚": "Australia", "澳洲": "Australia",
    # 联赛常用简称
    "中超": "China", "英超": "England", "西甲": "Spain",
    "德甲": "Germany", "意甲": "Italy", "法甲": "France",
    "日职": "Japan", "韩K联": "South-Korea", "美职联": "USA",
    "世界杯": "World Cup", "欧冠": "UEFA Champions",
    "欧联": "UEFA Europa", "欧国联": "UEFA Nations",
}

# ─── league_id → 中文名：/status、/fixtures 等展示时给英文库名加中文注释 ────────
# 服务器库里 league_name 多为英文（如 "Premier League"、"Super League"），
# 展示时按 league_id 查这张表，渲染成「中文 英文」便于辨认。
# 同名英文联赛（Super League / Premiership / Premier Division）必须靠 id 区分国家。
# 维护：新增启用联赛时把它的 league_id 补进来即可；查不到则原样显示英文。
LEAGUE_ZH_NAMES: dict[int, str] = {
    # 五大联赛
    39:  "英超", 140: "西甲", 78: "德甲", 135: "意甲", 61: "法甲",
    # 其他顶级国内联赛
    88:  "荷甲", 94: "葡超", 144: "比甲", 164: "冰岛超", 244: "芬兰超",
    103: "挪超", 113: "瑞典超", 365: "拉脱超",
    169: "中超", 179: "苏超", 357: "爱尔兰超", 307: "沙特联",
    98:  "日职联", 292: "韩K联", 188: "澳超", 253: "美职联",
    71:  "巴西甲", 262: "墨超",
    # 顶级杯赛
    45:  "英格兰足总杯", 171: "中国足协杯", 143: "西班牙国王杯",
    81:  "德国杯", 137: "意大利杯", 66: "法国杯",
    # 跨国/国家队
    2:   "欧冠", 3: "欧联", 848: "欧协联", 1: "世界杯", 5: "欧国联",
}


def league_label(league_id: int | None, league_name: str | None) -> str:
    """把英文库名渲染成「中文 英文」；查不到中文或库名已含中文则原样返回。"""
    name = (league_name or "").strip()
    zh = LEAGUE_ZH_NAMES.get(league_id) if league_id is not None else None
    if not zh:
        return name or (str(league_id) if league_id is not None else "")
    # 库名本身已带中文（如「冰岛超 Úrvalsdeild」）就不重复加
    if any('一' <= c <= '鿿' for c in name):
        return name
    return f"{zh} {name}".strip() if name else zh
