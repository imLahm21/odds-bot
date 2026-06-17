"""
The Odds API 足球赔率抓取脚本（交互式）
功能：
  1. 选择联赛 → 选择比赛 → 选择博彩公司
  2. 查看当前即时盘口快照
  3. 查看指定时间段内的赔率走势（历史盘口变化）
所有时间均以东八区（北京时间/CST）显示和输入
"""

import os
import sys
import requests
import pandas as pd
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

# ─── 初始化 ───────────────────────────────────────────────────────────────────
load_dotenv()

# 支持多 API Key 自动切换：主 Key 额度耗尽时自动切换到备用 Key
_API_KEYS: list[dict] = []

_primary = os.getenv("ODDS_API_KEY", "").strip()
_backup  = os.getenv("ODDS_API_KEY_BACKUP", "").strip()

if _primary:
    _API_KEYS.append({"key": _primary, "label": "主Key", "exhausted": False})
if _backup:
    _API_KEYS.append({"key": _backup, "label": "备用Key", "exhausted": False})

if not _API_KEYS:
    sys.exit("未找到任何 API Key，请在 .env 中配置 ODDS_API_KEY")

_current_key_index = 0


def get_api_key() -> str:
    """获取当前可用的 API Key"""
    return _API_KEYS[_current_key_index]["key"]


def get_api_key_label() -> str:
    """获取当前 Key 的标签"""
    return _API_KEYS[_current_key_index]["label"]


def switch_api_key() -> bool:
    """切换到下一个可用的 Key，返回是否切换成功"""
    global _current_key_index
    for i in range(len(_API_KEYS)):
        candidate = (i + _current_key_index + 1) % len(_API_KEYS)
        if not _API_KEYS[candidate]["exhausted"]:
            _current_key_index = candidate
            print(f"  [Key切换] 已切换至 {_API_KEYS[candidate]['label']}")
            return True
    return False


TZ_CST   = timezone(timedelta(hours=8))
TZ_UTC   = timezone.utc
BASE_URL = "https://api.the-odds-api.com/v4"

# 联赛分组：主菜单直接展示 + 折叠子菜单
SPORTS_MAIN = [
    # ── 五大联赛 ──
    ("soccer_epl",                               "英超 EPL"),
    ("soccer_spain_la_liga",                     "西甲 La Liga"),
    ("soccer_germany_bundesliga",                "德甲 Bundesliga"),
    ("soccer_italy_serie_a",                     "意甲 Serie A"),
    ("soccer_france_ligue_one",                  "法甲 Ligue 1"),
    # ── 欧洲杯赛 ──
    ("soccer_uefa_champs_league",                "欧冠 Champions League"),
    ("soccer_uefa_europa_league",                "欧联 Europa League"),
    ("soccer_uefa_europa_conference_league",     "欧协联 Conference League"),
    ("soccer_uefa_nations_league",               "欧国联 Nations League"),
    # ── 国际大赛 ──
    ("soccer_fifa_world_cup",                    "世界杯 FIFA World Cup"),
    # ── 欧洲其他顶级 ──
    ("soccer_netherlands_eredivisie",            "荷甲 Eredivisie"),
    ("soccer_portugal_primeira_liga",            "葡超 Primeira Liga"),
    ("soccer_belgium_first_div",                 "比甲 First Div"),
    ("soccer_turkey_super_league",               "土超 Super League"),
    ("soccer_spl",                               "苏超 Premiership"),
    # ── 亚洲 ──
    ("soccer_china_superleague",                 "中超 CSL"),
    ("soccer_japan_j_league",                    "日职联 J League"),
    ("soccer_korea_kleague1",                    "韩K联1 K League 1"),
    ("soccer_saudi_arabia_pro_league",           "沙特联 Saudi Pro League"),
    # ── 其他常用 ──
    ("soccer_australia_aleague",                 "澳超 A-League"),
]

SPORTS_MORE: list[tuple[str, list[tuple[str, str]]]] = [
    ("英格兰次级联赛 & 杯赛", [
        ("soccer_efl_champ",                     "英冠 Championship"),
        ("soccer_england_league1",               "英甲 League 1"),
        ("soccer_england_league2",               "英乙 League 2"),
        ("soccer_fa_cup",                        "足总杯 FA Cup"),
        ("soccer_england_efl_cup",               "联赛杯 EFL Cup"),
    ]),
    ("欧洲次级联赛", [
        ("soccer_spain_segunda_division",        "西乙 La Liga 2"),
        ("soccer_germany_bundesliga2",           "德乙 Bundesliga 2"),
        ("soccer_germany_liga3",                 "德丙 3. Liga"),
        ("soccer_italy_serie_b",                 "意乙 Serie B"),
        ("soccer_france_ligue_two",              "法乙 Ligue 2"),
        ("soccer_russia_premier_league",         "俄超 Premier League"),
        ("soccer_greece_super_league",           "希超 Super League"),
        ("soccer_switzerland_superleague",       "瑞超 Swiss Superleague"),
        ("soccer_austria_bundesliga",            "奥甲 Bundesliga"),
        ("soccer_poland_ekstraklasa",            "波超 Ekstraklasa"),
        ("soccer_league_of_ireland",             "爱尔兰联 League of Ireland"),
    ]),
    ("北欧联赛", [
        ("soccer_denmark_superliga",             "丹超 Superliga"),
        ("soccer_sweden_allsvenskan",            "瑞典超 Allsvenskan"),
        ("soccer_sweden_superettan",             "瑞典乙 Superettan"),
        ("soccer_norway_eliteserien",            "挪超 Eliteserien"),
        ("soccer_finland_veikkausliiga",         "芬超 Veikkausliiga"),
    ]),
    ("欧洲杯赛（附加）", [
        ("soccer_uefa_champs_league_qualification", "欧冠资格赛"),
        ("soccer_uefa_european_championship",       "欧洲杯 UEFA Euro"),
        ("soccer_uefa_euro_qualification",          "欧洲杯预选赛"),
    ]),
    ("各国国内杯赛", [
        ("soccer_germany_dfb_pokal",             "德国杯 DFB-Pokal"),
        ("soccer_italy_coppa_italia",            "意大利杯 Coppa Italia"),
        ("soccer_spain_copa_del_rey",            "西班牙国王杯 Copa del Rey"),
        ("soccer_france_coupe_de_france",        "法国杯 Coupe de France"),
    ]),
    ("美洲联赛 & 杯赛", [
        ("soccer_usa_mls",                       "美职联 MLS"),
        ("soccer_mexico_ligamx",                 "墨西哥甲 Liga MX"),
        ("soccer_argentina_primera_division",    "阿根廷甲 Primera División"),
        ("soccer_brazil_campeonato",             "巴西甲 Série A"),
        ("soccer_brazil_serie_b",                "巴西乙 Série B"),
        ("soccer_chile_campeonato",              "智利甲 Primera División"),
        ("soccer_conmebol_copa_libertadores",    "南美解放者杯 Copa Libertadores"),
        ("soccer_conmebol_copa_sudamericana",    "南美杯 Copa Sudamericana"),
        ("soccer_conmebol_copa_america",         "美洲杯 Copa América"),
        ("soccer_concacaf_leagues_cup",          "联合会联赛杯 Leagues Cup"),
    ]),
    ("女子 & 国际赛事", [
        ("soccer_uefa_champs_league_women",      "女子欧冠"),
        ("soccer_germany_bundesliga_women",      "德国女足甲级 Frauen-Bundesliga"),
        ("soccer_fifa_world_cup_womens",         "女足世界杯"),
        ("soccer_fifa_club_world_cup",           "世俱杯 FIFA Club World Cup"),
        ("soccer_fifa_world_cup_qualifiers_europe",        "世预赛欧洲区"),
        ("soccer_fifa_world_cup_qualifiers_south_america", "世预赛南美区"),
        ("soccer_africa_cup_of_nations",         "非洲杯 Africa Cup of Nations"),
        ("soccer_concacaf_gold_cup",             "中北美金杯 Gold Cup"),
    ]),
]

