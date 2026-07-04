"""
精算报告 → Ghost 博客发布。

自包含模块（不依赖 Ghost/ 项目目录）：把 Ghost Admin API 的 JWT 鉴权、
发文逻辑搬过来，并针对本项目的精算报告做定制转换（提标题、第 7 节前插付费墙）。

配置（.env）：
  GHOST_ADMIN_API_KEY   形如 id:secret（secret 为 hex）
  GHOST_ADMIN_API_URL   形如 https://blog.lahmxavi.top
  GHOST_DEFAULT_VISIBILITY  public/members/paid（默认 paid）

付费墙策略：第 1-6 节（数据/分析过程）免费，第 7 节「最终精算结论」付费解锁。
"""

import os
import re
import time
import logging
from datetime import datetime, timezone, timedelta

import jwt
import markdown
import requests
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("odds_bot.ghost_publish")

GHOST_ADMIN_API_KEY = os.getenv("GHOST_ADMIN_API_KEY", "").strip()
GHOST_ADMIN_API_URL = os.getenv("GHOST_ADMIN_API_URL", "").strip().rstrip("/")
GHOST_DEFAULT_VISIBILITY = os.getenv("GHOST_DEFAULT_VISIBILITY", "paid").strip().lower()
GHOST_API_VERSION = "v5.0"

# 可选：显式 Host 头。当 GHOST_ADMIN_API_URL 指向内网/环回地址（如
# http://127.0.0.1:2368，绕过 Cloudflare 直连同机 Ghost）时，Ghost 会因请求
# Host 与其配置的 canonical url 不符而 301 跳回公网域名 —— 又绕回 CF 被拦成 403。
# 把本项设为真实公网域名（如 blog.lahmxavi.top），请求即带上正确 Host，
# Ghost 视作本站请求、不再重定向。留空则不覆盖，走 URL 自身的 host。
GHOST_ADMIN_HOST_HEADER = os.getenv("GHOST_ADMIN_HOST_HEADER", "").strip()

_MD_EXTENSIONS = ["extra", "fenced_code", "tables", "sane_lists"]

# ── 收款引导（付费墙之前展示给未登录访客）──
# 图片：先在 Ghost 后台上传收款码拿到 URL，把下面两个占位 URL 换成真实地址。
# 留空字符串则不显示对应图片（只显示文字引导）。
PAY_WECHAT_ID = "Alonso_183"
PAY_WECHAT_QR_URL = ""   # ← 微信收款码图片 URL（上传后填）
PAY_ALIPAY_QR_URL = ""   # ← 支付宝收款码图片 URL（上传后填）


def _cta_html() -> str:
    """生成收款引导 HTML 区块（插在免费预览末尾、付费墙之前）。"""
    imgs = []
    if PAY_WECHAT_QR_URL:
        imgs.append(
            f'<figure style="margin:0;text-align:center">'
            f'<img src="{PAY_WECHAT_QR_URL}" alt="微信收款码" '
            f'style="max-width:220px;width:100%"/>'
            f'<figcaption>微信扫码</figcaption></figure>')
    if PAY_ALIPAY_QR_URL:
        imgs.append(
            f'<figure style="margin:0;text-align:center">'
            f'<img src="{PAY_ALIPAY_QR_URL}" alt="支付宝收款码" '
            f'style="max-width:220px;width:100%"/>'
            f'<figcaption>支付宝扫码</figcaption></figure>')
    qr_row = ""
    if imgs:
        qr_row = ('<div style="display:flex;gap:24px;justify-content:center;'
                  'flex-wrap:wrap;margin:16px 0">' + "".join(imgs) + "</div>")
    return (
        '<hr/>'
        '<div style="border:1px solid #e5e5e5;border-radius:8px;padding:20px;'
        'background:#fafafa">'
        '<h3 style="margin-top:0">🔒 完整精算结论为付费内容</h3>'
        '<p>以上为免费预览。<strong>最终下注方向、比分预测、置信度</strong>等核心结论，'
        '需成为会员后登录查看。</p>'
        f'<p><strong>如何成为会员：</strong>请联系站长微信 '
        f'<strong>{PAY_WECHAT_ID}</strong> 开通，开通后登录本站即可解锁全文。</p>'
        f'{qr_row}'
        '</div>'
    )


# 报告锚点（由 analyzer 的 prompt 固定产出，稳定）
# 首行： ## 比赛：墨西哥（…） vs 南非（…）
_MATCH_RE = re.compile(r"^\#\#\s*比赛[：:]\s*(.+?)\s+vs\s+(.+?)\s*$", re.MULTILINE)
# 第二行：## 赛事：… 开球时间：…
_EVENT_RE = re.compile(r"^\#\#\s*赛事[：:]\s*(.+?)\s*$", re.MULTILINE)
# 付费墙锚点：### 7. 最终精算结论（允许 7 后面是 . 、 中文顿号或空格）
_PAYWALL_RE = re.compile(r"^\#{3}\s*7\s*[\.、]?\s*最终精算结论", re.MULTILINE)
# 归档路径行：CLAUDE.md 步骤8 让 LLM 在报告末尾输出的 "> 归档路径：report/..."，
# 仅供后台落盘用，不应进入发布正文。整行剥除（含可选的引用前缀/空白）。
_ARCHIVE_LINE_RE = re.compile(r"(?m)^\s*>?\s*归档路径[：:].*$\n?")
# 复盘归档分隔：/review 两步全跑存成「# 第一步·盲推预判 … --- # 第二步·对照复盘 …」。
# 盲推是不看比分、无基本面的中间过程（会写出没有依据的"基本面"段），不该进公开文章；
# 发布时只取第二步对照。匹配「第二步·对照复盘」标题，取其后全部为发布正文。
_REVIEW_STEP2_RE = re.compile(r"(?m)^#{1,3}\s*第二步[·\s]*对照复盘\s*$")
# 复盘第二步付费墙锚点：第 3 节「盲推预判 vs 实际对照」起付费
# （免费展示 1.实际结果 + 2.盘口结算回放做引流）。允许 3 后接 . 、顿号或空格。
_REVIEW_PAYWALL_RE = re.compile(r"(?m)^#{3}\s*3\s*[\.、]?\s*盲推预判")
# 复盘第二步开头的「## 复盘：…」行（对应精算的「## 比赛：…」），发布正文里去重用。
_REVIEW_HEAD_RE = re.compile(r"(?m)^#{2}\s*复盘[：:].*$")


