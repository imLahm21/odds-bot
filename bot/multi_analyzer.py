"""全模型交叉会诊。"""

from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from . import analyzer, config, llm_client

log = logging.getLogger("odds_bot.multi_analyzer")

_ERR_PREFIXES = getattr(analyzer, "_LLM_ERR_PREFIXES", (
    "LLM 请求失败", "LLM 超时", "LLM 网络错误", "LLM 返回无 choices",
    "LLM 返回空内容", "LLM 输出 token 耗尽", "LLM_API_KEY",
))

_RULE_FILES = {
    "market": [
        "rules/方法论/reference_asian_handicap.md",
        "rules/方法论/reference_dynamic_analysis.md",
    ],
    "risk": [
        "rules/风控验证/reference_kelly_index.md",
        "rules/风控验证/reference_staking_kelly.md",
    ],
    "goals": ["rules/方法论/reference_over_under.md"],
    "lesson": ["rules/实战教训/reference_case_lessons.md"],
}


def _read_rules(kind: str) -> str:
    rels = _RULE_FILES.get(kind, [])
    if not rels:
        return ""
    text, missing = analyzer._read_rule_files(rels)
    if missing:
        text += "\n\n[规则文件缺失：%s]\n" % "、".join(missing)
    return text


def _json_instruction(module_id: str, role: str,
                      extra_instruction: str) -> str:
    focus = extra_instruction.strip() or "无额外侧重。"
    return (
        "你是全模型交叉会诊中的【%s】。模块ID=%s。\n"
        "只分析本模块职责，不生成完整赛前报告，不替其他模块下结论。\n"
        "所有事实必须来自输入；缺失就写无数据，不得编造。\n"
        "输出必须是合法 JSON，不要 Markdown、代码围栏或 JSON 外说明，"
        "结构必须包含 module、summary、findings、direction、missing_data、"
        "risks、disagreements 字段。findings 中的 evidence.quote 必须是输入"
        "原文片段，source 只能是 csv、fundamentals、goals 或 rules。\n"
        "direction 只能填 home、away、draw、over、under、neutral、"
        "not_applicable。\n"
        "用户自定义侧重：%s\n"
        "自定义侧重只能改变关注重点，不能违反项目SOP或证据层权限。"
    ) % (role, module_id, focus)


def _review_module_input(task: dict, bundle: dict) -> str:
    return (
        f"## 比赛：{bundle.get('home','')} vs {bundle.get('away','')}\n"
        f"## 联赛：{bundle.get('league','')}\n\n"
        f"### 全程盘口CSV\n{bundle.get('csv_text','')}\n\n"
        f"### 第一阶段盲推预判\n{bundle.get('forecast','')}\n\n"
        f"### 基本面（盲推时不可见，仅供事后归因）\n"
        f"{bundle.get('fundamentals','')}\n\n"
        f"### 进球状态\n{bundle.get('goals_block','')}\n\n"
        f"### 实际结果（现在才揭晓）\n{bundle.get('result_text','')}\n"
    )


def _review_module_system(task: dict, bundle: dict) -> str:
    module_id = task["id"]
    if module_id in ("fundamentals", "fundamentals_review"):
        rules = analyzer.load_fund_rules(
            league_name=bundle.get("league", ""),
            has_h2h=any(k in bundle.get("fundamentals", "")
                        for k in ("交锋", "H2H", "h2h")),
            has_form=any(k in bundle.get("fundamentals", "")
                         for k in ("近10场", "近 10 场", "近况", "战绩")),
        )
    elif module_id in ("market_primary", "market_challenge"):
        rules = _read_rules("market")
    elif module_id == "risk_review":
        rules = _read_rules("risk")
    elif module_id in ("numeric_summary", "cross_market"):
        rules = _read_rules("goals")
    elif module_id == "lesson_match":
        rules = _read_rules("lesson")
    else:
        rules = "审计盲推输入、实际结果和数据缺失，不得重写赛前事实。"
    task_rule = (
        "\n\n===== 全模型对照复盘模块 =====\n"
        "这是已结束比赛的第二阶段复盘。第一阶段盲推当时不知道比分和基本面；"
        "现在才揭晓结果。必须以盲推原文为基准判断哪里对、哪里错，禁止利用结果"
        "倒推并改写盲推。A层模块分析盘口信号有效性；B层只分析盲推未见的基本面"
        "是否能解释偏差；C层复核结算、概率与风险；D层只提炼教训。\n"
    )
    return rules + task_rule + _json_instruction(
        module_id, task["label"], bundle.get("extra_instruction", ""))