# 博彩公司 key → (英文名, 中文名/备注)
# 按 The Odds API 官方 region 分组，每组对应一个 regions 参数值
# region 说明：eu / uk / us / au / fr / se（每 10 家算一个 region 额度）

# ── EU 区（欧洲大陆）──
_BM_EU: dict[str, tuple[str, str]] = {
    "pinnacle":        ("Pinnacle",              "平博 ★亚盘参考首选★"),
    "bet365":          ("Bet365",                "365 ★欧指参考首选★"),
    "betfair_ex_eu":   ("Betfair Exchange (EU)", "必发交易所(欧) ★欧指交易所首选★"),
    "williamhill":     ("William Hill",          "威廉希尔"),
    "marathonbet":     ("Marathon Bet",          "马拉松 ★亚盘参考推荐★"),
    "onexbet":         ("1xBet",                 "1xBet ★亚盘参考推荐★"),
    "unibet_nl":       ("Unibet (NL)",           "联博(荷兰)"),
    "unibet_it":       ("Unibet (IT)",           "联博(意大利)"),
    "unibet_fr":       ("Unibet (FR)",           "联博(法国)"),
    "unibet_se":       ("Unibet (SE)",           "联博(瑞典)"),
    "betvictor":       ("Bet Victor",            "必胜客"),
    "sport888":        ("888sport",              "888体育"),
    "betsson":         ("Betsson",               "百胜 北欧庄"),
    "nordicbet":       ("NordicBet",             "北欧博彩"),
    "leovegas_se":     ("LeoVegas (SE)",         "雄狮维加斯(瑞典)"),
    "coolbet":         ("Coolbet",               "Coolbet 北欧"),
    "matchbook":       ("Matchbook",             "Matchbook 交易所"),
    "betclic_fr":      ("Betclic (FR)",          "百利(法国)"),
    "winamax_fr":      ("Winamax (FR)",          "Winamax(法国)"),
    "winamax_de":      ("Winamax (DE)",          "Winamax(德国)"),
    "pmu_fr":          ("PMU (FR)",              "法国赛马彩票"),
    "parionssport_fr": ("Parions Sport (FR)",    "法国体育彩票"),
    "tipico_de":       ("Tipico (DE)",           "Tipico(德国)"),
    "codere_it":       ("Codere (IT)",           "科德雷(意大利) ★意甲参考★"),
    "gtbets":          ("GTbets",                "GTbets"),
    "suprabets":       ("Suprabets",             "Suprabets"),
    "everygame":       ("Everygame",             "Everygame"),
    "betonlineag":     ("BetOnline.ag",          "BetOnline 美洲"),
    "betanysports":    ("BetAnySports",          "BetAnySports"),
    "mybookieag":      ("MyBookie.ag",           "MyBookie"),
}

# ── UK 区（英国/爱尔兰）──
_BM_UK: dict[str, tuple[str, str]] = {
    "betfair_ex_uk":   ("Betfair Exchange (UK)", "必发交易所(英) ★欧指交易所首选★"),
    "betfair_sb_uk":   ("Betfair Sportsbook",    "必发固定赔率(英)"),
    "williamhill":     ("William Hill",          "威廉希尔"),
    "ladbrokes_uk":    ("Ladbrokes",             "立博 老牌英国庄"),
    "betway":          ("Betway",                "Betway 综合庄"),
    "betvictor":       ("Bet Victor",            "必胜客"),
    "sport888":        ("888sport",              "888体育"),
    "skybet":          ("Sky Bet",               "天空投注"),
    "paddypower":      ("Paddy Power",           "帕迪鲍尔 爱尔兰老牌"),
    "coral":           ("Coral",                 "珊瑚 英国老牌"),
    "boylesports":     ("BoyleSports",           "博尔体育 爱尔兰"),
    "leovegas":        ("LeoVegas",              "雄狮维加斯"),
    "smarkets":        ("Smarkets",              "Smarkets 英国交易所"),
    "matchbook":       ("Matchbook",             "Matchbook 交易所"),
    "grosvenor":       ("Grosvenor",             "格罗夫纳"),
    "virginbet":       ("Virgin Bet",            "维珍投注"),
    "livescorebet":    ("LiveScore Bet",         "Livescore投注"),
    "casumo":          ("Casumo",                "Casumo"),
    "unibet_uk":       ("Unibet (UK)",           "联博(英国)"),
}

# ── AU 区（澳大利亚）──
_BM_AU: dict[str, tuple[str, str]] = {
    "betfair_ex_au":   ("Betfair Exchange (AU)", "必发交易所(澳)"),
    "bet365_au":       ("Bet365 (AU)",           "365(澳大利亚)"),
    "ladbrokes_au":    ("Ladbrokes (AU)",        "立博(澳大利亚)"),
    "sportsbet":       ("SportsBet",             "SportsBet 澳洲"),
    "tab":             ("TAB",                   "TAB 澳洲"),
    "tabtouch":        ("TABtouch",              "TABtouch 澳洲"),
    "unibet":          ("Unibet (AU)",           "联博(澳大利亚)"),
    "neds":            ("Neds",                  "Neds 澳洲"),
    "pointsbetau":     ("PointsBet (AU)",        "PointsBet 澳洲"),
    "betr_au":         ("Betr (AU)",             "Betr 澳洲"),
    "betright":        ("Bet Right",             "Bet Right 澳洲"),
    "playup":          ("PlayUp",                "PlayUp 澳洲"),
    "dabble_au":       ("Dabble (AU)",           "Dabble 澳洲"),
}

