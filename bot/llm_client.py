"""
LLM 端点池 —— 多端点故障转移 + 每端点熔断器 + 连通性探针

analyzer.py 只负责构造 prompt，真正的 HTTP 调用、重试、失败隔离全在这里。
设计对标 cc switch 的「多供应商 + 熔断」思路，落到本项目的既有模式：

  - 端点只来自 .env 的 LLM_ROUTE_ENDPOINTS（group|key|url|label），每条 key
    只属于一个授权组（IKuncode 按模型家族授权，一条 key 覆盖不了别的家族）。
    多 key 轮换/标记坏点/切换的模式移植自 api_client.py 的 _switch_key/api_get。
  - 每个槽位（档位×角色）有【主模型 + 回退模型】：先按主模型定组、组内轮转与故障转移；
    该组端点全走完仍无成功（全熔断/全停用/组内无端点）才升级到回退模型那组密钥。
  - 先按角色/档位选模型，再按模型选密钥组，只在组内轮转和故障转移；每端点一个
    内存态 Breaker（CLOSED→OPEN→HALF_OPEN→CLOSED），10 个可调参数
    来自 db.llm_settings（TG /llm 面板实时改，免重启），缺库/缺键回退 config 默认。
  - chat()：阻塞，按端点顺序故障转移，跳过 OPEN 端点，全挂返回错误串（不抛异常，
    保持 analyzer 既有契约：失败返回以「LLM 请求失败/超时/网络错误…」开头的说明串）。
  - stream_chat()：流式，只在【首字节前】故障转移（此时 UI 未显示任何内容，安全）；
    一旦开始吐正文再断，则不静默重启（会重复可见输出），但失败仍记进 Breaker
    以引导下一次请求避开坏端点。
  - probe()：对单端点发最小 chat 请求，返回 HTTP 状态/真实延迟/model/熔断态；
    纯诊断，不计入 Breaker 统计（健康检查不该污染故障转移的错误率）。
"""

import os
import re
import time
import json
import logging
import threading
from time import monotonic
from collections import deque

import requests
from dotenv import load_dotenv

from . import config, db

load_dotenv()
log = logging.getLogger("odds_bot.llm")


# ─── 管理员告警钩子（依赖注入，避免 llm_client → tgbot 循环 import）──────────
# tgbot 已 import llm_client；llm_client 不能反向 import tgbot。故这里留一个可注入
# 的回调，由 tgbot 启动时调 set_alert_hook(alert_admins) 装上。未装（如探针/离线）
# 时静默降级为只写日志。签名对齐 tgbot.alert_admins(text, dedup_key=None)。
_alert_hook = None
_alert_dedup_clear = None


def set_alert_hook(fn, dedup_clear=None) -> None:
    """注入管理员告警回调。
    fn(text: str, dedup_key: str | None) -> None —— 发告警（当日按 dedup_key 去重）。
    dedup_clear(dedup_key: str) -> None —— 可选，清掉某去重键（手动重置端点后调，
    使再次熔断/恢复能重新告警）。"""
    global _alert_hook, _alert_dedup_clear
    _alert_hook = fn
    _alert_dedup_clear = dedup_clear


def _alert(text: str, dedup_key: str | None = None) -> None:
    """向管理员告警（若已注入钩子），否则只记日志。绝不因告警失败影响主流程。"""
    log.warning("LLM 告警: %s", text)
    if _alert_hook is None:
        return
    try:
        _alert_hook(text, dedup_key)
    except Exception:
        log.exception("LLM 告警钩子执行失败（忽略，不影响主流程）")


def _clear_alert_dedup(dedup_key: str) -> None:
    """清掉某告警去重键（若注入了 dedup_clear）。静默失败。"""
    if _alert_dedup_clear is None:
        return
    try:
        _alert_dedup_clear(dedup_key)
    except Exception:
        log.exception("清告警去重键失败（忽略）")


# ─── 请求头清洗（从 analyzer 迁来，analyzer 改为 import 本函数）──────────────
def clean_header_value(raw: str) -> str:
    """清洗将放进 HTTP 头的配置值。

    从聊天/文档复制 key/url 时常混入非 ASCII 不可见字符（全角空格 U+3000、
    零宽空格 U+200B、BOM 等），会导致 requests 编码请求头时
    UnicodeEncodeError('latin-1')。这里去掉首尾常见不可见字符 + 所有非 ASCII，
    并记录告警，避免整条命令崩溃。
    """
    s = raw.strip().strip("　​‌‍﻿\xa0")
    ascii_only = s.encode("ascii", "ignore").decode("ascii")
    if ascii_only != s:
        log.warning("配置值含非 ASCII 字符，已剥离 %d 个（请检查 .env 是否复制带入"
                    "全角符号）", len(s) - len(ascii_only))
    return ascii_only


# ─── 端点池（.env 解析，进程启动一次）───────────────────────────────────────
def _legacy_env_present() -> list[str]:
    """检测 .env 里还留着的旧路由变量名（已不参与路由，仅用于面板提示）。

    这次故障的根因就是「新分组逻辑写好了，但 .env 只有旧变量，于是整套新逻辑被绕过
    且无任何告警」。旧变量现已彻底不参与路由，这里只负责把它们的存在显式报出来。
    """
    return [name for name in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_ENDPOINTS",
                              "LLM_SUPPORTED_MODELS")
            if os.getenv(name, "").strip()]


def _parse_route_endpoints(raw: str | None = None) -> list[dict]:
    """解析新分组格式：group|key|base_url|label。

    每条凭据只属于一个授权组；同一组可重复多条 key。未知组、缺 key/url、或同一
    key+url 被分配到多个组时拒绝该条，日志永不输出 key。
    """
    source = os.getenv("LLM_ROUTE_ENDPOINTS", "") if raw is None else raw
    eps: list[dict] = []
    seen: dict[str, str] = {}
    for item in re.split(r"[,\n]", source):
        item = item.strip()
        if not item:
            continue
        parts = [part.strip() for part in item.split("|")]
        if len(parts) < 4:
            log.error("LLM_ROUTE_ENDPOINTS 条目字段不足（需 group|key|url|label）")
            continue
        group = parts[0]
        key = clean_header_value(parts[1])
        url = clean_header_value(parts[2]).rstrip("/")
        label = parts[3] or f"{group}-{len(eps) + 1}"
        if group not in config.LLM_ROUTE_GROUPS:
            log.error("LLM_ROUTE_ENDPOINTS 含未知分组：%s", group)
            continue
        if not (key and url):
            log.error("LLM_ROUTE_ENDPOINTS 分组 %s 缺 key 或 base_url", group)
            continue
        previous_group = seen.get(key)
        if previous_group is not None:
            if previous_group != group:
                log.error("同一 LLM 凭据被分配到多个组：%s / %s",
                          previous_group, group)
            continue
        seen[key] = group
        eps.append({
            "key": key,
            "base_url": url,
            "label": label,
            "route_group": group,
        })
    return eps


_ENDPOINTS: list[dict] = _parse_route_endpoints()