class GhostError(Exception):
    """Ghost 返回的业务错误，message 已是可读文案。"""


def available() -> bool:
    """是否已配置 Ghost 发布（仿 analyzer.available()）。"""
    return bool(GHOST_ADMIN_API_KEY and ":" in GHOST_ADMIN_API_KEY
                and GHOST_ADMIN_API_URL)


# ─── JWT 鉴权（照搬 Ghost/bot/ghost_auth.py）─────────────────────────────────
def _make_token() -> str:
    key_id, secret_hex = GHOST_ADMIN_API_KEY.split(":", 1)
    secret_bytes = bytes.fromhex(secret_hex)
    iat = int(time.time())
    payload = {"iat": iat, "exp": iat + 300, "aud": "/admin/"}
    headers = {"kid": key_id, "alg": "HS256", "typ": "JWT"}
    return jwt.encode(payload, secret_bytes, algorithm="HS256", headers=headers)


# ─── 报告 → 文章 ─────────────────────────────────────────────────────────────
def _clean_team(name: str) -> str:
    """去掉队名里的括号注释，如 '墨西哥（Mexico）' → '墨西哥'，
    'South Africa（南非）' → 'South Africa'。中英文括号都处理。"""
    return re.sub(r"[（(].*?[）)]", "", name).strip()