# ── US 区（美国）──
_BM_US: dict[str, tuple[str, str]] = {
    "draftkings":      ("DraftKings",            "DraftKings 美国"),
    "fanduel":         ("FanDuel",               "FanDuel 美国"),
    "betmgm":          ("BetMGM",                "BetMGM 美国"),
    "williamhill_us":  ("Caesars",               "凯撒 美国"),
    "betrivers":       ("BetRivers",             "BetRivers 美国"),
    "betonlineag":     ("BetOnline.ag",          "BetOnline 美洲"),
    "bovada":          ("Bovada",                "Bovada 美洲"),
    "betus":           ("BetUS",                 "BetUS 美洲"),
    "mybookieag":      ("MyBookie.ag",           "MyBookie"),
    "fanatics":        ("Fanatics",              "Fanatics 美国"),
    "ballybet":        ("Bally Bet",             "Bally Bet 美国"),
    "betparx":         ("betPARX",               "betPARX 美国"),
    "espnbet":         ("ESPN Bet",              "ESPN Bet 美国"),
    "lowvig":          ("LowVig.ag",             "LowVig 美洲"),
    "betanysports":    ("BetAnySports",          "BetAnySports"),
}

# ── SE 区（瑞典）──
_BM_SE: dict[str, tuple[str, str]] = {
    "betsson":         ("Betsson",               "百胜 北欧庄"),
    "nordicbet":       ("NordicBet",             "北欧博彩"),
    "leovegas_se":     ("LeoVegas (SE)",         "雄狮维加斯(瑞典)"),
    "unibet_se":       ("Unibet (SE)",           "联博(瑞典)"),
    "sport888_se":     ("888sport (SE)",         "888体育(瑞典)"),
    "atg_se":          ("ATG (SE)",              "ATG 瑞典"),
    "campobet_se":     ("CampoBet (SE)",         "CampoBet 瑞典"),
    "mrgreen_se":      ("Mr Green (SE)",         "Mr Green 瑞典"),
    "svenskaspel_se":  ("Svenska Spel",          "Svenska Spel 瑞典"),
}

# ── 合并全表（用于展示和凯利计算）──
BOOKMAKER_INFO: dict[str, tuple[str, str]] = {
    **_BM_EU, **_BM_UK, **_BM_AU, **_BM_US, **_BM_SE,
}

# 每个 key 所属的 API region（用于自动推算 regions 参数）
# 跨区公司（如 williamhill、matchbook）取主要区域
BOOKMAKER_REGION: dict[str, str] = {
    **{k: "eu" for k in _BM_EU},
    **{k: "uk" for k in _BM_UK},
    **{k: "au" for k in _BM_AU},
    **{k: "us" for k in _BM_US},
    **{k: "se" for k in _BM_SE},
}

# 按区域分组展示（用于选择界面）
BOOKMAKER_GROUPS: list[tuple[str, dict]] = [
    ("EU 欧洲大陆", _BM_EU),
    ("UK 英国/爱尔兰", _BM_UK),
    ("AU 澳大利亚", _BM_AU),
    ("US 美国", _BM_US),
    ("SE 瑞典", _BM_SE),
]

# 推荐关注的博彩公司（核心4家：平博+必发EU+威廉+立博UK）
RECOMMEND_KEYS = {"pinnacle", "betfair_ex_eu", "williamhill", "ladbrokes_uk"}