def _sig(ep: dict) -> str:
    """端点签名 group|label|url（不含 key），作 DB 稳定主键——
    不依赖易变的数组下标，增删端点后仍能对上原来的开关记录。"""
    return f"{ep['route_group']}|{ep['label']}|{ep['base_url']}"


def _legacy_sig(ep: dict) -> str:
    """分组改造前的端点签名 label|url。

    只用于【继承 DB 里旧的面板开关记录】（与 .env 解析无关）：老库里停用记录存的是
    label|url，删掉这层继承会让已停用端点在升级后静默复活。
    """
    return f"{ep['label']}|{ep['base_url']}"


# ─── 三档模型运行时选定（DB 懒加载，TG 改后 reload_runtime_models 失效）─────────
# 按【档位 tier】路由，不再靠模型名判定——模型运行时可切（如 astra/grok），
# 名字比较会失效。tier ∈ heavy/balanced/light。
_runtime_models: dict[str, str] | None = None
_runtime_lock = threading.Lock()


def _rt_key(tier: str, visitor: bool, kind: str = "model") -> str:
    """runtime-state 键：kind='model' 取主模型槽、'fallback' 取回退模型槽；
    管理员 <kind>_<tier>，访客 <kind>_<tier>_visitor。"""
    return f"{kind}_{tier}_visitor" if visitor else f"{kind}_{tier}"


def _load_runtime_models() -> dict[str, str]:
    """从 db.llm_runtime_state 读 12 个槽位（6 主 + 6 回退）；
    DB 异常回退 config 默认（管理员+访客各自）。"""
    defaults: dict[str, str] = {}
    for t in config.LLM_TIER_MODELS:
        defaults[f"model_{t}"] = config.llm_tier_default(t, False)
        defaults[f"model_{t}_visitor"] = config.llm_tier_default(t, True)
        defaults[f"fallback_{t}"] = config.llm_tier_fallback(t, False)
        defaults[f"fallback_{t}_visitor"] = config.llm_tier_fallback(t, True)
    try:
        conn = db.get_conn()
        try:
            raw = db.get_llm_runtime_state(conn)
        finally:
            conn.close()
        return {k: raw.get(k, defaults[k]) for k in defaults}
    except Exception as e:
        log.warning("读 llm_runtime_state 失败，回退 config 默认: %s", e)
        return defaults


def get_tier_model(tier: str, visitor: bool = False) -> str:
    """取某档某角色当前选定模型（进程内缓存，懒加载）。
    visitor=True 取访客那份，否则取管理员那份。未知档回退该角色默认。"""
    global _runtime_models
    if _runtime_models is None:
        with _runtime_lock:
            if _runtime_models is None:
                _runtime_models = _load_runtime_models()
    return _runtime_models.get(_rt_key(tier, visitor),
                               config.llm_tier_default(tier, visitor)
                               or config.LLM_MODEL)


def reload_runtime_models() -> None:
    """令模型缓存失效（TG /llm 切换/回退后调用），下次 get_tier_model 重读 DB。"""
    global _runtime_models
    with _runtime_lock:
        _runtime_models = None


def get_fallback_model(tier: str, visitor: bool = False) -> str:
    """取某档某角色当前选定的【回退模型】；空串 = 未设回退（不做跨组逃生）。"""
    global _runtime_models
    if _runtime_models is None:
        with _runtime_lock:
            if _runtime_models is None:
                _runtime_models = _load_runtime_models()
    return _runtime_models.get(_rt_key(tier, visitor, "fallback"),
                               config.llm_tier_fallback(tier, visitor)) or ""


def _set_slot(tier: str, model: str, visitor: bool, kind: str) -> bool:
    """写单个槽位并刷新缓存。校验 tier 合法 + model ∈ 该档可选池（回退槽允许空串）。"""
    if tier not in config.LLM_TIER_MODELS:
        return False
    try:
        conn = db.get_conn()
        try:
            ok = db.set_llm_runtime_state(
                conn, _rt_key(tier, visitor, kind), model)
        finally:
            conn.close()
    except Exception as e:
        log.warning("写 llm_runtime_state 失败 tier=%s visitor=%s kind=%s: %s",
                    tier, visitor, kind, e)
        return False
    if ok:
        reload_runtime_models()
    return ok


def set_tier_model(tier: str, model: str, visitor: bool = False) -> bool:
    """切换某档某角色的【主模型】并落库、刷新缓存。非法返回 False。"""
    return _set_slot(tier, model, visitor, "model")


def set_fallback_model(tier: str, model: str, visitor: bool = False) -> bool:
    """切换某档某角色的【回退模型】并落库；model 传空串 = 清空回退。"""
    return _set_slot(tier, model, visitor, "fallback")


def apply_fallback_models() -> None:
    """一键回退：把每个槽位当前选定的【回退模型】写进【主模型】槽，免重启。

    回退槽本身不动，故可反复点（幂等）。回退值为空或与主模型相同的槽位跳过。
    模型由用户在 /llm 面板自选，这里不读 config 常量。
    """
    # 先失效缓存再读：本函数「读回退值 → 写进主模型槽」，若缓存是旧的（换过 DB
    # 或别处刚改过值），会把过期的回退模型写进主槽。
    reload_runtime_models()
    try:
        conn = db.get_conn()
        try:
            for tier in config.LLM_TIER_MODELS:
                for visitor in (False, True):
                    target = get_fallback_model(tier, visitor)
                    if not target or target == get_tier_model(tier, visitor):
                        continue
                    db.set_llm_runtime_state(
                        conn, _rt_key(tier, visitor, "model"), target,
                        allow_any=True)
        finally:
            conn.close()
    except Exception as e:
        log.warning("一键启用回退模型失败: %s", e)
    reload_runtime_models()


def reset_runtime_models() -> None:
    """把 12 个槽位（6 主 + 6 回退）恢复为 config 默认。"""
    try:
        conn = db.get_conn()
        try:
            db.reset_llm_runtime_state(conn)
        finally:
            conn.close()
    except Exception as e:
        log.warning("恢复默认失败: %s", e)
    reload_runtime_models()


def _supports_model(ep: dict, model: str) -> bool:
    """端点的授权组是否覆盖该模型。"""
    return ep["route_group"] in config.llm_route_groups_for_model(model)


def resolve_model_chain(tier: str, visitor: bool = False) -> list[str]:
    """某槽位的模型尝试顺序：[主模型, 回退模型]。

    去重去空后，丢掉「所在密钥组在 .env 里一条端点都没配」的模型——只买了 GPT 密钥时
    访客重档默认的 deepseek 就属于这种，靠回退模型自动兜住。全丢完返回 []，
    由调用方报出缺哪个组（**不静默替换成用户没选的模型**——静默替换正是这次故障
    难以察觉的原因）。

    链长封顶 2：一次请求最多升级一次，额度可预期。
    """
    chain: list[str] = []
    for model in (get_tier_model(tier, visitor),
                  get_fallback_model(tier, visitor)):
        if not model or model in chain:
            continue
        if not configured_count_for_model(model):
            continue
        # 未登记模型照样入链（可能是用户指向网关的私有模型），但记一条告警：
        # 前缀推导对已下线的名字也会推出「有 key」的组（gpt-5.6-terra → ik_gpt），
        # 这类请求会 404 model_not_found，日志里要能一眼看出是模型名的问题。
        if not config.llm_model_registered(model):
            log.warning("槽位 tier=%s visitor=%s 的模型 %s 未登记于 "
                        "config.LLM_MODELS，按前缀路由到 %s；若上游不认该名字会 404",
                        tier, visitor, model,
                        "/".join(config.llm_route_groups_for_model(model)))
        chain.append(model)
    return chain