# 英文队名 → 中文 映射表（命中则标题用中文；未命中退回规范化英文）。
# 数据源队名为英文（API-Football），这里只收常关注的联赛/球队，可随时补充。
# key 统一用小写匹配，避免大小写不一致（如 'SHANGHAI SIPG'）。
_TEAM_CN = {
    # ── 世界杯 / 国家队 ──
    "brazil": "巴西", "argentina": "阿根廷", "germany": "德国", "west germany": "德国",
    "france": "法国", "spain": "西班牙", "england": "英格兰", "italy": "意大利",
    "netherlands": "荷兰", "holland": "荷兰", "portugal": "葡萄牙", "belgium": "比利时",
    "croatia": "克罗地亚", "uruguay": "乌拉圭", "mexico": "墨西哥", "usa": "美国",
    "united states": "美国", "colombia": "哥伦比亚", "switzerland": "瑞士", "denmark": "丹麦",
    "sweden": "瑞典", "poland": "波兰", "wales": "威尔士", "scotland": "苏格兰",
    "republic of ireland": "爱尔兰", "ireland": "爱尔兰", "northern ireland": "北爱尔兰",
    "austria": "奥地利", "ukraine": "乌克兰", "russia": "俄罗斯", "serbia": "塞尔维亚",
    "czech republic": "捷克", "czechia": "捷克", "turkey": "土耳其", "turkiye": "土耳其",
    "greece": "希腊", "hungary": "匈牙利", "romania": "罗马尼亚", "bulgaria": "保加利亚",
    "norway": "挪威", "slovakia": "斯洛伐克", "slovenia": "斯洛文尼亚", "iceland": "冰岛",
    "finland": "芬兰", "albania": "阿尔巴尼亚", "north macedonia": "北马其顿",
    "bosnia and herzegovina": "波黑", "bosnia": "波黑", "georgia": "格鲁吉亚",
    "japan": "日本", "south korea": "韩国", "korea republic": "韩国", "north korea": "朝鲜",
    "iran": "伊朗", "saudi arabia": "沙特阿拉伯", "australia": "澳大利亚", "qatar": "卡塔尔",
    "iraq": "伊拉克", "united arab emirates": "阿联酋", "uae": "阿联酋", "china": "中国",
    "china pr": "中国", "uzbekistan": "乌兹别克斯坦", "jordan": "约旦", "bahrain": "巴林",
    "oman": "阿曼", "syria": "叙利亚", "kuwait": "科威特", "morocco": "摩洛哥",
    "senegal": "塞内加尔", "tunisia": "突尼斯", "algeria": "阿尔及利亚", "egypt": "埃及",
    "nigeria": "尼日利亚", "cameroon": "喀麦隆", "ghana": "加纳", "ivory coast": "科特迪瓦",
    "cote d'ivoire": "科特迪瓦", "south africa": "南非", "mali": "马里",
    "burkina faso": "布基纳法索", "dr congo": "刚果民主共和国", "congo dr": "刚果民主共和国",
    "togo": "多哥", "angola": "安哥拉", "zambia": "赞比亚", "cape verde": "佛得角",
    "guinea": "几内亚", "equatorial guinea": "赤道几内亚", "gabon": "加蓬",
    "costa rica": "哥斯达黎加", "canada": "加拿大", "honduras": "洪都拉斯", "panama": "巴拿马",
    "jamaica": "牙买加", "trinidad and tobago": "特立尼达和多巴哥", "el salvador": "萨尔瓦多",
    "guatemala": "危地马拉", "haiti": "海地", "curacao": "库拉索", "curaçao": "库拉索",
    "chile": "智利", "peru": "秘鲁", "ecuador": "厄瓜多尔", "paraguay": "巴拉圭",
    "bolivia": "玻利维亚", "venezuela": "委内瑞拉", "new zealand": "新西兰",
    # ── 英超 ──
    "manchester united": "曼联", "man utd": "曼联", "man united": "曼联",
    "manchester city": "曼城", "man city": "曼城", "liverpool": "利物浦",
    "arsenal": "阿森纳", "chelsea": "切尔西", "tottenham": "热刺",
    "tottenham hotspur": "热刺", "spurs": "热刺", "newcastle": "纽卡斯尔",
    "newcastle united": "纽卡斯尔", "brighton": "布莱顿",
    "brighton & hove albion": "布莱顿", "aston villa": "阿斯顿维拉",
    "west ham": "西汉姆联", "west ham united": "西汉姆联", "wolves": "狼队",
    "wolverhampton wanderers": "狼队", "everton": "埃弗顿", "crystal palace": "水晶宫",
    "brentford": "布伦特福德", "fulham": "富勒姆", "nottingham forest": "诺丁汉森林",
    "bournemouth": "伯恩茅斯", "afc bournemouth": "伯恩茅斯", "leicester": "莱斯特城",
    "leicester city": "莱斯特城", "ipswich": "伊普斯维奇", "ipswich town": "伊普斯维奇",
    "southampton": "南安普顿", "leeds": "利兹联", "leeds united": "利兹联",
    "burnley": "伯恩利", "sunderland": "桑德兰", "sheffield united": "谢菲尔德联",
    "sheffield wednesday": "谢菲尔德星期三", "luton": "卢顿", "luton town": "卢顿",
    "norwich": "诺维奇", "norwich city": "诺维奇", "watford": "沃特福德",
    "west brom": "西布罗姆维奇", "west bromwich albion": "西布罗姆维奇",
    "stoke": "斯托克城", "stoke city": "斯托克城", "middlesbrough": "米德尔斯堡",
    "hull city": "赫尔城", "coventry": "考文垂", "coventry city": "考文垂",
    # ── 苏超 ──
    "celtic": "凯尔特人", "rangers": "流浪者", "aberdeen": "阿伯丁", "hearts": "哈茨",
    "heart of midlothian": "哈茨", "hibernian": "希伯尼安", "hibs": "希伯尼安",
    "dundee united": "邓迪联", "dundee": "邓迪", "motherwell": "马瑟韦尔",
    "st mirren": "圣米伦", "kilmarnock": "基尔马诺克", "ross county": "罗斯郡",
    "st johnstone": "圣约翰斯通", "livingston": "利文斯顿", "falkirk": "福尔柯克",
    # ── 爱尔兰超 ──
    "shamrock rovers": "沙姆洛克流浪者", "derry city": "德里城", "derry": "德里城",
    "drogheda united": "德罗赫达联", "drogheda": "德罗赫达联", "bohemians": "波西米亚人",
    "st patrick's athletic": "圣帕特里克", "st patricks athletic": "圣帕特里克",
    "shelbourne": "谢尔本", "sligo rovers": "斯莱戈流浪者", "galway united": "高威联",
    "waterford": "沃特福德郡", "cork city": "科克城", "dundalk": "邓多克",
    # ── 德甲 ──
    "bayern munich": "拜仁慕尼黑", "bayern": "拜仁慕尼黑", "borussia dortmund": "多特蒙德",
    "dortmund": "多特蒙德", "rb leipzig": "莱比锡", "leipzig": "莱比锡",
    "bayer leverkusen": "勒沃库森", "leverkusen": "勒沃库森", "vfb stuttgart": "斯图加特",
    "stuttgart": "斯图加特", "eintracht frankfurt": "法兰克福", "frankfurt": "法兰克福",
    "sc freiburg": "弗赖堡", "freiburg": "弗赖堡", "vfl wolfsburg": "沃尔夫斯堡",
    "wolfsburg": "沃尔夫斯堡", "borussia monchengladbach": "门兴格拉德巴赫",
    "monchengladbach": "门兴格拉德巴赫", "werder bremen": "云达不莱梅",
    "fc augsburg": "奥格斯堡", "augsburg": "奥格斯堡", "mainz": "美因茨",
    "mainz 05": "美因茨", "fsv mainz 05": "美因茨", "hoffenheim": "霍芬海姆",
    "1899 hoffenheim": "霍芬海姆", "union berlin": "柏林联合", "fc st. pauli": "圣保利",
    "st pauli": "圣保利", "heidenheim": "海登海姆", "1. fc heidenheim": "海登海姆",
    "fc koln": "科隆", "koln": "科隆", "1. fc koln": "科隆", "cologne": "科隆",
    "hamburger sv": "汉堡", "hamburg": "汉堡", "schalke 04": "沙尔克04",
    "schalke": "沙尔克04", "hertha berlin": "柏林赫塔", "hertha bsc": "柏林赫塔",
    # ── 法甲 ──
    "paris saint germain": "巴黎圣日耳曼", "paris saint-germain": "巴黎圣日耳曼",
    "psg": "巴黎圣日耳曼", "paris sg": "巴黎圣日耳曼", "marseille": "马赛",
    "olympique marseille": "马赛", "monaco": "摩纳哥", "as monaco": "摩纳哥",
    "lyon": "里昂", "olympique lyonnais": "里昂", "lille": "里尔", "losc lille": "里尔",
    "nice": "尼斯", "ogc nice": "尼斯", "lens": "朗斯", "rc lens": "朗斯",
    "rennes": "雷恩", "stade rennais": "雷恩", "strasbourg": "斯特拉斯堡",
    "brest": "布雷斯特", "stade brestois": "布雷斯特", "toulouse": "图卢兹",
    "nantes": "南特", "fc nantes": "南特", "auxerre": "欧塞尔", "le havre": "勒阿弗尔",
    "angers": "昂热", "lorient": "洛里昂", "paris fc": "巴黎FC", "metz": "梅斯",
    "saint etienne": "圣埃蒂安", "saint-etienne": "圣埃蒂安", "reims": "兰斯",
    "montpellier": "蒙彼利埃", "bordeaux": "波尔多",
    # ── 荷甲 ──
    "ajax": "阿贾克斯", "psv eindhoven": "埃因霍温", "psv": "埃因霍温",
    "feyenoord": "费耶诺德", "az alkmaar": "阿尔克马尔", "az": "阿尔克马尔",
    "twente": "特温特", "fc twente": "特温特", "utrecht": "乌得勒支",
    "fc utrecht": "乌得勒支", "heerenveen": "海伦芬", "nec nijmegen": "奈梅亨",
    "nec": "奈梅亨", "go ahead eagles": "上进之鹰", "sparta rotterdam": "鹿特丹斯巴达",
    "fortuna sittard": "西塔德", "pec zwolle": "兹沃勒", "heracles": "赫拉克勒斯",
    "heracles almelo": "赫拉克勒斯", "groningen": "格罗宁根", "fc groningen": "格罗宁根",
    "nac breda": "布雷达", "telstar": "特尔斯达", "volendam": "福伦丹",
    "fc volendam": "福伦丹", "excelsior": "精英队",
    # ── 葡超 ──
    "benfica": "本菲卡", "sl benfica": "本菲卡", "porto": "波尔图", "fc porto": "波尔图",
    "sporting cp": "葡萄牙体育", "sporting": "葡萄牙体育", "sporting lisbon": "葡萄牙体育",
    "braga": "布拉加", "sc braga": "布拉加", "sporting braga": "布拉加",
    "vitoria guimaraes": "吉马良斯", "guimaraes": "吉马良斯", "famalicao": "法马利康",
    "moreirense": "摩雷伦斯", "santa clara": "圣克拉拉", "gil vicente": "吉尔维森特",
    "estoril": "埃斯托里尔", "casa pia": "卡萨皮亚", "rio ave": "里奥阿维",
    "nacional": "纳西奥纳尔", "arouca": "阿罗卡", "estrela": "阿马多拉红星",
    "avs": "AVS", "tondela": "通德拉", "alverca": "阿尔维卡",
    # ── 比甲 ──
    "club brugge kv": "布鲁日", "club brugge": "布鲁日", "anderlecht": "安德莱赫特",
    "rsc anderlecht": "安德莱赫特", "antwerp": "安特卫普", "royal antwerp": "安特卫普",
    "genk": "亨克", "krc genk": "亨克", "gent": "根特", "kaa gent": "根特",
    "standard liege": "标准列日", "standard": "标准列日",
    "union saint gilloise": "圣吉罗斯联", "union saint-gilloise": "圣吉罗斯联",
    "union sg": "圣吉罗斯联", "cercle brugge": "塞克莱布鲁日", "charleroi": "沙勒罗瓦",
    "mechelen": "梅赫伦", "kv mechelen": "梅赫伦", "oh leuven": "鲁汶",
    "westerlo": "韦斯特洛", "st truiden": "圣特雷登", "sint-truiden": "圣特雷登",
    "stvv": "圣特雷登", "dender": "登德尔", "zulte waregem": "祖尔特瓦雷格姆",
    "la louviere": "拉卢维耶",
    # ── 西甲 ──
    "real madrid": "皇马", "barcelona": "巴萨", "fc barcelona": "巴萨", "barca": "巴萨",
    "atletico madrid": "马德里竞技", "atletico": "马德里竞技", "athletic club": "毕尔巴鄂竞技",
    "athletic bilbao": "毕尔巴鄂竞技", "real sociedad": "皇家社会", "real betis": "皇家贝蒂斯",
    "betis": "皇家贝蒂斯", "villarreal": "比利亚雷亚尔", "valencia": "瓦伦西亚",
    "sevilla": "塞维利亚", "girona": "赫罗纳", "celta vigo": "塞尔塔", "celta": "塞尔塔",
    "osasuna": "奥萨苏纳", "rayo vallecano": "巴列卡诺", "rayo": "巴列卡诺",
    "getafe": "赫塔菲", "espanyol": "西班牙人", "alaves": "阿拉维斯",
    "deportivo alaves": "阿拉维斯", "mallorca": "马洛卡", "levante": "莱万特",
    "elche": "埃尔切", "real oviedo": "皇家奥维耶多", "oviedo": "皇家奥维耶多",
    # ── 意甲 ──
    "inter": "国际米兰", "inter milan": "国际米兰", "internazionale": "国际米兰",
    "ac milan": "AC米兰", "milan": "AC米兰", "juventus": "尤文图斯", "juve": "尤文图斯",
    "napoli": "那不勒斯", "roma": "罗马", "as roma": "罗马", "lazio": "拉齐奥",
    "atalanta": "亚特兰大", "fiorentina": "佛罗伦萨", "bologna": "博洛尼亚",
    "torino": "都灵", "udinese": "乌迪内斯", "genoa": "热那亚", "cagliari": "卡利亚里",
    "como": "科莫", "hellas verona": "维罗纳", "verona": "维罗纳", "lecce": "莱切",
    "parma": "帕尔马", "pisa": "比萨", "cremonese": "克雷莫纳", "sassuolo": "萨索洛",
    # ── 日职联 J1 ──
    "kashima antlers": "鹿岛鹿角", "urawa red diamonds": "浦和红钻", "urawa reds": "浦和红钻",
    "kashiwa reysol": "柏太阳神", "fc tokyo": "FC东京", "tokyo verdy": "东京绿茵",
    "machida zelvia": "町田泽维亚", "kawasaki frontale": "川崎前锋",
    "yokohama f. marinos": "横滨水手", "yokohama f marinos": "横滨水手",
    "yokohama fc": "横滨FC", "shonan bellmare": "湘南比马", "albirex niigata": "新潟天鹅",
    "shimizu s-pulse": "清水心跳", "nagoya grampus": "名古屋鲸八",
    "kyoto sanga": "京都不死鸟", "gamba osaka": "大阪钢巴", "cerezo osaka": "大阪樱花",
    "vissel kobe": "神户胜利船", "fagiano okayama": "冈山法加诺",
    "sanfrecce hiroshima": "广岛三箭", "avispa fukuoka": "福冈黄蜂",
    # ── 韩K联 ──
    "ulsan hd": "蔚山现代", "ulsan hyundai": "蔚山现代", "jeonbuk motors": "全北现代",
    "jeonbuk hyundai motors": "全北现代", "pohang steelers": "浦项制铁", "fc seoul": "FC首尔",
    "gwangju fc": "光州FC", "gimcheon sangmu": "金泉尚武", "incheon united": "仁川联",
    "jeju sk": "济州SK", "jeju united": "济州联", "daejeon hana citizen": "大田市民",
    "gangwon fc": "江原FC", "fc anyang": "安养FC", "bucheon fc 1995": "富川FC",
    # ── 中超 ──
    "shanghai sipg": "上海海港", "shanghai port": "上海海港", "shanghai shenhua": "上海申花",
    "beijing guoan": "北京国安", "shandong taishan": "山东泰山",
    "chengdu rongcheng": "成都蓉城", "zhejiang": "浙江", "wuhan three towns": "武汉三镇",
    "tianjin jinmen tiger": "天津津门虎", "henan": "河南", "henan jianye": "河南",
    "henan songshan longmen": "河南", "qingdao hainiu": "青岛海牛",
    "yunnan yukun": "云南玉昆", "dalian yingbo": "大连英博",
    "qingdao west coast": "青岛西海岸", "shenzhen peng city": "深圳新鹏城",
    "liaoning tieren": "辽宁铁人", "chongqing tongliang long": "重庆铜梁龙",
    "changchun yatai": "长春亚泰", "meizhou hakka": "梅州客家",
    "cangzhou mighty lions": "沧州雄狮", "nantong zhiyun": "南通支云",
    # ── 挪超 Eliteserien ──
    "bodo/glimt": "博德闪耀", "molde": "莫尔德", "rosenborg": "罗森博格", "brann": "布兰",
    "viking": "维京", "aalesund": "奥勒松", "fredrikstad": "弗雷德里克斯塔",
    "hamkam": "哈姆卡姆", "kfum oslo": "奥斯陆KFUM", "kristiansund": "克里斯蒂安松",
    "lillestrom": "利勒斯特罗姆", "sandefjord": "桑德菲尤尔", "sarpsborg 08": "萨普斯堡08",
    "start": "斯塔特", "tromso": "特罗姆瑟", "valerenga": "瓦勒伦加",
    # ── 冰岛超 ──
    "breidablik": "布雷扎布里克", "vikingur reykjavik": "雷克雅未克维京",
    "kr reykjavik": "KR雷克雅未克", "valur": "瓦鲁尔", "fram": "弗拉姆",
    "ia akranes": "阿克拉内斯", "keflavik": "凯夫拉维克", "stjarnan": "斯特扎南",
    "ibv": "韦斯特曼纳", "ka akureyri": "阿克雷里KA", "fh hafnarfjordur": "哈夫纳夫约杜尔FH",
    # ── 芬兰超 ──
    "hjk helsinki": "赫尔辛基HJK", "kups": "库普斯", "inter turku": "图尔库国际",
    "fc lahti": "拉赫蒂", "ff jaro": "亚罗", "gnistan": "格尼斯坦",
    "ifk mariehamn": "玛丽港", "ilves": "伊尔维斯", "sjk seinajoki": "塞伊奈约基SJK",
    "sjk": "塞伊奈约基SJK", "tps": "图尔库TPS", "vps": "瓦萨VPS", "ac oulu": "奥卢AC",
    # ── 拉脱维亚超 1. Liga / Virsliga ──
    "rfs": "里加RFS", "riga fs": "里加RFS", "riga": "里加",
    "riga fc": "里加", "valmiera": "瓦尔米耶拉", "valmiera fc": "瓦尔米耶拉",
    "auda": "奥达", "fk auda": "奥达", "liepaja": "利耶帕亚", "fk liepaja": "利耶帕亚",
    "spartaks": "尤尔马拉斯巴达", "metta": "梅塔", "tukums": "图库姆斯",
    "super nova": "超新星", "daugavpils": "陶格夫匹尔斯", "jelgava": "叶尔加瓦",
}