def _module_input(task: dict, bundle: dict) -> str:
    if bundle.get("review_mode"):
        return _review_module_input(task, bundle)
    csv_text = bundle.get("csv_text", "")
    fundamentals = bundle.get("fundamentals", "")
    goals = bundle.get("goals_block", "")
    audit = bundle.get("data_audit", "")
    module_id = task["id"]
    if module_id == "data_audit":
        body = f"### 盘口CSV\n{csv_text}\n"
    elif module_id in ("fundamentals", "fundamentals_review"):
        body = f"### 基本面原始数据\n{fundamentals}\n"
    elif module_id in ("market_primary", "market_challenge",
                       "numeric_summary", "risk_review"):
        body = f"### 盘口CSV\n{csv_text}\n### 进球状态\n{goals}\n"
    elif module_id == "cross_market":
        body = (f"### 盘口CSV\n{csv_text}\n### 进球状态\n{goals}\n"
                f"### 基本面\n{fundamentals}\n")
    else:
        body = (f"### 盘口CSV\n{csv_text}\n### 基本面\n{fundamentals}\n"
                f"### 进球状态\n{goals}\n### 数据审计\n{audit}\n")
    return (
        f"## 比赛：{bundle.get('home','')} vs {bundle.get('away','')}\n"
        f"## 联赛：{bundle.get('league','')}\n\n{body}"
    )


def _module_system(task: dict, bundle: dict) -> str:
    if bundle.get("review_mode"):
        return _review_module_system(task, bundle)
    module_id = task["id"]
    role = task["label"]
    if module_id in ("fundamentals", "fundamentals_review"):
        rules = analyzer.load_fund_rules(
            league_name=bundle.get("league", ""),
            has_h2h=any(k in bundle.get("fundamentals", "")
                        for k in ("交锋", "H2H", "h2h")),
            has_form=any(k in bundle.get("fundamentals", "")
                         for k in ("近10场", "近 10 场", "近况", "战绩")),
        )
    elif module_id in ("market_primary", "market_challenge"):
        rules = _read_rules("market")
    elif module_id == "risk_review":
        rules = _read_rules("risk")
    elif module_id in ("numeric_summary", "cross_market"):
        rules = _read_rules("goals")
    elif module_id == "lesson_match":
        rules = _read_rules("lesson")
    else:
        rules = ("数据审计只负责节点完整度、时间分类、字段缺失和报价同步性，"
                 "不得预测赛果。")
    return rules + "\n\n===== 模块任务 =====\n" + _json_instruction(
        module_id, role, bundle.get("extra_instruction", ""))


