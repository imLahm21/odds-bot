"""
LLM 精算 —— 读全量 SOP 规则 + 调 IKuncode(OpenAI 兼容) chat/completions

- 规则文件进程内缓存（启动读一次）
- 用 requests 直接打 /v1/chat/completions，不依赖 openai SDK
- gpt-5.5 是推理模型，不传 temperature（最稳）
"""

import os
import logging

from dotenv import load_dotenv

from . import config, llm_client

load_dotenv()
log = logging.getLogger("odds_bot.analyzer")

# 请求头清洗迁至 llm_client.clean_header_value；此处保留别名兼容旧引用。
_clean_header_value = llm_client.clean_header_value

# 主端点别名：probe_llm.py 直接读 analyzer.LLM_BASE_URL / LLM_API_KEY，保留不破坏。
# 真正的多端点池/故障转移/熔断在 llm_client；这里只是主端点的只读快照。
LLM_BASE_URL = _clean_header_value(os.getenv("LLM_BASE_URL", "")).rstrip("/")
LLM_API_KEY = _clean_header_value(os.getenv("LLM_API_KEY", ""))

_rules_cache: str | None = None
_live_rules_cache: str | None = None
_fund_rules_cache: str | None = None


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


def load_fund_rules() -> str:
    """读取基本面分析专用规则（国家队/赛事情境/大小球），独立缓存。
    仅供两阶段基本面预处理用，不含全套 SOP 精算规则。"""
    global _fund_rules_cache
    if _fund_rules_cache is not None:
        return _fund_rules_cache
    parts = []
    for rel in config.FUND_ANALYZE_RULE_FILES:
        try:
            with open(rel, encoding="utf-8") as f:
                parts.append(f"\n\n===== {rel} =====\n{f.read()}")
        except FileNotFoundError:
            log.warning("基本面规则文件缺失，跳过: %s", rel)
    _fund_rules_cache = "".join(parts)
    log.info("基本面规则已加载，共 %d 字符", len(_fund_rules_cache))
    return _fund_rules_cache


def available() -> bool:
    return llm_client.available()


def _call_llm(system: str, user: str, effort: str = "",
              model: str = "", timeout: int = 0, max_tokens: int = 0) -> str:
    """薄委托 llm_client.chat（端点池 + 故障转移 + 熔断）。签名与语义不变：
    effort 非空附带 reasoning_effort；model/timeout/max_tokens 非默认时覆盖 config
    （走地/基本面/SEO 各传自己的短超时，优先于 DB 的 non_stream_timeout）。
    失败返回错误说明串（不抛异常），前缀沿用旧格式（见 _LLM_ERR_PREFIXES）。
    """
    return llm_client.chat(system, user, effort=effort, model=model,
                           timeout=timeout, max_tokens=max_tokens)


def _stream_llm(system: str, user: str, effort: str = ""):
    """薄委托 llm_client.stream_chat。yield 事件契约不变：
    ('delta', 累积全文) / ('done', 全文) / ('error', 错误串)。
    流式超时用 DB 的 stream_first_byte_timeout + stream_idle_timeout；
    仅在首字节前跨端点故障转移（见 llm_client.stream_chat）。
    """
    yield from llm_client.stream_chat(system, user, effort=effort)


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
        "盘口数据为 CSV，基本面为【原始数据】文本（含两队近10场、历史交锋、"
        "未来5场赛程、积分榜，来自 API-Football）。你需先按 SOP 步骤1的读法"
        "自行分析这份原始基本面——判赛事情境（阶段/赛制/赛程密度）、近况分层加权、"
        "实力锚（无终指用排名/洲际强弱）、H2H 权重、攻防与大小球倾向——"
        "再将研判并入后续盘口精算。不要编造数据，缺失部分明确标注「无数据」，"
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
    return _call_llm(system, user,
                     effort=config.LLM_LIVE_EFFORT,
                     model=config.LLM_LIVE_MODEL,
                     timeout=config.LLM_LIVE_TIMEOUT,
                     max_tokens=config.LLM_LIVE_MAX_TOKENS)


