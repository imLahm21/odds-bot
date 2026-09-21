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

_ERR_PREFIXES = tuple(getattr(analyzer, "_LLM_ERR_PREFIXES", ())) + (
    "未配置 LLM_ROUTE_ENDPOINTS",
)

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


def _route_candidates(spec: dict) -> list[dict]:
    """Return the primary and explicit per-model fallback route."""
    raw = [{"model": spec.get("model", ""),
            "effort": spec.get("effort", "")}]
    raw.extend(spec.get("fallbacks", ()))
    # Read the old shape while migrating local worktrees, but do not use it
    # in the committed configuration because it cannot carry per-model effort.
    if not spec.get("fallbacks"):
        raw.extend({"model": model, "effort": spec.get("effort", "")}
                   for model in spec.get("fallback_models", ()))
    candidates = []
    seen = set()
    for item in raw:
        if isinstance(item, str):
            item = {"model": item, "effort": spec.get("effort", "")}
        model = str(item.get("model", "")).strip()
        if not model or model in seen:
            continue
        seen.add(model)
        candidates.append({"model": model,
                           "effort": str(item.get("effort", "")).strip()})
    return candidates


def _route_problems(spec: dict) -> list[str]:
    """Validate grade, registration, group escape, and effort declarations."""
    candidates = _route_candidates(spec)
    if not candidates:
        return ["未指定模型"]
    problems = []
    primary = candidates[0]["model"]
    primary_grade = config.FULL_CONSULT_MODEL_GRADES.get(primary)
    if not config.llm_model_registered(primary):
        problems.append(f"主模型未登记：{primary}")
    if not primary_grade:
        problems.append(f"主模型未声明会诊等级：{primary}")
    primary_groups = set(config.llm_route_groups_for_model(primary))
    for item in candidates:
        model, effort = item["model"], item["effort"]
        if not config.llm_model_registered(model):
            problems.append(f"模型未登记：{model}")
        if config.FULL_CONSULT_MODEL_GRADES.get(model) != primary_grade:
            problems.append(
                f"模型等级不一致：{primary}({primary_grade}) -> "
                f"{model}({config.FULL_CONSULT_MODEL_GRADES.get(model)})")
        if not config.llm_model_supports_effort(model, effort):
            problems.append(f"{model} 不支持 effort={effort}")
    if len(candidates) > 1:
        first_groups = set(config.llm_route_groups_for_model(
            candidates[1]["model"]))
        if primary_groups and first_groups and primary_groups & first_groups:
            problems.append(
                f"第一回退未离开主授权组：{primary} -> "
                f"{candidates[1]['model']}")
    return problems


def validate_full_consult_routes() -> list[str]:
    """Return configuration errors before any live request is attempted."""
    specs = list(config.FULL_CONSULT_TASKS) + [
        config.FULL_CONSULT_SYNTHESIS, config.FULL_CONSULT_AUDIT,
    ]
    problems = []
    for spec in specs:
        name = spec.get("id") or spec.get("model", "route")
        problems.extend(f"{name}: {problem}"
                        for problem in _route_problems(spec))
    return problems


def _route_meta(spec: dict, candidate: dict, position: int,
                attempts: list[dict], *, error: str = "") -> dict:
    return {
        "requested_model": spec.get("model", ""),
        "model": candidate.get("model", spec.get("model", "")),
        "requested_effort": spec.get("effort", ""),
        "effort": candidate.get("effort", ""),
        "fallback_used": position > 0,
        "fallback_attempted": len(attempts) > 1 or position > 0,
        "attempts": attempts,
        "error": error,
    }