# 按联赛推荐的 3 家组合
# 风控双锚（SOP 凯利报警需要，故每场保留）：
#   pinnacle —— 亚盘风控锚，<0.96 报警；全球最 sharp、低水位、收 sharp money
#   bet365   —— 欧指基准锚，>1.03 报警；全球最大流动性，欧指标准
# 第三家 = 按联赛特性挑「该联赛真实主力资金/最 sharp 信号」所在的庄源，逐联赛甄选：
#   betfair_ex_uk —— 英伦足球纯市场共识（交易所，英超/英冠等高流动性英赛）
#   betfair_ex_eu —— 欧陆交易所共识（仅用于高流动性大赛：欧冠/欧联/国家队大赛/世界杯）
#   marathonbet   —— 俄系低水位、信号干净；俄超/CIS + 广义亚盘 sharp 参考
#   onexbet(1xBet)—— 新兴市场本地主力资金（土耳其/沙特/希腊·巴尔干/波兰/非洲/华人）
#   betonlineag   —— 离岸美洲，南美足球早盘 sharp 首挂；南美各级联赛与杯赛
#   tipico_de     —— 德语区本土龙头（德甲/德乙/德丙/奥甲/德国杯/德女）
#   winamax_fr    —— 法国市场龙头（法甲/法乙/法国杯）
#   codere_it     —— 意大利持牌本土（意甲/意乙/意大利杯）
#   unibet_nl     —— 荷兰本土主力（荷甲）
#   sportsbet     —— 澳洲市场龙头（澳超）
#   draftkings    —— 美国主力流动性（美职联/金杯/联合会联赛杯）
#   betsson/nordicbet —— 北欧本地资金
#   williamhill/ladbrokes_uk/paddypower —— 英伦高街/爱尔兰本土资金（中低级别英赛、杯赛）
LEAGUE_BM_MAP: dict[str, list[str]] = {
    # ── 五大联赛（全球流动性，本地资金 / 英伦交易所代表）──
    "soccer_epl":                                ["pinnacle", "bet365", "betfair_ex_uk"],
    "soccer_spain_la_liga":                      ["pinnacle", "bet365", "marathonbet"],
    "soccer_germany_bundesliga":                 ["pinnacle", "bet365", "tipico_de"],
    "soccer_italy_serie_a":                      ["pinnacle", "bet365", "codere_it"],
    "soccer_france_ligue_one":                   ["pinnacle", "bet365", "winamax_fr"],
    # ── 欧洲杯赛（大赛用交易所，冷门多的用亚盘 sharp）──
    "soccer_uefa_champs_league":                 ["pinnacle", "bet365", "betfair_ex_eu"],
    "soccer_uefa_europa_league":                 ["pinnacle", "bet365", "betfair_ex_eu"],
    "soccer_uefa_europa_conference_league":      ["pinnacle", "bet365", "marathonbet"],
    "soccer_uefa_nations_league":                ["pinnacle", "bet365", "betfair_ex_eu"],
    "soccer_uefa_champs_league_qualification":   ["pinnacle", "bet365", "marathonbet"],
    "soccer_uefa_european_championship":         ["pinnacle", "bet365", "betfair_ex_eu"],
    "soccer_uefa_euro_qualification":            ["pinnacle", "bet365", "marathonbet"],
    # ── 欧洲其他顶级 ──
    "soccer_netherlands_eredivisie":             ["pinnacle", "bet365", "unibet_nl"],
    "soccer_portugal_primeira_liga":             ["pinnacle", "bet365", "marathonbet"],
    "soccer_belgium_first_div":                  ["pinnacle", "bet365", "marathonbet"],
    "soccer_turkey_super_league":                ["pinnacle", "bet365", "onexbet"],
    "soccer_spl":                                ["pinnacle", "bet365", "williamhill"],
    # ── 亚洲 ──
    "soccer_china_superleague":                  ["pinnacle", "bet365", "onexbet"],
    "soccer_japan_j_league":                     ["pinnacle", "bet365", "marathonbet"],
    "soccer_korea_kleague1":                     ["pinnacle", "bet365", "marathonbet"],
    "soccer_saudi_arabia_pro_league":            ["pinnacle", "bet365", "onexbet"],
    # ── 澳超 ──
    "soccer_australia_aleague":                  ["pinnacle", "bet365", "sportsbet"],
    # ── 英格兰次级 & 杯赛（高流动性用英盘交易所，低级别用高街庄）──
    "soccer_efl_champ":                          ["pinnacle", "bet365", "betfair_ex_uk"],
    "soccer_england_league1":                    ["pinnacle", "bet365", "williamhill"],
    "soccer_england_league2":                    ["pinnacle", "bet365", "williamhill"],
    "soccer_fa_cup":                             ["pinnacle", "bet365", "paddypower"],
    "soccer_england_efl_cup":                    ["pinnacle", "bet365", "williamhill"],
    # ── 欧洲次级 ──
    "soccer_spain_segunda_division":             ["pinnacle", "bet365", "marathonbet"],
    "soccer_germany_bundesliga2":                ["pinnacle", "bet365", "tipico_de"],
    "soccer_germany_liga3":                      ["pinnacle", "bet365", "tipico_de"],
    "soccer_italy_serie_b":                      ["pinnacle", "bet365", "codere_it"],
    "soccer_france_ligue_two":                   ["pinnacle", "bet365", "winamax_fr"],
    "soccer_russia_premier_league":              ["pinnacle", "bet365", "marathonbet"],
    "soccer_greece_super_league":                ["pinnacle", "bet365", "onexbet"],
    "soccer_switzerland_superleague":            ["pinnacle", "bet365", "marathonbet"],
    "soccer_austria_bundesliga":                 ["pinnacle", "bet365", "tipico_de"],
    "soccer_poland_ekstraklasa":                 ["pinnacle", "bet365", "onexbet"],
    "soccer_league_of_ireland":                  ["pinnacle", "bet365", "paddypower"],
    # ── 北欧（本地北欧庄）──
    "soccer_denmark_superliga":                  ["pinnacle", "bet365", "betsson"],
    "soccer_sweden_allsvenskan":                 ["pinnacle", "bet365", "betsson"],
    "soccer_sweden_superettan":                  ["pinnacle", "bet365", "nordicbet"],
    "soccer_norway_eliteserien":                 ["pinnacle", "bet365", "nordicbet"],
    "soccer_finland_veikkausliiga":              ["pinnacle", "bet365", "nordicbet"],
    # ── 美洲（南美用离岸早盘 sharp，北美用美国主力）──
    "soccer_usa_mls":                            ["pinnacle", "bet365", "draftkings"],
    "soccer_mexico_ligamx":                      ["pinnacle", "bet365", "betonlineag"],
    "soccer_argentina_primera_division":         ["pinnacle", "bet365", "betonlineag"],
    "soccer_brazil_campeonato":                  ["pinnacle", "bet365", "betonlineag"],
    "soccer_brazil_serie_b":                     ["pinnacle", "bet365", "betonlineag"],
    "soccer_chile_campeonato":                   ["pinnacle", "bet365", "betonlineag"],
    "soccer_conmebol_copa_libertadores":         ["pinnacle", "bet365", "betonlineag"],
    "soccer_conmebol_copa_sudamericana":         ["pinnacle", "bet365", "betonlineag"],
    "soccer_conmebol_copa_america":              ["pinnacle", "bet365", "betfair_ex_eu"],
    "soccer_concacaf_leagues_cup":               ["pinnacle", "bet365", "draftkings"],
    # ── 女子 / 国际 ──
    "soccer_uefa_champs_league_women":           ["pinnacle", "bet365", "betfair_ex_eu"],
    "soccer_germany_bundesliga_women":           ["pinnacle", "bet365", "tipico_de"],
    "soccer_fifa_world_cup_womens":              ["pinnacle", "bet365", "betfair_ex_eu"],
    "soccer_fifa_club_world_cup":                ["pinnacle", "bet365", "marathonbet"],
    "soccer_fifa_world_cup":                     ["pinnacle", "bet365", "betfair_ex_eu"],
    "soccer_fifa_world_cup_qualifiers_europe":          ["pinnacle", "bet365", "marathonbet"],
    "soccer_fifa_world_cup_qualifiers_south_america":   ["pinnacle", "bet365", "betonlineag"],
    "soccer_africa_cup_of_nations":              ["pinnacle", "bet365", "onexbet"],
    "soccer_concacaf_gold_cup":                  ["pinnacle", "bet365", "draftkings"],
    # ── 各国国内杯赛（沿用本国联赛本土庄）──
    "soccer_germany_dfb_pokal":                  ["pinnacle", "bet365", "tipico_de"],
    "soccer_italy_coppa_italia":                 ["pinnacle", "bet365", "codere_it"],
    "soccer_spain_copa_del_rey":                 ["pinnacle", "bet365", "marathonbet"],
    "soccer_france_coupe_de_france":             ["pinnacle", "bet365", "winamax_fr"],
}

# 联赛未在映射中时的回落组合（覆盖 + 风控 + 欧陆资金）
DEFAULT_LEAGUE_BM = ["pinnacle", "bet365", "marathonbet"]

# 当联赛推荐公司在本场不可用时，按此优先级补位（信号纯净的参考庄家）
FALLBACK_BM_PRIORITY = [
    "pinnacle", "bet365", "betfair_ex_eu", "marathonbet", "williamhill",
    "onexbet", "betfair_ex_uk", "ladbrokes_uk", "betsson", "draftkings",
]


def get_league_recommend(sport_key: str) -> list[str]:
    """返回该联赛推荐的 3 家公司 key 列表（不在映射中时返回默认组合）"""
    return LEAGUE_BM_MAP.get(sport_key, DEFAULT_LEAGUE_BM)


def resolve_league_recommend(sport_key: str, avail_keys: set[str]) -> list[str]:
    """在本场实际可用的公司中确定推荐 3 家。
    优先取该联赛映射的公司；若某家本场不可用，则按 FALLBACK_BM_PRIORITY 补位，
    确保返回的每一家都能在下方列表中找到（最多 3 家，可用不足时尽力补齐）。
    """
    resolved: list[str] = []
    for k in get_league_recommend(sport_key):
        if k in avail_keys and k not in resolved:
            resolved.append(k)
    # 不足 3 家时按全局优先级补位
    for k in FALLBACK_BM_PRIORITY:
        if len(resolved) >= 3:
            break
        if k in avail_keys and k not in resolved:
            resolved.append(k)
    return resolved[:3]