def _normalize_en(name: str) -> str:
    """规范化英文队名大小写：'SHANGHAI SIPG' → 'Shanghai Sipg' 风格的标题化。
    全大写或全小写时做 title-case；已是混合大小写（如 'Henan Jianye'）则保持原样。"""
    n = name.strip()
    if n.isupper() or n.islower():
        return n.title()
    return n


def _cn_or_en(name: str) -> str:
    """队名优先取中文映射，未命中则规范化英文。"""
    return _TEAM_CN.get(name.strip().lower(), _normalize_en(name))


def _slugify(text: str) -> str:
    """生成 URL slug：仅保留 ASCII 字母数字，其余转连字符。
    非 ASCII（中文）会被丢弃 → 若结果为空则返回 ''（调用方据此回退）。"""
    s = text.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)   # 非字母数字 → 连字符
    return s.strip("-")


def report_to_post(report_md: str, *, title: str | None = None,
                   is_review: bool = False
                   ) -> tuple[str, str, str, str | None, str, str, str | None]:
    """精算报告 markdown → (title, html, excerpt, slug, meta_title,
    meta_description, seo_err)。

    title 传入则用之（管理员自定义）；否则「中文队名 vs 中文队名」做基底，
      LLM 概括出的看点拼成「X vs Y｜看点」（LLM 不可用时退回「X vs Y · 推算预测」）。
    slug 始终从报告里的英文队名生成（如 derry-city-vs-drogheda-united-prediction），
      与标题语言无关，保证 URL 是干净英文；无法生成时返回 None（让 Ghost 自动生成）。
    excerpt / meta_description 由 LLM 据【免费正文】概括（不泄露第7节结论），
      失败回退固定模板。meta_title 直接用 title。
    seo_err：LLM 概括失败原因（成功为 None）；调用方可据此先发 TG 提示再正常发布。
    付费墙：第 7 节「最终精算结论」之前免费，之后付费。
    """
    text = report_md.replace("\r\n", "\n").replace("\r", "\n")
    text = _ARCHIVE_LINE_RE.sub("", text)   # 剥掉「> 归档路径：…」行，不进发布正文

    # 队名/赛事等元信息始终从完整原文提取（复盘的「## 比赛/赛事」在第一步盲推开头）。
    meta_src = text
    # 复盘：只发布「第二步·对照复盘」，丢弃第一步盲推（无基本面的中间过程，
    # 其正文会写出没有依据的"基本面"段，不该进公开文章）。/review 命令本身两步照跑，
    # 只是发布环节剔除盲推。找不到分隔标题（如只跑了对照/旧格式）时回退用全文。
    if is_review:
        sm = _REVIEW_STEP2_RE.search(text)
        if sm:
            text = text[sm.end():].lstrip("\n")

    # 队名匹配（home/away 保留英文原名，供 slug 用；标题另取中文/规范英文）
    m = _MATCH_RE.search(meta_src)
    home = _clean_team(m.group(1)) if m else ""
    away = _clean_team(m.group(2)) if m else ""

    # 标题：管理员自定义优先；否则「中文队名 vs 中文队名」做基底，
    # 看点副标题在免费正文切出后由 LLM 生成再拼上（见下方 SEO 段）。
    custom_title = bool(title)
    vs_cn = ""
    if m:
        vs_cn = f"{_cn_or_en(home)} vs {_cn_or_en(away)}"
    if not title:
        if m:
            suffix = "复盘" if is_review else "推算预测"
            title = f"{vs_cn} · {suffix}"
        else:
            title = "精算复盘" if is_review else "推算预测"

    # slug：英文队名 + prediction/review 后缀；队名无 ASCII（纯中文）时退回 None
    slug = None
    if m:
        home_slug = _slugify(home)
        away_slug = _slugify(away)
        if home_slug and away_slug:   # 两队都有英文才生成，避免 'vs-prediction' 这种残缺
            suffix_en = "review" if is_review else "prediction"
            # 末尾加发布日期（CST，UTC+8）做后缀，避免同两队多场比赛 slug 冲突，
            # 如 liao-ning-tie-ren-chong-qing-tong-liang-long-prediction-20260704
            pub_date = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d")
            slug = f"{home_slug}-vs-{away_slug}-{suffix_en}-{pub_date}"

    # 摘要：取「## 赛事：…」一行。始终从 meta_src 取——盲推开头有标准的
    # 「## 赛事：… 开球时间：…」，比第二步对照的「开球：」格式更规整（利于后续联赛名提取）。
    em = _EVENT_RE.search(meta_src)
    excerpt = em.group(1).strip() if em else ""

    # 去掉开头元信息行（标题/摘要已含，正文重复且丑）：
    # 精算是「## 比赛：…」，复盘第二步是「## 复盘：…」，两者的「## 赛事：…」都去掉。
    text = _MATCH_RE.sub("", text, count=1)
    text = _REVIEW_HEAD_RE.sub("", text, count=1)
    text = _EVENT_RE.sub("", text, count=1)
    text = text.lstrip("\n")

    # 付费墙切分：精算切在第7节「最终精算结论」，复盘第二步切在第3节「盲推预判 vs 实际对照」
    # （免费展示实际结果+盘口结算回放引流）。
    paywall_re = _REVIEW_PAYWALL_RE if is_review else _PAYWALL_RE
    pm = paywall_re.search(text)
    if pm:
        free_md = text[:pm.start()].rstrip()
        paid_md = text[pm.start():].strip()
    else:
        # 找不到付费锚点 → 整篇付费（安全兜底）
        free_md, paid_md = "", text.strip()

    # 联赛名：从赛事行提取纯中文（去英文/轮次/开球时间），供科普段/SEO 用。
    # 如「世界杯 FIFA World Cup（小组赛首轮）」→「世界杯」。
    event_line = excerpt   # 此时 excerpt 仍是原始赛事行
    lm = re.search(r"[一-鿿·]+", event_line.split("开球时间")[0]) if event_line else None
    league_cn = lm.group(0) if lm else ""

    free_html = _render(free_md)
    paid_html = _render(paid_md)

    # 发布期增强：把免费正文的近况/交锋提炼成一段口语化「基本面速览」，插在免费区
    # 最前做引流（不含结论，不泄露付费段）。失败/未配置返回空则不插，行为回退现状。
    if free_md.strip():
        try:
            from . import analyzer
            fan_brief = analyzer.fan_fundamentals_brief(
                free_md, home or "主队", away or "客队", league_cn or "足球")
        except Exception as e:
            log.warning("科普基本面段生成异常，跳过: %s", e)
            fan_brief = ""
        if fan_brief:
            fan_html = _render(f"## 基本面速览\n\n{fan_brief}")
            free_html = f"{fan_html}\n{free_html}"

    if paid_html:
        # 有付费内容：免费段末尾插收款引导 CTA，再接付费墙分隔符
        cta = _cta_html()
        if free_html:
            html = f"{free_html}\n{cta}\n<!--members-only-->\n{paid_html}"
        else:
            html = f"{cta}\n<!--members-only-->\n{paid_html}"
    else:
        # 整篇免费，无付费墙、无 CTA
        html = free_html

    # ── SEO 元数据（中文，供 Ghost Meta data + 列表 Excerpt）──
    league_paren = f"（{league_cn}）" if league_cn else ""
    vs_or_title = vs_cn or title

    # 固定模板（LLM 不可用或失败时的回退）——保持改动前的行为
    if is_review:
        tpl_meta_desc = (
            f"{vs_or_title}{league_paren}赔率复盘：回溯盘口异动、资金流向与凯利信号，"
            "解析主流机构操盘意图与赛果偏差。")
        tpl_excerpt_tail = "赔率复盘：回溯盘口异动、资金流向与凯利信号，解析主流机构操盘意图与赛果偏差。"
    else:
        tpl_meta_desc = (
            f"{vs_or_title}{league_paren}赔率推算：欧赔亚盘资金流向、凯利指数风控、"
            "近况与历史交锋推演比分与胜平负方向。")
        tpl_excerpt_tail = "本场赔率推算：欧赔亚盘资金流向、凯利风控、近况与交锋全维度推演。"
    tpl_excerpt = f"{event_line}。{tpl_excerpt_tail}" if event_line else tpl_excerpt_tail

    # LLM 概括（只喂免费正文 free_md，天然不泄露第7节结论）。失败/未配置则回退模板，
    # 并把失败原因 seo_err 带回调用方（供 TG 先发一条提示，再继续用模板正常发布）。
    seo, seo_err = None, None
    if free_md.strip():
        try:
            from . import analyzer
            seo, seo_err = analyzer.seo_summarize(
                free_md, vs_or_title, "", league_cn or "足球", is_review=is_review)
        except Exception as e:
            log.exception("SEO 概括异常，回退模板")
            seo, seo_err = None, f"SEO 概括异常：{e}"

    if seo:
        excerpt = seo["excerpt"]
        # meta description / 社交卡片描述统一复用 excerpt，保证后台三处描述一致
        # （不再单独用 LLM 的 meta_desc，避免 Excerpt 与 Meta/X/Facebook 描述不一致）
        meta_description = excerpt
        # 看点拼进标题（仅自动标题；管理员自定义标题时不动）。meta_title = title。
        if not custom_title and vs_cn and seo.get("hook"):
            title = f"{vs_cn}｜{seo['hook']}"
    else:
        excerpt = tpl_excerpt
        meta_description = tpl_excerpt   # 回退时同样让两者一致

    meta_title = title   # 规范：Meta title 直接用文章标题

    return title, html, excerpt, slug, meta_title, meta_description, seo_err


