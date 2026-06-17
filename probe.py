"""
阶段 0 探针脚本 —— 实测 API-Football 真实 JSON 结构

目的：在写任何解析代码前，用真实请求确认以下事实，避免靠记忆赌字段名：
  1. 账号套餐 / 每日额度 / 速率限制      (/status)
  2. 关注联赛的数字 league_id + 当前 season (/leagues)
  3. fixtures 的 JSON 结构                (/fixtures)
  4. odds 的 JSON 结构 + bookmaker ID + bet ID + 亚盘/欧赔字段 (/odds)

用法：
  python probe.py            # 全部探测
  python probe.py status     # 只看额度
  python probe.py leagues    # 只看联赛 ID
  python probe.py odds       # 只看赔率结构

所有原始 JSON 样例会保存到 probe_samples/ 供离线开发参考（该目录已被 .gitignore 排除）。
"""

import os
import sys
import json
import time
from datetime import datetime, timezone, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("APIFOOTBALL_KEY", "").strip()
if not API_KEY:
    sys.exit("未找到 APIFOOTBALL_KEY，请在 .env 中配置")

# API-Football 直连端点（非 RapidAPI）
BASE_URL = "https://v3.football.api-sports.io"
HEADERS = {"x-apisports-key": API_KEY}

TZ_CST = timezone(timedelta(hours=8))

# 关注的联赛（中文名 → 用于在 /leagues 结果中匹配出数字 id）
# 探针阶段用英文关键词模糊匹配，确认后写入 config.py
WATCH_LEAGUES = {
    "英超":   "Premier League",      # 注意：英格兰，需排除其他国家同名
    "西甲":   "La Liga",
    "德甲":   "Bundesliga",
    "意甲":   "Serie A",
    "法甲":   "Ligue 1",
    "欧冠":   "UEFA Champions League",
    "欧联":   "UEFA Europa League",
    "欧协联": "UEFA Europa Conference League",
    "欧国联": "UEFA Nations League",
    "世界杯": "World Cup",
    "中超":   "Super League",         # 中国，需排除其他国家
    "日职联": "J1 League",
    "韩K联":  "K League 1",
    "沙特联": "Pro League",
    "冰岛超": "Úrvalsdeild",          # 冰岛顶级，英文常写 Besta deild / Premier League
}

SAMPLE_DIR = "probe_samples"


