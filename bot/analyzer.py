"""
LLM 精算 —— 读全量 SOP 规则 + 调 IKuncode(OpenAI 兼容) chat/completions

- 规则文件进程内缓存（启动读一次）
- 用 requests 直接打 /v1/chat/completions，不依赖 openai SDK
- gpt-5.5 是推理模型，不传 temperature（最稳）
"""

import os
import logging

import requests
from dotenv import load_dotenv

from . import config

load_dotenv()
log = logging.getLogger("odds_bot.analyzer")

LLM_BASE_URL = os.getenv("LLM_BASE_URL", "").strip().rstrip("/")
LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip()

_rules_cache: str | None = None


def load_rules() -> str:
    """读取并拼接全量规则文件，模块级缓存。"""
    global _rules_cache
    if _rules_cache is not None:
        return _rules_cache
    parts = []
    for rel in config.ANALYZE_RULE_FILES:
        try:
            with open(rel, encoding="utf-8") as f:
                parts.append(f"\n\n===== {rel} =====\n{f.read()}")
        except FileNotFoundError:
            log.warning("规则文件缺失，跳过: %s", rel)
    _rules_cache = "".join(parts)
    log.info("规则已加载，共 %d 字符", len(_rules_cache))
    return _rules_cache


def available() -> bool:
    return bool(LLM_BASE_URL and LLM_API_KEY)


def _call_llm(system: str, user: str) -> str:
    """统一的 chat/completions 调用 + 错误处理；失败返回错误说明串。"""
    payload = {
        "model": config.LLM_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": config.LLM_MAX_TOKENS,
    }
    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        r = requests.post(f"{LLM_BASE_URL}/chat/completions",
                          json=payload, headers=headers,
                          timeout=config.LLM_TIMEOUT)
        if r.status_code != 200:
            log.error("LLM HTTP %s: %s", r.status_code, r.text[:500])
            return f"LLM 请求失败 HTTP {r.status_code}：{r.text[:300]}"
        data = r.json()
        choices = data.get("choices", [])
        if not choices:
            return f"LLM 返回无 choices：{str(data)[:300]}"
        return choices[0].get("message", {}).get("content", "").strip() \
            or "LLM 返回空内容"
    except requests.exceptions.Timeout:
        return f"LLM 超时（>{config.LLM_TIMEOUT}s）。gpt-5.5 推理较慢，可稍后重试。"
    except requests.exceptions.RequestException as e:
        log.error("LLM 网络错误: %s", e)
        return f"LLM 网络错误：{e}"


def _stream_llm(system: str, user: str):
    """流式 chat/completions。逐增量 yield ('delta', 累积全文)；
    正常结束 yield ('done', 全文)，出错 yield ('error', 错误串)。
    """
    import json
    payload = {
        "model": config.LLM_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": config.LLM_MAX_TOKENS,
        "stream": True,
    }
    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
    }
    acc = ""
    try:
        r = requests.post(f"{LLM_BASE_URL}/chat/completions",
                          json=payload, headers=headers, stream=True,
                          timeout=config.LLM_TIMEOUT)
        if r.status_code != 200:
            body = r.text[:300]
            log.error("LLM HTTP %s: %s", r.status_code, body)
            yield ("error", f"LLM 请求失败 HTTP {r.status_code}：{body}")
            return
        for line in r.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            body = line[6:].strip()
            if body == "[DONE]":
                break
            try:
                d = json.loads(body)
            except json.JSONDecodeError:
                continue
            delta = (d.get("choices", [{}])[0].get("delta", {})
                     .get("content", ""))
            if delta:
                acc += delta
                yield ("delta", acc)
        if not acc.strip():
            yield ("error", "LLM 返回空内容")
            return
        yield ("done", acc.strip())
    except requests.exceptions.Timeout:
        yield ("error", f"LLM 超时（>{config.LLM_TIMEOUT}s）。"
                        f"gpt-5.5 推理较慢，可稍后重试。")
    except requests.exceptions.RequestException as e:
        log.error("LLM 流式网络错误: %s", e)
        yield ("error", f"LLM 网络错误：{e}")


def _analyze_prompts(csv_text: str, fundamentals: str,
                     home: str, away: str, league: str) -> tuple[str, str]:
    """构造精算的 (system, user) prompt，供阻塞版与流式版共用。"""
    system = (
        load_rules()
        + "\n\n===== 任务 =====\n"
        "你是拥有20年经验的庄家操盘手和数据精算师。严格按上述 SOP 文档的"
        "步骤1~7执行分析，按文档「输出格式」章节的结构输出完整精算报告。"
        "盘口数据为 CSV，基本面为文本。注意：基本面来自 API-Football，"
        "无99家终指数据，不要编造终指，按战绩/比分/排名/交锋综合加权。"
    )
    user = (
        f"## 比赛：{home} vs {away}\n## 联赛：{league}\n\n"
        f"### 盘口快照（CSV）\n{csv_text}\n\n"
        f"### 基本面\n{fundamentals}\n"
    )
    return system, user