def _render(md_text: str) -> str:
    if not md_text.strip():
        return ""
    return markdown.markdown(md_text, extensions=_MD_EXTENSIONS)


# ─── 发文（照搬 Ghost/bot/ghost_client.py）───────────────────────────────────
def _admin_url(path: str) -> str:
    # Ghost 5.x：版本号不在 URL 路径里，通过 Accept-Version 请求头传
    return f"{GHOST_ADMIN_API_URL}/ghost/api/admin/{path}"


def _admin_headers() -> dict:
    """Admin API 通用请求头（JWT 鉴权 + 版本；直连内网时补 Host/协议头）。"""
    headers = {
        "Authorization": f"Ghost {_make_token()}",
        "Content-Type": "application/json",
        "Accept-Version": GHOST_API_VERSION,
    }
    # 直连内网/环回地址时，用真实域名覆盖 Host，避免 Ghost 301 跳回公网域名；
    # 并带 X-Forwarded-Proto=https 让 Ghost 视作 https 请求、不再跳转（详见 create_post 注释）。
    if GHOST_ADMIN_HOST_HEADER:
        headers["Host"] = GHOST_ADMIN_HOST_HEADER
        headers["X-Forwarded-Proto"] = "https"
    return headers


def _find_post_by_slug(slug: str) -> dict | None:
    """按 slug 查已存在文章，返回 post 对象（含 id/updated_at）或 None。
    查询失败（网络/权限等）一律返回 None，让调用方回退到「新建」，不阻断发布。"""
    if not slug:
        return None
    try:
        r = requests.get(_admin_url(f"posts/slug/{slug}/"),
                         headers=_admin_headers(), timeout=30,
                         allow_redirects=False)
        if r.status_code == 200:
            data = r.json()
            posts = data.get("posts") or []
            return posts[0] if posts else None
        # 404 = 该 slug 不存在（正常，首次发布）；其它状态记日志后当作不存在
        if r.status_code != 404:
            log.warning("查 slug=%s 返回 HTTP %s，按不存在处理", slug, r.status_code)
    except requests.exceptions.RequestException as e:
        log.warning("查 slug=%s 失败（按不存在处理）：%s", slug, e)
    except ValueError:
        log.warning("查 slug=%s 响应非 JSON（按不存在处理）", slug)
    return None