# _call_llm 失败时返回的错误串前缀（见 _call_llm 各分支），据此判定基本面预处理失败
_LLM_ERR_PREFIXES = (
    "LLM 请求失败", "LLM 超时", "LLM 网络错误",
    "LLM 返回无 choices", "LLM 返回空内容", "LLM_API_KEY",
)


def analyze_fundamentals(raw_funds: str, home: str, away: str,
                         league: str) -> tuple[str, bool]:
    """两阶段预处理：用轻量模型把原始基本面数据分析成一份「基本面研判」。

    返回 (文本, ok)：
      ok=True  → 文本为 mini 产出的研判，供主 SOP 精算用；
      ok=False → 文本回退为原始 raw_funds（未配置/失败/超时/空），调用方据此标注。
    失败绝不抛异常、不阻断精算（沿用 _call_llm 的错误串返回约定）。
    """
    if not available():
        return raw_funds, False
    system = (
        load_fund_rules()
        + "\n\n===== 任务（基本面分析） =====\n"
        "你是足球赛事基本面分析师。下面是某场比赛的原始基本面数据"
        "（两队近 10 场、历史交锋、未来 5 场赛程、积分榜，来自 API-Football）。"
        "请严格依据上述方法论规则，把原始数据分析成一份结构化【基本面研判】，"
        "供操盘手后续盘口精算参考：\n"
        "1. 先判赛事情境：赛事阶段（小组赛/淘汰赛/联赛轮次）、赛制（单/双循环）、"
        "俱乐部赛程情境（恢复天数/下场对手/多线/留力）；国家队赛事则先分赛制"
        "（赛会制中立场 vs 主客场制有地利）。\n"
        "2. 再逐项研判：近况按赛事性质分层加权、实力锚（无终指时用排名/洲际强弱）、"
        "H2H 权重、出线形势与战意、两队攻防与大小球倾向。\n"
        "3. 产出研判结论（不是复述数据），指出对盘口的参考意义与风险点。\n"
        "数据缺失的部分明确标注「无数据」，不要编造。控制在合理篇幅内。\n"
        "输出用纯文字，不要使用 Markdown 符号（不要出现 #、*、**、>、--- 等），"
        "分点可用「1. 2. 3.」或「·」，标题直接用文字，方便在纯文本聊天窗展示。"
    )
    user = (
        f"## 比赛：{home} vs {away}\n## 联赛：{league}\n\n"
        f"### 原始基本面数据\n{raw_funds}\n"
    )
    out = _call_llm(system, user,
                    effort=config.FUND_ANALYZE_EFFORT,
                    model=config.FUND_ANALYZE_MODEL,
                    timeout=config.FUND_ANALYZE_TIMEOUT,
                    max_tokens=config.FUND_ANALYZE_MAX_TOKENS)
    if not out or out.startswith(_LLM_ERR_PREFIXES):
        log.warning("基本面预处理失败，回退原始数据: %s", out[:120])
        return raw_funds, False
    return out, True


