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
                   ) -> tuple[str, str, str, str | None, str, str]:
    """精算报告 markdown → (title, html, excerpt, slug, meta_title, meta_description)。

    title 传入则用之（管理员自定义）；否则从首行 '## 比赛：X vs Y' 生成。
    slug 始终从报告里的英文队名生成（如 derry-city-vs-drogheda-united-prediction），
    与标题语言无关，保证 URL 是干净英文；无法生成时返回 None（让 Ghost 自动生成）。
    付费墙：第 7 节「最终精算结论」之前免费，之后付费。
    """
    text = report_md.replace("\r\n", "\n").replace("\r", "\n")

    # 队名匹配（home/away 保留英文原名，供 slug 用；标题另取中文/规范英文）
    m = _MATCH_RE.search(text)
    home = _clean_team(m.group(1)) if m else ""
    away = _clean_team(m.group(2)) if m else ""

    # 标题：队名优先中文映射，未命中规范化英文；分隔符用 ·
    if not title:
        if m:
            suffix = "复盘" if is_review else "精算预测"
            title = f"{_cn_or_en(home)} vs {_cn_or_en(away)} · {suffix}"
        else:
            title = "精算复盘" if is_review else "精算预测"

    # slug：英文队名 + prediction/review 后缀；队名无 ASCII（纯中文）时退回 None
    slug = None
    if m:
        home_slug = _slugify(home)
        away_slug = _slugify(away)
        if home_slug and away_slug:   # 两队都有英文才生成，避免 'vs-prediction' 这种残缺
            suffix_en = "review" if is_review else "prediction"
            slug = f"{home_slug}-vs-{away_slug}-{suffix_en}"

    # 摘要：取「## 赛事：…」一行
    em = _EVENT_RE.search(text)
    excerpt = em.group(1).strip() if em else ""

    # 去掉开头「## 比赛：…」「## 赛事：…」两行元信息（标题/摘要已含，正文重复且丑）
    text = _MATCH_RE.sub("", text, count=1)
    text = _EVENT_RE.sub("", text, count=1)
    text = text.lstrip("\n")

    # 付费墙切分
    pm = _PAYWALL_RE.search(text)
    if pm:
        free_md = text[:pm.start()].rstrip()
        paid_md = text[pm.start():].strip()
    else:
        # 找不到第 7 节锚点 → 整篇付费（安全兜底）
        free_md, paid_md = "", text.strip()

    free_html = _render(free_md)
    paid_html = _render(paid_md)
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
    home_cn = _cn_or_en(home) if home else ""
    away_cn = _cn_or_en(away) if away else ""
    vs_cn = f"{home_cn} vs {away_cn}" if (home_cn or away_cn) else title
    # 从赛事行提取纯中文联赛名（去掉英文/轮次/开球时间），如「世界杯 FIFA World Cup（小组赛首轮）」→「世界杯」
    lm = re.search(r"[一-鿿·]+", excerpt.split("开球时间")[0]) if excerpt else None
    league_cn = lm.group(0) if lm else ""
    league_paren = f"（{league_cn}）" if league_cn else ""

    if is_review:
        meta_title = f"{vs_cn}赔率分析" + (f"｜{league_cn}复盘解盘" if league_cn else "｜复盘解盘")
        meta_description = (
            f"{vs_cn}{league_paren}赔率复盘：回溯盘口异动、资金流向与凯利信号，"
            "解析庄家操盘意图与赛果偏差。完整复盘见正文。")
        excerpt_tail = "赔率复盘：回溯盘口异动、资金流向与凯利信号，解析操盘意图与赛果偏差。"
    else:
        meta_title = f"{vs_cn}赔率分析" + (f"｜{league_cn}精算预测" if league_cn else "｜精算预测")
        meta_description = (
            f"{vs_cn}{league_paren}赔率精算：欧赔亚盘资金流向、凯利指数风控、"
            "近况与历史交锋推演比分与胜平负方向。完整结论见正文。")
        excerpt_tail = "本场赔率精算：欧赔亚盘资金流向、凯利风控、近况与交锋全维度推演，完整结论见正文。"

    # 列表卡片 Excerpt：原赛事行（含开球时间，站内浏览有用）+ 内容简介
    excerpt = f"{excerpt}。{excerpt_tail}" if excerpt else excerpt_tail

    return title, html, excerpt, slug, meta_title, meta_description


def _render(md_text: str) -> str:
    if not md_text.strip():
        return ""
    return markdown.markdown(md_text, extensions=_MD_EXTENSIONS)


# ─── 发文（照搬 Ghost/bot/ghost_client.py）───────────────────────────────────
def _admin_url(path: str) -> str:
    # Ghost 5.x：版本号不在 URL 路径里，通过 Accept-Version 请求头传
    return f"{GHOST_ADMIN_API_URL}/ghost/api/admin/{path}"


def create_post(title: str, html: str, *, status: str = "published",
                visibility: str = "paid",
                custom_excerpt: str | None = None,
                slug: str | None = None,
                meta_title: str | None = None,
                meta_description: str | None = None) -> dict:
    """创建文章，返回 Ghost 的 post 对象（含前台 url / id）。失败抛 GhostError。"""
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

    body = {"posts": [post]}
    headers = {
        "Authorization": f"Ghost {_make_token()}",
        "Content-Type": "application/json",
        "Accept-Version": GHOST_API_VERSION,
    }
    try:
        r = requests.post(_admin_url("posts/"), params={"source": "html"},
                          json=body, headers=headers, timeout=60)
    except requests.exceptions.RequestException as e:
        log.warning("Ghost 请求异常: %s", e)
        raise GhostError(f"网络错误：{e}") from e

    data = {}
    try:
        data = r.json()
    except ValueError:
        pass

    if r.status_code >= 400 or "errors" in data:
        msg = _extract_error(data) or f"HTTP {r.status_code}"
        log.warning("Ghost 发文失败: %s", msg)
        raise GhostError(msg)

    try:
        return data["posts"][0]
    except (KeyError, IndexError) as e:
        raise GhostError(f"响应格式异常：{data}") from e


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
