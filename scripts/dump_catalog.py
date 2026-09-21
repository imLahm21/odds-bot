"""
目录导出工具 —— 把 API-Football 支持的全部联赛/博彩公司导出成易读清单

用法：
  python -m scripts.dump_catalog            # 用已缓存的 probe_samples，离线生成
  python -m scripts.dump_catalog --fetch    # 重新联网拉取最新目录再生成

产出：
  scripts/output/catalog_leagues.txt      所有有当前赛季的联赛，按国家分组（含 id/season）
  scripts/output/catalog_bookmakers.txt   所有博彩公司（含 id）

想新增联赛时，在 catalog_leagues.txt 里找到 id，填进 bot/config.py 的
EXTRA_LEAGUES，或直接在 Telegram bot 用 /add <id> <season> 添加。
"""

import os
import sys
import json
from pathlib import Path

import requests
from dotenv import load_dotenv

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = Path(__file__).resolve().parent / "output"
SAMPLE_DIR = PROJECT_ROOT / "probe_samples"

load_dotenv(PROJECT_ROOT / ".env")

BASE = "https://v3.football.api-sports.io"


def _load(name: str, endpoint: str, fetch: bool):
    """优先读缓存；--fetch 或缓存缺失时联网。"""
    path = SAMPLE_DIR / f"{name}.json"
    if not fetch and path.exists():
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    key = os.getenv("APIFOOTBALL_KEY", "").strip()
    if not key:
        sys.exit("缓存缺失且未配置 APIFOOTBALL_KEY，无法联网拉取")
    r = requests.get(f"{BASE}{endpoint}", headers={"x-apisports-key": key},
                     timeout=20)
    data = r.json()
    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
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

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = OUTPUT_DIR / "catalog_leagues.txt"
    with output_path.open("w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"✓ {output_path} 已生成（{len(by_country)} 国 / "
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
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = OUTPUT_DIR / "catalog_bookmakers.txt"
    with output_path.open("w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"✓ {output_path} 已生成（{len(resp)} 家）")


if __name__ == "__main__":
    fetch = "--fetch" in sys.argv
    dump_leagues(fetch)
    dump_bookmakers(fetch)