def distill_lesson(review_report: str, home: str, away: str,
                   league: str) -> tuple[str, bool]:
    """把一份【对照复盘报告】蒸馏成一张「实战教训卡」，对齐 rules/实战教训/case_*.md
    的结构，供归入教训规则库。返回 (markdown文本, ok)。

    只喂已成文的复盘报告（不重拉数据）。失败/未配置/空 → (错误说明或提示, False)，
    调用方据此不落盘。用轻量模型 + 中等超时（这是复盘后的可选增值，不该拖慢）。
    """
    if not available():
        return "未配置 LLM_BASE_URL / LLM_API_KEY", False
    if not review_report.strip():
        return "复盘报告为空", False
    system = (
        "你是足球赔率复盘教练。用户给你一份【赛后对照复盘报告】，请把其中可复用的"
        "操盘教训提炼成一张精炼的「实战教训卡」（Markdown），供沉淀进教训规则库。"
        "严格按以下结构输出，不要多余前后缀、不要代码块围栏：\n\n"
        "# 案例（数据存档）：[主队] [比分] [客队]（[日期] [联赛][轮次如有]）\n\n"
        "## 结果\n"
        "- **预测**：[盲推的亚盘/胜平负/比分/置信度，命中或偏差]\n"
        "- **实际**：[真实比分与结算]\n"
        "- **正确结论**：[事后看应得的判断]\n\n"
        "## 关键信号\n"
        "[3~6 条最能解释对/错的盘口/凯利/水位/欧赔/基本面信号，用短句或小表格]\n\n"
        "## 教训与规则\n"
        "[本场印证或修正了哪条军规/既有教训（引用编号如有）；根因是什么；"
        "可沉淀的防错提醒。若复盘判断正确，则记录被验证有效的信号组合]\n\n"
        "硬规则：① 只依据给定复盘报告，不编造数据、不新增未提及的结论；"
        "② 中文；③ 简洁——整张卡控制在 400 字内，突出可复用规律而非复述全文；"
        "④ 若报告显示预测正确，如实写成「正确案例」，不要硬凑错误。"
    )
    user = (
        f"## 比赛：{home} vs {away}（{league}）\n\n"
        f"### 对照复盘报告\n{review_report}\n"
    )
    out = _call_llm(system, user,
                    effort=config.FUND_ANALYZE_EFFORT,
                    model=config.FUND_ANALYZE_MODEL,
                    timeout=config.FUND_ANALYZE_TIMEOUT,
                    max_tokens=config.FUND_ANALYZE_MAX_TOKENS)
    if not out or out.startswith(_LLM_ERR_PREFIXES):
        log.warning("实战教训蒸馏失败: %s", (out or "")[:120])
        return (out or "LLM 无返回"), False
    return out.strip(), True


def fan_fundamentals_brief(free_md: str, home: str, away: str,
                           league: str) -> str:
    """发布期：把报告免费正文里的球队近况/交锋/赛程改写成一段面向普通球迷的
    口语化「基本面速览」，供 /publish 插入文章免费区引流。

    素材取已成文的 free_md（报告 1b/1c/1d 段），不重新拉数据——publish 读的是
    历史归档报告，对应 fixture 可能已过 CLEANUP_DAYS 被清。
    失败/未配置返回 ""（调用方据此不插入，发布不中断）。
    """
    if not available() or not free_md.strip():
        return ""
    system = (
        "你是足球博客编辑。下面是一篇赛前分析报告的正文（含球队近况、历史交锋、"
        "赛程等）。请据此写一段【面向普通球迷的口语化基本面速览】：\n"
        "- 150~300 字，一段或两段，通俗易懂，不用盘口/亚盘/凯利等术语；\n"
        "- 只讲两队近期状态、交锋恩怨、伤停赛程等基本面看点，帮读者快速了解看点；\n"
        "- 不要给出比分预测或胜负结论（结论是付费内容，不可泄露）；\n"
        "- 只讲报告里【实际有】的信息；报告没提供的（如伤停、后续赛程等）"
        "直接略过、当它不存在，【严禁】写出「报告没有给出…」「缺少…信息」"
        "「资料未提供…」这类说明数据缺失的话，也不要编造；\n"
        "- 直接输出正文，不要标题、不要 markdown 标记。"
    )
    user = (
        f"## 比赛：{home} vs {away}\n## 联赛：{league}\n\n"
        f"### 报告正文（据此提炼基本面速览）\n{free_md}\n"
    )
    out = _call_llm(system, user,
                    effort=config.FUND_ANALYZE_EFFORT,
                    model=config.FUND_ANALYZE_MODEL,
                    timeout=config.FUND_ANALYZE_TIMEOUT,
                    max_tokens=config.FUND_ANALYZE_MAX_TOKENS)
    if not out or out.startswith(_LLM_ERR_PREFIXES):
        log.warning("科普基本面段生成失败，跳过: %s", out[:120])
        return ""
    return out.strip()