# ─── 工具函数 ─────────────────────────────────────────────────────────────────

def to_cst(iso_str: str) -> str:
    """ISO UTC 字符串 → 北京时间字符串"""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.astimezone(TZ_CST).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso_str


def print_quota(headers: dict, prefix: str = ""):
    remaining = headers.get("x-requests-remaining", "?")
    used      = headers.get("x-requests-used", "?")
    print(f"{prefix}[API额度] 已用:{used}  剩余:{remaining}")


def pick(prompt: str, count: int) -> int:
    """让用户输入 1~count 的整数，返回 0-based 索引"""
    while True:
        try:
            n = int(input(prompt).strip())
            if 1 <= n <= count:
                return n - 1
        except ValueError:
            pass
        print(f"  请输入 1~{count} 之间的数字")


def multi_pick(prompt: str, items: list) -> list:
    """让用户输入多个编号（逗号分隔），返回选中的元素列表"""
    while True:
        raw = input(prompt).strip()
        try:
            indices = [int(x.strip()) - 1 for x in raw.split(",") if x.strip()]
            if indices and all(0 <= i < len(items) for i in indices):
                return [items[i] for i in indices]
        except ValueError:
            pass
        print(f"  请输入 1~{len(items)} 之间的编号，用逗号分隔，例如：1,3")


def sep(char="─", width=60):
    print(char * width)


# ─── API 请求函数 ─────────────────────────────────────────────────────────────

def api_get(url: str, params: dict) -> tuple[dict | list | None, dict]:
    """通用 GET 请求，返回 (data, response_headers)。
    遇到 429（额度耗尽）或 401（Key无效）时自动切换备用 Key 重试。
    """
    while True:
        # 每次请求使用当前活跃的 Key
        params["apiKey"] = get_api_key()

        try:
            resp = requests.get(url, params=params, timeout=20)
            print_quota(resp.headers, prefix=f"  [{get_api_key_label()}]")
            resp.raise_for_status()
            return resp.json(), resp.headers

        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response else None

            if status in (429, 401):
                label = get_api_key_label()
                reason = "额度已耗尽" if status == 429 else "Key无效"
                print(f"  [{label}] {reason}（HTTP {status}）")
                _API_KEYS[_current_key_index]["exhausted"] = True

                if switch_api_key():
                    print(f"  正在用 {get_api_key_label()} 重试...")
                    continue  # 用新 Key 重试
                else:
                    print("  [错误] 所有 API Key 均不可用")
                    return None, {}

            msgs = {
                422: "参数不合法（运动代码或时间格式错误）",
            }
            print(f"  [HTTP {status}] {msgs.get(status, str(e))}")
            return None, {}

        except requests.exceptions.ConnectionError:
            print("  [错误] 网络连接失败")
            return None, {}
        except requests.exceptions.Timeout:
            print("  [错误] 请求超时")
            return None, {}


def _regions_for_keys(bm_keys: list[str]) -> str:
    """根据选中的博彩公司 key 列表，自动推算需要请求的 regions 参数。
    每 10 家博彩公司算一个 region 额度，指定 bookmakers 时优先于 regions。
    """
    regions = {BOOKMAKER_REGION.get(k, "eu") for k in bm_keys}
    return ",".join(sorted(regions))


def fetch_events(sport_key: str) -> list:
    """获取某联赛的赛事列表（消耗 1 个额度）"""
    print(f"  正在获取赛事列表...")
    data, _ = api_get(f"{BASE_URL}/sports/{sport_key}/events/",
                      {"dateFormat": "iso"})
    return data or []


def fetch_scores(sport_key: str, days: int = 3) -> list:
    """获取某联赛近 N 天已完成的比赛及比分（消耗 1 个额度）"""
    print(f"  正在获取近 {days} 天已结束比赛...")
    data, _ = api_get(f"{BASE_URL}/sports/{sport_key}/scores/",
                      {"dateFormat": "iso", "daysFrom": days})
    if not data:
        return []
    # 只保留已完成的比赛
    return [g for g in data if g.get("completed")]


def fetch_event_odds(sport_key: str, event_id: str, regions: str = "eu,uk") -> list:
    """获取单场比赛的当前赔率，不过滤公司（消耗 1 个额度）"""
    data, _ = api_get(
        f"{BASE_URL}/sports/{sport_key}/events/{event_id}/odds/",
        {
            "regions":      regions,
            "markets":      "h2h,spreads",
            "oddsFormat":   "decimal",
            "dateFormat":   "iso",
        },
    )
    return [data] if data else []


def fetch_historical_odds(sport_key: str, event_id: str, dt_utc: datetime,
                          regions: str = "eu,uk") -> list:
    """获取某时间点的历史赔率，不过滤公司（消耗 1 个额度）"""
    date_str = dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    data, _ = api_get(
        f"{BASE_URL}/historical/sports/{sport_key}/odds/",
        {
            "regions":    regions,
            "markets":    "h2h,spreads",
            "oddsFormat": "decimal",
            "dateFormat": "iso",
            "eventIds":   event_id,
            "date":       date_str,
        },
    )
    # 历史接口返回 {"timestamp":..., "previous_timestamp":..., "next_timestamp":..., "data":[...]}
    if isinstance(data, dict):
        return data.get("data", [])
    return []


# ─── 解析函数 ─────────────────────────────────────────────────────────────────

