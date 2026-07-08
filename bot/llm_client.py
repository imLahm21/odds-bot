"""
LLM 端点池 —— 多端点故障转移 + 每端点熔断器 + 连通性探针

analyzer.py 只负责构造 prompt，真正的 HTTP 调用、重试、失败隔离全在这里。
设计对标 cc switch 的「多供应商 + 熔断」思路，落到本项目的既有模式：

  - 多端点来自 .env（secret 不落库）：主 LLM_BASE_URL/LLM_API_KEY 为 0 号端点，
    LLM_ENDPOINTS 追加更多（key|base_url|标签，base_url 省略则复用主端点 URL）。
    多 key 轮换/标记坏点/切换的模式移植自 api_client.py 的 _switch_key/api_get。
  - 每端点一个内存态 Breaker（CLOSED→OPEN→HALF_OPEN→CLOSED），9 个可调参数
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
def _parse_model_map(spec: str) -> dict:
    """解析端点第 4 段「重模型:轻模型」→ {"heavy": ..., "light": ...}。
      - 空/缺省 → {}（该端点不映射，两档都用全局默认）
      - "gpt-5.5:gpt-5-codex" → 重档映射 gpt-5.5、轻档映射 gpt-5-codex
      - "gpt-5.5"（无冒号）→ 只映射重档，轻档不变（{"heavy": "gpt-5.5"}）
      - ":gpt-5-codex"（重档留空）→ 只映射轻档
    """
    spec = (spec or "").strip()
    if not spec:
        return {}
    heavy, _, light = spec.partition(":")
    m: dict = {}
    heavy, light = heavy.strip(), light.strip()
    if heavy:
        m["heavy"] = heavy
    if light:
        m["light"] = light
    return m


def _parse_endpoints() -> list[dict]:
    """主端点 = LLM_BASE_URL/LLM_API_KEY（0 号，向后兼容）。
    追加端点 = LLM_ENDPOINTS，逗号或换行分隔，每条 `key|base_url|标签|重模型:轻模型`：
      - base_url 省略 → 复用主端点 URL（用户「通常同一 base_url」的场景）
      - 标签省略 → 自动编号「端点N」
      - 第 4 段（模型映射）省略 → 不映射，两档都用全局默认（向后兼容）
    条数不限（想加几条加几条）。按 (key, base_url) 去重，避免同一端点被重复探测/统计。
    """
    main_url = clean_header_value(os.getenv("LLM_BASE_URL", "")).rstrip("/")
    main_key = clean_header_value(os.getenv("LLM_API_KEY", ""))
    eps: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def _add(key: str, url: str, label: str, model_map: dict) -> None:
        if not (key and url):
            return
        sig = (key, url)
        if sig in seen:
            return
        seen.add(sig)
        eps.append({"key": key, "base_url": url, "label": label,
                    "model_map": model_map})

    if main_url and main_key:
        _add(main_key, main_url, "主端点", {})   # 主端点默认不映射

    raw = os.getenv("LLM_ENDPOINTS", "")
    for item in re.split(r"[,\n]", raw):
        item = item.strip()
        if not item:
            continue
        parts = [p.strip() for p in item.split("|")]
        key = clean_header_value(parts[0]) if parts and parts[0] else ""
        url = (clean_header_value(parts[1]).rstrip("/")
               if len(parts) > 1 and parts[1] else main_url)
        label = parts[2] if len(parts) > 2 and parts[2] else f"端点{len(eps) + 1}"
        model_map = _parse_model_map(parts[3]) if len(parts) > 3 else {}
        _add(key, url, label, model_map)
    return eps


_ENDPOINTS: list[dict] = _parse_endpoints()


def _sig(ep: dict) -> str:
    """端点签名（label|base_url，不含 key），作 DB 开关状态的稳定主键——
    不依赖易变的数组下标，增删端点后仍能对上原来的开关记录。"""
    return f"{ep['label']}|{ep['base_url']}"


# 轻档逻辑模型名集合（走地/基本面/SEO 都用同名 gpt-5.4-mini，用 set 兼容将来分化）
_LIGHT_MODELS = {config.LLM_LIVE_MODEL, config.FUND_ANALYZE_MODEL}


def _resolve_model(logical: str, ep: dict) -> str:
    """把调用方传的【逻辑模型名】按该端点的映射翻译成【真实模型名】。
      - 端点无映射（无第 4 段）→ 原样返回 logical。
      - logical == config.LLM_MODEL（重档）→ 用映射 heavy（无则原样）。
      - logical ∈ 轻档集合 → 用映射 light（无则原样）。
      - 其它（调用方显式传了别的模型）→ 原样返回，稳妥兜底。
    例：Anyrouter 映射 {heavy:gpt-5.5, light:gpt-5-codex}，
        _resolve_model("gpt-5.4-mini", anyrouter) == "gpt-5-codex"。
    """
    mm = ep.get("model_map") or {}
    if not mm:
        return logical
    if logical == config.LLM_MODEL:
        return mm.get("heavy", logical)
    if logical in _LIGHT_MODELS:
        return mm.get("light", logical)
    return logical


def available() -> bool:
    """至少有一个可用端点（key + base_url 齐全）。"""
    return bool(_ENDPOINTS)


def endpoints() -> list[dict]:
    """只读端点列表（label/base_url/model_map，不含 key）供 TG 面板展示。"""
    return [{"label": e["label"], "base_url": e["base_url"],
             "model_map": e.get("model_map") or {}} for e in _ENDPOINTS]


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
            return db.get_disabled_endpoints(conn)
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


def set_enabled(idx: int, enabled: bool) -> bool:
    """手动开/关指定端点并落库，刷新缓存。越界返回 False。"""
    if not (0 <= idx < len(_ENDPOINTS)):
        return False
    try:
        conn = db.get_conn()
        try:
            db.set_endpoint_disabled(conn, _sig(_ENDPOINTS[idx]), not enabled)
        finally:
            conn.close()
    except Exception as e:
        log.warning("写端点开关失败 idx=%d: %s", idx, e)
        return False
    reload_endpoint_state()
    return True


# ─── 9 个可调参数缓存（DB 懒加载，TG 改后 reload_settings 失效）──────────────
_settings_cache: dict[str, float] | None = None
_settings_lock = threading.Lock()


def _load_settings() -> dict[str, float]:
    """从 db.llm_settings 读 9 参数；DB 未初始化/异常时回退 config 默认，
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
    """取 9 参数（进程内缓存，首次访问懒加载）。走地 1min 循环高频调用，走缓存。"""
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
def _payload(model: str, system: str, user: str, max_tokens: int,
             effort: str, stream: bool) -> dict:
    p = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
    }
    if effort:
        p["reasoning_effort"] = effort
    if stream:
        p["stream"] = True
    return p