def seo_summarize(free_body: str, home: str, away: str, league: str,
                  is_review: bool = False) -> tuple[dict | None, str | None]:
    """据报告【免费正文】生成 SEO 三件套，返回 (结果 dict 或 None, 错误说明 或 None)。

    成功：({"hook":…, "excerpt":…, "meta_desc":…}, None)
    失败/未配置：(None, "原因串")——调用方据此回退模板，并可把原因发回 TG 提示。
    只喂免费正文（第1~6节），天然不泄露第7节结论。
    用轻量模型 + 短超时（这是发布期的锦上添花，不该拖慢/卡死发布）。
    """
    import json
    import re as _re
    if not available():
        return None, "未配置 LLM_BASE_URL / LLM_API_KEY"
    view = "赛后复盘" if is_review else "赛前预测"
    system = (
        "你是足球赔率分析博客的中文 SEO 编辑。根据用户给的一篇文章免费正文，"
        "生成用于搜索引擎与列表展示的文案。只输出一个 JSON 对象，不要 markdown "
        "代码块、不要多余文字。字段：\n"
        '  "hook": 一句话看点（≤20字），勾起读者兴趣，用作标题副标题；\n'
        '  "excerpt": 文章摘要（120~160字），描述本场盘口看点与分析维度；\n'
        '  "meta_desc": 搜索引擎描述（≤145字），在 excerpt 基础上做 SEO 优化、'
        "核心关键词前置。\n"
        "硬规则：① 绝不出现比分/胜负/上下盘等结论性预测，只描述分析了什么；"
        "② 不出现任何具体庄家名（如 365/Pinnacle/威廉），连「庄家」这个通用词也避开，"
        "统一用「主流机构」指代；③ 全中文；④ 视角是【{view}】；⑤ 用「推算」而非「精算」"
        "描述分析行为；⑥ 只依据给定正文，不编造数据；"
        "⑦ 少罗列具体盘口数字：全文最多提一个让球盘口、一个大小球盘口（如「让球主盘」"
        "「2.5 大小球」），【严禁】列举「2.5、2.75、3.0、3.5」这种多档位数字堆砌。"
    ).replace("{view}", view)
    user = (
        f"## 比赛：{home} vs {away}（{league}）\n"
        f"## 视角：{view}\n\n"
        f"### 免费正文\n{free_body}\n"
    )
    raw = _call_llm(system, user,
                    effort=config.LLM_LIVE_EFFORT,
                    model=config.LLM_LIVE_MODEL,
                    timeout=config.LLM_LIVE_TIMEOUT,
                    max_tokens=config.LLM_LIVE_MAX_TOKENS)
    # _call_llm 失败时返回错误说明串（非 JSON），下面解析失败即回退并带回原因
    m = _re.search(r"\{.*\}", raw, _re.S)   # 容忍模型偶尔包裹代码块/前后缀
    if not m:
        log.warning("SEO 概括未返回 JSON，回退模板：%s", raw[:120])
        return None, raw[:200]
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        log.warning("SEO 概括 JSON 解析失败，回退模板：%s", m.group(0)[:120])
        return None, "LLM 返回非合法 JSON，无法解析"
    hook = str(d.get("hook", "")).strip()
    excerpt = str(d.get("excerpt", "")).strip()
    meta_desc = str(d.get("meta_desc", "")).strip()
    if not (hook and excerpt and meta_desc):   # 任一缺失即整体回退，避免半套文案
        log.warning("SEO 概括字段不全，回退模板：%s", str(d)[:120])
        return None, "LLM 返回字段不全（hook/excerpt/meta_desc 缺失）"
    return {"hook": hook[:30], "excerpt": excerpt[:200],
            "meta_desc": meta_desc[:145]}, None


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
                    home: str, away: str, league: str,
                    fund_brief: str = "") -> tuple[str, str]:
    """构造复盘第二遍【对照】的 (system, user)。

    关键：第一遍已在不知道比分的情况下正向推出预判（forecast_text）。
    本遍才揭晓真实比分，让模型对照「盲推预判 vs 实际结果」做归因，
    而非拿结果倒推 SOP。

    fund_brief：基本面研判（两阶段预处理产物）。非空时本遍结合基本面做归因，
    解释盲推（纯盘口、无基本面）对/错背后是否有基本面成因；空则退回纯盘口复盘。
    """
    has_fund = bool(fund_brief.strip())
    fund_clause = (
        "本遍额外提供【基本面研判】（盲推时模型看不到，故基本面正是盲推的盲区）。"
        "请结合基本面研判解释盲推对/错的成因——盘口对了是否与基本面一致、"
        "盘口错了是否基本面早有预警。缺失数据不要编造。"
        if has_fund else
        "只依据盘口走势 + 预判 + 实际结果，不使用基本面，缺失数据不要编造。"
    )
    system = (
        load_rules()
        + "\n\n===== 任务（赛后对照复盘）=====\n"
        "你是拥有20年经验的庄家操盘手和数据精算师。这是一场【已结束】比赛的复盘"
        "第二阶段。第一阶段已在【完全不知道比分】的前提下，仅凭盘口走势正向跑完"
        "SOP 得出了赛前预判（见下方『第一遍盲推预判』）。现在揭晓真实比分，请你"
        "对照【盲推预判】与【实际结果】做归因复盘。\n"
        "要求：以第一遍的正向预判为基准做检验，不要重新拿结果倒推 SOP；"
        "客观指出盲推哪里对、哪里错、为何错。" + fund_clause + "\n\n"
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
        + ("- 基本面归因：结合基本面研判，指出盲推的盲区里哪些基本面因素"
           "（近况/实力锚/赛程情境/出线战意）本可修正或印证盘口信号\n"
           if has_fund else "")
        + "- 凯利/返还率事后检验（报警是否兑现）\n\n"
        "### 5. 经验教训\n"
        "- 本场印证/修正了哪条军规或既有教训（引用规则库编号）\n"
        "- 盲推若判错，根因是什么"
        + ("（区分是纯盘口不可知，还是基本面本可补正的盲区）" if has_fund else "")
        + "；可沉淀的防错提醒\n\n"
        "### 6. 盘口指示强度评分\n"
        "- 盘口对结果的预示强度：[0~100]（事前仅凭盘口能多大程度预判此结果）\n"
        "- 一句话总结\n"
    )
    user = (
        f"## 比赛：{home} vs {away}\n## 联赛：{league}\n\n"
        f"### 全程盘口快照（CSV）\n{csv_text}\n\n"
        f"### 第一遍盲推预判（模型当时不知道比分）\n{forecast_text}\n\n"
        + (f"### 基本面研判（盲推时不可见，供本遍归因）\n{fund_brief}\n\n"
           if has_fund else "")
        + f"### 实际结果（现在才揭晓）\n{result_text}\n"
    )
    return system, user