def chain_error(tier: str, visitor: bool = False) -> str:
    """模型链为空时的精确错误串：点名每个候选模型缺哪个密钥组。"""
    role = "访客" if visitor else "管理员"
    label = (config.LLM_TIER_MODELS.get(tier) or {}).get("label", tier)
    parts = []
    for model in (get_tier_model(tier, visitor),
                  get_fallback_model(tier, visitor)):
        if not model:
            continue
        groups = config.llm_route_groups_for_model(model)
        why = ("缺密钥组 " + "/".join(groups)) if groups else "未登记密钥组"
        parts.append(f"{model}（{why}）")
    detail = "；".join(parts) or "未选定任何模型"
    return (f"LLM 请求失败（{label}·{role} 无可用模型：{detail}。"
            "请在 .env 的 LLM_ROUTE_ENDPOINTS 补该组密钥，或用 /llm 面板改选模型）")


def route_groups_for_model(model: str) -> tuple[str, ...]:
    """公开模型的有序路由组，供面板与审计展示。"""
    return config.llm_route_groups_for_model(model)


def routing_issues() -> list[str]:
    """逐个检查 12 个槽位当前选定的模型有没有已配密钥组；不读取或暴露 key。

    同时把 .env 里残留的旧路由变量报出来——这次故障就是「旧变量在、新变量缺，
    整套分组逻辑被静默绕过」，所以残留必须显式可见。
    """
    issues: list[str] = []
    seen: set[str] = set()
    for tier, spec in config.LLM_TIER_MODELS.items():
        label = spec.get("label", tier)
        for visitor in (False, True):
            role = "访客" if visitor else "管理员"
            for kind, model in (("主", get_tier_model(tier, visitor)),
                                ("回退", get_fallback_model(tier, visitor))):
                if not model:
                    continue        # 回退可以不设，不算问题
                groups = config.llm_route_groups_for_model(model)
                if not groups:
                    reason = f"模型 {model} 未登记密钥组"
                elif not configured_count_for_model(model):
                    reason = f"模型 {model} 缺密钥组：{'/'.join(groups)}"
                elif not config.llm_model_registered(model):
                    # 不算硬故障（可能是网关私有模型），但要提示无法本地校验。
                    reason = (f"模型 {model} 不在 config.LLM_MODELS，"
                              f"已按前缀路由到 {'/'.join(groups)}；"
                              "若上游不认该名字会 404，请确认或改选")
                else:
                    continue
                item = f"{label}·{role}·{kind}：{reason}"
                if item not in seen:
                    seen.add(item)
                    issues.append(item)
            # 回退 == 主模型 → 逃生毫无意义（同模型同组，主组挂了回退必然也挂）。
            # 不静默改值（可能是用户刻意为之），但必须报出来。
            primary = get_tier_model(tier, visitor)
            fallback = get_fallback_model(tier, visitor)
            if fallback and fallback == primary:
                issues.append(f"{label}·{role}：回退模型与主模型同为 {primary}，"
                              "主组挂掉时回退也必然挂，等于没有回退——"
                              "请在 /llm 面板改选同档其它模型")
    leftovers = _legacy_env_present()
    if leftovers:
        issues.append(f"⚠️ .env 仍有旧变量 {'/'.join(leftovers)}，"
                      "它们已不参与路由，可删除以免误判配置生效")
    return issues


def available() -> bool:
    """至少有一个可用端点（key + base_url 齐全）。"""
    return bool(_ENDPOINTS)


def endpoints() -> list[dict]:
    """只读端点列表（含授权组，不含 key）供 TG 面板展示。"""
    return [{"label": e["label"], "base_url": e["base_url"],
             "route_group": e["route_group"]}
            for e in _ENDPOINTS]


def configured_groups() -> set[str]:
    """.env 里实际配了密钥的授权组集合。"""
    return {ep["route_group"] for ep in _ENDPOINTS}


# ─── 端点手动开关（DB 懒加载，TG 改后 reload_endpoint_state 失效）────────────
# 与熔断（自动隔离故障端点）正交：这里是运维「只连哪个」的手动控制。
# 停用集合存被停用的端点签名；表中无记录的端点默认启用。
_disabled_cache: set[str] | None = None
_disabled_lock = threading.Lock()


def _load_disabled() -> set[str]:
    """从 db.llm_endpoint_state 读被停用的端点签名；DB 异常时回退空集（全启用）。"""
    try:
        conn = db.get_conn()
        try:
            disabled = db.get_disabled_endpoints(conn)
            # 新签名加入 group；若旧 label|url 曾被停用，先在内存继承该状态。
            for ep in _ENDPOINTS:
                if _legacy_sig(ep) in disabled:
                    disabled.add(_sig(ep))
            return disabled
        finally:
            conn.close()
    except Exception as e:
        log.warning("读 llm_endpoint_state 失败，默认全部端点启用: %s", e)
        return set()


def _get_disabled() -> set[str]:
    """取停用签名集合（进程内缓存，首次访问懒加载）。"""
    global _disabled_cache
    if _disabled_cache is None:
        with _disabled_lock:
            if _disabled_cache is None:
                _disabled_cache = _load_disabled()
    return _disabled_cache


def reload_endpoint_state() -> None:
    """令开关缓存失效（TG 改开关后调用），下次选路重读 DB。"""
    global _disabled_cache
    with _disabled_lock:
        _disabled_cache = None


def is_enabled(idx: int) -> bool:
    """指定端点当前是否启用（未被手动停用）。越界视为未启用。"""
    if not (0 <= idx < len(_ENDPOINTS)):
        return False
    return _sig(_ENDPOINTS[idx]) not in _get_disabled()


def enabled_count() -> int:
    """当前启用（未被手动停用）的端点数。"""
    return sum(1 for i in range(len(_ENDPOINTS)) if is_enabled(i))


def configured_count_for_model(model: str) -> int:
    """配置中可承载指定模型的端点数（含手动停用的）。"""
    return sum(1 for ep in _ENDPOINTS if _supports_model(ep, model))


def enabled_count_for_model(model: str) -> int:
    """当前启用且可承载指定模型的端点数。"""
    return sum(1 for i, ep in enumerate(_ENDPOINTS)
               if _supports_model(ep, model) and is_enabled(i))


def set_enabled(idx: int, enabled: bool) -> bool:
    """手动开/关指定端点并落库，刷新缓存。越界返回 False。"""
    if not (0 <= idx < len(_ENDPOINTS)):
        return False
    try:
        conn = db.get_conn()
        try:
            db.set_endpoint_disabled(conn, _sig(_ENDPOINTS[idx]), not enabled)
            # 用户重新启用时清掉旧签名，否则下次重启会再次继承为停用。
            if enabled:
                db.set_endpoint_disabled(
                    conn, _legacy_sig(_ENDPOINTS[idx]), False)
        finally:
            conn.close()
    except Exception as e:
        log.warning("写端点开关失败 idx=%d: %s", idx, e)
        return False
    reload_endpoint_state()
    return True