def analyze(csv_text: str, fundamentals: str,
            home: str, away: str, league: str) -> str:
    """调 LLM 跑精算 SOP，返回报告文本；失败返回错误说明串。"""
    if not available():
        return "未配置 LLM_BASE_URL / LLM_API_KEY，无法分析。请在 .env 配置。"
    system, user = _analyze_prompts(csv_text, fundamentals, home, away, league)
    return _call_llm(system, user)


# SOP 报告主段标题 → 进度阶段名（按 ### N. 数字识别，子段 1b/1c/1d 不计）
_STAGE_NAMES = {
    1: "数据提取",
    2: "盘口定性",
    3: "资金流向与热度",
    4: "操盘手法匹配",
    5: "风控验证",
    6: "缺失节点预测",
    7: "最终精算结论",
}
_TOTAL_STAGES = 7


def analyze_stream(csv_text: str, fundamentals: str,
                   home: str, away: str, league: str):
    """流式精算。yield 进度/结果事件，供 bot 实时播报：
      ('stage', n, 阶段名)  —— 模型开始写第 n 段（n=1..7）
      ('done', 完整报告)
      ('error', 错误串)
    阶段识别：检测累积全文里新出现的 `### N.` 主段标题。
    """
    import re
    if not available():
        yield ("error", "未配置 LLM_BASE_URL / LLM_API_KEY，无法分析。请在 .env 配置。")
        return
    system, user = _analyze_prompts(csv_text, fundamentals, home, away, league)
    # 匹配行首的 "### 3." / "###3." 等主段标题，捕获段号
    head_re = re.compile(r"(?m)^#{2,3}\s*(\d+)\s*[\.、]")
    seen: set[int] = set()
    for kind, payload in _stream_llm(system, user):
        if kind == "delta":
            for m in head_re.finditer(payload):
                n = int(m.group(1))
                if n in _STAGE_NAMES and n not in seen:
                    seen.add(n)
                    yield ("stage", n, _STAGE_NAMES[n])
        elif kind == "done":
            yield ("done", payload)
        elif kind == "error":
            yield ("error", payload)


def review(csv_text: str, result_text: str,
           home: str, away: str, league: str) -> str:
    """已结束比赛的事后复盘：盘口全程走势 + 实际结果 → 信号有效性归因。

    与 analyze 区别：这是赛后复盘而非赛前预测，不喂基本面、不读旧报告，
    专注「盘口走势事前能多大程度预示此结果、哪些信号准/误导」。
    """
    if not available():
        return "未配置 LLM_BASE_URL / LLM_API_KEY，无法复盘。请在 .env 配置。"

    system = (
        load_rules()
        + "\n\n===== 任务（赛后复盘，非赛前预测）=====\n"
        "你是拥有20年经验的庄家操盘手和数据精算师。现在对一场【已结束】的比赛"
        "做事后复盘。已知该场全程盘口走势（CSV）与最终比分。请对照上述规则库的"
        "军规、动态走势形态、凯利/返还率判别与既有实战教训，回放并检验盘口信号。"
        "注意：本次复盘只依据盘口走势 + 实际结果，不使用基本面，也不要编造终指。\n\n"
        "严格按以下结构输出复盘报告：\n"
        "## 复盘：[主队] [比分] [客队]\n"
        "## 赛事：[联赛]  开球：[CST]\n\n"
        "### 1. 实际结果\n"
        "- 全场比分 / 半场比分（如有加时·点球一并列出）\n"
        "- 胜平负：[主胜/平/客胜]；总进球数与大小球倾向\n\n"
        "### 2. 盘口结算回放\n"
        "- 主流亚盘主盘口（如 -0.75）最终结算：上盘[赢/输/走水]，并说明赢半/输半\n"
        "- 关键节点其它盘口的结算结果\n\n"
        "### 3. 全程走势复核\n"
        "- 变盘路径回放（让球/水位/欧赔的时间线）\n"
        "- 庄家赛前意图 vs 实际结果：是否兑现（诱上/诱下/阻盘/降赔是否奏效）\n"
        "- 形态（给水/阻上/诱上）事后定性是否成立\n\n"
        "### 4. 信号有效性复盘\n"
        "- 正确信号：哪些变盘/凯利/水位/欧赔信号正确预示了结果\n"
        "- 误导信号：哪些是噪音或反向\n"
        "- 凯利/返还率事后检验（报警是否兑现）\n\n"
        "### 5. 经验教训\n"
        "- 本场印证/修正了哪条军规或既有教训（引用规则库编号）\n"
        "- 可沉淀的防错提醒\n\n"
        "### 6. 盘口指示强度评分\n"
        "- 盘口对结果的预示强度：[0~100]（事前仅凭盘口能多大程度预判此结果）\n"
        "- 一句话总结\n"
    )
    user = (
        f"## 比赛：{home} vs {away}\n## 联赛：{league}\n\n"
        f"### 全程盘口快照（CSV）\n{csv_text}\n\n"
        f"### 最终结果\n{result_text}\n"
    )
    return _call_llm(system, user)
