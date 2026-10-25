"""Pure defaults and validation for independently configurable full-model profiles.

No credentials, database connections or provider requests belong in this module.
"""

from copy import deepcopy

from . import config


PROFILES = {"analyze": "全模型交叉精算", "review": "全模型交叉复盘"}


def model_allowed(model: str) -> bool:
    return (config.llm_model_registered(model)
            and model not in config.FULL_CONSULT_EXCLUDED_MODELS
            and model in config.FULL_CONSULT_MODEL_GRADES)


def require_model(model: str) -> None:
    if model in config.FULL_CONSULT_EXCLUDED_MODELS:
        raise ValueError(f"{model} 已从全模型精算/复盘中排除")
    if not model_allowed(model):
        raise ValueError("模型未登记为全模型精算/复盘可选项")


def rescue_defaults(profile: str) -> dict:
    if profile not in PROFILES:
        raise ValueError("未知全模型配置")
    return deepcopy(config.FULL_CONSULT_RESCUE_DEFAULTS[profile])


def rescue_pair(raw: dict) -> dict:
    """An empty model disables rescue; enabled rescue must pin an allowed group."""
    if not isinstance(raw, dict):
        raise ValueError("公共兜底配置格式错误")
    pair = {}
    for key in ("model", "effort", "route_group"):
        value = raw.get(key, "")
        if not isinstance(value, str):
            raise ValueError("兜底模型、强度和授权组必须为文本")
        pair[key] = value.strip()
    model, effort, group = pair["model"], pair["effort"], pair["route_group"]
    if not model:
        if effort or group:
            raise ValueError("关闭兜底时必须同时清空模型、强度和授权组")
        return pair
    require_model(model)
    if not config.llm_model_supports_effort(model, effort):
        raise ValueError("该兜底模型不支持所选推理强度")
    if not group or group not in config.llm_route_groups_for_model(model):
        raise ValueError("请选择该兜底模型允许的明确授权组")
    return pair


def defaults(profile: str) -> dict[str, dict]:
    if profile not in PROFILES:
        raise ValueError("未知全模型配置")
    roles = [*config.FULL_CONSULT_TASKS,
             {**config.FULL_CONSULT_SYNTHESIS, "id": "synthesis", "label": "最终报告合并"},
             {**config.FULL_CONSULT_AUDIT, "id": "audit", "label": "最终审计"}]
    return {role["id"]: deepcopy(role) for role in roles}


def slots(spec: dict) -> list[dict]:
    """Keep two fallback positions, including deliberately empty ones."""
    fallbacks = spec.get("fallbacks")
    if fallbacks is None:
        fallbacks = [{"model": model, "effort": spec.get("effort", "")}
                     for model in spec.get("fallback_models", ())]
    if not isinstance(fallbacks, (list, tuple)) or len(fallbacks) > 2:
        raise ValueError("最多配置两个回退模型")
    raw = [{"model": spec.get("model", ""), "effort": spec.get("effort", "")},
           *fallbacks]
    raw.extend({"model": "", "effort": ""} for _ in range(3 - len(raw)))
    result = []
    for pair in raw:
        if not isinstance(pair, dict):
            raise ValueError("模型与强度配置格式错误")
        model, effort = pair.get("model", ""), pair.get("effort", "")
        if not isinstance(model, str) or not isinstance(effort, str):
            raise ValueError("模型名和推理强度必须为文本")
        result.append({"model": model.strip(), "effort": effort.strip()})
    return result


def candidates(spec: dict) -> list[dict]:
    return [pair for pair in slots(spec) if pair["model"]]


