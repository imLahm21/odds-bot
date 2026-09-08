"""
LLM 连通性探针 —— 实测分组端点的 chat/completions

用法（推荐先在 .env 配好 LLM_ROUTE_ENDPOINTS）：
  python probe_llm.py          # 发一个最小请求，确认通
  python probe_llm.py effort    # 逐档测 reasoning_effort（重点验 xhigh 是否被网关接受）
  python probe_llm.py full     # 用真实规则+一场比赛跑完整精算（耗 token）
  python probe_llm.py pool      # 端点池：解析 .env 端点、逐条探针、打印熔断态与 9 参数

先跑无参数版确认连通和返回结构，再决定要不要 effort / full / pool。
"""

import sys
import os
from dotenv import load_dotenv
load_dotenv()

# Windows 控制台默认 GBK，LLM 响应含中文/emoji 会 UnicodeEncodeError，强制 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from bot import analyzer, config


def probe_minimal():
    """按管理员重档的模型链，逐条测该模型所属密钥组的端点。"""
    from bot import db, llm_client
    db.init_db()
    issues = llm_client.routing_issues()
    if issues:
        print("⚠️ 路由配置问题：")
        for item in issues:
            print("  ·", item)
    chain = llm_client.resolve_model_chain("heavy", visitor=False)
    if not chain:
        sys.exit(llm_client.chain_error("heavy", visitor=False))
    eps = llm_client.endpoints()
    print("模型链（主>回退）:", ">".join(chain))
    for model in chain:
        groups = set(llm_client.route_groups_for_model(model))
        print(f"\n[{model}] 密钥组 {'/'.join(sorted(groups))}")
        for idx, ep in enumerate(eps):
            if ep["route_group"] not in groups:
                continue
            result = llm_client.probe_model(idx, model)
            status = "OK" if result["ok"] else result.get("error", "失败")
            print(f"  [{idx}] {ep['label']} {status}")


def probe_effort():
    """逐档发最小请求，验证网关是否接受 reasoning_effort（尤其 xhigh）。
    每档单独打一枪，区分「整体不支持该字段」与「仅某档（如 xhigh）不认」。"""
    from bot import db, llm_client
    db.init_db()
    print("按管理员重档的真实密钥组逐档测试 reasoning_effort：")
    for eff, label in config.LLM_EFFORT_LABELS.items():
        result = llm_client.chat(
            "你是连通性探针。", "回复两个字：通了",
            effort=eff, tier="heavy", timeout=60, max_tokens=50)
        ok = not result.startswith(("LLM 请求失败", "LLM 超时", "LLM 网络错误"))
        print(f"[{eff:<6} {label}] {'OK' if ok else '失败'}  {result[:120]}")
    print("=" * 50)
    print("提示：若某档报 HTTP 400 且错误提到 reasoning_effort / unsupported value，"
          "说明该网关/模型不认那一档——把它从 config.LLM_EFFORT_LABELS 移除即可。")


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


def probe_pool(which: str = "heavy"):
    """端点池验证：解析 .env 端点、对每条跑最小 chat 探针、打印熔断态 + 9 参数。
    which ∈ heavy/balanced/light：测哪档（按该档管理员选定的主模型）。
    需先 init_db（读 llm_settings）；未 init 时 llm_client 回退 config 默认。"""
    from bot import llm_client, db
    try:
        db.init_db()   # 确保 llm_settings 表存在并已 seed
    except Exception as e:
        print(f"（init_db 失败，参数将回退 config 默认）：{e}")

    eps = llm_client.endpoints()
    print(f"解析到 {len(eps)} 个端点：")
    for i, ep in enumerate(eps):
        models = config.llm_models_in_group(ep["route_group"])
        print(f"  [{i}] [{ep['route_group']}] {ep['label']} → {ep['base_url']}"
              f"  组内模型：{'、'.join(models) or '未绑定'}")
    if not eps:
        sys.exit("未配置任何端点（.env 缺 LLM_ROUTE_ENDPOINTS）")

    issues = llm_client.routing_issues()
    if issues:
        print("\n⚠️ 路由配置问题：")
        for item in issues:
            print("  ·", item)

    print("\n12 个模型槽位（主>回退；❌=缺密钥组）：")
    for slot in llm_client.slot_snapshot():
        role = "访客" if slot["role"] == "visitor" else "管理员"
        p = ("✅" if slot["primary_ready"] else "❌") + slot["primary"]
        f = (("✅" if slot["fallback_ready"] else "❌") + slot["fallback"]
             if slot["fallback"] else "—未设")
        print(f"  {slot['label']:<16} {role:<4} 主 {p:<22} 回退 {f}")

    print("\n当前 9 参数（DB llm_settings / 回退 config 默认）：")
    for k, v in llm_client.get_settings().items():
        print(f"  {k:<28} = {v}")

    tier = {"heavy": "重档", "balanced": "平衡档", "light": "轻档"}.get(which, "重档")
    print(f"\n逐端点连通性探针（{tier}，最小 chat 请求）：")
    print("=" * 60)
    for r in llm_client.probe_all(which):
        req = r.get("req_model", "")
        if r["ok"]:
            print(f"  ✅ [{r['label']}] {req} · HTTP {r['http_status']} · "
                  f"{r['latency_ms']}ms · 应答 {r.get('model', '')}")
        else:
            status = r["http_status"] if r["http_status"] is not None else "无响应"
            icon = ("⏭️" if r.get("skipped") else
                    ("❗" if r.get("http_status") == 200 else "❌"))
            print(f"  {icon} [{r['label']}] {req} · {status} · {r['latency_ms']}ms · "
                  f"{r.get('error', '')}")

    print("\n各端点熔断器初始态：")
    for st in llm_client.breaker_stats():
        print(f"  [{st['label']}] state={st['state']} 连续失败={st['consecutive']} "
              f"错误率={st['error_rate']:.0f}% 样本={st['total']}")


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "full":
        probe_full()
    elif arg == "effort":
        probe_effort()
    elif arg == "pool":
        # python probe_llm.py pool [heavy|balanced|light]，默认 heavy
        w = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] in (
            "heavy", "balanced", "light") else "heavy"
        probe_pool(w)
    else:
        probe_minimal()