def _headers(ep: dict) -> dict:
    return {"Authorization": f"Bearer {ep['key']}", "Content-Type": "application/json"}


_CONNECT_TIMEOUT = 10   # 连接超时（秒），与读超时分开


# ─── 阻塞调用（带端点内重试 + 跨端点故障转移）────────────────────────────────
def _do_chat(ep: dict, payload: dict, read_to: int,
             max_retries: int) -> tuple[str | None, bool, str, bool]:
    """对单端点发一次非流式请求（含端点内重试）。
    返回 (正文 or None, 成功?, 错误串, 是否超时类)。错误串沿用 analyzer 旧格式。
    重试仅针对瞬时错误（网络/超时/429/5xx）；4xx / 空内容 / 无 choices 为确定性
    错误，不在同端点重试（重试同请求结果相同），直接判失败交由上层切下一端点。
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
                return None, False, f"LLM 超时（>{read_to}s）。推理较慢，可稍后重试。", True
            time.sleep(min(2 ** attempt, 8))
            continue
        except UnicodeEncodeError as e:
            log.error("请求头编码失败（key/url 含非 ASCII）: %s", e)
            return None, False, ("LLM_API_KEY 或 LLM_BASE_URL 含非 ASCII 字符（可能"
                                 "复制时混入了全角符号/空格）。请检查服务器 .env 后重启。"), False
        except requests.exceptions.RequestException as e:
            attempt += 1
            if attempt > max_retries:
                return None, False, f"LLM 网络错误：{e}", False
            time.sleep(min(2 ** attempt, 8))
            continue

        if r.status_code != 200:
            body = r.text[:300]
            if r.status_code == 429 or 500 <= r.status_code < 600:
                attempt += 1
                if attempt > max_retries:
                    return None, False, f"LLM 请求失败 HTTP {r.status_code}：{body}", False
                time.sleep(min(2 ** attempt, 8))
                continue
            # 4xx（400/401/403 等）确定性错误：不重试，直接切端点
            return None, False, f"LLM 请求失败 HTTP {r.status_code}：{body}", False

        try:
            data = r.json()
        except ValueError:
            return None, False, f"LLM 请求失败 HTTP 200：响应非 JSON：{r.text[:200]}", False
        choices = data.get("choices", [])
        if not choices:
            return None, False, f"LLM 返回无 choices：{str(data)[:300]}", False
        content = choices[0].get("message", {}).get("content", "").strip()
        if not content:
            return None, False, "LLM 返回空内容", False
        return content, True, "", False


# 选路优先级：正常/半开端点优先，降级端点次之（仍用但靠后），OPEN 交由 allow() 冷却门控。
# 手动停用端点直接排除。返回按优先级排好序的 [(idx, ep)]，调用方仍需逐个 br.allow()。
_STATE_PRIORITY = {"CLOSED": 0, "HALF_OPEN": 0, "OPEN": 1, "DEGRADED": 2}


def _endpoints_by_priority() -> list[tuple[int, dict]]:
    """未手动停用的端点按选路优先级排序：正常/半开(0) < OPEN(1) < 降级(2)。
    稳定排序，同优先级保持 .env 原顺序（多 key 冗余的先后不被打乱）。
    降级端点排最后——健康端点先跑，降级的只在健康端点都用不上时才轮到。"""
    disabled = _get_disabled()
    cands = [(i, ep) for i, ep in enumerate(_ENDPOINTS)
             if _sig(ep) not in disabled]
    return sorted(cands, key=lambda t: _STATE_PRIORITY.get(
        _breakers[t[0]].state, 1))


def chat(system: str, user: str, effort: str = "",
         model: str = "", timeout: int = 0, max_tokens: int = 0) -> str:
    """阻塞式调用，按选路优先级故障转移（正常端点先、降级端点后）。
    失败返回错误说明串（不抛异常）。
    timeout/model/max_tokens 非默认时覆盖 config（走地/基本面/SEO 各传自己的短超时）；
    未显式传 timeout 时用 DB 的 non_stream_timeout。
    """
    if not available():
        return "未配置 LLM_BASE_URL / LLM_API_KEY，无法分析。请在 .env 配置。"
    st = get_settings()
    read_to = int(timeout or st["non_stream_timeout"])
    max_retries = int(st["max_retries"])
    logical = model or config.LLM_MODEL        # 逻辑模型名，选到端点后按映射翻译
    tok = max_tokens or config.LLM_MAX_TOKENS

    raw_errs: list[str] = []      # 各端点原始错误串（单端点时原样返回，保持旧文案）
    labeled: list[str] = []       # 带端点标签的错误（多端点聚合展示）
    all_timeout = True            # 是否全部端点都是超时类失败
    for idx, ep in _endpoints_by_priority():   # 正常端点先、降级端点后
        br = _breakers[idx]
        if not br.allow():
            labeled.append(f"{ep['label']}熔断跳过")
            all_timeout = False
            continue
        # 每端点按映射翻译模型名再构造 payload（Anyrouter 轻档→gpt-5-codex 等）
        payload = _payload(_resolve_model(logical, ep), system, user,
                           tok, effort, False)
        content, ok, err, was_to = _do_chat(ep, payload, read_to, max_retries)
        br.record(ok)
        if ok:
            return content
        raw_errs.append(err)
        labeled.append(f"{ep['label']}：{err}")
        if not was_to:
            all_timeout = False

    if not raw_errs:   # 无任何端点尝试：要么全被手动停用，要么全被熔断跳过
        if enabled_count() == 0:
            return ("LLM 请求失败（所有端点均被手动停用，请用 /llm 面板开启至少"
                    "一个端点）")
        return ("LLM 请求失败（所有端点均处于熔断中，暂无可用端点，"
                "请稍后重试或用 /llm 重置熔断）")
    if len(_ENDPOINTS) == 1:
        return raw_errs[0]          # 单端点部署：原样返回，与改造前文案一致
    if all_timeout:
        return "LLM 超时（全部端点无响应）：" + "；".join(labeled)
    return "LLM 请求失败（全部端点不可用）：" + "；".join(labeled)


# ─── 流式调用（仅首字节前故障转移）──────────────────────────────────────────
def _stream_one(ep: dict, payload: dict, first_byte_to: int, idle_to: int):
    """对单端点发一次流式请求。yield ('delta', 累积全文) / ('done', 全文) /
    ('error', 串)。首字节超时用 monotonic 手动计时（socket 读超时取较大值兜底，
    避免推理模型正常的长首字节/块间静默被过早掐断）；idle_to=0 时禁用块间超时。
    """
    url = f"{ep['base_url']}/chat/completions"
    # socket 读超时：取语义上限的较大者兜底；真正的语义判定由下方 monotonic 检查负责。
    sock_read = max(first_byte_to, idle_to) if idle_to > 0 else max(first_byte_to, 300)
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

    if r.status_code != 200:
        yield ("error", f"LLM 请求失败 HTTP {r.status_code}：{r.text[:300]}")
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
            hint = ("（finish_reason=length：推理把 max_tokens 吃光了，正文没产出。"
                    "已建议调高 LLM_MAX_TOKENS 或缩短规则。）")
        elif finish_reason == "content_filter":
            hint = "（finish_reason=content_filter：被内容审查拦截。）"
        elif reasoning_acc.strip():
            hint = ("（只产出了推理内容、无正文，可能 max_tokens 不足或网关吞了"
                    " content 字段。）")
        elif finish_reason:
            hint = f"（finish_reason={finish_reason}）"
        yield ("error", f"LLM 返回空内容{hint}")
        return
    yield ("done", acc.strip())


def stream_chat(system: str, user: str, effort: str = "",
                model: str = "", max_tokens: int = 0):
    """流式调用，只在【首字节前】跨端点故障转移。yield 与旧 _stream_llm 完全一致：
    ('delta', 累积全文) / ('done', 全文) / ('error', 串)。
    model/max_tokens 非默认时覆盖（基本面预处理传轻档 gpt-5.4-mini + 较小预算，
    使其也能流式跑、令停止按钮低延迟生效）；未传则原样用重档 gpt-5.5 + 全局预算。
    """
    if not available():
        yield ("error", "未配置 LLM_BASE_URL / LLM_API_KEY，无法分析。请在 .env 配置。")
        return
    st = get_settings()
    first_byte_to = int(st["stream_first_byte_timeout"])
    idle_to = int(st["stream_idle_timeout"])
    logical = model or config.LLM_MODEL        # 逻辑模型名，选到端点后按映射翻译
    tok = max_tokens or config.LLM_MAX_TOKENS

    last_err = None
    for idx, ep in _endpoints_by_priority():   # 正常端点先、降级端点后
        br = _breakers[idx]
        if not br.allow():
            last_err = f"{ep['label']} 熔断中（已跳过）"
            continue
        # 主 SOP 走重档、基本面走轻档；每端点按映射翻译（Anyrouter 兜底翻译真实模型名）
        payload = _payload(_resolve_model(logical, ep), system, user,
                           tok, effort, True)
        produced = False        # 是否已向用户吐过正文 delta
        failed_pre = False      # 首字节前失败 → 可切下一端点
        for ev in _stream_one(ep, payload, first_byte_to, idle_to):
            if ev[0] == "delta":
                produced = True
                yield ev
            elif ev[0] == "done":
                br.record(True)
                yield ev
                return
            elif ev[0] == "error":
                br.record(False)
                if produced:
                    # 已吐正文再断：不能静默换端点重来（会重复可见输出），直接报错。
                    yield ev
                    return
                last_err = ev[1]
                failed_pre = True
                break
        if failed_pre:
            continue   # 首字节前失败，尝试下一端点
    if last_err is None and enabled_count() == 0:
        yield ("error", "LLM 全部端点均被手动停用（请用 /llm 面板开启至少一个端点）")
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


def probe(idx: int, which: str = "heavy") -> dict:
    """对指定端点发一个最小 chat 请求，测真实连通 + 延迟。
    which='heavy' 测重档逻辑模型（config.LLM_MODEL，如 gpt-5.5），'light' 测轻档
    （config.LLM_LIVE_MODEL，如 gpt-5.4-mini）；都经该端点映射翻译成真实模型名
    （Anyrouter 测重→gpt-5.5、测轻→gpt-5-codex）。
    返回 {ok, http_status, latency_ms, model, req_model, which, error, breaker_state}。
    max_tokens 用 16（而非 1）：部分推理模型对过小预算会 400，16 既够连通判定又极廉价。
    纯诊断——不喂 Breaker，避免健康检查污染故障转移的错误率。

    ⚠️ 假通判定：不再「HTTP 200 就算通」。要求 200 且返回体解析出 choices（有正文或
    有效结构）才判 ✅。200 但无 choices（如网关回错误体/无该模型权限/base_url 缺 /v1）
    → ok=False，标「200 但无补全内容」，让 Anyrouter 那种 53ms 假通当场露馅。
    """
    if not (0 <= idx < len(_ENDPOINTS)):
        return {"ok": False, "http_status": None, "latency_ms": 0,
                "model": "", "req_model": "", "which": which,
                "error": "端点序号越界", "breaker_state": "-"}
    ep = _ENDPOINTS[idx]
    logical = config.LLM_LIVE_MODEL if which == "light" else config.LLM_MODEL
    req_model = _resolve_model(logical, ep)
    st = get_settings()
    probe_to = min(30, int(st["non_stream_timeout"]))   # 探针用短超时，不等满
    payload = {"model": req_model,
               "messages": [{"role": "user", "content": "ping"}],
               "max_tokens": 16}
    bstate = _breakers[idx].state
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