def route_problems(spec: dict) -> list[str]:
    try:
        pairs = slots(spec)
    except (AttributeError, TypeError, ValueError) as exc:
        return [str(exc)]
    if not pairs[0]["model"]:
        return ["必须指定主模型"]
    primary = pairs[0]["model"]
    grade = config.FULL_CONSULT_MODEL_GRADES.get(primary)
    problems = []
    if not grade:
        problems.append(f"主模型未声明精算等级：{primary}")
    seen = set()
    for pair in pairs:
        model, effort = pair["model"], pair["effort"]
        if not model:
            if effort:
                problems.append("空回退不能设置推理强度")
            continue
        if model in seen:
            problems.append(f"同一职责的模型不能重复：{model}")
        seen.add(model)
        try:
            require_model(model)
        except ValueError as exc:
            problems.append(str(exc))
        if config.FULL_CONSULT_MODEL_GRADES.get(model) != grade:
            problems.append(f"主模型与回退须同等级：{primary} -> {model}")
        if not config.llm_model_supports_effort(model, effort):
            problems.append(f"{model} 不支持 effort={effort}")
    chain = [pair for pair in pairs if pair["model"]]
    if len(chain) > 1:
        primary_groups = set(config.llm_route_groups_for_model(primary))
        first_groups = set(config.llm_route_groups_for_model(chain[1]["model"]))
        if primary_groups & first_groups:
            problems.append("第一有效回退必须离开主模型授权组")
    return problems


def with_slots(template: dict, pairs: list[dict]) -> dict:
    """Only model/effort pairs are persisted; role metadata stays code-owned."""
    if not isinstance(pairs, list) or len(pairs) != 3:
        raise ValueError("必须提供主模型及两个回退位置")
    if not all(isinstance(pair, dict) for pair in pairs):
        raise ValueError("模型与强度配置格式错误")
    spec = deepcopy(template)
    spec.update(model=pairs[0].get("model", ""), effort=pairs[0].get("effort", ""),
                fallbacks=deepcopy(pairs[1:]))
    problems = route_problems(spec)
    if problems:
        raise ValueError("；".join(problems))
    normalized = slots(spec)
    spec.update(normalized[0], fallbacks=tuple(normalized[1:]))
    return spec


def replace_slot(spec: dict, position: int, model: str, effort: str) -> dict:
    if type(position) is not int or position not in (0, 1, 2):
        raise ValueError("模型槽位不存在")
    if not isinstance(model, str) or not isinstance(effort, str):
        raise ValueError("模型名和强度格式错误")
    pairs = slots(spec)
    pairs[position] = {"model": model, "effort": effort}
    return with_slots(spec, pairs)


def stored_route(template: dict, pairs: list[dict]) -> dict:
    """Remove excluded IDs from legacy overrides without rewriting their archive.

    Keep unaffected pairs and empty slots. If the primary was excluded, promote
    its first remaining fallback (or use the new code primary when none remain).
    A fallback that becomes an invalid same-group first escape is also cleared.
    Other malformed overrides still follow the established default/error path.
    """
    if not isinstance(pairs, list) or len(pairs) != 3 or not all(isinstance(p, dict) for p in pairs):
        return with_slots(template, pairs)
    raw = {"model": pairs[0].get("model", ""), "effort": pairs[0].get("effort", ""),
           "fallbacks": pairs[1:]}
    normalized = slots(raw)
    if not any(pair["model"] in config.FULL_CONSULT_EXCLUDED_MODELS for pair in normalized):
        return with_slots(template, pairs)
    for index, pair in enumerate(normalized):
        if pair["model"] in config.FULL_CONSULT_EXCLUDED_MODELS:
            normalized[index] = {"model": "", "effort": ""}
    if not normalized[0]["model"]:
        for index in (1, 2):
            if normalized[index]["model"]:
                normalized[0] = normalized[index]
                normalized[index] = {"model": "", "effort": ""}
                break
        else:
            normalized[0] = slots(template)[0]
    primary_groups = set(config.llm_route_groups_for_model(normalized[0]["model"]))
    for index in (1, 2):
        model = normalized[index]["model"]
        if not model:
            continue
        if primary_groups & set(config.llm_route_groups_for_model(model)):
            normalized[index] = {"model": "", "effort": ""}
        else:
            break
    return with_slots(template, normalized)


def model_identity(model: str) -> str:
    """CPA -high aliases do not constitute independent foundation-model audits."""
    if model.endswith("-high") and config.llm_model_registered(model[:-5]):
        return model[:-5]
    return model
