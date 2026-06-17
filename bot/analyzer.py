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


def analyze(csv_text: str, fundamentals: str,
            home: str, away: str, league: str) -> str:
    """调 LLM 跑精算 SOP，返回报告文本；失败返回错误说明串。"""
    if not available():
        return "未配置 LLM_BASE_URL / LLM_API_KEY，无法分析。请在 .env 配置。"

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