# ─── 10 个可调参数缓存（DB 懒加载，TG 改后 reload_settings 失效）─────────────
_settings_cache: dict[str, float] | None = None
_settings_lock = threading.Lock()


def _load_settings() -> dict[str, float]:
    """从 db.llm_settings 读 10 参数；DB 未初始化/异常时回退 config 默认，
    保证 llm_client 在任何环境（含未 init_db 的探针）都能拿到完整参数。"""
    try:
        conn = db.get_conn()
        try:
            return db.get_llm_settings(conn)
        finally:
            conn.close()
    except Exception as e:
        log.warning("读 llm_settings 失败，回退 config 默认: %s", e)
        return {k: float(s["default"])
                for k, s in config.LLM_SETTING_SPECS.items()}


def get_settings() -> dict[str, float]:
    """取 10 参数（进程内缓存，首次访问懒加载）。走地 1min 循环高频调用，走缓存。"""
    global _settings_cache
    if _settings_cache is None:
        with _settings_lock:
            if _settings_cache is None:
                _settings_cache = _load_settings()
    return _settings_cache


def reload_settings() -> None:
    """令参数缓存失效（TG /llm 改值后调用），下次 get_settings 重读 DB。"""
    global _settings_cache
    with _settings_lock:
        _settings_cache = None


# ─── 熔断器（每端点一个，纯内存态，重启自然重置）────────────────────────────
class Breaker:
    """CLOSED→OPEN→HALF_OPEN→CLOSED 状态机。参数实时读 get_settings()，
    故 TG 改阈值后立即对在途判定生效。用滚动计数窗口(deque)算错误率，
    非精确时间桶——1C1G 够用。计时用 time.monotonic()（不受系统时钟跳变影响）。"""

    def __init__(self, idx: int, label: str = "") -> None:
        self.idx = idx
        self.label = label or f"端点{idx}"
        self._lock = threading.Lock()
        self.state = "CLOSED"
        self.consecutive = 0                 # 连续失败数（成功即清零）
        self.window: deque[bool] = deque(maxlen=200)  # True=成功，滚动错误率窗口
        self.opened_at = 0.0                 # 进 OPEN 的 monotonic 时刻
        self.half_ok = 0                     # 半开态累计成功数

    def allow(self) -> bool:
        """是否放行本次请求。OPEN 且未到恢复时间 → 拒绝(跳过该端点)。"""
        with self._lock:
            if self.state == "CLOSED":
                return True
            if self.state == "OPEN":
                wait = get_settings()["recovery_wait_seconds"]
                if monotonic() - self.opened_at >= wait:
                    self.state = "HALF_OPEN"   # 到点转半开，放一个探测请求过去
                    self.half_ok = 0
                    return True
                return False
            return True   # DEGRADED（仍放行、仅选路降优先）/ HALF_OPEN（放行探测）

    def record(self, ok: bool) -> None:
        """喂一次调用结果，驱动状态迁移。
        状态迁移在锁内决策、锁外发告警（Telegram HTTP 不可持锁调用）。"""
        event = None            # 'open' | 'recover' | 'degrade'，锁外据此告警
        reason = ""
        undegraded = False      # 降级→正常（静默），锁外清降级去重键
        with self._lock:
            self.window.append(bool(ok))
            if ok:
                self.consecutive = 0
                if self.state == "HALF_OPEN":
                    self.half_ok += 1
                    if self.half_ok >= get_settings()["recovery_success_threshold"]:
                        self._close()
                        event = "recover"
                elif self.state == "DEGRADED":
                    # 降级态一次成功即恢复正常（抖动收敛）。静默恢复不告警，
                    # 只清降级去重键，使日后再降级能重新告警；仅 log。
                    self._close()
                    undegraded = True
                    log.info("LLM 端点【%s】降级已自动恢复正常", self.label)
            else:
                self.consecutive += 1
                if self.state == "HALF_OPEN":
                    self._open()               # 半开期任一失败 → 立刻回 OPEN
                    # 半开探测又失败：不算「新打开」，避免与首次 open 告警重复刷屏
                elif self.state in ("CLOSED", "DEGRADED"):
                    st = get_settings()
                    # 降级阈值须 ≤ 失败阈值（config 已约束；运行时再兜底防误配）
                    deg_th = min(int(st["degrade_threshold"]),
                                 int(st["failure_threshold"]))
                    if self.consecutive >= st["failure_threshold"]:
                        self._open()
                        event = "open"
                        reason = f"连续失败 {self.consecutive} 次"
                    elif self._rate_tripped(st):
                        self._open()
                        event = "open"
                        total = len(self.window)
                        fails = sum(1 for x in self.window if not x)
                        reason = f"错误率 {fails / total * 100:.0f}%（{fails}/{total}）"
                    elif self.state == "CLOSED" and self.consecutive >= deg_th:
                        self.state = "DEGRADED"   # 轻度失败 → 降级预警（仍放行）
                        event = "degrade"
                        reason = f"连续失败 {self.consecutive} 次（未达熔断线）"
        if event == "open":
            wait = int(get_settings()["recovery_wait_seconds"])
            _alert(f"🔴 LLM 端点【{self.label}】已熔断（{reason}），暂停派发，"
                   f"{wait}s 后自动半开探活。可在 TG 发 /llm 查看或手动重置。",
                   dedup_key=f"llm_open_{self.idx}")
        elif event == "degrade":
            _alert(f"🟠 LLM 端点【{self.label}】已降级（{reason}），仍在用但选路已降优先，"
                   f"继续失败将熔断。可在 TG 发 /llm 查看。",
                   dedup_key=f"llm_degrade_{self.idx}")
        elif event == "recover":
            _alert(f"✅ LLM 端点【{self.label}】已自动恢复（半开探测成功，熔断关闭），"
                   f"重新纳入派发。", dedup_key=f"llm_recover_{self.idx}")
        # 降级→正常：清降级去重键，使日后再降级能重新告警（自身静默不发）
        if undegraded or event == "recover":
            _clear_alert_dedup(f"llm_degrade_{self.idx}")

    def _rate_tripped(self, st: dict[str, float]) -> bool:
        total = len(self.window)
        if total < st["min_requests"]:
            return False
        fails = sum(1 for x in self.window if not x)
        return (fails / total * 100.0) >= st["error_rate_threshold_pct"]

    def _open(self) -> None:
        self.state = "OPEN"
        self.opened_at = monotonic()
        self.half_ok = 0

    def _close(self) -> None:
        self.state = "CLOSED"
        self.consecutive = 0
        self.half_ok = 0
        self.window.clear()

    def reset(self) -> None:
        """管理员在 /llm 手动重置：强制回 CLOSED、清统计。手动重置不告警
        （是管理员主动操作，无需再通知自己）。同时清 open/recover 的当日去重键，
        使重置后若再次熔断/恢复能重新告警。"""
        with self._lock:
            self._close()
        _clear_alert_dedup(f"llm_open_{self.idx}")
        _clear_alert_dedup(f"llm_degrade_{self.idx}")
        _clear_alert_dedup(f"llm_recover_{self.idx}")

    def stats(self) -> dict:
        """供 /llm 面板展示：状态/连续失败/错误率/距半开剩余秒。"""
        with self._lock:
            total = len(self.window)
            fails = sum(1 for x in self.window if not x)
            rate = (fails / total * 100.0) if total else 0.0
            remain = 0
            if self.state == "OPEN":
                wait = get_settings()["recovery_wait_seconds"]
                remain = max(0, int(wait - (monotonic() - self.opened_at)))
            return {"state": self.state, "consecutive": self.consecutive,
                    "total": total, "fails": fails, "error_rate": rate,
                    "half_ok": self.half_ok, "open_remain": remain}