def _call_route(system: str, user: str, spec: dict, *, parse_json: bool,
                phase: str, input_metrics: dict | None = None
                ) -> tuple[object | None, dict]:
    """Call a route one model at a time so contract errors also fail over."""
    problems = _route_problems(spec)
    if problems:
        error = "会诊路由配置错误：" + "；".join(problems)
        log.error("全模型路由拒绝 phase=%s reason=%s", phase, error)
        candidates = _route_candidates(spec)
        meta = _route_meta(spec, candidates[0] if candidates else {}, 0, [],
                           error=error)
        return None, meta

    attempts = []
    candidates = _route_candidates(spec)
    for position, candidate in enumerate(candidates):
        model, effort = candidate["model"], candidate["effort"]
        try:
            raw = llm_client.chat_model(
                system, user, model=model, effort=effort,
                timeout=config.FULL_CONSULT_TIMEOUT,
                max_tokens=int(spec.get("max_tokens", 0) or 0),
                input_metrics=input_metrics,
            )
        except Exception as exc:  # noqa: BLE001
            error = str(exc)[:500]
            log.exception("全模型调用异常 phase=%s model=%s", phase, model)
        else:
            if not raw or str(raw).startswith(_ERR_PREFIXES):
                error = (raw or "模型无返回")[:500]
            elif parse_json:
                value, error = _parse_json(str(raw))
                if value is not None:
                    attempts.append({"model": model, "effort": effort,
                                     "status": "ok"})
                    meta = _route_meta(spec, candidate, position, attempts)
                    log.info(
                        "全模型调用成功 phase=%s requested=%s actual=%s "
                        "fallback=%s attempts=%d",
                        phase, spec.get("model"), model, position > 0,
                        len(attempts))
                    return value, meta
            else:
                value = str(raw).strip()
                if value:
                    attempts.append({"model": model, "effort": effort,
                                     "status": "ok"})
                    meta = _route_meta(spec, candidate, position, attempts)
                    log.info(
                        "全模型调用成功 phase=%s requested=%s actual=%s "
                        "fallback=%s attempts=%d",
                        phase, spec.get("model"), model, position > 0,
                        len(attempts))
                    return value, meta
                error = "模型返回空内容"
        attempts.append({"model": model, "effort": effort,
                         "status": "error", "error": str(error)[:500]})
        next_model = (candidates[position + 1]["model"]
                      if position + 1 < len(candidates) else "")
        log.warning(
            "全模型故障转移尝试失败 phase=%s requested=%s failed=%s "
            "reason=%s%s",
            phase, spec.get("model"), model, str(error)[:200],
            f" next={next_model}" if next_model else "",
        )
    error = "；".join(
        f"{item['model']}：{item.get('error', '失败')}"
        for item in attempts)[:1500]
    meta = _route_meta(spec, candidates[-1], len(candidates) - 1,
                       attempts, error=error)
    log.error("全模型故障转移耗尽 phase=%s requested=%s attempts=%d",
              phase, spec.get("model"), len(attempts))
    return None, meta