def _build_post_payload(title: str, html: str, *, status: str, visibility: str,
                        custom_excerpt: str | None, slug: str | None,
                        meta_title: str | None,
                        meta_description: str | None) -> dict:
    """组装 Ghost post 字段（create/update 共用）。"""
    post: dict = {
        "title": title,
        "html": html,
        "status": status,
        "visibility": visibility,
    }
    if custom_excerpt:
        post["custom_excerpt"] = custom_excerpt[:300]
    if slug:
        post["slug"] = slug
    # ── SEO 元数据（搜索结果实际显示 ~60 字符标题 / ~155 字符描述）──
    # 后台可逐篇手动覆盖；此处仅写入 /publish 默认值。
    if meta_title:
        post["meta_title"] = meta_title[:300]
    if meta_description:
        post["meta_description"] = meta_description[:500]
        # 社交分享卡片（微信/Facebook OG + Twitter）复用同一份标题/描述
        post["og_title"] = (meta_title or title)[:300]
        post["og_description"] = meta_description[:500]
        post["twitter_title"] = (meta_title or title)[:300]
        post["twitter_description"] = meta_description[:500]
    return post


def _post_response(r: "requests.Response", action: str) -> dict:
    """统一解析 Ghost 发文/改文响应：处理重定向/错误，返回 post 对象。"""
    data = {}
    try:
        data = r.json()
    except ValueError:
        pass
    # 直连内网时若仍被 3xx 跳转（Host 未生效等），明确报错而非静默跟随回公网 CF
    if 300 <= r.status_code < 400:
        loc = r.headers.get("Location", "")
        raise GhostError(
            f"Ghost 返回重定向 HTTP {r.status_code} → {loc}；"
            "多为直连时 Host 头与站点 url 不符，请检查 GHOST_ADMIN_HOST_HEADER")
    if r.status_code >= 400 or "errors" in data:
        msg = _extract_error(data) or f"HTTP {r.status_code}"
        log.warning("Ghost %s失败: %s", action, msg)
        raise GhostError(msg)
    try:
        return data["posts"][0]
    except (KeyError, IndexError) as e:
        raise GhostError(f"响应格式异常：{data}") from e