_breakers: list[Breaker] = [Breaker(i, _ENDPOINTS[i]["label"])
                            for i in range(len(_ENDPOINTS))]


def breaker_stats() -> list[dict]:
    """全部端点的熔断统计（含 label），供 /llm 面板。"""
    return [{"label": _ENDPOINTS[i]["label"], **_breakers[i].stats()}
            for i in range(len(_ENDPOINTS))]


def reset_breaker(idx: int) -> bool:
    """重置指定端点熔断器。越界返回 False。"""
    if 0 <= idx < len(_breakers):
        _breakers[idx].reset()
        return True
    return False


# ─── 载荷与请求头 ────────────────────────────────────────────────────────────
def _token_limit_field(route_group: str) -> str:
    """按供应商选择 Chat Completions 的输出 token 参数名。

    OpenAI 官方已用 max_completion_tokens 取代 max_tokens；IKuncode 当前仍接受
    max_tokens。按端点所属 provider 判断，避免只凭 gpt-* 模型名前缀误伤中转端点。
    """
    provider = (config.LLM_ROUTE_GROUPS.get(route_group) or {}).get("provider")
    return "max_completion_tokens" if provider == "openai" else "max_tokens"


def _apply_completion_options(payload: dict, model: str, route_group: str,
                              max_tokens: int, effort: str = "") -> dict:
    """给普通请求与探针统一加入 token 上限，并按模型能力安全附加推理强度。"""
    payload[_token_limit_field(route_group)] = max_tokens
    if effort:
        if config.llm_model_supports_effort(model, effort):
            payload["reasoning_effort"] = effort
        else:
            # 主模型切到能力不同的回退模型时，保留请求但让回退模型使用自身默认强度，
            # 避免一个不支持的 reasoning_effort 令整条逃生链再次 400。
            log.warning("模型 %s 不支持 reasoning_effort=%s，已省略并使用模型默认强度",
                        model, effort)
    return payload


def _payload(model: str, system: str, user: str, max_tokens: int,
             effort: str, stream: bool, route_group: str) -> dict:
    p = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    _apply_completion_options(p, model, route_group, max_tokens, effort)
    if stream:
        p["stream"] = True
    return p


# 客户端标识：部分中转商分组做 User-Agent 白名单，只放行特定客户端
# （如 codex / claude-code / gemini-cli / droid / crush），检测到 requests 默认的
# "python-requests/x.y" 会直接 403 channel:client_restricted（探针与精算全被拒）。
# 故统一声明为白名单内的标识；对不校验的端点只是多一个请求头、无副作用。
# 可用 .env 的 LLM_USER_AGENT 覆盖（换中转商/白名单变化时无需改代码）。
LLM_USER_AGENT = clean_header_value(os.getenv("LLM_USER_AGENT", "")) or "codex"


def _headers(ep: dict) -> dict:
    return {"Authorization": f"Bearer {ep['key']}",
            "Content-Type": "application/json",
            "User-Agent": LLM_USER_AGENT}


_CONNECT_TIMEOUT = 10   # 连接超时（秒），与读超时分开


# ─── 阻塞调用（带端点内重试 + 跨端点故障转移）────────────────────────────────
def _do_chat(ep: dict, payload: dict, read_to: int,
             max_retries: int) -> tuple[str | None, bool, str, bool, bool]:
    """对单端点发一次非流式请求（含端点内重试）。
    返回 (正文 or None, 成功?, 错误串, 是否超时类, 是否限流429)。错误串沿用 analyzer 旧格式。
    重试仅针对瞬时错误（网络/超时/429/5xx）；4xx / 空内容 / 无 choices 为确定性
    错误，不在同端点重试（重试同请求结果相同），直接判失败交由上层切下一端点。

    末位 rate_limited=True 表示「被限流(429)」——限流 ≠ 端点故障，上层据此
    【不喂熔断器】，避免把健康但暂时超 TPM 的 key 误熔断 90s（详见 chat()）。
    """
    url = f"{ep['base_url']}/chat/completions"
    headers = _headers(ep)
    attempt = 0
    while True:
        try:
            r = requests.post(url, json=payload, headers=headers,
                              timeout=(_CONNECT_TIMEOUT, read_to))
        except requests.exceptions.Timeout:
            attempt += 1
            if attempt > max_retries:
                return (None, False,
                        f"LLM 超时（>{read_to}s）。推理较慢，可稍后重试。", True, False)
            time.sleep(min(2 ** attempt, 8))
            continue
        except UnicodeEncodeError as e:
            log.error("请求头编码失败（key/url 含非 ASCII）: %s", e)
            return (None, False,
                    "LLM_API_KEY 或 LLM_BASE_URL 含非 ASCII 字符（可能复制时混入了"
                    "全角符号/空格）。请检查服务器 .env 后重启。", False, False)
        except requests.exceptions.RequestException as e:
            attempt += 1
            if attempt > max_retries:
                return None, False, f"LLM 网络错误：{e}", False, False
            time.sleep(min(2 ** attempt, 8))
            continue

        if r.status_code != 200:
            body = r.text[:300]
            if r.status_code == 429 or 500 <= r.status_code < 600:
                attempt += 1
                if attempt > max_retries:
                    # 429 标记为限流（上层不喂熔断器）；5xx 是真故障，照旧计入
                    is_429 = r.status_code == 429
                    if is_429:
                        log.warning("端点【%s】被限流 429（重试 %d 次仍限流），"
                                    "换下一端点、不计入熔断", ep["label"], max_retries)
                    return (None, False,
                            f"LLM 请求失败 HTTP {r.status_code}：{body}", False, is_429)
                time.sleep(min(2 ** attempt, 8))
                continue
            # 4xx（400/401/403 等）确定性错误：不重试，直接切端点
            return None, False, f"LLM 请求失败 HTTP {r.status_code}：{body}", False, False

        try:
            data = r.json()
        except ValueError:
            return (None, False,
                    f"LLM 请求失败 HTTP 200：响应非 JSON：{r.text[:200]}", False, False)
        choices = data.get("choices", [])
        if not choices:
            return None, False, f"LLM 返回无 choices：{str(data)[:300]}", False, False
        content = choices[0].get("message", {}).get("content", "").strip()
        if not content:
            return None, False, "LLM 返回空内容", False, False
        return content, True, "", False, False