def parse_game(game: dict, league: str, snapshot_time: str = "",
               selected_bm_keys: list | None = None) -> list:
    """将单场比赛的 JSON 解析为行列表，含凯利指数计算。
    selected_bm_keys: 只输出这些公司的行（但市场平均值基于全部公司计算）
    """
    home = game.get("home_team", "")
    away = game.get("away_team", "")
    kick = to_cst(game.get("commence_time", ""))

    # ── 第一轮：收集全部公司赔率，计算市场平均值 ──
    h2h_all = {"home": [], "draw": [], "away": []}
    sp_all  = {"home": [], "away": []}

    for bm in game.get("bookmakers", []):
        for mkt in bm.get("markets", []):
            mkey = mkt.get("key", "")
            for o in mkt.get("outcomes", []):
                name  = o.get("name", "")
                price = o.get("price")
                if not price:
                    continue
                if mkey == "h2h":
                    if name == home:
                        h2h_all["home"].append(price)
                    elif name == away:
                        h2h_all["away"].append(price)
                    elif name.lower() == "draw":
                        h2h_all["draw"].append(price)
                elif mkey == "spreads":
                    if name == home:
                        sp_all["home"].append(price)
                    elif name == away:
                        sp_all["away"].append(price)

    def _avg(lst):
        return sum(lst) / len(lst) if lst else None

    def _kelly(odds, market_avg):
        if odds and market_avg and market_avg > 0:
            return round(odds / market_avg, 3)
        return None

    avg_h = {k: _avg(v) for k, v in h2h_all.items()}
    avg_s = {k: _avg(v) for k, v in sp_all.items()}

    # ── 第二轮：为选中公司构建行数据（含凯利） ──
    rows = []
    for bm in game.get("bookmakers", []):
        bm_key  = bm.get("key", "")
        bm_name = bm.get("title", bm_key)

        if selected_bm_keys and bm_key not in selected_bm_keys:
            continue

        for mkt in bm.get("markets", []):
            mkey    = mkt.get("key", "")
            updated = to_cst(mkt.get("last_update", ""))
            outs    = mkt.get("outcomes", [])

            if mkey == "h2h":
                ho = do = ao = None
                for o in outs:
                    name = o.get("name", "")
                    p    = o.get("price")
                    if name == home:
                        ho = p
                    elif name == away:
                        ao = p
                    elif name.lower() == "draw":
                        do = p
                if ho and ao:
                    rows.append({
                        "快照时间(CST)":   snapshot_time,
                        "联赛":            league,
                        "开球时间(CST)":   kick,
                        "主队":            home,
                        "客队":            away,
                        "博彩公司":        bm_name,
                        "盘口类型":        "欧指",
                        "主胜赔率":        ho,
                        "平局赔率":        do,
                        "客胜赔率":        ao,
                        "凯利(主胜)":      _kelly(ho, avg_h["home"]),
                        "凯利(平局)":      _kelly(do, avg_h["draw"]),
                        "凯利(客胜)":      _kelly(ao, avg_h["away"]),
                        "让球":            None,
                        "主队水位":        None,
                        "客队水位":        None,
                        "凯利(主)":        None,
                        "凯利(客)":        None,
                        "数据更新(CST)":   updated,
                    })

            elif mkey == "spreads":
                hp = ap = hpt = None
                for o in outs:
                    name = o.get("name", "")
                    p    = o.get("price")
                    pt   = o.get("point")
                    if name == home:
                        hp  = p
                        hpt = pt
                    elif name == away:
                        ap = p
                if hp and ap:
                    rows.append({
                        "快照时间(CST)":   snapshot_time,
                        "联赛":            league,
                        "开球时间(CST)":   kick,
                        "主队":            home,
                        "客队":            away,
                        "博彩公司":        bm_name,
                        "盘口类型":        "亚盘",
                        "主胜赔率":        None,
                        "平局赔率":        None,
                        "客胜赔率":        None,
                        "凯利(主胜)":      None,
                        "凯利(平局)":      None,
                        "凯利(客胜)":      None,
                        "让球":            hpt,
                        "主队水位":        hp,
                        "客队水位":        ap,
                        "凯利(主)":        _kelly(hp, avg_s["home"]),
                        "凯利(客)":        _kelly(ap, avg_s["away"]),
                        "数据更新(CST)":   updated,
                    })

    return rows


# ─── 主交互流程 ───────────────────────────────────────────────────────────────

def step1_select_league() -> tuple[str, str]:
    sep("═")
    print("  第一步：选择联赛")
    sep("═")

    # 主菜单：常用联赛 + 折叠分组入口
    for i, (_, name) in enumerate(SPORTS_MAIN, 1):
        print(f"  {i}. {name}")

    sep("-", 50)
    more_start = len(SPORTS_MAIN) + 1
    for j, (group_name, _) in enumerate(SPORTS_MORE):
        print(f"  {more_start + j}. ▸ {group_name}")

    total = len(SPORTS_MAIN) + len(SPORTS_MORE)
    idx = pick("\n请输入编号：", total)

    if idx < len(SPORTS_MAIN):
        return SPORTS_MAIN[idx]

    # 展开子菜单
    group_idx = idx - len(SPORTS_MAIN)
    group_name, group_items = SPORTS_MORE[group_idx]
    sep("-", 50)
    print(f"  ▾ {group_name}")
    for i, (_, name) in enumerate(group_items, 1):
        print(f"  {i}. {name}")
    sub_idx = pick("\n请输入编号：", len(group_items))
    return group_items[sub_idx]


def step2_select_match(sport_key: str, sport_name: str) -> dict:
    sep()
    print(f"  第二步：选择比赛（{sport_name}）")
    sep()
    print("  1. 未来比赛（赛前分析）")
    print("  2. 已结束比赛（赛后复盘）")
    mode = pick("\n请选择：", 2)

    if mode == 0:
        # ── 未来比赛 ──
        events = fetch_events(sport_key)
        if not events:
            sys.exit("  未获取到赛事，请稍后再试")
        events.sort(key=lambda e: e.get("commence_time", ""))
        print()
        for i, ev in enumerate(events, 1):
            kick = to_cst(ev.get("commence_time", ""))
            home = ev.get("home_team", "")
            away = ev.get("away_team", "")
            print(f"  {i:>2}. [{kick}]  {home}  vs  {away}")
        idx = pick("\n请输入编号：", len(events))
        return events[idx]

    else:
        # ── 已结束比赛（复盘） ──
        games = fetch_scores(sport_key, days=3)
        if not games:
            print("  近 3 天无已结束比赛，尝试扩展到 7 天...")
            # scores 端点最多支持 daysFrom=3，若无结果提示用户
            sys.exit("  无已结束比赛数据，请确认联赛近期有比赛")
        games.sort(key=lambda e: e.get("commence_time", ""), reverse=True)
        print()
        for i, g in enumerate(games, 1):
            kick = to_cst(g.get("commence_time", ""))
            home = g.get("home_team", "")
            away = g.get("away_team", "")
            scores = g.get("scores") or []
            sc_map = {s["name"]: s["score"] for s in scores}
            h_sc = sc_map.get(home, "?")
            a_sc = sc_map.get(away, "?")
            print(f"  {i:>2}. [{kick}]  {home} {h_sc}:{a_sc} {away}")
        idx = pick("\n请输入编号：", len(games))
        chosen = games[idx]
        # 显示比分
        scores = chosen.get("scores") or []
        sc_map = {s["name"]: s["score"] for s in scores}
        home = chosen.get("home_team", "")
        away = chosen.get("away_team", "")
        print(f"\n  ▶ 复盘：{home} {sc_map.get(home,'?')}:{sc_map.get(away,'?')} {away}")
        return chosen


