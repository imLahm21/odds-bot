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

from . import (analyzer, candidates, config, db, focus, full_model_settings,
               hypothesis_settle, llm_client, typesafe_decisions)

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
        config.HYPOTHESIS_REGISTRY,
    ],
    "risk": [
        "rules/风控验证/reference_kelly_index.md",
        "rules/风控验证/reference_staking_kelly.md",
    ],
    "goals": ["rules/方法论/reference_over_under.md"],
    "lesson": ["rules/实战教训/reference_case_lessons.md",
               config.HYPOTHESIS_REGISTRY],
}


def _read_rules(kind: str) -> str:
    rels = _RULE_FILES.get(kind, [])
    if not rels:
        return ""
    text, missing = analyzer._read_rule_files(rels)
    if missing:
        text += "\n\n[规则文件缺失：%s]\n" % "、".join(missing)
    return text


def _focus_enabled(bundle: dict) -> bool:
    """本次运行是否把 extra_instruction 当作用户侧重解析。
    复盘盲推传入的是系统写的盲推说明（不是用户侧重），不走侧重任务段与第 9 节。"""
    return bool(bundle.get("focus_enabled", True))


def _json_instruction(module_id: str, role: str,
                      extra_instruction: str, *, review: bool = False) -> str:
    # 复盘模块的侧重保持原文（本次不改复盘侧重逻辑）；精算模块用解析后的侧重标签
    if review:
        focus_text = extra_instruction.strip() or "无额外侧重。"
    else:
        focus_text = focus.label(focus.parse(extra_instruction)) or "无额外侧重。"
    return (
        "你是全模型交叉精算中的【%s】。模块ID=%s。\n"
        "只分析本模块职责，不生成完整赛前报告，不替其他模块下结论。\n"
        "所有事实必须来自输入；缺失就写无数据，不得编造。\n"
        "输出必须是合法 JSON，不要 Markdown、代码围栏或 JSON 外说明，"
        "结构必须包含 module、summary、findings、direction、direction_basis、"
        "missing_data、risks、disagreements 字段。findings 中的 evidence.quote 必须是输入"
        "原文片段，source 只能是 csv、fundamentals、goals 或 rules。\n"
        "direction 只能填 home、away、draw、over、under、neutral、"
        "not_applicable。\n"
        "用户自定义侧重：%s\n"
        "自定义侧重只能改变关注重点，不能违反项目SOP或证据层权限。\n"
    ) % (role, module_id, focus_text) + _FOUR_LAYER_RULE


_FOUR_LAYER_RULE = (
    "【证据四层权限】市场价格是基准，默认无倾向。direction 只有两种情况可以填方向：\n"
    "① 本模块命中了假设登记表中状态为「已验证」的假设；② 基本面明文覆盖条件成立\n"
    "（碾压级五要素 ≥3 项 / 极端动力差异 ≥3 项）。其余一律填 neutral（或 not_applicable）。\n"
    "盘路形态、凯利、返还率、「假设」「观察中」状态的条目只写进 findings，不得据此填方向。\n"
    "若输入里有【市场事实】【已触发假设】【代码候选表】，相关数字以其为准，不要自己重算。\n"
    "必须额外输出 direction_basis 字段：verified_hypothesis / fundamental_override / none。"
)


def _market_block(bundle: dict) -> str:
    """run / run_review 已一次算好放进 bundle；直接调 _run_one 的旧调用方没有时现算并缓存。"""
    if "market_block" not in bundle:
        bundle["market_block"] = analyzer.market_hypotheses_block(
            bundle.get("csv_text", ""), bundle.get("league", ""))
    return bundle["market_block"]


def _review_module_input(task: dict, bundle: dict) -> str:
    return (
        f"## 比赛：{bundle.get('home','')} vs {bundle.get('away','')}\n"
        f"## 联赛：{bundle.get('league','')}\n\n"
        f"### 全程盘口CSV\n{bundle.get('csv_text','')}\n\n"
        + _market_block(bundle)
        + f"### 第一阶段盲推预判\n{bundle.get('forecast','')}\n\n"
        f"### 基本面（盲推时不可见，仅供事后归因）\n"
        f"{bundle.get('fundamentals','')}\n\n"
        f"### 进球状态\n{bundle.get('goals_block','')}\n\n"
        f"### 实际结果（现在才揭晓）\n{bundle.get('result_text','')}\n"
        + (f"\n{bundle['settlement_block']}\n" if bundle.get("settlement_block") else "")
    )


def _review_module_system(task: dict, bundle: dict) -> str:
    module_id = task["id"]
    if module_id in ("fundamentals", "fundamentals_review"):
        has_h2h, has_form = analyzer.fund_has(bundle.get("fundamentals", ""))
        rules = analyzer.load_fund_rules(
            league_name=bundle.get("league", ""),
            has_h2h=has_h2h, has_form=has_form,
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
        "倒推并改写盲推。按证据四层分工：市场层模块核对 p_市场、热门方与变盘事实；"
        "假设层模块逐条检验已触发假设（符合 / 不符合 / 无方向），只记样本、"
        "不因单场结果改变假设状态；基本面层模块只分析盲推未见的基本面是否能解释偏差；"
        "风险与数字模块复核结算、概率与注额；教训模块只把新发现登记为待验证假设。"
        "盲推写「无倾向」不算判错。\n"
    )
    return rules + task_rule + _json_instruction(
        module_id, task["label"], bundle.get("extra_instruction", ""), review=True)