def create_post(title: str, html: str, *, status: str = "published",
                visibility: str = "paid",
                custom_excerpt: str | None = None,
                slug: str | None = None,
                meta_title: str | None = None,
                meta_description: str | None = None) -> dict:
    """发布文章：slug 已存在则【更新】原文（避免 Ghost 生成 -2/-3 后缀），
    否则新建。返回 Ghost 的 post 对象（含前台 url / id）。失败抛 GhostError。"""
    post = _build_post_payload(
        title, html, status=status, visibility=visibility,
        custom_excerpt=custom_excerpt, slug=slug,
        meta_title=meta_title, meta_description=meta_description)

    # 先查同 slug 是否已存在：存在则走更新，避免重复发布产生 xxx-2 这种脏 URL。
    existing = _find_post_by_slug(slug) if slug else None
    if existing and existing.get("id"):
        # Ghost 更新用乐观锁：必须回传原文的 updated_at，否则报 "Saving failed..."。
        # 复用原文 updated_at；slug 保持不变（就更新这篇），避免又生成新 slug。
        post["updated_at"] = existing.get("updated_at")
        post.pop("slug", None)
        body = {"posts": [post]}
        try:
            r = requests.put(_admin_url(f"posts/{existing['id']}/"),
                             params={"source": "html"}, json=body,
                             headers=_admin_headers(), timeout=60,
                             allow_redirects=False)
        except requests.exceptions.RequestException as e:
            log.warning("Ghost 更新请求异常: %s", e)
            raise GhostError(f"网络错误：{e}") from e
        return _post_response(r, "更新")

    # 不存在 → 新建
    body = {"posts": [post]}
    try:
        r = requests.post(_admin_url("posts/"), params={"source": "html"},
                          json=body, headers=_admin_headers(), timeout=60,
                          allow_redirects=False)
    except requests.exceptions.RequestException as e:
        log.warning("Ghost 请求异常: %s", e)
        raise GhostError(f"网络错误：{e}") from e
    return _post_response(r, "发文")


def _extract_error(data: dict) -> str:
    try:
        errs = data.get("errors") or []
        if errs:
            e = errs[0]
            parts = [e.get("message", "")]
            if e.get("context"):
                parts.append(str(e["context"]))
            return " — ".join(p for p in parts if p)
    except Exception:
        pass
    return ""