def step3_select_bookmakers(sport_key: str, event_id: str) -> list[str]:
    sep()
    print("  第三步：选择博彩公司")
    sep()
    print("  正在获取该场次可用博彩公司...")

    # 先从数据源抓取本场实际可用的全部公司（推荐 3 家在此基础上确定，
    # 避免推荐到本场未开盘、下方列表里找不到的公司）
    data, _ = api_get(
        f"{BASE_URL}/sports/{sport_key}/events/{event_id}/odds/",
        {
            "apiKey":     get_api_key(),
            "regions":    "eu,uk,us,au,se",
            "markets":    "h2h,spreads",
            "oddsFormat": "decimal",
            "dateFormat": "iso",
        },
    )

    # 收集 API 返回的可用公司 key 集合
    avail_map: dict[str, tuple[str, str]] = {}
    if data and isinstance(data, dict):
        for bm in data.get("bookmakers", []):
            key  = bm.get("key", "")
            info = BOOKMAKER_INFO.get(key)
            en   = info[0] if info else bm.get("title", key)
            zh   = info[1] if info else ""
            avail_map[key] = (en, zh)

    if not avail_map:
        print("  未找到可用博彩公司，将使用内置列表")
        avail_map = {k: v for k, v in BOOKMAKER_INFO.items()}

    # 基于本场实际可用公司确定推荐 3 家（缺失则按全局优先级补位）
    avail_keys = set(avail_map)
    league_rec = resolve_league_recommend(sport_key, avail_keys)
    rec_keys = set(league_rec)
    flat_list: list[tuple[str, str, str]] = []  # (key, en, zh) 按展示顺序

    print()
    idx = 0
    for region_label, region_dict in BOOKMAKER_GROUPS:
        group_items = [(k, avail_map[k]) for k in region_dict if k in avail_map]
        if not group_items:
            continue
        print(f"  ── {region_label}（{len(group_items)} 家）──")
        for key, (en, zh) in group_items:
            idx += 1
            marker = " ◀推荐" if key in rec_keys else ""
            print(f"  {idx:>3}. {en:<26} {zh:<30} ({key}){marker}")
            flat_list.append((key, en, zh))
        # 检查是否有不在任何分组里的可用公司（API 返回了但我们没收录）
    ungrouped_keys = set(avail_map) - {k for k, _, _ in flat_list}
    if ungrouped_keys:
        print(f"  ── 其他（{len(ungrouped_keys)} 家）──")
        for key in sorted(ungrouped_keys):
            idx += 1
            en, zh = avail_map[key]
            marker = " ◀推荐" if key in rec_keys else ""
            print(f"  {idx:>3}. {en:<26} {zh:<30} ({key}){marker}")
            flat_list.append((key, en, zh))

    region_count = len({BOOKMAKER_REGION.get(k, "eu") for k in avail_keys})

    # 联赛推荐 3 家组合（已按本场可用性解析，每家都能在上方列表中找到）
    league_rec_names = " + ".join(BOOKMAKER_INFO.get(k, (k, ""))[0] for k in league_rec) if league_rec else "（本场暂无可用）"
    # 标注哪些映射公司本场缺失、已被补位替换
    mapped = get_league_recommend(sport_key)
    dropped = [k for k in mapped if k not in avail_keys]

    print()
    print(f"  共 {len(flat_list)} 家可用，覆盖 {region_count} 个区域")
    print(f"  [额度提示] 每 10 家博彩公司 = 1 个区域额度")
    print(f"  [联赛推荐3家] {league_rec_names}")
    if dropped:
        dropped_names = "、".join(BOOKMAKER_INFO.get(k, (k, ""))[0] for k in dropped)
        print(f"    （原推荐 {dropped_names} 本场未开盘，已自动补位）")
    print(f"  快捷输入：L = 联赛推荐3家，支持混合输入")

    while True:
        print()
        raw = input("请输入编号（逗号分隔）或 L 或混合：").strip().upper()

        # 解析 tokens：L=联赛推荐3家，数字=手动选择
        tokens = [t.strip() for t in raw.split(",") if t.strip()]
        selected_keys = []
        valid = True
        seen_keys: set[str] = set()

        for token in tokens:
            if token == "L":
                if not league_rec:
                    print("  联赛推荐公司在本场均不可用，请手动选择")
                    valid = False
                    break
                for k in league_rec:
                    if k not in seen_keys:
                        selected_keys.append(k)
                        seen_keys.add(k)
            else:
                try:
                    i = int(token) - 1
                    if 0 <= i < len(flat_list):
                        k = flat_list[i][0]
                        if k not in seen_keys:
                            selected_keys.append(k)
                            seen_keys.add(k)
                    else:
                        valid = False
                        break
                except ValueError:
                    valid = False
                    break

        if valid and selected_keys:
            names = ", ".join(BOOKMAKER_INFO[k][0] for k in selected_keys if k in BOOKMAKER_INFO)
            print(f"  已选：{names}")
            return selected_keys

        print(f"  输入无效，请输入 1~{len(flat_list)} 的编号或 L")


