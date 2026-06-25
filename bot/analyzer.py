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


def _clean_header_value(raw: str) -> str:
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


LLM_BASE_URL = _clean_header_value(os.getenv("LLM_BASE_URL", "")).rstrip("/")
LLM_API_KEY = _clean_header_value(os.getenv("LLM_API_KEY", ""))

_rules_cache: str | None = None
_live_rules_cache: str | None = None


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


def load_live_rules() -> str:
    """读取走地规则文件，独立缓存(不混进赛前 SOP 规则)。"""
    global _live_rules_cache
    if _live_rules_cache is not None:
        return _live_rules_cache
    parts = []
    for rel in config.LIVE_RULE_FILES:
        try:
            with open(rel, encoding="utf-8") as f:
                parts.append(f"\n\n===== {rel} =====\n{f.read()}")
        except FileNotFoundError:
            log.warning("走地规则文件缺失，跳过: %s", rel)
    _live_rules_cache = "".join(parts)
    log.info("走地规则已加载，共 %d 字符", len(_live_rules_cache))
    return _live_rules_cache


def available() -> bool:
    return bool(LLM_BASE_URL and LLM_API_KEY)


def _call_llm(system: str, user: str, effort: str = "") -> str:
    """统一的 chat/completions 调用 + 错误处理；失败返回错误说明串。
    effort 非空时附带 reasoning_effort（low/medium/high/xhigh）；空则不传（旧行为）。
    """
    payload = {
        "model": config.LLM_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": config.LLM_MAX_TOKENS,
    }
    if effort:
        payload["reasoning_effort"] = effort
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
    except UnicodeEncodeError as e:
        log.error("LLM 请求头编码失败（key/url 含非 ASCII 字符）: %s", e)
        return ("LLM_API_KEY 或 LLM_BASE_URL 含非 ASCII 字符（可能复制时混入了"
                "全角符号/空格）。请检查服务器 .env 后重启。")
    except requests.exceptions.RequestException as e:
        log.error("LLM 网络错误: %s", e)
        return f"LLM 网络错误：{e}"