def save_sample(name: str, data) -> None:
    os.makedirs(SAMPLE_DIR, exist_ok=True)
    path = os.path.join(SAMPLE_DIR, f"{name}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"    → 原始 JSON 已保存：{path}")


def api_get(endpoint: str, params: dict | None = None) -> dict:
    """发请求并打印额度头。返回完整 JSON（含 response/errors/results）。"""
    url = f"{BASE_URL}{endpoint}"
    resp = requests.get(url, headers=HEADERS, params=params or {}, timeout=20)
    # API-Football 的额度信息在响应头
    rl_limit = resp.headers.get("x-ratelimit-requests-limit", "?")
    rl_remain = resp.headers.get("x-ratelimit-requests-remaining", "?")
    rl_min = resp.headers.get("X-RateLimit-Limit", "?")          # 每分钟限制
    rl_min_remain = resp.headers.get("X-RateLimit-Remaining", "?")
    print(f"    [额度] 每日剩余 {rl_remain}/{rl_limit}  |  本分钟剩余 {rl_min_remain}/{rl_min}")
    resp.raise_for_status()
    return resp.json()


def show_errors(data: dict) -> bool:
    """API-Football 即使 HTTP 200 也可能在 errors 字段报错。返回是否有错。"""
    errors = data.get("errors")
    if errors and (isinstance(errors, dict) and errors or isinstance(errors, list) and errors):
        print(f"    ⚠️ API 返回 errors：{errors}")
        return True
    return False


def probe_status() -> None:
    print("\n" + "=" * 70)
    print("1. 账号状态 / 套餐 / 额度  (/status)")
    print("=" * 70)
    data = api_get("/status")
    if show_errors(data):
        return
    save_sample("status", data)
    resp = data.get("response", {})
    sub = resp.get("subscription", {})
    reqs = resp.get("requests", {})
    print(f"    套餐：{sub.get('plan')}   状态：{sub.get('active')}   到期：{sub.get('end')}")
    print(f"    今日已用：{reqs.get('current')} / {reqs.get('limit_day')}")


def probe_leagues() -> dict:
    print("\n" + "=" * 70)
    print("2. 联赛 ID 匹配  (/leagues)  —— 用于填 config.py")
    print("=" * 70)
    data = api_get("/leagues")
    if show_errors(data):
        return {}
    save_sample("leagues_all", data)
    leagues = data.get("response", [])
    print(f"    API 共返回 {len(leagues)} 个联赛，下面匹配关注的 {len(WATCH_LEAGUES)} 个：\n")

    found = {}
    for zh, en_kw in WATCH_LEAGUES.items():
        matches = []
        for item in leagues:
            lg = item.get("league", {})
            country = item.get("country", {})
            name = lg.get("name", "")
            if en_kw.lower() in name.lower():
                # 取当前赛季
                seasons = item.get("seasons", [])
                cur = next((s["year"] for s in seasons if s.get("current")), None)
                matches.append((lg.get("id"), name, country.get("name"), cur))
        if matches:
            for lid, name, ctry, season in matches:
                print(f"    {zh:<6} → id={lid:<5} {name} ({ctry})  当前赛季={season}")
            found[zh] = matches
        else:
            print(f"    {zh:<6} → ❌ 未匹配到 '{en_kw}'，需手动在 leagues_all.json 中查找")
    return found


def probe_fixtures(league_id: int, season: int) -> dict | None:
    print("\n" + "=" * 70)
    print(f"3. 赛程结构  (/fixtures?league={league_id}&season={season})")
    print("=" * 70)
    # 先拉未来 14 天；休赛期可能为空，则回退到最近 14 天已结束比赛
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    nxt = (datetime.now(timezone.utc) + timedelta(days=14)).strftime("%Y-%m-%d")
    data = api_get("/fixtures", {
        "league": league_id, "season": season,
        "from": today, "to": nxt,
    })
    if show_errors(data):
        return None
    fixtures = data.get("response", [])
    print(f"    未来 14 天共 {len(fixtures)} 场")

    if not fixtures:
        prev = (datetime.now(timezone.utc) - timedelta(days=14)).strftime("%Y-%m-%d")
        print(f"    （休赛期？回退抓最近 14 天已结束比赛 {prev}~{today}）")
        time.sleep(1)
        data = api_get("/fixtures", {
            "league": league_id, "season": season,
            "from": prev, "to": today,
        })
        fixtures = data.get("response", [])
        print(f"    最近 14 天共 {len(fixtures)} 场")

    save_sample("fixtures_sample", data)
    if fixtures:
        f0 = fixtures[-1]  # 取最近一场（已结束的也可能仍带盘口）
        print("    样本场关键字段：")
        fx = f0.get("fixture", {})
        teams = f0.get("teams", {})
        print(f"      fixture.id   = {fx.get('id')}")
        print(f"      fixture.date = {fx.get('date')}")
        print(f"      status       = {fx.get('status', {}).get('short')}")
        print(f"      home         = {teams.get('home', {}).get('name')}")
        print(f"      away         = {teams.get('away', {}).get('name')}")
        return f0
    return None


def probe_odds(fixture_id: int) -> None:
    print("\n" + "=" * 70)
    print(f"4. 赔率结构  (/odds?fixture={fixture_id})  —— 最关键")
    print("=" * 70)
    data = api_get("/odds", {"fixture": fixture_id})
    if show_errors(data):
        print("    （该场可能尚无盘口，换一场临近开赛的比赛再试）")
        return
    save_sample("odds_sample", data)
    resp = data.get("response", [])
    if not resp:
        print("    response 为空——该场暂无赔率，建议换临近开赛的比赛")
        return

    entry = resp[0]
    bookmakers = entry.get("bookmakers", [])
    print(f"    update 时间：{entry.get('update')}")
    print(f"    bookmakers 数量：{len(bookmakers)}\n")

    # 列出所有 bookmaker id + name（确认 Pinnacle / Bet365 的 ID）
    print("    ── 全部 bookmaker（确认 Pinnacle/Bet365 的数字 ID）──")
    for bm in bookmakers:
        print(f"      id={bm.get('id'):<5} {bm.get('name')}")

    # 用第一家展开 bet 类型，确认亚盘/欧赔的 bet id 和 value 文本格式
    if bookmakers:
        bm0 = bookmakers[0]
        print(f"\n    ── 以 [{bm0.get('name')}] 为例，列出全部 bet 类型 ──")
        for bet in bm0.get("bets", []):
            bid = bet.get("id")
            bname = bet.get("name")
            values = bet.get("values", [])
            sample_vals = values[:4]
            print(f"      bet id={bid:<4} {bname:<28} values样例={sample_vals}")

    print("\n    ⚠️ 重点核对：")
    print("      - 'Match Winner'(欧赔1X2) 的 bet id = ?")
    print("      - 'Asian Handicap'(亚盘) 的 bet id = ?  value 里让球数怎么表示？")
    print("      - Pinnacle / Bet365 的 bookmaker id = ?")


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else "all"

    print("API-Football 探针  |  BASE =", BASE_URL)
    print("时间（CST）：", datetime.now(TZ_CST).strftime("%Y-%m-%d %H:%M"))

    if arg in ("all", "status"):
        probe_status()
        time.sleep(1)

    found = {}
    if arg in ("all", "leagues"):
        found = probe_leagues()
        time.sleep(1)

    # fixtures + odds 需要先有 league_id
    # 支持命令行指定：python probe.py odds <league_id> <season>
    if arg in ("all", "fixtures", "odds"):
        if len(sys.argv) >= 4:
            league_id = int(sys.argv[2])
            season = int(sys.argv[3])
        else:
            # 默认用世界杯(id=1, season=2026)——6月休赛期时它正在进行
            epl = found.get("英超")
            league_id = epl[0][0] if epl else 1
            season = epl[0][3] if (epl and epl[0][3]) else 2026

        f0 = probe_fixtures(league_id, season)
        time.sleep(1)

        if f0 and arg in ("all", "odds"):
            fid = f0.get("fixture", {}).get("id")
            if fid:
                probe_odds(fid)

    print("\n完成。原始样例在", SAMPLE_DIR, "目录，请把上面输出贴给我核对映射。")


if __name__ == "__main__":
    main()