def step4_select_stages(event: dict) -> list[tuple[str, datetime | None]]:
    """
    根据开球时间自动计算 10 个关键节点，覆盖盘口完整生命周期。
    用户可勾选要抓取的节点，直接回车 = 全选可用节点。

    10 节点定义（推荐方案，每场约 2 + 40×N credits，N=历史节点数）：
      初盘①  —— 开球前 72h  庄家挂牌定价
      初盘②  —— 开球前 48h  初盘首轮调整
      中盘①  —— 开球前 36h  早期资金介入
      中盘②  —— 开球前 24h  主力资金注入
      中盘③  —— 开球前 12h  伤停/阵容消息消化
      临场①  —— 开球前  6h  临场前期调整
      临场②  —— 开球前  3h  经典临场参考点
      临场③  —— 开球前 1.5h 封盘前精调
      临场④  —— 开球前 0.5h 最后异动（知情资金窗口）
      即时    —— 当前时间
    """
    sep()
    print("  第四步：选择抓取节点")
    sep()

    kick_utc = datetime.fromisoformat(
        event.get("commence_time", "").replace("Z", "+00:00")
    )
    kick_cst      = kick_utc.astimezone(TZ_CST)
    now_utc       = datetime.now(TZ_UTC)
    now_cst       = now_utc.astimezone(TZ_CST)
    hours_to_kick = (kick_utc - now_utc).total_seconds() / 3600

    # 10 个预设节点
    stage_defs = [
        ("初盘①", 72,   kick_utc - timedelta(hours=72)),
        ("初盘②", 48,   kick_utc - timedelta(hours=48)),
        ("中盘①", 36,   kick_utc - timedelta(hours=36)),
        ("中盘②", 24,   kick_utc - timedelta(hours=24)),
        ("中盘③", 12,   kick_utc - timedelta(hours=12)),
        ("临场①", 6,    kick_utc - timedelta(hours=6)),
        ("临场②", 3,    kick_utc - timedelta(hours=3)),
        ("临场③", 1.5,  kick_utc - timedelta(hours=1.5)),
        ("临场④", 0.5,  kick_utc - timedelta(hours=0.5)),
        ("即时",  0,    None),
    ]

    # 判断当前所处阶段
    if hours_to_kick > 72:
        current_stage = "初盘前（庄家尚未全面挂牌）"
    elif hours_to_kick > 24:
        current_stage = "初盘阶段  ▶ 距开球 {:.0f} 小时".format(hours_to_kick)
    elif hours_to_kick > 3:
        current_stage = "中盘阶段  ▶ 距开球 {:.1f} 小时".format(hours_to_kick)
    elif hours_to_kick > 0:
        current_stage = "临场阶段  ▶ 距开球 {:.0f} 分钟".format(hours_to_kick * 60)
    else:
        current_stage = "比赛已开始或结束"

    print(f"\n  开球时间（北京）：{kick_cst.strftime('%Y-%m-%d %H:%M')}")
    print(f"  当前时间（北京）：{now_cst.strftime('%Y-%m-%d %H:%M')}")
    print(f"  ▶ 当前所处阶段：{current_stage}")

    # 额度预估
    hist_count = sum(1 for _, _, dt in stage_defs if dt is not None and dt < now_utc)
    print(f"\n  [额度预估] 历史节点每个约 40 credits，即时节点 0 credits")
    print(f"              当前可选历史节点 {hist_count} 个 → 全选约 {hist_count * 40} credits")
    print()

    print(f"  {'编号':<6} {'节点':<8} {'距开球':<10} {'时间（北京）':<20} 状态")
    sep("-", 78)

    selectable = []
    selectable_map = {}  # 原始编号 → (label, dt_utc)

    for i, (label, hours_before, dt_utc) in enumerate(stage_defs, 1):
        if dt_utc is None:
            time_str = now_cst.strftime("%Y-%m-%d %H:%M")
            dist_str = "当前"
            status   = "可选 — 即时快照"
            item = (label, dt_utc)
            selectable.append(item)
            selectable_map[i] = item
        elif dt_utc < now_utc:
            time_str = dt_utc.astimezone(TZ_CST).strftime("%Y-%m-%d %H:%M")
            dist_str = f"-{hours_before}h"
            status   = "可选 — 历史数据"
            item = (label, dt_utc)
            selectable.append(item)
            selectable_map[i] = item
        else:
            time_str = dt_utc.astimezone(TZ_CST).strftime("%Y-%m-%d %H:%M")
            dist_str = f"-{hours_before}h"
            status   = "— 尚未到达，不可选"
        print(f"  {i:<6} {label:<8} {dist_str:<10} {time_str:<20} {status}")

    print()
    hist_selected = sum(1 for _, dt in selectable if dt is not None)
    total_cost = hist_selected * 40
    print(f"  直接回车 = 全选可用节点（{len(selectable)} 个节点，约 {total_cost} credits）")
    raw = input("  请输入节点编号（逗号分隔，或直接回车全选）：").strip()

    if not raw:
        return selectable

    try:
        chosen = []
        for x in raw.split(","):
            n = int(x.strip())
            if n in selectable_map:
                chosen.append(selectable_map[n])
        if chosen:
            hist_chosen = sum(1 for _, dt in chosen if dt is not None)
            cost = hist_chosen * 40
            print(f"  已选 {len(chosen)} 个节点（{hist_chosen} 个历史），约 {cost} credits")
            return chosen
    except ValueError:
        pass

    print("  输入无效，已全选")
    return selectable


def run_stages(sport_key: str, event: dict, bm_keys: list,
               league_name: str, stages: list[tuple[str, datetime | None]]):
    """按选定节点抓取赔率并对比展示"""
    home = event.get("home_team", "")
    away = event.get("away_team", "")
    sep()
    print(f"  比赛：{home} vs {away}")
    sep()

    regions = _regions_for_keys(bm_keys)

    all_rows = []
    for label, dt_utc in stages:
        if dt_utc is None:
            cst_label = datetime.now(TZ_CST).strftime("%Y-%m-%d %H:%M") + f"（{label}）"
            print(f"  抓取【{label}】（当前即时）...")
            games = fetch_event_odds(sport_key, event["id"], regions)
        else:
            dt_cst    = dt_utc.astimezone(TZ_CST)
            cst_label = dt_cst.strftime("%Y-%m-%d %H:%M") + f"（{label}）"
            print(f"  抓取【{label}】{dt_cst.strftime('%Y-%m-%d %H:%M')} ...")
            games = fetch_historical_odds(sport_key, event["id"], dt_utc, regions)

        for g in games:
            all_rows.extend(parse_game(g, league_name, snapshot_time=cst_label,
                                       selected_bm_keys=bm_keys))

    if not all_rows:
        print("  未获取到任何数据（所选博彩公司在各节点均无盘口记录）")
        return

    df = pd.DataFrame(all_rows)
    # 按开球日期（北京时间）建子目录
    kick_date = to_cst(event.get("commence_time", ""))[:10]  # YYYY-MM-DD
    safe = home.replace(" ", "_") + "_vs_" + away.replace(" ", "_")
    out_dir = f"data/{kick_date}"
    _print_and_export(df, f"{out_dir}/{safe}_stages.csv")


def _print_and_export(df: pd.DataFrame, filename: str):
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 240)
    pd.set_option("display.max_rows", 500)

    h2h_cols = ["快照时间(CST)", "联赛", "开球时间(CST)", "主队", "客队",
                "博彩公司", "主胜赔率", "平局赔率", "客胜赔率",
                "凯利(主胜)", "凯利(平局)", "凯利(客胜)", "数据更新(CST)"]
    sp_cols  = ["快照时间(CST)", "联赛", "开球时间(CST)", "主队", "客队",
                "博彩公司", "让球", "主队水位", "客队水位",
                "凯利(主)", "凯利(客)", "数据更新(CST)"]

    sep("═")
    print("【欧指数据（胜平负）】")
    sep("═")
    h2h = df[df["盘口类型"] == "欧指"]
    if not h2h.empty:
        print(h2h[h2h_cols].to_string(index=False))
    else:
        print("  无欧指数据")

    sep("═")
    print("【亚盘数据（让球）】")
    sep("═")
    sp = df[df["盘口类型"] == "亚盘"]
    if not sp.empty:
        print(sp[sp_cols].to_string(index=False))
    else:
        print("  无亚盘数据")

    os.makedirs(os.path.dirname(filename), exist_ok=True)
    df.to_csv(filename, index=False, encoding="utf-8-sig")
    print(f"\n  数据已导出至 {filename}（共 {len(df)} 行）")
    sep("═")


# ─── 入口 ─────────────────────────────────────────────────────────────────────

def main():
    print()
    sep("═")
    print("  The Odds API — 足球赔率查询工具（北京时间）")
    sep("═")

    # 显示 API Key 状态
    for i, info in enumerate(_API_KEYS):
        marker = " ◀当前" if i == _current_key_index else ""
        masked = info["key"][:6] + "..." + info["key"][-4:]
        print(f"  {info['label']}: {masked}{marker}")
    print()

    sport_key, sport_name = step1_select_league()
    event                 = step2_select_match(sport_key, sport_name)
    bm_keys               = step3_select_bookmakers(sport_key, event["id"])
    stages                = step4_select_stages(event)

    print()
    run_stages(sport_key, event, bm_keys, sport_name, stages)


if __name__ == "__main__":
    main()
