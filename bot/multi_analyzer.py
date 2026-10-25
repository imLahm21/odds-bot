"""全模型交叉精算。"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from . import analyzer, config, db, full_model_settings, llm_client

log = logging.getLogger("odds_bot.multi_analyzer")


def profile_snapshot(profile: str = "analyze") -> dict[str, dict]:
    """Take one job-local snapshot; menu edits only affect subsequent jobs."""
    fallback = full_model_settings.defaults(profile)
    fallback["_rescue"] = full_model_settings.rescue_defaults(profile)
    try:
        conn = db.get_conn()
        try:
            with conn:
                conn.execute("BEGIN")
                snapshot = db.get_full_model_profile(conn, profile)
                snapshot["_rescue"] = db.get_full_model_rescue(conn, profile)
            return snapshot
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.warning("读取全模型配置 %s 失败，使用代码默认：%s", profile, exc)
        return fallback

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
        "你是全模型交叉精算中的【%s】。模块ID=%s。\n"
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
    return full_model_settings.candidates(spec)


def _route_problems(spec: dict) -> list[str]:
    """Validate grade, registration, group escape, and effort declarations."""
    return full_model_settings.route_problems(spec)


def validate_full_consult_routes(routes: dict | None = None) -> list[str]:
    """Return configuration errors before any live request is attempted."""
    defaults = full_model_settings.defaults("analyze")
    if routes is None:
        routes = defaults
    problems = []
    for name in defaults:
        if name not in routes:
            problems.append(f"缺少职责配置：{name}")
            continue
        problems.extend(f"{name}: {problem}"
                        for problem in _route_problems(routes[name]))
    if "_rescue" in routes:
        try:
            full_model_settings.rescue_pair(routes["_rescue"])
        except ValueError as exc:
            problems.append(f"公共兜底：{exc}")
    return problems


def _route_meta(spec: dict, candidate: dict, position: int,
                attempts: list[dict], *, error: str = "", rescue_configured: bool = False) -> dict:
    return {
        "requested_model": spec.get("model", ""),
        "model": candidate.get("model", spec.get("model", "")),
        "requested_effort": spec.get("effort", ""),
        "effort": candidate.get("effort", ""),
        "fallback_used": position > 0,
        "fallback_attempted": sum(item.get("status") != "skipped" for item in attempts) > 1 or position > 0,
        "attempts": attempts,
        "error": error,
        "route_group": candidate.get("route_group", ""),
        "rescue_configured": rescue_configured,
        "rescue_used": bool(candidate.get("rescue")) and not error,
        "rescue_attempted": any(item.get("rescue") and item.get("status") != "skipped"
                                for item in attempts),
        "rescue_skipped": any(item.get("rescue") and item.get("status") == "skipped"
                              for item in attempts),
    }


def _call_route(system: str, user: str, spec: dict, *, parse_json: bool,
                phase: str, input_metrics: dict | None = None,
                excluded_models=(), rescue: dict | None = None, cancel=None
                ) -> tuple[object | None, dict]:
    """Call a route one model at a time so contract errors also fail over."""
    problems = _route_problems(spec)
    if problems:
        error = "精算路由配置错误：" + "；".join(problems)
        log.error("全模型路由拒绝 phase=%s reason=%s", phase, error)
        candidates = []
        meta = _route_meta(spec, candidates[0] if candidates else {}, 0, [],
                           error=error)
        return None, meta

    try:
        rescue = full_model_settings.rescue_pair(rescue) if rescue is not None else None
    except ValueError as exc:
        return None, _route_meta(spec, {}, 0, [], error=f"公共兜底配置错误：{exc}")
    rescue_configured = bool(rescue and rescue["model"])

    attempts = []
    excluded = {full_model_settings.model_identity(model) for model in excluded_models}
    candidates = []
    chain = _route_candidates(spec)
    if rescue_configured:
        chain.append({**rescue, "rescue": True})
    for position, candidate in enumerate(chain):
        if full_model_settings.model_identity(candidate["model"]) in excluded:
            attempts.append({**candidate, "status": "skipped", "error": "避免合并模型自审"})
        else:
            candidates.append((position, candidate))
    if not candidates:
        error = "无独立审计模型：配置中的模型均参与了本次合并或修复"
        return None, _route_meta(spec, {"model": "", "effort": ""}, 0, attempts,
                                error=error, rescue_configured=rescue_configured)
    for chain_index, (position, candidate) in enumerate(candidates):
        if cancel is not None and cancel.is_set():
            return None, _route_meta(spec, {"model": "", "effort": ""}, position, attempts,
                                    error="已取消", rescue_configured=rescue_configured)
        model, effort = candidate["model"], candidate["effort"]
        if candidate.get("rescue"):
            log.warning("全模型常规链耗尽，尝试公共兜底 phase=%s model=%s group=%s",
                        phase, model, candidate["route_group"])
        try:
            raw = llm_client.chat_model(
                system, user, model=model, effort=effort,
                timeout=config.FULL_CONSULT_TIMEOUT,
                max_tokens=int(spec.get("max_tokens", 0) or 0),
                input_metrics=input_metrics,
                **({"route_group": candidate["route_group"]} if candidate.get("route_group") else {}),
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
                    attempts.append({**candidate, "status": "ok"})
                    meta = _route_meta(spec, candidate, position, attempts, rescue_configured=rescue_configured)
                    log.info(
                        "全模型调用成功 phase=%s requested=%s actual=%s "
                        "fallback=%s attempts=%d",
                        phase, spec.get("model"), model, position > 0,
                        len(attempts))
                    return value, meta
            else:
                value = str(raw).strip()
                if value:
                    attempts.append({**candidate, "status": "ok"})
                    meta = _route_meta(spec, candidate, position, attempts, rescue_configured=rescue_configured)
                    log.info(
                        "全模型调用成功 phase=%s requested=%s actual=%s "
                        "fallback=%s attempts=%d",
                        phase, spec.get("model"), model, position > 0,
                        len(attempts))
                    return value, meta
                error = "模型返回空内容"
        attempts.append({**candidate, "status": "error", "error": str(error)[:500]})
        next_model = (candidates[chain_index + 1][1]["model"]
                      if chain_index + 1 < len(candidates) else "")
        log.warning(
            "全模型故障转移尝试失败 phase=%s requested=%s failed=%s "
            "reason=%s%s",
            phase, spec.get("model"), model, str(error)[:200],
            f" next={next_model}" if next_model else "",
        )
    error = "；".join(
        f"{item['model']}：{item.get('error', '失败')}"
        for item in attempts)[:1500]
    meta = _route_meta(spec, candidates[-1][1], candidates[-1][0],
                       attempts, error=error, rescue_configured=rescue_configured)
    log.error("全模型故障转移耗尽 phase=%s requested=%s attempts=%d",
              phase, spec.get("model"), len(attempts))
    return None, meta


def _run_one(task: dict, bundle: dict) -> dict:
    started = time.monotonic()
    value, route = _call_route(
        _module_system(task, bundle), _module_input(task, bundle), task,
        parse_json=True, phase=f"module:{task['id']}",
        rescue=bundle.get("rescue"), cancel=bundle.get("cancel"),
    )
    if value is None:
        return {
            "id": task["id"], "label": task["label"], **route,
            "status": "error",
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


def _audit_report(bundle: dict, report: str, cards: list[dict], *,
                  spec: dict | None = None, excluded_models=(), rescue: dict | None = None) -> dict:
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
        spec if spec is not None else config.FULL_CONSULT_AUDIT,
        parse_json=True, phase="audit", excluded_models=excluded_models,
        rescue=rescue, cancel=bundle.get("cancel"),
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
        + "\n\n===== 精算合并要求 =====\n"
        "请基于同一份原始数据完成最终报告。专家卡片是证据辅助，不是投票结果。\n"
        "A层盘口定性和操盘手法决定方向；B层仅在SOP明文条件覆盖时改方向；"
        "C/D层不得改方向，只能扣置信度、列次选和风险。\n"
        "必须保留现有输出格式的 ### 1 到 ### 8，必须包含 ### 8. 投注决策。\n"
    )
    return system, user, metrics


def _stream_report(system: str, user: str, metrics: dict, spec: dict, *,
                    rescue: dict | None, trace: dict | None, cancel, progress, stage: str) -> str:
    """Try one rescue round only after a pre-content normal-chain failure."""
    trace = trace if trace is not None else {}
    problems = _route_problems(spec)
    if problems:
        trace["error"] = "精算路由配置错误：" + "；".join(problems)
        log.warning("全模型流式路由拒绝：%s", trace["error"])
        return ""
    rescue = full_model_settings.rescue_pair(rescue) if rescue is not None else None
    plans = [(spec, False)]
    if rescue and rescue["model"]:
        plans.append(({**rescue, "max_tokens": 0, "fallbacks": ()}, True))
    for current, is_rescue in plans:
        if cancel is not None and cancel.is_set():
            return ""
        if is_rescue:
            trace.update(model=current["model"], effort=current["effort"],
                         route_group=current["route_group"], rescue_attempted=True, rescue_used=False)
            log.warning("全模型合并常规链耗尽，尝试公共兜底 phase=%s model=%s group=%s",
                        stage, current["model"], current["route_group"])
            if progress:
                progress(stage + "_rescue")
        fallbacks = _route_candidates(current)[1:]

        def model_used(model, effort):
            trace.update(model=model, effort=effort)
            if is_rescue:
                trace["route_group"] = current["route_group"]

        accumulated = ""
        produced = False
        failed = False
        for kind, payload in llm_client.stream_chat_model(
                system, user, model=current["model"], effort=current["effort"],
                max_tokens=current["max_tokens"],
                fallback_models=tuple(item["model"] for item in fallbacks),
                fallback_efforts=tuple(item["effort"] for item in fallbacks),
                input_metrics=metrics, on_model_used=model_used,
                **({"route_group": current["route_group"]} if current.get("route_group") else {})):
            if cancel is not None and cancel.is_set():
                return ""
            if kind == "delta":
                accumulated = payload
                produced = produced or bool(payload)
                if progress:
                    progress(stage)
            elif kind == "done":
                if is_rescue and payload.strip():
                    trace["rescue_used"] = True
                return payload.strip()
            elif kind == "warning" and progress:
                progress("warning:" + payload)
            elif kind == "error":
                log.warning("全模型合并失败 phase=%s rescue=%s：%s", stage, is_rescue, payload)
                if produced:
                    trace["partial_output"] = True
                    if not is_rescue:
                        trace["rescue_skipped_reason"] = "partial_output"
                    return ""
                failed = True
                break
        if not failed:
            if is_rescue and accumulated.strip():
                trace["rescue_used"] = True
            return accumulated.strip()
        trace["rescue_configured"] = bool(rescue and rescue["model"])
    return ""


def _synthesize(bundle: dict, cards: list[dict], cancel=None,
                progress: Callable[[str], None] | None = None, *,
                spec: dict | None = None, trace: dict | None = None,
                rescue: dict | None = None) -> str:
    if cancel is not None and cancel.is_set():
        return ""
    system, user, metrics = _synthesis_prompt(bundle, cards)
    return _stream_report(system, user, metrics,
        spec if spec is not None else config.FULL_CONSULT_SYNTHESIS,
        rescue=rescue, trace=trace, cancel=cancel, progress=progress, stage="synthesis")


def execution_note(result: dict) -> str:
    """Explain the role result, not just the originally requested model."""
    if result.get("status") == "ok":
        if result.get("rescue_used"):
            return "公共兜底成功 · " + result.get("route_group", "")
        return "回退成功" if result.get("fallback_used") else "主模型成功"
    if not result.get("attempts"):
        return "未执行或配置校验失败"
    if result.get("rescue_attempted"):
        return "常规链已耗尽，公共兜底也失败" + (
            " · " + result["route_group"] if result.get("route_group") else "")
    if result.get("rescue_skipped"):
        return "常规链已耗尽，公共兜底因防自审跳过"
    return "常规链已耗尽，未启用公共兜底"


def _render_appendix(results: list[dict], audit: dict, synthesis: dict | None = None) -> str:
    lines = ["### 全模型执行记录"]
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
        if result.get("status") != "ok":
            model_text = f"初始 {requested}；最后尝试 {used or '未执行'}"
        lines.append(
            f"- {status} {result.get('label')}："
            f"{model_text} / {result.get('effort')} / "
            f"{result.get('elapsed_ms', 0)}ms；{execution_note(result)}{tail}"
        )
    if synthesis:
        source = f"（公共兜底 · {synthesis.get('route_group', '')}）" if synthesis.get("rescue_used") else ""
        lines.append(f"- ✅ 最终合并：{synthesis['model']} / {synthesis['effort'] or '模型默认'}{source}")
    if audit.get("ok"):
        route = audit.get("route") or {}
        source = f"（公共兜底 · {route.get('route_group', '')}）" if route.get("rescue_used") else ""
        lines.append(f"- ✅ {route.get('model') or '独立审计模型'}最终审计：通过{source}")
    else:
        issues = audit.get("issues") or []
        route = audit.get("route") or {}
        rescue_note = (f"（公共兜底 · {route.get('route_group', '')}）"
                       if route.get("rescue_attempted") else "")
        lines.append(
            f"- ⚠️ {route.get('model') or '独立审计模型'}最终审计{rescue_note}："
            f"{len(issues)}项问题，已标注或尝试修复；{str(issues[:1])[:180]}")
    return "\n".join(lines)


def run(csv_text: str, fundamentals: str, home: str, away: str,
        league: str, goals_block: str = "", extra_instruction: str = "",
        cancel=None,
        progress: Callable[[str, dict | None], None] | None = None, *,
        profile: str = "analyze", settings: dict | None = None
        ) -> tuple[str, dict]:
    """执行全模型精算，返回 (报告, 执行摘要)。"""
    if profile not in full_model_settings.PROFILES:
        return "", {"results": [], "error": "未知全模型配置"}
    if cancel is not None and cancel.is_set():
        return "", {"cancelled": True, "results": []}
    routes = deepcopy(settings if settings is not None else profile_snapshot(profile))
    problems = validate_full_consult_routes(routes)
    if problems:
        return "", {"results": [], "error": "全模型配置错误：" + "；".join(problems)}
    tasks = [routes[task["id"]] for task in config.FULL_CONSULT_TASKS]
    rescue = full_model_settings.rescue_pair(
        routes.get("_rescue", full_model_settings.rescue_defaults(profile)))
    bundle = {
        "rescue": rescue, "cancel": cancel,
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
               for task in tasks]
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

    order = {task["id"]: i for i, task in enumerate(tasks)}
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
    synthesis = {"model": routes["synthesis"]["model"], "effort": routes["synthesis"]["effort"]}
    report = _synthesize(
        bundle, cards, cancel=cancel,
        progress=(lambda kind: progress(kind, None) if progress else None),
        spec=routes["synthesis"], trace=synthesis,
        rescue=rescue,
    )
    if not report:
        return "", {"results": results, "synthesis": synthesis,
                    "error": "最终合并常规链与公共兜底未能生成完整报告"}

    writers = {synthesis["model"]}
    audit = _audit_report(bundle, report, cards, spec=routes["audit"], excluded_models=writers, rescue=rescue)
    if not audit["ok"] and not (cancel is not None and cancel.is_set()):
        issues = json.dumps(audit["issues"], ensure_ascii=False)
        repair_system = (
            "你是报告修复器。只修复给定问题，保留原报告事实和方向权限，"
            "输出完整修复后的 ### 1 到 ### 8 报告，不要解释修复过程。"
        )
        repaired, repair_route = _call_route(
            repair_system,
            "### 原报告\n" + report + "\n### 审计问题\n" + issues,
            routes["synthesis"], parse_json=False,
            phase="repair",
            rescue=rescue, cancel=cancel,
        )
        if repaired:
            report = str(repaired).strip()
            if repair_route.get("model"):
                writers.add(repair_route["model"])
                synthesis.update(model=repair_route["model"], effort=repair_route.get("effort", ""))
                synthesis.update({key: repair_route.get(key, False)
                                  for key in ("rescue_used", "rescue_attempted", "rescue_configured")},
                                 route_group=repair_route.get("route_group", ""))
            audit = _audit_report(bundle, report, cards, spec=routes["audit"], excluded_models=writers, rescue=rescue)
    if cancel is not None and cancel.is_set():
        return "", {"cancelled": True, "results": results, "audit": audit}

    # Reuse the ordinary SOP's bounded decision layer only after the last LLM
    # repair. This also finalizes review's result-blind first-stage forecast;
    # run_review must never reselect a wager after seeing the actual result.
    report = analyzer.finalize_decision(report, home, away, league)
    if cancel is not None and cancel.is_set():
        return "", {"cancelled": True, "results": results, "audit": audit}
    report += "\n\n" + _render_appendix(results, audit, synthesis)
    return report, {"results": results, "audit": audit, "synthesis": synthesis,
                    "profile": profile, "writers": sorted(writers)}


def _basic_review_issues(report: str) -> list[str]:
    issues = []
    for number in range(1, 7):
        if not re.search(rf"(?m)^###\s*{number}\s*[.、]", report or ""):
            issues.append(f"缺少复盘 ### {number} 主段")
    if len(report or "") < 400:
        issues.append("复盘正文过短")
    return issues


def _review_audit(bundle: dict, report: str, cards: list[dict], *,
                  spec: dict | None = None, excluded_models=(), rescue: dict | None = None) -> dict:
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
        system, audit_user, spec if spec is not None else config.FULL_CONSULT_AUDIT,
        parse_json=True, phase="review_audit", excluded_models=excluded_models,
        rescue=rescue, cancel=bundle.get("cancel"),
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
        + "\n\n===== 精算合并要求 =====\n"
        "专家卡片是分模块证据，不按多数投票。必须以第一阶段盲推原文为"
        "事前基准，严格输出现有复盘格式的 ### 1 到 ### 6。\n"
    )
    return system, user, metrics


def _review_synthesize(bundle: dict, cards: list[dict], cancel=None,
                       progress: Callable[[str], None] | None = None, *,
                       spec: dict | None = None, trace: dict | None = None,
                       rescue: dict | None = None) -> str:
    system, user, metrics = _review_synthesis_prompt(bundle, cards)
    return _stream_report(system, user, metrics,
        spec if spec is not None else config.FULL_CONSULT_SYNTHESIS,
        rescue=rescue, trace=trace, cancel=cancel, progress=progress, stage="review_synthesis")


def run_review(csv_text: str, forecast: str, result_text: str,
               fundamentals: str, home: str, away: str, league: str,
               goals_block: str = "", extra_instruction: str = "",
               cancel=None,
               progress: Callable[[str, dict | None], None] | None = None, *,
               settings: dict | None = None
               ) -> tuple[str, dict]:
    """执行第二阶段全模型对照复盘，返回 (六段报告, 执行摘要)。

    第一阶段盲推已在 run 中完成有界决策；揭晓结果后不再重选或改写盲推。
    """
    if cancel is not None and cancel.is_set():
        return "", {"cancelled": True, "results": []}
    routes = deepcopy(settings if settings is not None else profile_snapshot("review"))
    problems = validate_full_consult_routes(routes)
    if problems:
        return "", {"results": [], "error": "全模型复盘配置错误：" + "；".join(problems)}
    tasks = [routes[task["id"]] for task in config.FULL_CONSULT_TASKS]
    rescue = full_model_settings.rescue_pair(
        routes.get("_rescue", full_model_settings.rescue_defaults("review")))
    bundle = {
        "rescue": rescue, "cancel": cancel,
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
               for task in tasks]
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

    order = {task["id"]: i for i, task in enumerate(tasks)}
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
    synthesis = {"model": routes["synthesis"]["model"], "effort": routes["synthesis"]["effort"]}
    report = _review_synthesize(
        bundle, cards, cancel=cancel,
        progress=(lambda kind: progress(kind, None) if progress else None),
        spec=routes["synthesis"], trace=synthesis,
        rescue=rescue,
    )
    if not report:
        return "", {"results": results, "synthesis": synthesis,
                    "error": "复盘合并常规链与公共兜底未能生成完整报告"}

    writers = {synthesis["model"]}
    audit = _review_audit(bundle, report, cards, spec=routes["audit"], excluded_models=writers, rescue=rescue)
    if not audit["ok"] and not (cancel is not None and cancel.is_set()):
        repair_system = (
            "你是复盘报告修复器。只修复审计指出的问题，保留第一阶段盲推原文"
            "与赛果揭晓边界，输出完整的 ### 1 到 ### 6，不解释修复过程。"
        )
        repaired, repair_route = _call_route(
            repair_system,
            "### 原复盘\n" + report + "\n### 审计问题\n"
            + json.dumps(audit["issues"], ensure_ascii=False),
            routes["synthesis"], parse_json=False,
            phase="review_repair",
            rescue=rescue, cancel=cancel,
        )
        if repaired:
            report = str(repaired).strip()
            if repair_route.get("model"):
                writers.add(repair_route["model"])
                synthesis.update(model=repair_route["model"], effort=repair_route.get("effort", ""))
                synthesis.update({key: repair_route.get(key, False)
                                  for key in ("rescue_used", "rescue_attempted", "rescue_configured")},
                                 route_group=repair_route.get("route_group", ""))
            audit = _review_audit(bundle, report, cards, spec=routes["audit"], excluded_models=writers, rescue=rescue)
    if cancel is not None and cancel.is_set():
        return "", {"cancelled": True, "results": results, "audit": audit}
    report += "\n\n" + _render_appendix(results, audit, synthesis)
    return report, {"results": results, "audit": audit, "synthesis": synthesis,
                    "profile": "review", "writers": sorted(writers)}