def _run_one(task: dict, bundle: dict) -> dict:
    started = time.monotonic()
    value, route = _call_route(
        _module_system(task, bundle), _module_input(task, bundle), task,
        parse_json=True, phase=f"module:{task['id']}",
    )
    if value is None:
        return {
            "id": task["id"], "label": task["label"], **route,
            "model": task["model"], "status": "error",
            "error": route.get("error", "模型链全部失败"),
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }
    parsed = value
    parsed.setdefault("module", task["id"])
    parsed.setdefault("summary", "")
    parsed.setdefault("findings", [])
    parsed.setdefault("direction", "not_applicable")
    parsed.setdefault("missing_data", [])
    parsed.setdefault("risks", [])
    parsed.setdefault("disagreements", [])
    return {
        "id": task["id"], "label": task["label"], **route,
        "status": "ok", "card": parsed,
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
    parsed, route = _call_route(
        system, _audit_input(bundle, report, cards),
        config.FULL_CONSULT_AUDIT, parse_json=True, phase="audit",
    )
    if parsed is None:
        return {"ok": False, "issues": deterministic + [
            route.get("error", "审计模型链全部失败")
        ], "route": route}
    issues = deterministic + list(parsed.get("issues") or [])
    return {"ok": not issues and bool(parsed.get("ok", True)),
            "issues": issues, "route": route}


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
    fallbacks = config.FULL_CONSULT_SYNTHESIS.get("fallbacks", ())
    accumulated = ""
    for event in llm_client.stream_chat_model(
            system, user,
            model=config.FULL_CONSULT_SYNTHESIS["model"],
            effort=config.FULL_CONSULT_SYNTHESIS["effort"],
            max_tokens=config.FULL_CONSULT_SYNTHESIS["max_tokens"],
            fallback_models=tuple(item["model"] for item in fallbacks),
            fallback_efforts=tuple(item["effort"] for item in fallbacks),
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
        requested = result.get("requested_model", result.get("model"))
        used = result.get("model")
        model_text = (
            f"{requested} → {used}（故障转移）"
            if result.get("fallback_used") else str(used)
        )
        lines.append(
            f"- {status} {result.get('label')}："
            f"{model_text} / {result.get('effort')} / "
            f"{result.get('elapsed_ms', 0)}ms{tail}"
        )
    if audit.get("ok"):
        route = audit.get("route") or {}
        lines.append(f"- ✅ {route.get('model', '审计模型')}最终审计：通过")
    else:
        issues = audit.get("issues") or []
        route = audit.get("route") or {}
        lines.append(
            f"- ⚠️ {route.get('model', '审计模型')}最终审计："
            f"{len(issues)}项问题，已标注或尝试修复")
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
            "requested_model": item.get("requested_model", item["model"]),
            "effort": item["effort"], "status": item["status"],
            "fallback_used": item.get("fallback_used", False),
            "attempts": item.get("attempts", []),
            "card": item.get("card"), "error": item.get("error"),
        }
        for item in results
    ]
    report = _synthesize(
        bundle, cards, cancel=cancel,
        progress=(lambda kind: progress(kind, None) if progress else None),
    )
    if not report:
        return "", {"results": results, "error": "最终合并模型链全部失败"}

    audit = _audit_report(bundle, report, cards)
    if not audit["ok"] and not (cancel is not None and cancel.is_set()):
        issues = json.dumps(audit["issues"], ensure_ascii=False)
        repair_system = (
            "你是报告修复器。只修复给定问题，保留原报告事实和方向权限，"
            "输出完整修复后的 ### 1 到 ### 8 报告，不要解释修复过程。"
        )
        repaired, repair_route = _call_route(
            repair_system,
            "### 原报告\n" + report + "\n### 审计问题\n" + issues,
            config.FULL_CONSULT_SYNTHESIS, parse_json=False,
            phase="repair",
        )
        if repaired:
            report = str(repaired).strip()
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
    parsed, route = _call_route(
        system, audit_user, config.FULL_CONSULT_AUDIT,
        parse_json=True, phase="review_audit",
    )
    if parsed is None:
        return {"ok": False, "issues": deterministic + [
            route.get("error", "复盘审计模型链全部失败")
        ], "route": route}
    issues = deterministic + list(parsed.get("issues") or [])
    return {"ok": not issues and bool(parsed.get("ok", True)),
            "issues": issues, "route": route}


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
    fallbacks = config.FULL_CONSULT_SYNTHESIS.get("fallbacks", ())
    accumulated = ""
    for event in llm_client.stream_chat_model(
            system, user,
            model=config.FULL_CONSULT_SYNTHESIS["model"],
            effort=config.FULL_CONSULT_SYNTHESIS["effort"],
            max_tokens=config.FULL_CONSULT_SYNTHESIS["max_tokens"],
            fallback_models=tuple(item["model"] for item in fallbacks),
            fallback_efforts=tuple(item["effort"] for item in fallbacks),
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
            "requested_model": item.get("requested_model", item["model"]),
            "effort": item["effort"], "status": item["status"],
            "fallback_used": item.get("fallback_used", False),
            "attempts": item.get("attempts", []),
            "card": item.get("card"), "error": item.get("error"),
        }
        for item in results
    ]
    report = _review_synthesize(
        bundle, cards, cancel=cancel,
        progress=(lambda kind: progress(kind, None) if progress else None),
    )
    if not report:
        return "", {"results": results, "error": "复盘合并模型链全部失败"}

    audit = _review_audit(bundle, report, cards)
    if not audit["ok"] and not (cancel is not None and cancel.is_set()):
        repair_system = (
            "你是复盘报告修复器。只修复审计指出的问题，保留第一阶段盲推原文"
            "与赛果揭晓边界，输出完整的 ### 1 到 ### 6，不解释修复过程。"
        )
        repaired, repair_route = _call_route(
            repair_system,
            "### 原复盘\n" + report + "\n### 审计问题\n"
            + json.dumps(audit["issues"], ensure_ascii=False),
            config.FULL_CONSULT_SYNTHESIS, parse_json=False,
            phase="review_repair",
        )
        if repaired:
            report = str(repaired).strip()
            audit = _review_audit(bundle, report, cards)
    report += "\n\n" + _render_appendix(results, audit)
    return report, {"results": results, "audit": audit}
