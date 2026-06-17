"""
LLM 连通性探针 —— 实测 IKuncode chat/completions 能否打通

用法（先在 .env 配好 LLM_BASE_URL / LLM_API_KEY）：
  python probe_llm.py          # 发一个最小请求，确认通
  python probe_llm.py full     # 用真实规则+一场比赛跑完整精算（耗 token）

先跑无参数版确认连通和返回结构，再决定要不要 full。
"""

import sys
import os
from dotenv import load_dotenv
load_dotenv()

from bot import analyzer, config


def probe_minimal():
    print("BASE:", analyzer.LLM_BASE_URL, "| MODEL:", config.LLM_MODEL)
    print("key 已配:", bool(analyzer.LLM_API_KEY))
    if not analyzer.available():
        sys.exit("未配置 LLM_BASE_URL / LLM_API_KEY")
    import requests
    payload = {"model": config.LLM_MODEL,
               "messages": [{"role": "user", "content": "回复两个字：通了"}],
               "max_tokens": 50}
    r = requests.post(f"{analyzer.LLM_BASE_URL}/chat/completions",
                      json=payload,
                      headers={"Authorization": f"Bearer {analyzer.LLM_API_KEY}"},
                      timeout=60)
    print("HTTP:", r.status_code)
    print("响应前 500 字:", r.text[:500])


def probe_full():
    """用真实规则跑一次，需要本地 odds.db 有数据。"""
    from bot import db, fundamentals
    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT fixture_id FROM odds_history LIMIT 1").fetchone()
        if not row:
            sys.exit("本地 odds.db 无盘口数据，无法 full 测试")
        fid = row[0]
        meta_row = db.get_fixture_meta(conn, fid)
        funds = fundamentals.build_fundamentals(conn, fid)
    finally:
        conn.close()
    # 复用 tgbot 的 CSV 构建
    from bot import tgbot
    csv_str, meta = tgbot._build_csv(fid)
    print(f"用 fixture {fid}: {meta['home']} vs {meta['away']}")
    print("调用 LLM（可能 1~3 分钟）…")
    report = analyzer.analyze(csv_str, funds, meta["home"], meta["away"],
                              meta["league"])
    print("=" * 50)
    print(report[:2000])


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "full":
        probe_full()
    else:
        probe_minimal()