def _module_input(task: dict, bundle: dict) -> str:
    if bundle.get("review_mode"):
        return _review_module_input(task, bundle)
    csv_text = bundle.get("csv_text", "")
    fundamentals = bundle.get("fundamentals", "")
    goals = bundle.get("goals_block", "")
    market = bundle.get("market_block", "")
    cands = bundle.get("candidates_block", "")
    module_id = task["id"]
    if module_id == "data_audit":
        body = f"### 盘口CSV\n{csv_text}\n"
    elif module_id in ("fundamentals", "fundamentals_review"):
        body = f"### 基本面原始数据\n{fundamentals}\n"
    elif module_id in ("market_primary", "market_challenge"):
        body = f"### 盘口CSV\n{csv_text}\n### 进球状态\n{goals}\n{market}"
    elif module_id in ("numeric_summary", "risk_review"):
        body = (f"### 盘口CSV\n{csv_text}\n### 进球状态\n{goals}\n"
                f"{market}{cands}")
    elif module_id == "cross_market":
        body = (f"### 盘口CSV\n{csv_text}\n### 进球状态\n{goals}\n"
                f"### 基本面\n{fundamentals}\n{cands}")
    else:  # lesson_match
        body = (f"### 盘口CSV\n{csv_text}\n### 基本面\n{fundamentals}\n"
                f"### 进球状态\n{goals}\n{market}")
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
        has_h2h, has_form = analyzer.fund_has(bundle.get("fundamentals", ""))
        rules = analyzer.load_fund_rules(
            league_name=bundle.get("league", ""),
            has_h2h=has_h2h, has_form=has_form,
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
        module_id, role, bundle.get("extra_instruction", ""),
        review=not _focus_enabled(bundle))


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
                              and item.get("error") != _DEADLINE_SKIP
                              for item in attempts),
        "deadline_skipped": any(item.get("error") == _DEADLINE_SKIP for item in attempts),
    }


