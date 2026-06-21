"""
滚球(走地/in-play)能力探针 —— 实测 API-Football 是否提供实时滚球盘

目的：在决定是否做"走地档"前，用真实请求确认三件事，避免盲目动工：
  1. 套餐是否开放 /odds/live 端点（部分套餐不含，会返回 errors/空）
  2. /odds/live 的 JSON 结构：是否带比赛进行分钟数、当前比分
  3. 滚球盘里有没有大小球(bet id=5)与亚盘(bet id=4)，value 文本格式

用法：
  python probe_live.py            # 探 /odds/live（全量进行中比赛）+ /odds/live/bets（可用盘口字典）
  python probe_live.py bets       # 只看滚球支持哪些 bet 类型

样例 JSON 存到 probe_samples/ 供离线参考（该目录已被 .gitignore 排除）。
"""

import os
import sys
import json
from datetime import datetime, timezone, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("APIFOOTBALL_KEY", "").strip()
if not API_KEY:
    sys.exit("未找到 APIFOOTBALL_KEY，请在 .env 中配置")

BASE_URL = "https://v3.football.api-sports.io"
HEADERS = {"x-apisports-key": API_KEY}
TZ_CST = timezone(timedelta(hours=8))
SAMPLE_DIR = "probe_samples"


def save_sample(name: str, data) -> None:
    os.makedirs(SAMPLE_DIR, exist_ok=True)
    path = os.path.join(SAMPLE_DIR, f"{name}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"    → 原始 JSON 已保存：{path}")


def api_get(endpoint: str, params: dict | None = None) -> dict:
    url = f"{BASE_URL}{endpoint}"
    resp = requests.get(url, headers=HEADERS, params=params or {}, timeout=20)
    rl_limit = resp.headers.get("x-ratelimit-requests-limit", "?")
    rl_remain = resp.headers.get("x-ratelimit-requests-remaining", "?")
    print(f"    [额度] 每日剩余 {rl_remain}/{rl_limit}")
    resp.raise_for_status()
    return resp.json()


def show_errors(data: dict) -> bool:
    errors = data.get("errors")
    if errors and (isinstance(errors, dict) and errors
                   or isinstance(errors, list) and errors):
        print(f"    ⚠️ API 返回 errors：{errors}")
        print("    → 若提示套餐不支持/无权限，则当前套餐无滚球能力，走地档无法落地。")
        return True
    return False


def probe_live_bets() -> None:
    """/odds/live/bets —— 滚球支持的 bet 类型字典（确认大小球/亚盘在不在）。"""
    print("\n" + "=" * 70)
    print("A. 滚球可用盘口类型  (/odds/live/bets)")
    print("=" * 70)
    data = api_get("/odds/live/bets")
    if show_errors(data):
        return
    save_sample("live_bets", data)
    bets = data.get("response", [])
    print(f"    滚球共支持 {len(bets)} 种盘口，重点找大小球/亚盘：\n")
    for b in bets:
        bid = b.get("id")
        name = b.get("name", "")
        flag = ""
        low = name.lower()
        if "over/under" in low or "goals" in low:
            flag = "  ← 大小球?"
        elif "asian" in low and "handicap" in low:
            flag = "  ← 亚盘?"
        elif low == "match winner" or "1x2" in low:
            flag = "  ← 欧赔?"
        print(f"      id={bid:<5} {name}{flag}")


def probe_live() -> None:
    """/odds/live —— 当前进行中的全部比赛实时盘口。"""
    print("\n" + "=" * 70)
    print("B. 实时滚球盘  (/odds/live)")
    print("=" * 70)
    data = api_get("/odds/live")
    if show_errors(data):
        return
    save_sample("live_odds_sample", data)
    resp = data.get("response", [])
    print(f"    当前进行中比赛数：{len(resp)}")
    if not resp:
        print("    response 为空——当前无进行中的比赛（换个有比赛的时段再探）。")
        print("    注意：空结果≠无能力；若上面没报 errors，端点本身是通的。")
        return

    entry = resp[0]
    fx = entry.get("fixture", {})
    status = fx.get("status", {})
    teams = entry.get("teams", {})
    print("\n    ── 样本场 ──")
    print(f"      fixture.id      = {fx.get('id')}")
    print(f"      进行分钟 elapsed = {status.get('elapsed')}  (这是走地'第几分钟'，走地档打标的关键)")
    print(f"      status          = {status.get('long')}")
    print(f"      home vs away    = {teams.get('home', {}).get('name')} vs {teams.get('away', {}).get('name')}")
    print(f"      goals(实时比分)  = {entry.get('teams', {})}")
    # 比分字段在不同版本可能叫 goals 或在 status 里
    if "goals" in entry:
        print(f"      goals           = {entry.get('goals')}")

    odds = entry.get("odds", [])
    print(f"\n    该场滚球盘口类型数：{len(odds)}")
    print("    ── 列出全部 bet 类型，确认大小球(找 Over/Under)在不在 ──")
    for bet in odds:
        bid = bet.get("id")
        bname = bet.get("name", "")
        values = bet.get("values", [])
        sample = [(v.get("value"), v.get("odd")) for v in values[:4]]
        flag = "  ← 大小球?" if ("over" in bname.lower() or "goals" in bname.lower()) else ""
        print(f"      bet id={bid:<5} {bname:<26} 样例={sample}{flag}")

    print("\n    ⚠️ 重点核对：")
    print("      - elapsed 字段有没有？(决定能否打'走地20'标签)")
    print("      - 有没有 Over/Under 大小球盘？value 文本格式？")
    print("      - 实时比分字段在哪？(走地分析要知道当前比分)")


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else "all"
    print("API-Football 滚球能力探针  |  BASE =", BASE_URL)
    print("时间（CST）：", datetime.now(TZ_CST).strftime("%Y-%m-%d %H:%M"))

    if arg in ("all", "bets"):
        probe_live_bets()
    if arg in ("all", "live"):
        probe_live()

    print("\n完成。把上面输出贴给我，据此判断走地档能否落地、怎么打标。")


if __name__ == "__main__":
    main()