# 选路优先级：正常/半开端点优先，降级端点次之（仍用但靠后），OPEN 交由 allow() 冷却门控。
# 手动停用端点直接排除。返回按优先级排好序的 [(idx, ep)]，调用方仍需逐个 br.allow()。
_STATE_PRIORITY = {"CLOSED": 0, "HALF_OPEN": 0, "OPEN": 1, "DEGRADED": 2}

# 每个授权组独立轮转：把并发请求的【起始端点】错开，避免全压同一个 key。
# 背景：中转商按令牌分组限流（各 key 129K~285K TPM），单次精算下行 15~28 万 token；
# 几秒内连点多场若都从同一端点起头，瞬时 token 叠加必撞 429（实测 429 集中在容量最小的
# 分组）。轮转让每场落到不同 key，各自远低于自身 TPM。
_rr_counters: dict[str, int] = {}
_rr_lock = threading.Lock()


def _endpoints_by_priority(model: str = "") -> list[tuple[int, dict]]:
    """该模型可用的端点（已排除手动停用）按选路优先级排序：
    正常/半开(0) < OPEN(1) < 降级(2)。降级端点排最后——健康端点先跑，
    降级的只在健康端点都用不上时才轮到。

    先按 model 限定授权组，再只在该组内轮转：GPT 的 key 永远不会被拿去请求 grok。
    """
    disabled = _get_disabled()
    groups = config.llm_route_groups_for_model(model)
    group_set = set(groups)
    cands = [(i, ep) for i, ep in enumerate(_ENDPOINTS)
             if _sig(ep) not in disabled and ep["route_group"] in group_set]
    route_key = "|".join(groups) or f"unmapped:{model}"
    if len(cands) > 1:
        with _rr_lock:
            current = (_rr_counters.get(route_key, -1) + 1) % len(cands)
            _rr_counters[route_key] = current
            shift = current
        cands = cands[shift:] + cands[:shift]   # 旋转起点
    # 稳定排序：组内保持上面旋转后的相对次序，仅按熔断状态分层
    return sorted(cands, key=lambda t: _STATE_PRIORITY.get(
        _breakers[t[0]].state, 1))


def chat(system: str, user: str, effort: str = "", tier: str = "heavy",
         timeout: int = 0, max_tokens: int = 0, visitor: bool = False) -> str:
    """阻塞式调用，按选路优先级故障转移（正常端点先、降级端点后）。
    失败返回错误说明串（不抛异常）。
    visitor=True 时该档用访客那份选定模型（访客触发的 /analyze /review）。
    tier ∈ heavy/balanced/light：决定用哪档运行时选定模型（每端点再按映射翻译）。
    timeout/max_tokens 非默认时覆盖 config（走地/基本面/SEO 各传自己的短超时）；
    未显式传 timeout 时用 DB 的 non_stream_timeout。
    """
    if not available():
        return "未配置 LLM_ROUTE_ENDPOINTS，无法分析。请在 .env 配置。"
    st = get_settings()
    read_to = int(timeout or st["non_stream_timeout"])
    max_retries = int(st["max_retries"])
    tok = max_tokens or config.LLM_MAX_TOKENS

    chain = resolve_model_chain(tier, visitor)
    if not chain:
        return chain_error(tier, visitor)

    raw_errs: list[str] = []      # 各端点原始错误串（单端点时原样返回，保持旧文案）
    labeled: list[str] = []       # 带端点标签的错误（多端点聚合展示）
    all_timeout = True            # 是否全部端点都是超时类失败
    for pos, model in enumerate(chain):
        if pos:
            log.warning("模型 %s 的密钥组全部不可用，升级到回退模型 %s",
                        chain[pos - 1], model)
        for idx, ep in _endpoints_by_priority(model):
            br = _breakers[idx]
            if not br.allow():
                labeled.append(f"{ep['label']}熔断跳过")
                all_timeout = False
                continue
            log.info("LLM路由 role=%s tier=%s model=%s group=%s endpoint=%s",
                     "访客" if visitor else "管理员", tier, model,
                     ep["route_group"], ep["label"])
            payload = _payload(model, system, user, tok, effort, False,
                               ep["route_group"])
            content, ok, err, was_to, rate_limited = _do_chat(
                ep, payload, read_to, max_retries)
            # 限流(429)不喂熔断器：它是「稍后再来」而非「端点坏了」，计入会把健康但
            # 暂时超 TPM 的 key 误熔断 90s，反而加剧其余 key 压力。5xx/超时仍照记。
            if not rate_limited:
                br.record(ok)
            if ok:
                return content
            raw_errs.append(err)
            labeled.append(f"{ep['label']}({model})：{err}")
            if not was_to:
                all_timeout = False

    if not raw_errs:   # 无任何端点尝试：要么全被手动停用，要么全被熔断跳过
        if enabled_count() == 0:
            return ("LLM 请求失败（所有端点均被手动停用，请用 /llm 面板开启至少"
                    "一个端点）")
        if not any(enabled_count_for_model(m) for m in chain):
            groups = "/".join(sorted(
                g for m in chain
                for g in config.llm_route_groups_for_model(m)))
            return (f"LLM 请求失败（模型 {'、'.join(chain)} 的密钥组 "
                    f"{groups} 已全部手动停用）")
        return ("LLM 请求失败（所有端点均处于熔断中，暂无可用端点，"
                "请稍后重试或用 /llm 重置熔断）")
    if len(_ENDPOINTS) == 1:
        return raw_errs[0]          # 单端点部署：原样返回，与改造前文案一致
    if all_timeout:
        return "LLM 超时（全部端点无响应）：" + "；".join(labeled)
    return "LLM 请求失败（全部端点不可用）：" + "；".join(labeled)