_DEADLINE_SKIP = "超过职责总时限"


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
    started = time.monotonic()
    last = candidates[-1]
    for chain_index, (position, candidate) in enumerate(candidates):
        if cancel is not None and cancel.is_set():
            return None, _route_meta(spec, {"model": "", "effort": ""}, position, attempts,
                                    error="已取消", rescue_configured=rescue_configured)
        if chain_index > 0 and time.monotonic() - started > config.FULL_CONSULT_ROLE_DEADLINE:
            # 单个职责的模型链总时限：主模型 + 回退 + 公共兜底逐个超时最坏会拖一小时
            for _, rest in candidates[chain_index:]:
                attempts.append({**rest, "status": "skipped", "error": _DEADLINE_SKIP})
            last = candidates[chain_index - 1]
            log.warning("全模型职责超过总时限 phase=%s requested=%s elapsed=%.0fs，跳过剩余 %d 个模型",
                        phase, spec.get("model"), time.monotonic() - started,
                        len(candidates) - chain_index)
            break
        model, effort = candidate["model"], candidate["effort"]
        if candidate.get("rescue"):
            log.warning("全模型常规链耗尽，尝试公共兜底 phase=%s model=%s group=%s",
                        phase, model, candidate["route_group"])
        try:
            raw = llm_client.collect_model(
                system, user, model=model, effort=effort,
                max_tokens=int(spec.get("max_tokens", 0) or 0),
                input_metrics=input_metrics, cancel=cancel,
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
    meta = _route_meta(spec, last[1], last[0],
                       attempts, error=error, rescue_configured=rescue_configured)
    log.error("全模型故障转移耗尽 phase=%s requested=%s attempts=%d",
              phase, spec.get("model"), len(attempts))
    return None, meta


_NO_DIRECTION = ("neutral", "not_applicable")
_FUNDAMENTAL_MODULES = ("fundamentals", "fundamentals_review")


def _enforce_direction_permission(module_id: str, card: dict) -> None:
    """证据四层的方向权限（V1）：无权限的方向由代码降级为 neutral，原方向记在 direction_downgraded。"""
    direction = str(card.get("direction") or "not_applicable").strip()
    if direction in _NO_DIRECTION:
        return
    basis = str(card.get("direction_basis") or "none").strip()
    if basis == "verified_hypothesis":
        allowed = typesafe_decisions.verified_hypothesis_count() > 0
    elif basis == "fundamental_override":
        allowed = module_id in _FUNDAMENTAL_MODULES
    else:
        allowed = False
    if not allowed:
        card["direction_downgraded"] = direction
        card["direction"] = "neutral"


def _quote_sources(bundle: dict) -> dict[str, str]:
    """evidence.source → 可核对的原文。代码由 CSV 算出的市场事实 / 候选表也算 csv 来源。"""
    csv_text = "".join((bundle.get("csv_text", ""), bundle.get("market_block", ""),
                        bundle.get("candidates_block", "")))
    # 复盘模块的输入另有盲推原文、实际结果与假设结算，但 source 只有四个可选值：
    # 这些段落的引文无论标成哪个来源都算在输入里。
    extra = "".join((bundle.get("forecast", ""), bundle.get("result_text", ""),
                     bundle.get("settlement_block", ""))) if bundle.get("review_mode") else ""
    return {"csv": csv_text + extra, "fundamentals": bundle.get("fundamentals", "") + extra,
            "goals": bundle.get("goals_block", "") + extra}


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def _verify_quotes(card: dict, bundle: dict) -> None:
    """引文核对（V4）：evidence.quote 去空白后须是对应原文的子串；不匹配的标 verified=false，
    卡片记 unverified_quotes。只做标记，不判模块失败。rules 来源与空引文不核对。"""
    sources = _quote_sources(bundle)
    squashed: dict[str, str] = {}
    everything = None
    unverified = 0
    for finding in card.get("findings") or []:
        if not isinstance(finding, dict):
            continue
        evidence = finding.get("evidence")
        items = evidence if isinstance(evidence, list) else [evidence]
        for item in items:
            if not isinstance(item, dict):
                continue
            source = str(item.get("source") or "").strip().lower()
            quote = _squash(item.get("quote"))
            if source == "rules" or not quote:
                continue
            if source in sources:
                if source not in squashed:
                    squashed[source] = _squash(sources[source])
                haystack = squashed[source]
            else:  # 未知来源：在全部输入里找
                if everything is None:
                    everything = _squash("".join(sources.values()))
                haystack = everything
            if quote not in haystack:
                item["verified"] = False
                unverified += 1
    if unverified:
        card["unverified_quotes"] = unverified


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
    parsed.setdefault("direction_basis", "none")
    _enforce_direction_permission(task["id"], parsed)
    _verify_quotes(parsed, bundle)
    return {
        "id": task["id"], "label": task["label"], **route,
        "status": "ok", "card": parsed,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }


def _compact_cards(cards: list[dict]) -> str:
    """合并与审计共用的卡片摘要（V6）：去掉 attempts 明细、失败卡片只留错误前 200 字、
    单张卡片超过 FULL_CONSULT_CARD_MAX_CHARS 时从 findings 末尾逐条删。
    整体永远是合法 JSON，不做字符串切片。"""
    limit = int(config.FULL_CONSULT_CARD_MAX_CHARS)
    out = []
    for raw in cards:
        item = deepcopy(raw)
        attempts = item.pop("attempts", None)
        item["attempts_count"] = len(attempts or [])
        if item.get("status") != "ok" and item.get("error"):
            item["error"] = str(item["error"])[:200]
        body = item.get("card")
        findings = body.get("findings") if isinstance(body, dict) else None
        if isinstance(findings, list):
            dropped = 0
            while findings and len(json.dumps(item, ensure_ascii=False, indent=2)) > limit:
                findings.pop()
                dropped += 1
                item["findings_truncated"] = dropped   # 计入长度：标记本身也占字符
        out.append(item)
    return json.dumps(out, ensure_ascii=False, indent=2)


def _audit_input(bundle: dict, report: str, cards: list[dict]) -> str:
    return (
        f"## 比赛：{bundle.get('home','')} vs {bundle.get('away','')}\n"
        f"## 联赛：{bundle.get('league','')}\n"
        "### 现有报告\n" + report + "\n"
        "### 专家卡片摘要\n" + _compact_cards(cards) + "\n"
        "### 用户自定义侧重\n" + (bundle.get("extra_instruction") or "无") + "\n"
        "### 代码计算的市场事实与已触发假设\n" + (bundle.get("market_block") or "无（数据不足）") + "\n"
        + ("" if bundle.get("review_mode") else  # 复盘不重选投注，没有候选表可核
           "### 代码候选表\n" + (bundle.get("candidates_block") or "无（数据不足）") + "\n")
    )


def _candidate_mismatches(report: str, bundle: dict) -> list[str]:
    """第 8 节候选表与代码候选表逐行比对：赔率差 >0.0051（两位小数的舍入误差以内不算）、edge 差 >0.2pp、缺行。
    模型在表末追加的行（主/客队大小球）不比。代码表或报告表无法得到时返回空列表。"""
    try:
        table = candidates.build_table(bundle.get("csv_text", ""), bundle.get("league", ""),
                                       bundle.get("home", ""), bundle.get("away", ""))
    except Exception as exc:  # noqa: BLE001
        log.warning("候选表一致性检查跳过：代码候选表计算失败（%s）", type(exc).__name__)
        return []
    if table is None or not table.rows:
        return []
    pool = typesafe_decisions._selection_pool(report)
    if pool is None:
        return []  # 无法解析另有一条阻断项
    rows = pool[0]
    out = []
    for index, code_row in enumerate(table.rows):
        if index >= len(rows):
            out.append(f"候选表缺 {len(table.rows) - len(rows)} 行")
            break
        row, cid = rows[index], f"C{index + 1:02d}"
        # 报告里赔率只写两位小数，而报价可能是三位（如 1.925）：容差留出舍入与浮点误差
        if row.odds is None or abs(row.odds - code_row.odds) > 0.0051:
            shown = "无" if row.odds is None else f"{row.odds:.2f}"
            out.append(f"{cid} 赔率 {shown}≠{code_row.odds:.2f}")
        if (row.edge is not None and code_row.edge is not None
                and abs(row.edge - code_row.edge) > 0.002):
            out.append(f"{cid} edge {row.edge * 100:+.1f}%≠{code_row.edge * 100:+.1f}%")
    return out


def _basic_report_issues(report: str, focus_required: bool = False,
                         bundle: dict | None = None) -> list[str]:
    issues = []
    for number in range(1, 9):
        if not re.search(rf"(?m)^###\s*{number}\s*[.、]", report or ""):
            issues.append(f"缺少 ### {number} 主段")
    if report and "### 8. 投注决策" not in report:
        issues.append("缺少 ### 8. 投注决策")
    if focus_required and not focus.has_section_9(report):
        issues.append("缺少 ### 9. 侧重专题")
    if len(report or "") < 500:
        issues.append("报告正文过短")
    signal = typesafe_decisions.parse_signal_strength(report or "")
    if signal is None:
        issues.append("第 7 节缺「信号强度」行")
    if typesafe_decisions.parse_data_quality(report or "") is None:
        issues.append("第 7 节缺「数据质量」行")
    if typesafe_decisions._selection_pool(report or "") is None:
        issues.append("第 8 节候选表无法解析")
    if signal in ("medium", "strong") and typesafe_decisions.verified_hypothesis_count() == 0:
        issues.append("信号强度越权：登记表没有「已验证」假设，信号强度不得为中/强")
    if bundle:
        mismatches = _candidate_mismatches(report or "", bundle)
        if mismatches:
            issues.append("第 8 节候选表与代码候选表不一致：" + "；".join(mismatches[:5]))
    return issues


def _focus_required(bundle: dict) -> bool:
    return (_focus_enabled(bundle)
            and not focus.parse(bundle.get("extra_instruction", "")).empty)


def _blocker(problem: str) -> dict:
    return {"section": "", "severity": "blocker", "problem": problem}


def _split_severity(deterministic: list[str], model_issues) -> dict:
    """确定性问题一律 blocker；模型给的 issue 只有 severity=="blocker" 才阻断，
    没有 severity 字段的旧式返回（含纯字符串）按 minor 处理。"""
    blockers = [_blocker(text) for text in deterministic]
    minors = []
    for issue in model_issues or []:
        if isinstance(issue, dict) and str(issue.get("severity", "")).strip().lower() == "blocker":
            blockers.append(issue)
        else:
            minors.append(issue)
    return {"issues": blockers + minors, "blockers": blockers, "minors": minors}


_SEVERITY_RULE = (
    "severity 只能是 blocker 或 minor。blocker 仅限：%s。"
    "措辞、详略、可读性问题一律 minor。拿不准时标 minor。\n"
)


def _audit_report(bundle: dict, report: str, cards: list[dict], *,
                  spec: dict | None = None, excluded_models=(), rescue: dict | None = None) -> dict:
    deterministic = _basic_report_issues(
        report, focus_required=_focus_required(bundle), bundle=bundle)
    system = (
        "你是最终报告审计员。只输出合法JSON，不要Markdown。\n"
        "检查报告是否遵守项目SOP的证据四层：市场层只给 p_市场 基准、不定方向；"
        "方向只来自「已验证」假设或基本面明文覆盖条件，两者都没有时各判定必须写「无倾向」；"
        "「假设」「观察中」只记录命中；数据质量只影响注额。"
        "检查第1到第8节、第7节的信号强度与数据质量两行、数据缺失、edge、Kelly、"
        "档位（价值注/探索注/空仓）、比分，以及有侧重时第 9 节是否逐条回应。"
        "第 8 节数字以输入里的「代码候选表」为准核对。不要重写报告。\n"
        '{"ok":true,"issues":[{"section":"7","severity":"blocker","problem":"...",'
        '"required_fix":"..."}]}\n'
        + _SEVERITY_RULE % (
            "缺少必需章节；无已验证假设且无基本面明文覆盖却给出方向；"
            "第 7 节各判定互相矛盾（如判小球却预测 3:2）；第 8 节数字与代码候选表不一致；"
            "有侧重但第 9 节未回应")
    )
    parsed, route = _call_route(
        system, _audit_input(bundle, report, cards),
        spec if spec is not None else config.FULL_CONSULT_AUDIT,
        parse_json=True, phase="audit", excluded_models=excluded_models,
        rescue=rescue, cancel=bundle.get("cancel"),
    )
    return _audit_result(deterministic, parsed, route, "审计模型链全部失败")


def _audit_result(deterministic: list[str], parsed: dict | None, route: dict,
                  failure: str) -> dict:
    """ok = 没有 blocker。审计模型链失败时 ok=False 并记 audit_error，但模型失败本身
    不是报告问题，不进 blockers（不会为它触发修复）。"""
    if parsed is None:
        split = _split_severity(deterministic, [])
        error = route.get("error", failure)
        return {"ok": False, **split, "issues": split["issues"] + [error],
                "audit_error": error, "route": route}
    split = _split_severity(deterministic, parsed.get("issues"))
    return {"ok": not split["blockers"], **split, "route": route}


def _audit_blockers(audit: dict) -> list:
    """修复只处理 blocker；没有 blockers 字段的旧式结果（测试替身）退回 issues。"""
    blockers = audit.get("blockers")
    return list(audit.get("issues") or []) if blockers is None else list(blockers)


def _accept_repair(original: str, repaired: str, bundle: dict) -> bool:
    """修复稿验收（V9）：长度不低于原稿的 FULL_CONSULT_REPAIR_MIN_RATIO、确定性问题不增加、
    第 8 节候选表可解析——全部满足才替换原报告。"""
    if not repaired or len(repaired) < config.FULL_CONSULT_REPAIR_MIN_RATIO * len(original or ""):
        return False
    focus_required = _focus_required(bundle)
    if (len(_basic_report_issues(repaired, focus_required, bundle=bundle))
            > len(_basic_report_issues(original, focus_required, bundle=bundle))):
        return False
    return typesafe_decisions._selection_pool(repaired) is not None


def _synthesis_prompt(bundle: dict, cards: list[dict]) -> tuple[str, str, dict]:
    metrics: dict[str, int] = {}
    instruction = bundle.get("extra_instruction", "")
    focus_on = _focus_enabled(bundle)
    system, user = analyzer._analyze_prompts(
        bundle["csv_text"], bundle["fundamentals"], bundle["home"],
        bundle["away"], bundle["league"], instruction if focus_on else "",
        metrics,
    )
    if not focus_on and instruction.strip():
        # 复盘盲推说明：是系统约束而非用户侧重，按原方式接在任务说明之后
        system += (
            "\n\n===== 本次运行说明 =====\n"
            "在不违背上述 SOP 步骤与「输出格式」章节结构（必须保留 ### 1~8 各段标题）"
            "的前提下，遵守以下要求：\n" + instruction.strip()
        )
    user += (
        "\n### 全模型专家卡片（失败模块也必须保留其状态）\n"
        + _compact_cards(cards)
        + "\n\n===== 精算合并要求 =====\n"
        "请基于同一份原始数据完成最终报告。专家卡片是证据辅助，不是投票结果。\n"
        "按证据四层定结论：市场层只给 p_市场 基准；方向只来自「已验证」假设或"
        "基本面明文覆盖条件，都没有就写「无倾向」；专家卡片里未验证的盘路判断"
        "只作为假设命中记录，不得改方向；数据质量只调注额。\n"
        "卡片里 direction_downgraded 表示该方向因无权限已被代码降级；"
        "unverified_quotes > 0 的卡片，其对应 findings 不得作为关键依据。\n"
        "必须保留现有输出格式的 ### 1 到 ### 8，必须包含 ### 8. 投注决策。\n"
    )
    # 侧重任务段 _analyze_prompts 已加在数据之后，但专家卡片与合并要求又接在其后；
    # 再追加一次，保证它仍是整个输入的最后一段。
    focus_obj = focus.parse(instruction)
    if focus_on and not focus_obj.empty:
        user += "\n" + analyzer.focus_task_block(focus_obj)
    return system, user, metrics


def _stream_report(system: str, user: str, metrics: dict, spec: dict, *,
                    rescue: dict | None, trace: dict | None, cancel, progress, stage: str) -> str:
    """合并（V12、V13）：逐个候选流式调用——职责的主模型、各回退，最后公共兜底。

    任何候选出错（不论是否已有部分正文）都换下一个：全模型的合并正文不实时展示给
    用户，可以安全重来。整条链受 FULL_CONSULT_ROLE_DEADLINE 约束。"""
    trace = trace if trace is not None else {}
    problems = _route_problems(spec)
    if problems:
        trace["error"] = "精算路由配置错误：" + "；".join(problems)
        log.warning("全模型流式路由拒绝：%s", trace["error"])
        return ""
    rescue = full_model_settings.rescue_pair(rescue) if rescue is not None else None
    max_tokens = int(spec.get("max_tokens", 0) or 0)
    plans = [({**item, "max_tokens": max_tokens}, False) for item in _route_candidates(spec)]
    if rescue and rescue["model"]:
        plans.append(({**rescue, "max_tokens": 0}, True))
    started = time.monotonic()
    for index, (current, is_rescue) in enumerate(plans):
        if cancel is not None and cancel.is_set():
            return ""
        if index > 0 and time.monotonic() - started > config.FULL_CONSULT_ROLE_DEADLINE:
            trace["deadline_skipped"] = True
            log.warning("全模型合并超过职责总时限 phase=%s elapsed=%.0fs，跳过剩余 %d 个模型",
                        stage, time.monotonic() - started, len(plans) - index)
            break
        if is_rescue:
            trace.update(model=current["model"], effort=current["effort"],
                         route_group=current["route_group"], rescue_attempted=True, rescue_used=False)
            log.warning("全模型合并常规链耗尽，尝试公共兜底 phase=%s model=%s group=%s",
                        stage, current["model"], current["route_group"])
            if progress:
                progress(stage + "_rescue")

        def model_used(model, effort, _current=current, _rescue=is_rescue):
            trace.update(model=model, effort=effort)
            if _rescue:
                trace["route_group"] = _current["route_group"]

        accumulated = ""
        produced = False
        for kind, payload in llm_client.stream_chat_model(
                system, user, model=current["model"], effort=current["effort"],
                max_tokens=current["max_tokens"],
                fallback_models=(),
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
                accumulated = payload or accumulated
                break
            elif kind == "warning" and progress:
                progress("warning:" + payload)
            elif kind == "error":
                log.warning("全模型合并失败 phase=%s model=%s rescue=%s：%s",
                            stage, current["model"], is_rescue, payload)
                if produced:
                    trace["partial_output"] = True
                accumulated = ""
                break
        if accumulated.strip():
            if is_rescue:
                trace["rescue_used"] = True
            return accumulated.strip()
        if index + 1 < len(plans) and not plans[index + 1][1]:
            log.warning("全模型合并换下一个候选 phase=%s failed=%s next=%s",
                        stage, current["model"], plans[index + 1][0]["model"])
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
    if result.get("deadline_skipped"):
        return "超过职责总时限，剩余模型未尝试"
    if result.get("rescue_attempted"):
        return "常规链已耗尽，公共兜底也失败" + (
            " · " + result["route_group"] if result.get("route_group") else "")
    if result.get("rescue_skipped"):
        return "常规链已耗尽，公共兜底因防自审跳过"
    return "常规链已耗尽，未启用公共兜底"


def _issue_text(issue) -> str:
    if isinstance(issue, dict):
        section = str(issue.get("section") or "").strip()
        text = str(issue.get("problem") or issue.get("required_fix") or issue)
        return f"§{section} {text}" if section else text
    return str(issue)


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
    route = audit.get("route") or {}
    auditor = route.get("model") or "独立审计模型"
    minors = audit.get("minors") or []
    if audit.get("ok"):
        source = f"（公共兜底 · {route.get('route_group', '')}）" if route.get("rescue_used") else ""
        verdict = f"通过（minor {len(minors)} 项）" if minors else "通过"
        lines.append(f"- ✅ {auditor}最终审计：{verdict}{source}")
    else:
        blockers = _audit_blockers(audit)
        rescue_note = (f"（公共兜底 · {route.get('route_group', '')}）"
                       if route.get("rescue_attempted") else "")
        if audit.get("audit_error") and not blockers:
            head = f"审计未完成（{str(audit['audit_error'])[:160]}）"
        else:
            head = f"未通过：{len(blockers)} 项阻断问题"
        lines.append(f"- ⚠️ {auditor}最终审计{rescue_note}：{head}")
        lines.extend(f"  - {_issue_text(issue)[:160]}" for issue in blockers[:5])
        if minors:
            lines.append(f"  - minor {len(minors)} 项")
        if audit.get("repair_rejected"):
            lines.append("  - 修复稿未通过验收，保留原稿")
    return "\n".join(lines)


def _expert_cards(results: list[dict]) -> list[dict]:
    return [
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


def _run_modules(tasks: list[dict], bundle: dict, cancel, progress, kind: str,
                 prefix: str) -> list[dict]:
    results: list[dict] = []
    executor = ThreadPoolExecutor(
        max_workers=config.FULL_CONSULT_MAX_WORKERS, thread_name_prefix=prefix)
    futures = [executor.submit(_run_one, task, bundle) for task in tasks]
    try:
        for future in as_completed(futures):
            if cancel is not None and cancel.is_set():
                break
            result = future.result()
            results.append(result)
            if progress:
                progress(kind, result)
    finally:
        if cancel is not None and cancel.is_set():
            executor.shutdown(wait=False, cancel_futures=True)
        else:
            executor.shutdown(wait=True)
    order = {task["id"]: i for i, task in enumerate(tasks)}
    results.sort(key=lambda item: order.get(item.get("id"), 999))
    return results


def _too_few_modules(results: list[dict]) -> str:
    """V5：成功模块少于 FULL_CONSULT_MIN_OK_MODULES 时不进入合并，返回错误说明。"""
    ok = sum(1 for item in results if item.get("status") == "ok")
    if ok >= config.FULL_CONSULT_MIN_OK_MODULES:
        return ""
    return (f"专家模块成功 {ok}/{len(results)}，低于最低要求 "
            f"{config.FULL_CONSULT_MIN_OK_MODULES}；未进入合并，可改用预设精算")


def _adopt_repair_route(repair_route: dict, writers: set, synthesis: dict) -> None:
    if repair_route.get("model"):
        writers.add(repair_route["model"])
        synthesis.update(model=repair_route["model"], effort=repair_route.get("effort", ""))
        synthesis.update({key: repair_route.get(key, False)
                          for key in ("rescue_used", "rescue_attempted", "rescue_configured")},
                         route_group=repair_route.get("route_group", ""))


def run(csv_text: str, fundamentals: str, home: str, away: str,
        league: str, goals_block: str = "", extra_instruction: str = "",
        cancel=None,
        progress: Callable[[str, dict | None], None] | None = None, *,
        profile: str = "analyze", settings: dict | None = None,
        apply_focus_modes: bool = True, count_explore: bool = True,
        ) -> tuple[str, dict]:
    """执行全模型精算，返回 (报告, 执行摘要)。

    全模型入口只有自由文字：侧重模式由 focus.parse 的关键词检测得到。
    apply_focus_modes=False 时定稿不使用侧重模式（复盘盲推用）。
    count_explore=False 时定稿不查、不占当日探索注额度（复盘盲推用）。"""
    focus_obj = focus.parse(extra_instruction)
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
        "focus_enabled": apply_focus_modes,
        "csv_text": csv_text or "",
        "fundamentals": fundamentals or "",
        "home": home or "",
        "away": away or "",
        "league": league or "",
        "goals_block": goals_block or "",
        "extra_instruction": extra_instruction or "",
        # CSV 的纯函数：一次算好，模块、审计、修复共用（V2）
        "market_block": analyzer.market_hypotheses_block(csv_text or "", league or ""),
        "candidates_block": analyzer.candidates_block(
            csv_text or "", league or "", home or "", away or ""),
    }
    results = _run_modules(tasks, bundle, cancel, progress, "module", "full-consult")
    if cancel is not None and cancel.is_set():
        return "", {"cancelled": True, "results": results}
    too_few = _too_few_modules(results)
    if too_few:
        return "", {"results": results, "error": too_few}

    cards = _expert_cards(results)
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
    if progress:
        progress("audit", None)
    audit = _audit_report(bundle, report, cards, spec=routes["audit"], excluded_models=writers, rescue=rescue)
    blockers = _audit_blockers(audit)
    if not audit["ok"] and blockers and not (cancel is not None and cancel.is_set()):
        if progress:
            progress("repair", None)
        repair_system = (
            "你是报告修复器。只修复给定问题，保留原报告事实和方向权限，"
            "输出完整修复后的 ### 1 到 ### 8 报告，不要解释修复过程。"
            "第 8 节候选表的行与数字必须与「代码候选表」逐字一致；只改被指出的问题，其余原样保留。"
        )
        if apply_focus_modes and not focus_obj.empty:
            repair_system += "本场有用户侧重：须保留或补齐 ### 9. 侧重专题（放在第 8 节之后）。"
        repaired, repair_route = _call_route(
            repair_system,
            "### 原报告\n" + report
            + "\n### 审计问题\n" + json.dumps(blockers, ensure_ascii=False)
            + "\n### 代码计算的市场事实与已触发假设\n" + (bundle["market_block"] or "无（数据不足）")
            + "\n### 代码候选表\n" + (bundle["candidates_block"] or "无（数据不足）"),
            routes["synthesis"], parse_json=False,
            phase="repair",
            rescue=rescue, cancel=cancel,
        )
        if repaired:
            repaired = str(repaired).strip()
            if _accept_repair(report, repaired, bundle):
                report = repaired
                _adopt_repair_route(repair_route, writers, synthesis)
                if progress:
                    progress("audit", None)
                audit = _audit_report(bundle, report, cards, spec=routes["audit"],
                                      excluded_models=writers, rescue=rescue)
            else:
                log.warning("全模型修复稿未通过验收，保留原稿 model=%s", repair_route.get("model"))
                audit["repair_rejected"] = True
    if cancel is not None and cancel.is_set():
        return "", {"cancelled": True, "results": results, "audit": audit}

    # Reuse the ordinary SOP's bounded decision layer only after the last LLM
    # repair. This also finalizes review's result-blind first-stage forecast;
    # run_review must never reselect a wager after seeing the actual result.
    # apply_focus_modes=False（复盘盲推）：侧重不改变盲推的定稿与注额，也不标注侧重。
    finalize_kwargs = {}
    if apply_focus_modes and focus_obj.modes:
        finalize_kwargs["modes"] = focus_obj.modes
    if not count_explore:
        finalize_kwargs["count_explore"] = False
    report = analyzer.finalize_decision(report, home, away, league, **finalize_kwargs)
    if apply_focus_modes:
        report = focus.stamp(report, focus_obj)
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
        "若输入里有「假设结算」表，第 4、5 节的假设结论须与之一致。\n"
        '{"ok":true,"issues":[{"section":"4","severity":"blocker","problem":"...",'
        '"required_fix":"..."}]}\n'
        + _SEVERITY_RULE % "缺少必需章节；改写了第一阶段盲推；结算算错"
    )
    audit_user = (
        _audit_input(bundle, report, cards)
        + "\n### 第一阶段盲推原文\n" + bundle.get("forecast", "")
        + "\n### 实际结果\n" + bundle.get("result_text", "")
        + (("\n" + bundle["settlement_block"]) if bundle.get("settlement_block") else "")
    )
    parsed, route = _call_route(
        system, audit_user, spec if spec is not None else config.FULL_CONSULT_AUDIT,
        parse_json=True, phase="review_audit", excluded_models=excluded_models,
        rescue=rescue, cancel=bundle.get("cancel"),
    )
    return _audit_result(deterministic, parsed, route, "复盘审计模型链全部失败")


def _accept_review_repair(original: str, repaired: str) -> bool:
    """复盘修复稿验收：长度比例同精算；六段确定性问题不增加（复盘没有第 8 节候选表）。"""
    if not repaired or len(repaired) < config.FULL_CONSULT_REPAIR_MIN_RATIO * len(original or ""):
        return False
    return len(_basic_review_issues(repaired)) <= len(_basic_review_issues(original))


def _review_synthesis_prompt(bundle: dict,
                             cards: list[dict]) -> tuple[str, str, dict]:
    metrics: dict[str, int] = {}
    system, user = analyzer._review_prompts(
        bundle["csv_text"], bundle["forecast"], bundle["result_text"],
        bundle["home"], bundle["away"], bundle["league"],
        bundle.get("fundamentals", ""), metrics,
        **({"score": bundle["score"]} if bundle.get("score") else {}),
    )
    review_focus = bundle.get("extra_instruction", "").strip()
    if review_focus:
        system += (
            "\n\n===== 用户复盘侧重 =====\n"
            "在不改变盲推事实、不泄漏赛果到第一阶段的前提下，重点回应：\n"
            + review_focus
        )
    user += (
        "\n### 全模型复盘专家卡片\n"
        + _compact_cards(cards)
        + "\n\n===== 精算合并要求 =====\n"
        "专家卡片是分模块证据，不按多数投票。必须以第一阶段盲推原文为"
        "事前基准，严格输出现有复盘格式的 ### 1 到 ### 6。\n"
        "卡片里 direction_downgraded 表示该方向因无权限已被代码降级；"
        "unverified_quotes > 0 的卡片，其对应 findings 不得作为关键依据。\n"
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
               settings: dict | None = None,
               score: tuple[int, int] | None = None,
               ) -> tuple[str, dict]:
    """执行第二阶段全模型对照复盘，返回 (六段报告, 执行摘要)。

    第一阶段盲推已在 run 中完成有界决策；揭晓结果后不再重选或改写盲推。
    score：全场比分 (主, 客)；给出时由代码结算已触发假设，注入模块、合并与审计。
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
        "market_block": analyzer.market_hypotheses_block(csv_text or "", league or ""),
        "score": score,
        "settlement_block": (hypothesis_settle.settlement_block(csv_text or "", league or "", score)
                             if score else ""),
    }
    results = _run_modules(tasks, bundle, cancel, progress, "review_module", "full-review")
    if cancel is not None and cancel.is_set():
        return "", {"cancelled": True, "results": results}
    too_few = _too_few_modules(results)
    if too_few:
        return "", {"results": results, "error": too_few}

    cards = _expert_cards(results)
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
    if progress:
        progress("review_audit", None)
    audit = _review_audit(bundle, report, cards, spec=routes["audit"], excluded_models=writers, rescue=rescue)
    blockers = _audit_blockers(audit)
    if not audit["ok"] and blockers and not (cancel is not None and cancel.is_set()):
        if progress:
            progress("review_repair", None)
        repair_system = (
            "你是复盘报告修复器。只修复审计指出的问题，保留第一阶段盲推原文"
            "与赛果揭晓边界，输出完整的 ### 1 到 ### 6，不解释修复过程。"
            "若提供了「假设结算」表，第 4、5 节的假设结论照抄该表；只改被指出的问题，其余原样保留。"
        )
        repaired, repair_route = _call_route(
            repair_system,
            "### 原复盘\n" + report + "\n### 审计问题\n"
            + json.dumps(blockers, ensure_ascii=False)
            + "\n### 代码计算的市场事实与已触发假设\n" + (bundle["market_block"] or "无（数据不足）")
            + (("\n" + bundle["settlement_block"]) if bundle["settlement_block"] else ""),
            routes["synthesis"], parse_json=False,
            phase="review_repair",
            rescue=rescue, cancel=cancel,
        )
        if repaired:
            repaired = str(repaired).strip()
            if _accept_review_repair(report, repaired):
                report = repaired
                _adopt_repair_route(repair_route, writers, synthesis)
                if progress:
                    progress("review_audit", None)
                audit = _review_audit(bundle, report, cards, spec=routes["audit"],
                                      excluded_models=writers, rescue=rescue)
            else:
                log.warning("全模型复盘修复稿未通过验收，保留原稿 model=%s", repair_route.get("model"))
                audit["repair_rejected"] = True
    if cancel is not None and cancel.is_set():
        return "", {"cancelled": True, "results": results, "audit": audit}
    report += "\n\n" + _render_appendix(results, audit, synthesis)
    return report, {"results": results, "audit": audit, "synthesis": synthesis,
                    "profile": "review", "writers": sorted(writers)}
