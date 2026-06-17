"""
目录导出工具 —— 把 API-Football 支持的全部联赛/博彩公司导出成易读清单

用法：
  python dump_catalog.py            # 用已缓存的 probe_samples，离线生成
  python dump_catalog.py --fetch    # 重新联网拉取最新目录再生成

产出：
  catalog_leagues.txt      所有有当前赛季的联赛，按国家分组（含 id/season）
  catalog_bookmakers.txt   所有博彩公司（含 id）

想新增联赛时，在 catalog_leagues.txt 里找到 id，填进 bot/config.py 的
EXTRA_LEAGUES，或直接在 Telegram bot 用 /add <id> <season> 添加。
"""

import os
import sys
import json

import requests
from dotenv import load_dotenv

load_dotenv()

BASE = "https://v3.football.api-sports.io"
SAMPLE_DIR = "probe_samples"


def _load(name: str, endpoint: str, fetch: bool):
    """优先读缓存；--fetch 或缓存缺失时联网。"""
    path = os.path.join(SAMPLE_DIR, f"{name}.json")
    if not fetch and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    key = os.getenv("APIFOOTBALL_KEY", "").strip()
    if not key:
        sys.exit("缓存缺失且未配置 APIFOOTBALL_KEY，无法联网拉取")
    r = requests.get(f"{BASE}{endpoint}", headers={"x-apisports-key": key},
                     timeout=20)
    data = r.json()
    os.makedirs(SAMPLE_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return data


def _cur(item) -> int | None:
    return next((s["year"] for s in item.get("seasons", []) if s.get("current")),
                None)


def dump_leagues(fetch: bool) -> None:
    data = _load("leagues_all", "/leagues", fetch)
    resp = data.get("response", [])
    # 按国家分组，只保留有当前赛季的
    by_country: dict[str, list] = {}
    for x in resp:
        season = _cur(x)
        if season is None:
            continue
        ctry = x.get("country", {}).get("name", "?")
        lg = x.get("league", {})
        by_country.setdefault(ctry, []).append(
            (lg.get("id"), lg.get("name"), lg.get("type"), season))

    lines = [f"# API-Football 联赛目录（有当前赛季的，共 "
             f"{sum(len(v) for v in by_country.values())} 个，{len(by_country)} 个国家/地区）",
             "# 格式： id  名称  [类型]  season", ""]
    for ctry in sorted(by_country):
        items = sorted(by_country[ctry], key=lambda t: t[0])
        lines.append(f"== {ctry} ({len(items)}) ==")
        for lid, name, typ, season in items:
            lines.append(f"  {lid:<6} {name}  [{typ}]  {season}")
        lines.append("")

    with open("catalog_leagues.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"✓ catalog_leagues.txt 已生成（{len(by_country)} 国 / "
          f"{sum(len(v) for v in by_country.values())} 联赛）")


def dump_bookmakers(fetch: bool) -> None:
    data = _load("bookmakers_all", "/odds/bookmakers", fetch)
    resp = data.get("response", [])
    lines = [f"# API-Football 博彩公司目录（共 {len(resp)} 家）",
             "# 格式： id  名称", ""]
    for b in sorted(resp, key=lambda x: x.get("id", 0)):
        name = b.get("name")
        if name:
            lines.append(f"  {b.get('id'):<6} {name}")
    with open("catalog_bookmakers.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"✓ catalog_bookmakers.txt 已生成（{len(resp)} 家）")


if __name__ == "__main__":
    fetch = "--fetch" in sys.argv
    dump_leagues(fetch)
    dump_bookmakers(fetch)