def review(csv_text: str, forecast_text: str, result_text: str,
           home: str, away: str, league: str, effort: str = "",
           fund_brief: str = "") -> str:
    """复盘第二遍对照（阻塞版）。fund_brief 非空则结合基本面研判归因。"""
    if not available():
        return "未配置 LLM_BASE_URL / LLM_API_KEY，无法复盘。请在 .env 配置。"
    system, user = _review_prompts(csv_text, forecast_text, result_text,
                                   home, away, league, fund_brief)
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
                  home: str, away: str, league: str, effort: str = "",
                  fund_brief: str = ""):
    """复盘第二遍对照（流式）。yield 进度/结果事件（同 analyze_stream）：
      ('stage', n, 阶段名)  —— 模型开始写第 n 段（n=1..6）
      ('done', 完整报告)
      ('error', 错误串)
    forecast_text 为第一遍盲推产出的预判全文。
    fund_brief 非空则结合基本面研判做归因（盲推的盲区）。
    effort: 推理强度，透传给 _stream_llm。
    """
    import re
    if not available():
        yield ("error", "未配置 LLM_BASE_URL / LLM_API_KEY，无法复盘。请在 .env 配置。")
        return
    system, user = _review_prompts(csv_text, forecast_text, result_text,
                                   home, away, league, fund_brief)
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