def _stream_llm(system: str, user: str, effort: str = ""):
    """流式 chat/completions。逐增量 yield ('delta', 累积全文)；
    正常结束 yield ('done', 全文)，出错 yield ('error', 错误串)。
    effort 非空时附带 reasoning_effort（low/medium/high/xhigh）；空则不传。
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
    if effort:
        payload["reasoning_effort"] = effort
    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
    }
    acc = ""
    reasoning_acc = ""          # 推理模型把思考放 delta.reasoning_content
    finish_reason = None
    usage = None
    try:
        r = requests.post(f"{LLM_BASE_URL}/chat/completions",
                          json=payload, headers=headers, stream=True,
                          timeout=config.LLM_TIMEOUT)
        if r.status_code != 200:
            body = r.text[:300]
            log.error("LLM HTTP %s: %s", r.status_code, body)
            yield ("error", f"LLM 请求失败 HTTP {r.status_code}：{body}")
            return
        # 强制 UTF-8 解码：部分网关流式响应头不声明 charset，requests 会
        # 默认按 latin-1 解 iter_lines(decode_unicode=True) → 中文乱码。
        r.encoding = "utf-8"
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
            if d.get("usage"):
                usage = d["usage"]
            # 末尾常有只带 usage、choices 为空的收尾帧；choices[0] 前必须判空
            choices = d.get("choices") or []
            if not choices:
                continue
            if choices[0].get("finish_reason"):
                finish_reason = choices[0]["finish_reason"]
            delta = choices[0].get("delta") or {}
            # 推理模型的思考过程：常见字段名 reasoning_content / reasoning
            reasoning_acc += (delta.get("reasoning_content")
                              or delta.get("reasoning") or "")
            content = delta.get("content", "")
            if content:
                acc += content
                yield ("delta", acc)
        if not acc.strip():
            # 空正文：把能拿到的诊断信息全记下来，定位是 length(推理吃光额度)/
            # content_filter(被审查)/还是只产出了推理无正文。
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
    except requests.exceptions.Timeout:
        yield ("error", f"LLM 超时（>{config.LLM_TIMEOUT}s）。"
                        f"gpt-5.5 推理较慢，可稍后重试。")
    except UnicodeEncodeError as e:
        log.error("LLM 请求头编码失败（key/url 含非 ASCII 字符）: %s", e)
        yield ("error", "LLM_API_KEY 或 LLM_BASE_URL 含非 ASCII 字符（可能复制时"
                        "混入了全角符号/空格）。请检查服务器 .env 后重启。")
    except requests.exceptions.RequestException as e:
        log.error("LLM 流式网络错误: %s", e)
        yield ("error", f"LLM 网络错误：{e}")


def _analyze_prompts(csv_text: str, fundamentals: str,
                     home: str, away: str, league: str,
                     extra_instruction: str = "") -> tuple[str, str]:
    """构造精算的 (system, user) prompt，供阻塞版与流式版共用。

    extra_instruction: 用户自定义侧重，非空时追加到标准任务说明之后；
    明确要求不违背 SOP 与输出格式，以保护进度条依赖的 ### 1~7 段落结构。
    """
    system = (
        load_rules()
        + "\n\n===== 任务 =====\n"
        "你是拥有20年经验的庄家操盘手和数据精算师。严格按上述 SOP 文档的"
        "步骤1~7执行分析，按文档「输出格式」章节的结构输出完整精算报告。"
        "盘口数据为 CSV，基本面为文本（含两队近10场、历史交锋、未来5场赛程、积分榜）。"
        "注意：基本面来自 API-Football，不要编造数据，"
        "按战绩/比分/排名/交锋综合加权。"
    )
    if extra_instruction.strip():
        system += (
            "\n\n===== 用户额外侧重 =====\n"
            "在不违背上述 SOP 步骤与「输出格式」章节结构（必须保留 ### 1~7 各段标题）"
            "的前提下，优先满足以下用户要求：\n" + extra_instruction.strip()
        )
    user = (
        f"## 比赛：{home} vs {away}\n## 联赛：{league}\n\n"
        f"### 盘口快照（CSV）\n{csv_text}\n\n"
        f"### 基本面\n{fundamentals}\n"
    )
    return system, user


def analyze(csv_text: str, fundamentals: str,
            home: str, away: str, league: str,
            extra_instruction: str = "", effort: str = "") -> str:
    """调 LLM 跑精算 SOP，返回报告文本；失败返回错误说明串。"""
    if not available():
        return "未配置 LLM_BASE_URL / LLM_API_KEY，无法分析。请在 .env 配置。"
    system, user = _analyze_prompts(csv_text, fundamentals, home, away, league,
                                    extra_instruction)
    return _call_llm(system, user, effort)


def live_brief(live_lines: str, deltas: list[str], home: str, away: str,
               elapsed, score: str) -> str:
    """走地实时研判(非流式，要快)。喂走地规则库 + 当前主盘口走势 + 异动 + 比分分钟，
    让 LLM 给一段简短研判(看大/看小/看反超/封盘观望)。失败返回错误串。
    走地不分 7 段(那是赛前)，只要一段结论。"""
    if not available():
        return ""   # 未配置 LLM 时静默(推送仍会带盘口快报)
    system = (
        load_live_rules()
        + "\n\n===== 任务(走地实时研判) =====\n"
        "你是拥有20年经验的走地操盘手。下面是一场【进行中】比赛的实时滚球盘口与"
        "刚检测到的异动。请严格依据上述走地规则，给出一段【简短】研判(3~5句话即可，"
        "不要分段、不要套赛前7步格式)：当前盘口在暗示什么(看大/看小/看反超/看封盘观望)、"
        "异动的操盘含义、以及接下来值得关注的方向。数据缺失不要编造。"
    )
    user = (
        f"## 比赛：{home} vs {away}\n"
        f"## 当前：第 {elapsed} 分钟，比分 {score}\n\n"
        f"### 检测到的异动\n" + "\n".join(f"- {d}" for d in deltas) + "\n\n"
        f"### 当前走地主盘口\n{live_lines}\n"
    )
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
                   home: str, away: str, league: str,
                   extra_instruction: str = "", effort: str = ""):
    """流式精算。yield 进度/结果事件，供 bot 实时播报：
      ('stage', n, 阶段名)  —— 模型开始写第 n 段（n=1..7）
      ('done', 完整报告)
      ('error', 错误串)
    阶段识别：检测累积全文里新出现的 `### N.` 主段标题。
    extra_instruction: 用户自定义侧重，透传给 _analyze_prompts。
    effort: 推理强度（low/medium/high/xhigh），透传给 _stream_llm。
    """
    import re
    if not available():
        yield ("error", "未配置 LLM_BASE_URL / LLM_API_KEY，无法分析。请在 .env 配置。")
        return
    system, user = _analyze_prompts(csv_text, fundamentals, home, away, league,
                                    extra_instruction)
    # 匹配行首的 "### 3." / "###3." 等主段标题，捕获段号
    head_re = re.compile(r"(?m)^#{2,3}\s*(\d+)\s*[\.、]")
    seen: set[int] = set()
    for kind, payload in _stream_llm(system, user, effort):
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


def review_blind_stream(csv_text: str, home: str, away: str, league: str,
                        effort: str = ""):
    """复盘第一遍【盲推】：只喂盘口 CSV，不给比分、不给基本面，
    让模型从上到下正向跑 SOP 步骤 1~7 得出赛前预判（它此时并不知道结果）。
    直接复用 analyze_stream（基本面置空），阶段名沿用精算 7 段。
    effort: 推理强度，透传给 analyze_stream。
    """
    blind_note = ("（赛后复盘·第一遍盲推：本次不提供基本面与比赛结果，"
                  "请仅依据盘口走势正向执行 SOP 步骤1~7，给出赛前预判结论。）")
    yield from analyze_stream(csv_text, blind_note, home, away, league,
                              effort=effort)


def _review_prompts(csv_text: str, forecast_text: str, result_text: str,
                    home: str, away: str, league: str) -> tuple[str, str]:
    """构造复盘第二遍【对照】的 (system, user)。

    关键：第一遍已在不知道比分的情况下正向推出预判（forecast_text）。
    本遍才揭晓真实比分，让模型对照「盲推预判 vs 实际结果」做归因，
    而非拿结果倒推 SOP。
    """
    system = (
        load_rules()
        + "\n\n===== 任务（赛后对照复盘）=====\n"
        "你是拥有20年经验的庄家操盘手和数据精算师。这是一场【已结束】比赛的复盘"
        "第二阶段。第一阶段已在【完全不知道比分】的前提下，仅凭盘口走势正向跑完"
        "SOP 得出了赛前预判（见下方『第一遍盲推预判』）。现在揭晓真实比分，请你"
        "对照【盲推预判】与【实际结果】做归因复盘。\n"
        "要求：以第一遍的正向预判为基准做检验，不要重新拿结果倒推 SOP；"
        "客观指出盲推哪里对、哪里错、为何错。只依据盘口走势 + 预判 + 实际结果，"
        "不使用基本面，缺失数据不要编造。\n\n"
        "严格按以下结构输出对照复盘报告：\n"
        "## 复盘：[主队] [比分] [客队]\n"
        "## 赛事：[联赛]  开球：[CST]\n\n"
        "### 1. 实际结果\n"
        "- 全场比分 / 半场比分（如有加时·点球一并列出）\n"
        "- 胜平负：[主胜/平/客胜]；总进球数与大小球倾向\n\n"
        "### 2. 盘口结算回放\n"
        "- 主流亚盘主盘口（如 -0.75）最终结算：上盘[赢/输/走水]，并说明赢半/输半\n"
        "- 关键节点其它盘口的结算结果\n\n"
        "### 3. 盲推预判 vs 实际对照\n"
        "- 第一遍盲推的亚盘/胜平负/比分/置信度逐项列出\n"
        "- 与实际结果逐项比对：命中 / 偏差 / 完全错\n\n"
        "### 4. 信号有效性复盘\n"
        "- 正确信号：盲推中哪些变盘/凯利/水位/欧赔信号正确预示了结果\n"
        "- 误导信号：哪些把盲推带偏了（噪音或反向）\n"
        "- 凯利/返还率事后检验（报警是否兑现）\n\n"
        "### 5. 经验教训\n"
        "- 本场印证/修正了哪条军规或既有教训（引用规则库编号）\n"
        "- 盲推若判错，根因是什么；可沉淀的防错提醒\n\n"
        "### 6. 盘口指示强度评分\n"
        "- 盘口对结果的预示强度：[0~100]（事前仅凭盘口能多大程度预判此结果）\n"
        "- 一句话总结\n"
    )
    user = (
        f"## 比赛：{home} vs {away}\n## 联赛：{league}\n\n"
        f"### 全程盘口快照（CSV）\n{csv_text}\n\n"
        f"### 第一遍盲推预判（模型当时不知道比分）\n{forecast_text}\n\n"
        f"### 实际结果（现在才揭晓）\n{result_text}\n"
    )
    return system, user


def review(csv_text: str, forecast_text: str, result_text: str,
           home: str, away: str, league: str, effort: str = "") -> str:
    """复盘第二遍对照（阻塞版）。"""
    if not available():
        return "未配置 LLM_BASE_URL / LLM_API_KEY，无法复盘。请在 .env 配置。"
    system, user = _review_prompts(csv_text, forecast_text, result_text,
                                   home, away, league)
    return _call_llm(system, user, effort)


# 复盘报告 ### N. 段标题 → 进度阶段名（对照复盘第二遍）
_REVIEW_STAGE_NAMES = {
    1: "实际结果",
    2: "盘口结算回放",
    3: "盲推预判 vs 实际对照",
    4: "信号有效性复盘",
    5: "经验教训",
    6: "盘口指示强度评分",
}
_REVIEW_TOTAL_STAGES = 6


def review_stream(csv_text: str, forecast_text: str, result_text: str,
                  home: str, away: str, league: str, effort: str = ""):
    """复盘第二遍对照（流式）。yield 进度/结果事件（同 analyze_stream）：
      ('stage', n, 阶段名)  —— 模型开始写第 n 段（n=1..6）
      ('done', 完整报告)
      ('error', 错误串)
    forecast_text 为第一遍盲推产出的预判全文。
    effort: 推理强度，透传给 _stream_llm。
    """
    import re
    if not available():
        yield ("error", "未配置 LLM_BASE_URL / LLM_API_KEY，无法复盘。请在 .env 配置。")
        return
    system, user = _review_prompts(csv_text, forecast_text, result_text,
                                   home, away, league)
    head_re = re.compile(r"(?m)^#{2,3}\s*(\d+)\s*[\.、]")
    seen: set[int] = set()
    for kind, payload in _stream_llm(system, user, effort):
        if kind == "delta":
            for m in head_re.finditer(payload):
                n = int(m.group(1))
                if n in _REVIEW_STAGE_NAMES and n not in seen:
                    seen.add(n)
                    yield ("stage", n, _REVIEW_STAGE_NAMES[n])
        elif kind == "done":
            yield ("done", payload)
        elif kind == "error":
            yield ("error", payload)