def _parse_json(raw: str) -> tuple[dict | None, str]:
    text = (raw or "").strip()
    if not text:
        return None, "模型返回空内容"
    if text.startswith("\x60\x60\x60"):
        text = re.sub(r"^\x60\x60\x60(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*\x60\x60\x60$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        text = text[start:end + 1]
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"JSON解析失败：{exc}"
    if not isinstance(value, dict):
        return None, "JSON顶层不是对象"
    return value, ""


def _run_one(task: dict, bundle: dict) -> dict:
    started = time.monotonic()
    model = task["model"]
    try:
        raw = llm_client.chat_model(
            _module_system(task, bundle), _module_input(task, bundle),
            model=model, effort=task["effort"],
            timeout=config.FULL_CONSULT_TIMEOUT,
            max_tokens=task["max_tokens"],
        )
        elapsed = int((time.monotonic() - started) * 1000)
        if not raw or raw.startswith(_ERR_PREFIXES):
            return {
                "id": task["id"], "label": task["label"], "model": model,
                "effort": task["effort"], "status": "error",
                "error": (raw or "模型无返回")[:500], "elapsed_ms": elapsed,
            }
        parsed, error = _parse_json(raw)
        if parsed is None:
            return {
                "id": task["id"], "label": task["label"], "model": model,
                "effort": task["effort"], "status": "error",
                "error": error, "raw_preview": raw[:500],
                "elapsed_ms": elapsed,
            }
        parsed.setdefault("module", task["id"])
        parsed.setdefault("summary", "")
        parsed.setdefault("findings", [])
        parsed.setdefault("direction", "not_applicable")
        parsed.setdefault("missing_data", [])
        parsed.setdefault("risks", [])
        parsed.setdefault("disagreements", [])
        return {
            "id": task["id"], "label": task["label"], "model": model,
            "effort": task["effort"], "status": "ok", "card": parsed,
            "elapsed_ms": elapsed,
        }
    except Exception as exc:  # noqa: BLE001
        log.exception("全模型模块失败 module=%s model=%s", task["id"], model)
        return {
            "id": task["id"], "label": task["label"], "model": model,
            "effort": task["effort"], "status": "error",
            "error": str(exc)[:500],
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }


def _audit_input(bundle: dict, report: str, cards: list[dict]) -> str:
    compact = json.dumps(cards, ensure_ascii=False, indent=2)
    return (
        f"## 比赛：{bundle.get('home','')} vs {bundle.get('away','')}\n"
        f"## 联赛：{bundle.get('league','')}\n"
        "### 现有报告\n" + report + "\n"
        "### 专家卡片摘要\n" + compact[:30000] + "\n"
        "### 用户自定义侧重\n" + (bundle.get("extra_instruction") or "无") + "\n"
    )


def _basic_report_issues(report: str) -> list[str]:
    issues = []
    for number in range(1, 9):
        if not re.search(rf"(?m)^###\s*{number}\s*[.、]", report or ""):
            issues.append(f"缺少 ### {number} 主段")
    if report and "### 8. 投注决策" not in report:
        issues.append("缺少 ### 8. 投注决策")
    if len(report or "") < 500:
        issues.append("报告正文过短")
    return issues


def _audit_report(bundle: dict, report: str, cards: list[dict]) -> dict:
    deterministic = _basic_report_issues(report)
    system = (
        "你是最终报告审计员。只输出合法JSON，不要Markdown。\n"
        "检查报告是否遵守项目SOP：A层定方向，B层只有条件覆盖才能改方向，"
        "C/D层只能影响置信度；检查第1到第8节、数据缺失、edge、Kelly、"
        "比分和用户自定义侧重。不要重写报告。\n"
        '{"ok":true,"issues":[{"section":"7","problem":"...",'
        '"required_fix":"..."}]}'
    )
    raw = llm_client.chat_model(
        system, _audit_input(bundle, report, cards),
        model=config.FULL_CONSULT_AUDIT["model"],
        effort=config.FULL_CONSULT_AUDIT["effort"],
        timeout=config.FULL_CONSULT_TIMEOUT,
        max_tokens=config.FULL_CONSULT_AUDIT["max_tokens"],
    )
    parsed, error = _parse_json(raw)
    if parsed is None:
        return {"ok": False, "issues": deterministic + [error]}
    issues = deterministic + list(parsed.get("issues") or [])
    return {"ok": not issues and bool(parsed.get("ok", True)), "issues": issues}


def _synthesis_prompt(bundle: dict, cards: list[dict]) -> tuple[str, str, dict]:
    metrics: dict[str, int] = {}
    system, user = analyzer._analyze_prompts(
        bundle["csv_text"], bundle["fundamentals"], bundle["home"],
        bundle["away"], bundle["league"], bundle.get("extra_instruction", ""),
        metrics,
    )
    card_text = json.dumps(cards, ensure_ascii=False, indent=2)
    user += (
        "\n### 全模型专家卡片（失败模块也必须保留其状态）\n"
        + card_text
        + "\n\n===== 会诊合并要求 =====\n"
        "请基于同一份原始数据完成最终报告。专家卡片是证据辅助，不是投票结果。\n"
        "A层盘口定性和操盘手法决定方向；B层仅在SOP明文条件覆盖时改方向；"
        "C/D层不得改方向，只能扣置信度、列次选和风险。\n"
        "必须保留现有输出格式的 ### 1 到 ### 8，必须包含 ### 8. 投注决策。\n"
    )
    return system, user, metrics


def _synthesize(bundle: dict, cards: list[dict], cancel=None,
                progress: Callable[[str], None] | None = None) -> str:
    if cancel is not None and cancel.is_set():
        return ""
    system, user, metrics = _synthesis_prompt(bundle, cards)
    accumulated = ""
    for event in llm_client.stream_chat_model(
            system, user,
            model=config.FULL_CONSULT_SYNTHESIS["model"],
            effort=config.FULL_CONSULT_SYNTHESIS["effort"],
            max_tokens=config.FULL_CONSULT_SYNTHESIS["max_tokens"],
            fallback_models=("gpt-5.6-sol",),
            input_metrics=metrics):
        if cancel is not None and cancel.is_set():
            return ""
        kind, payload = event[0], event[1]
        if kind == "delta":
            accumulated = payload
            if progress:
                progress("synthesis")
        elif kind == "done":
            accumulated = payload
        elif kind == "warning" and progress:
            progress("warning:" + payload)
        elif kind == "error":
            log.warning("全模型最终合并失败：%s", payload)
            return ""
    return accumulated.strip()


def _render_appendix(results: list[dict], audit: dict) -> str:
    lines = ["### 模型会诊记录"]
    for result in results:
        status = "✅" if result.get("status") == "ok" else "❌"
        error = result.get("error", "")
        tail = f"；{error[:120]}" if error else ""
        lines.append(
            f"- {status} {result.get('label')}："
            f"{result.get('model')} / {result.get('effort')} / "
            f"{result.get('elapsed_ms', 0)}ms{tail}"
        )
    if audit.get("ok"):
        lines.append("- ✅ Sol最终审计：通过")
    else:
        issues = audit.get("issues") or []
        lines.append(f"- ⚠️ Sol最终审计：{len(issues)}项问题，已标注或尝试修复")
    return "\n".join(lines)


def run(csv_text: str, fundamentals: str, home: str, away: str,
        league: str, goals_block: str = "", extra_instruction: str = "",
        cancel=None,
        progress: Callable[[str, dict | None], None] | None = None
        ) -> tuple[str, dict]:
    """执行全模型会诊，返回 (报告, 执行摘要)。"""
    bundle = {
        "csv_text": csv_text or "",
        "fundamentals": fundamentals or "",
        "home": home or "",
        "away": away or "",
        "league": league or "",
        "goals_block": goals_block or "",
        "extra_instruction": extra_instruction or "",
    }
    results: list[dict] = []
    executor = ThreadPoolExecutor(
        max_workers=config.FULL_CONSULT_MAX_WORKERS,
        thread_name_prefix="full-consult")
    futures = [executor.submit(_run_one, task, bundle)
               for task in config.FULL_CONSULT_TASKS]
    try:
        for future in as_completed(futures):
            if cancel is not None and cancel.is_set():
                break
            result = future.result()
            results.append(result)
            if progress:
                progress("module", result)
    finally:
        if cancel is not None and cancel.is_set():
            executor.shutdown(wait=False, cancel_futures=True)
        else:
            executor.shutdown(wait=True)
    if cancel is not None and cancel.is_set():
        return "", {"cancelled": True, "results": results}

    order = {task["id"]: i for i, task in enumerate(config.FULL_CONSULT_TASKS)}
    results.sort(key=lambda item: order.get(item.get("id"), 999))
    cards = [
        {
            "id": item["id"], "label": item["label"], "model": item["model"],
            "effort": item["effort"], "status": item["status"],
            "card": item.get("card"), "error": item.get("error"),
        }
        for item in results
    ]
    report = _synthesize(
        bundle, cards, cancel=cancel,
        progress=(lambda kind: progress(kind, None) if progress else None),
    )
    if not report:
        return "", {"results": results, "error": "Astra最终合并失败"}

    audit = _audit_report(bundle, report, cards)
    if not audit["ok"] and not (cancel is not None and cancel.is_set()):
        issues = json.dumps(audit["issues"], ensure_ascii=False)
        repair_system = (
            "你是报告修复器。只修复给定问题，保留原报告事实和方向权限，"
            "输出完整修复后的 ### 1 到 ### 8 报告，不要解释修复过程。"
        )
        repaired = llm_client.chat_model(
            repair_system,
            "### 原报告\n" + report + "\n### 审计问题\n" + issues,
            model=config.FULL_CONSULT_SYNTHESIS["model"],
            effort=config.FULL_CONSULT_SYNTHESIS["effort"],
            timeout=config.FULL_CONSULT_TIMEOUT,
            max_tokens=config.FULL_CONSULT_SYNTHESIS["max_tokens"],
        )
        if repaired and not repaired.startswith(_ERR_PREFIXES):
            report = repaired.strip()
            audit = _audit_report(bundle, report, cards)
    report += "\n\n" + _render_appendix(results, audit)
    return report, {"results": results, "audit": audit}


def _basic_review_issues(report: str) -> list[str]:
    issues = []
    for number in range(1, 7):
        if not re.search(rf"(?m)^###\s*{number}\s*[.、]", report or ""):
            issues.append(f"缺少复盘 ### {number} 主段")
    if len(report or "") < 400:
        issues.append("复盘正文过短")
    return issues


def _review_audit(bundle: dict, report: str, cards: list[dict]) -> dict:
    deterministic = _basic_review_issues(report)
    system = (
        "你是全模型赛后复盘审计员。只输出合法JSON，不要Markdown。"
        "检查六段复盘是否完整，是否严格区分盲推阶段与赛果揭晓阶段，"
        "是否出现拿结果倒推或改写盲推，结算是否正确，教训是否来自本场证据。"
        '{"ok":true,"issues":[{"section":"4","problem":"...",'
        '"required_fix":"..."}]}'
    )
    audit_user = (
        _audit_input(bundle, report, cards)
        + "\n### 第一阶段盲推原文\n" + bundle.get("forecast", "")
        + "\n### 实际结果\n" + bundle.get("result_text", "")
    )
    raw = llm_client.chat_model(
        system,
        audit_user,
        model=config.FULL_CONSULT_AUDIT["model"],
        effort=config.FULL_CONSULT_AUDIT["effort"],
        timeout=config.FULL_CONSULT_TIMEOUT,
        max_tokens=config.FULL_CONSULT_AUDIT["max_tokens"],
    )
    parsed, error = _parse_json(raw)
    if parsed is None:
        return {"ok": False, "issues": deterministic + [error]}
    issues = deterministic + list(parsed.get("issues") or [])
    return {"ok": not issues and bool(parsed.get("ok", True)), "issues": issues}


def _review_synthesis_prompt(bundle: dict,
                             cards: list[dict]) -> tuple[str, str, dict]:
    metrics: dict[str, int] = {}
    system, user = analyzer._review_prompts(
        bundle["csv_text"], bundle["forecast"], bundle["result_text"],
        bundle["home"], bundle["away"], bundle["league"],
        bundle.get("fundamentals", ""), metrics,
    )
    focus = bundle.get("extra_instruction", "").strip()
    if focus:
        system += (
            "\n\n===== 用户复盘侧重 =====\n"
            "在不改变盲推事实、不泄漏赛果到第一阶段的前提下，重点回应：\n"
            + focus
        )
    user += (
        "\n### 全模型复盘专家卡片\n"
        + json.dumps(cards, ensure_ascii=False, indent=2)
        + "\n\n===== 会诊合并要求 =====\n"
        "专家卡片是分模块证据，不按多数投票。必须以第一阶段盲推原文为"
        "事前基准，严格输出现有复盘格式的 ### 1 到 ### 6。\n"
    )
    return system, user, metrics


def _review_synthesize(bundle: dict, cards: list[dict], cancel=None,
                       progress: Callable[[str], None] | None = None) -> str:
    system, user, metrics = _review_synthesis_prompt(bundle, cards)
    accumulated = ""
    for event in llm_client.stream_chat_model(
            system, user,
            model=config.FULL_CONSULT_SYNTHESIS["model"],
            effort=config.FULL_CONSULT_SYNTHESIS["effort"],
            max_tokens=config.FULL_CONSULT_SYNTHESIS["max_tokens"],
            fallback_models=("gpt-5.6-sol",),
            input_metrics=metrics):
        if cancel is not None and cancel.is_set():
            return ""
        kind, payload = event[0], event[1]
        if kind == "delta":
            accumulated = payload
            if progress:
                progress("review_synthesis")
        elif kind == "done":
            accumulated = payload
        elif kind == "warning" and progress:
            progress("warning:" + payload)
        elif kind == "error":
            log.warning("全模型复盘最终合并失败：%s", payload)
            return ""
    return accumulated.strip()


def run_review(csv_text: str, forecast: str, result_text: str,
               fundamentals: str, home: str, away: str, league: str,
               goals_block: str = "", extra_instruction: str = "",
               cancel=None,
               progress: Callable[[str, dict | None], None] | None = None
               ) -> tuple[str, dict]:
    """执行第二阶段全模型对照复盘，返回 (六段报告, 执行摘要)。"""
    bundle = {
        "review_mode": True,
        "csv_text": csv_text or "",
        "forecast": forecast or "",
        "result_text": result_text or "",
        "fundamentals": fundamentals or "",
        "home": home or "",
        "away": away or "",
        "league": league or "",
        "goals_block": goals_block or "",
        "extra_instruction": extra_instruction or "",
    }
    results: list[dict] = []
    executor = ThreadPoolExecutor(
        max_workers=config.FULL_CONSULT_MAX_WORKERS,
        thread_name_prefix="full-review")
    futures = [executor.submit(_run_one, task, bundle)
               for task in config.FULL_CONSULT_TASKS]
    try:
        for future in as_completed(futures):
            if cancel is not None and cancel.is_set():
                break
            result = future.result()
            results.append(result)
            if progress:
                progress("review_module", result)
    finally:
        if cancel is not None and cancel.is_set():
            executor.shutdown(wait=False, cancel_futures=True)
        else:
            executor.shutdown(wait=True)
    if cancel is not None and cancel.is_set():
        return "", {"cancelled": True, "results": results}

    order = {task["id"]: i for i, task in enumerate(config.FULL_CONSULT_TASKS)}
    results.sort(key=lambda item: order.get(item.get("id"), 999))
    cards = [
        {
            "id": item["id"], "label": item["label"], "model": item["model"],
            "effort": item["effort"], "status": item["status"],
            "card": item.get("card"), "error": item.get("error"),
        }
        for item in results
    ]
    report = _review_synthesize(
        bundle, cards, cancel=cancel,
        progress=(lambda kind: progress(kind, None) if progress else None),
    )
    if not report:
        return "", {"results": results, "error": "Astra复盘合并失败"}

    audit = _review_audit(bundle, report, cards)
    if not audit["ok"] and not (cancel is not None and cancel.is_set()):
        repair_system = (
            "你是复盘报告修复器。只修复审计指出的问题，保留第一阶段盲推原文"
            "与赛果揭晓边界，输出完整的 ### 1 到 ### 6，不解释修复过程。"
        )
        repaired = llm_client.chat_model(
            repair_system,
            "### 原复盘\n" + report + "\n### 审计问题\n"
            + json.dumps(audit["issues"], ensure_ascii=False),
            model=config.FULL_CONSULT_SYNTHESIS["model"],
            effort=config.FULL_CONSULT_SYNTHESIS["effort"],
            timeout=config.FULL_CONSULT_TIMEOUT,
            max_tokens=config.FULL_CONSULT_SYNTHESIS["max_tokens"],
        )
        if repaired and not repaired.startswith(_ERR_PREFIXES):
            report = repaired.strip()
            audit = _review_audit(bundle, report, cards)
    report += "\n\n" + _render_appendix(results, audit)
    return report, {"results": results, "audit": audit}