# ─── 流式调用（仅首字节前故障转移）──────────────────────────────────────────
def _stream_one(ep: dict, payload: dict, first_byte_to: int, idle_to: int,
                max_retries: int = 0):
    """对单端点发一次流式请求。yield ('delta', 累积全文) / ('done', 全文) /
    ('error', 串) / ('ratelimit', 串)。首字节超时用 monotonic 手动计时（socket 读超时
    取较大值兜底，避免推理模型正常的长首字节/块间静默被过早掐断）；idle_to=0 时禁用块间超时。

    429/5xx 在【发起阶段】按 max_retries 退避重试（此时还没吐任何内容，重试安全）。
    重试用尽仍 429 → yield ('ratelimit', ...)，上层据此【不喂熔断器】直接换下一端点；
    5xx 等真故障仍走 ('error', ...) 正常计入熔断。
    """
    url = f"{ep['base_url']}/chat/completions"
    # socket 读超时：取语义上限的较大者兜底；真正的语义判定由下方 monotonic 检查负责。
    sock_read = max(first_byte_to, idle_to) if idle_to > 0 else max(first_byte_to, 300)
    attempt = 0
    while True:
        try:
            r = requests.post(url, json=payload, headers=_headers(ep),
                              stream=True, timeout=(_CONNECT_TIMEOUT, sock_read))
        except requests.exceptions.Timeout:
            yield ("error", f"LLM 超时（连接/读取 >{sock_read}s）。可稍后重试。")
            return
        except UnicodeEncodeError as e:
            log.error("流式请求头编码失败（key/url 含非 ASCII）: %s", e)
            yield ("error", "LLM_API_KEY 或 LLM_BASE_URL 含非 ASCII 字符（可能复制时"
                            "混入了全角符号/空格）。请检查服务器 .env 后重启。")
            return
        except requests.exceptions.RequestException as e:
            yield ("error", f"LLM 网络错误：{e}")
            return

        if r.status_code == 200:
            break                       # 进入下方流式读取
        # 非 200：429/5xx 可退避重试（尚未吐内容，安全）
        body = r.text[:300]
        if r.status_code == 429 or 500 <= r.status_code < 600:
            attempt += 1
            if attempt <= max_retries:
                time.sleep(min(2 ** attempt, 8))
                continue
            if r.status_code == 429:
                log.warning("端点【%s】流式被限流 429（重试 %d 次仍限流），"
                            "换下一端点、不计入熔断", ep["label"], max_retries)
                yield ("ratelimit", f"LLM 限流 HTTP 429：{body}")
                return
        yield ("error", f"LLM 请求失败 HTTP {r.status_code}：{body}")
        return

    # 强制 UTF-8：部分网关流式响应头不声明 charset，默认 latin-1 会中文乱码。
    r.encoding = "utf-8"
    acc = ""
    reasoning_acc = ""
    finish_reason = None
    usage = None
    start = monotonic()
    last = start
    got_first = False
    try:
        for line in r.iter_lines(decode_unicode=True):
            now = monotonic()
            if not got_first and (now - start) > first_byte_to:
                yield ("error", f"LLM 首字节超时（>{first_byte_to}s 无响应）")
                return
            if got_first and idle_to > 0 and (now - last) > idle_to:
                yield ("error", f"LLM 静默超时（数据块间隔 >{idle_to}s）")
                return
            if not line or not line.startswith("data: "):
                continue
            body = line[6:].strip()
            if body == "[DONE]":
                break
            try:
                d = json.loads(body)
            except json.JSONDecodeError:
                continue
            if d.get("usage"):
                usage = d["usage"]
            choices = d.get("choices") or []
            if not choices:
                continue
            if choices[0].get("finish_reason"):
                finish_reason = choices[0]["finish_reason"]
            delta = choices[0].get("delta") or {}
            # 推理模型的思考过程也算「已开始响应」，用于首字节判定；但不作为正文 yield，
            # 故上层据 delta 事件判定的 produced 仍为 False（空正文可安全故障转移）。
            rc = delta.get("reasoning_content") or delta.get("reasoning") or ""
            if rc:
                reasoning_acc += rc
                if not got_first:
                    got_first = True
                last = now
            content = delta.get("content", "")
            if content:
                got_first = True
                last = now
                acc += content
                yield ("delta", acc)
    except requests.exceptions.Timeout:
        yield ("error", "LLM 超时（读取中断）。可稍后重试。")
        return
    except requests.exceptions.RequestException as e:
        yield ("error", f"LLM 网络错误：{e}")
        return

    if not acc.strip():
        log.error("LLM 空正文 finish_reason=%s usage=%s reasoning_len=%d",
                  finish_reason, usage, len(reasoning_acc))
        hint = ""
        if finish_reason == "length":
            hint = ("（finish_reason=length：推理把输出 token 上限吃光了，正文没产出。"
                    "已建议调高 LLM_MAX_TOKENS 或缩短规则。）")
        elif finish_reason == "content_filter":
            hint = "（finish_reason=content_filter：被内容审查拦截。）"
        elif reasoning_acc.strip():
            hint = ("（只产出了推理内容、无正文，可能输出 token 上限不足或网关吞了"
                    " content 字段。）")
        elif finish_reason:
            hint = f"（finish_reason={finish_reason}）"
        yield ("error", f"LLM 返回空内容{hint}")
        return
    yield ("done", acc.strip())


def stream_chat(system: str, user: str, effort: str = "", tier: str = "heavy",
                max_tokens: int = 0, visitor: bool = False):
    """流式调用，只在【首字节前】跨端点故障转移。yield 与旧 _stream_llm 完全一致：
    ('delta', 累积全文) / ('done', 全文) / ('error', 串)。
    visitor=True 时该档用访客那份选定模型。
    tier ∈ heavy/balanced/light：决定用哪档运行时选定模型（主 SOP=heavy、基本面=balanced）。
    max_tokens 非默认时覆盖（基本面预处理传较小预算，使其也能流式跑、停止按钮低延迟生效）。
    """
    if not available():
        yield ("error", "未配置 LLM_ROUTE_ENDPOINTS，无法分析。请在 .env 配置。")
        return
    st = get_settings()
    first_byte_to = int(st["stream_first_byte_timeout"])
    idle_to = int(st["stream_idle_timeout"])
    tok = max_tokens or config.LLM_MAX_TOKENS

    chain = resolve_model_chain(tier, visitor)
    if not chain:
        yield ("error", chain_error(tier, visitor))
        return

    last_err = None
    for pos, model in enumerate(chain):
        if pos:
            log.warning("模型 %s 的密钥组全部不可用，流式升级到回退模型 %s",
                        chain[pos - 1], model)
        for idx, ep in _endpoints_by_priority(model):
            br = _breakers[idx]
            if not br.allow():
                last_err = f"{ep['label']} 熔断中（已跳过）"
                continue
            log.info("LLM流式路由 role=%s tier=%s model=%s group=%s endpoint=%s",
                     "访客" if visitor else "管理员", tier, model,
                     ep["route_group"], ep["label"])
            payload = _payload(model, system, user, tok, effort, True,
                               ep["route_group"])
            produced = False        # 是否已向用户吐过正文 delta
            for ev in _stream_one(ep, payload, first_byte_to, idle_to,
                                  int(st["max_retries"])):
                if ev[0] == "delta":
                    produced = True
                    yield ev
                elif ev[0] == "done":
                    br.record(True)
                    yield ev
                    return
                elif ev[0] == "ratelimit":
                    # 限流(429)：不喂熔断器（健康 key 只是暂时超 TPM，误熔断会加剧其余
                    # key 压力），直接换下一端点。ratelimit 只在首字节前产生。
                    last_err = ev[1]
                    break
                elif ev[0] == "error":
                    br.record(False)
                    if produced:
                        # 已吐正文再断：不能静默换端点/换模型重来（会重复可见输出），
                        # 直接报错返回。
                        yield ev
                        return
                    last_err = ev[1]
                    break
    if last_err is None and enabled_count() == 0:
        yield ("error", "LLM 全部端点均被手动停用（请用 /llm 面板开启至少一个端点）")
        return
    if last_err is None:
        groups = "/".join(sorted(
            g for m in chain for g in config.llm_route_groups_for_model(m)))
        yield ("error", f"模型 {'、'.join(chain)} 的密钥组 {groups} "
                        "已全部手动停用")
        return
    yield ("error", last_err or "LLM 全部端点不可用（请用 /llm 测试/重置端点）")


