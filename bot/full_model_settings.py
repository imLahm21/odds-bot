"""Pure defaults and validation for independently configurable full-model profiles.

No credentials, database connections or provider requests belong in this module.
"""

from copy import deepcopy

from . import config


PROFILES = {"analyze": "全模型交叉精算", "review": "全模型交叉复盘"}


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
        if not config.llm_model_registered(model):
            problems.append(f"模型未登记：{model}")
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


def model_identity(model: str) -> str:
    """CPA -high aliases do not constitute independent foundation-model audits."""
    if model.endswith("-high") and config.llm_model_registered(model[:-5]):
        return model[:-5]
    return model