# ─── 连通性探针（最小 chat 请求；不计入 Breaker 统计）───────────────────────
# 探针结果留痕：idx → 上次测试结果 dict（含 ts=epoch 秒）。供 /llm 面板显示「上次测试」，
# 让「测过 404 但没进真实流量、熔断状态仍正常」的困惑得到解释。故意与 Breaker 分离——
# 探针是运维主动诊断，不该污染故障转移的错误率统计。
_last_probe: dict[int, dict] = {}
_last_probe_lock = threading.Lock()


def last_probe(idx: int) -> dict | None:
    """取某端点上次探针结果（含 ts epoch 秒）；没测过返回 None。供面板显示。"""
    with _last_probe_lock:
        return _last_probe.get(idx)


def _save_probe(idx: int, res: dict) -> dict:
    """把探针结果留痕（打时间戳），并原样返回 res（便于 return _save_probe(...)）。"""
    import time as _t
    with _last_probe_lock:
        _last_probe[idx] = {**res, "ts": _t.time()}
    return res


def probe(idx: int, which: str = "heavy", *, model: str = "") -> dict:
    """对指定端点发一个最小 chat 请求，测真实连通 + 延迟。
    which ∈ heavy/balanced/light：测哪档——按该档运行时选定模型 + 端点映射解析真实模型名
    （端点有映射时测映射模型，否则测该角色当前档位模型）。
    返回 {ok, http_status, latency_ms, model, req_model, which, error, breaker_state}。
    max_tokens 用 16（而非 1）：部分推理模型对过小预算会 400，16 既够连通判定又极廉价。
    纯诊断——不喂 Breaker，避免健康检查污染故障转移的错误率。

    ⚠️ 假通判定：不再「HTTP 200 就算通」。要求 200 且返回体解析出 choices（有正文或
    有效结构）才判 ✅。200 但无 choices（如网关回错误体/无该模型权限/base_url 缺 /v1）
    → ok=False，标「200 但无补全内容」，让 Anyrouter 那种 53ms 假通当场露馅。
    """
    if not model and which not in config.LLM_TIER_MODELS:
        which = "heavy"
    if not (0 <= idx < len(_ENDPOINTS)):
        return {"ok": False, "http_status": None, "latency_ms": 0,
                "model": "", "req_model": "", "which": which,
                "error": "端点序号越界", "breaker_state": "-"}
    ep = _ENDPOINTS[idx]
    req_model = model or get_tier_model(which, False)
    bstate = _breakers[idx].state
    if not _supports_model(ep, req_model):
        return _save_probe(idx, {
            "ok": False, "skipped": True, "http_status": None,
            "latency_ms": 0, "model": "", "req_model": req_model,
            "which": which,
            "error": "端点未声明支持该模型，未发请求",
            "breaker_state": bstate,
        })
    st = get_settings()
    probe_to = min(30, int(st["non_stream_timeout"]))   # 探针用短超时，不等满
    payload = {"model": req_model,
               "messages": [{"role": "user", "content": "ping"}]}
    _apply_completion_options(payload, req_model, ep["route_group"], 16)
    t0 = monotonic()
    try:
        r = requests.post(f"{ep['base_url']}/chat/completions",
                          json=payload, headers=_headers(ep),
                          timeout=(_CONNECT_TIMEOUT, probe_to))
    except requests.exceptions.Timeout:
        return _save_probe(idx, {"ok": False, "http_status": None,
                "latency_ms": int((monotonic() - t0) * 1000),
                "model": "", "req_model": req_model, "which": which,
                "error": f"超时（>{probe_to}s）", "breaker_state": bstate})
    except requests.exceptions.RequestException as e:
        return _save_probe(idx, {"ok": False, "http_status": None,
                "latency_ms": int((monotonic() - t0) * 1000),
                "model": "", "req_model": req_model, "which": which,
                "error": str(e)[:120], "breaker_state": bstate})
    latency = int((monotonic() - t0) * 1000)
    model = ""
    err = ""
    ok = False
    try:
        data = r.json()
    except ValueError:
        data = None
    if r.status_code == 200:
        # 假通拆穿：200 必须真有 choices 结构才算通
        choices = (data or {}).get("choices") if isinstance(data, dict) else None
        if choices:
            ok = True
            model = str((data.get("model") or req_model))[:40]  # 回显真实响应模型名
        else:
            body = (str(data)[:150] if data is not None else r.text[:150])
            err = (f"HTTP 200 但无补全内容（疑似假通：检查 base_url 是否缺 /v1、"
                   f"或该端点无 {req_model} 权限）：{body}")
    else:
        if isinstance(data, dict):
            err = str(data.get("error", data))[:150]
        else:
            err = r.text[:150]
    return _save_probe(idx, {"ok": ok, "http_status": r.status_code,
            "latency_ms": latency, "model": model, "req_model": req_model,
            "which": which, "error": err, "breaker_state": bstate})


def probe_all(which: str = "heavy") -> list[dict]:
    """对全部端点依次探针，返回每条结果（含 label）。which 透传给 probe。"""
    out = []
    for i in range(len(_ENDPOINTS)):
        res = probe(i, which)
        res["label"] = _ENDPOINTS[i]["label"]
        out.append(res)
    return out


def probe_model(idx: int, model: str) -> dict:
    """对指定端点测试明确模型，用于新密钥组面板；不经过当前档位选择。"""
    return probe(idx, "model", model=model)


def _slot_ready(model: str) -> bool:
    """该模型此刻可派发：其密钥组已配端点。（是否在服役清单另有 *_retired 标记）"""
    return bool(model and configured_count_for_model(model))


def slot_snapshot() -> list[dict]:
    """12 个槽位的模型链快照（不含 key），供面板/探针/测试展示。"""
    out: list[dict] = []
    for tier, spec in config.LLM_TIER_MODELS.items():
        for visitor in (False, True):
            primary = get_tier_model(tier, visitor)
            fallback = get_fallback_model(tier, visitor)
            out.append({
                "tier": tier,
                "label": spec.get("label", tier),
                "role": "visitor" if visitor else "admin",
                "primary": primary,
                "primary_ready": _slot_ready(primary),
                "primary_retired": bool(
                    primary and not config.llm_model_registered(primary)),
                "fallback": fallback,
                "fallback_ready": _slot_ready(fallback),
                "fallback_retired": bool(
                    fallback and not config.llm_model_registered(fallback)),
                "chain": resolve_model_chain(tier, visitor),
            })
    return out


def routing_snapshot() -> dict:
    """返回不含密钥的静态路由快照，供部署前检查和测试。"""
    group_counts = {
        group: sum(1 for ep in _ENDPOINTS if ep["route_group"] == group)
        for group in config.LLM_ROUTE_GROUPS
    }
    model_routes = {
        model: list(config.llm_route_groups_for_model(model))
        for model in config.LLM_MODELS
    }
    return {
        "issues": routing_issues(),
        "group_counts": group_counts,
        "model_routes": model_routes,
        "slots": slot_snapshot(),
        "endpoints": endpoints(),
    }


if __name__ == "__main__":
    snapshot = routing_snapshot()
    print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    raise SystemExit(1 if snapshot["issues"] else 0)
