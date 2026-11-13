"""自定义侧重：把按钮模式与自由文字编码进 extra_instruction，并供提示词与定稿读取。

全程仍用字符串 extra_instruction 传递（不改任何函数签名），模式用前缀标记编码：

    [[focus:ou,conservative]] 主队主力前锋确认缺阵

模式（见 docs/自定义侧重优化计划.md）：
  ou           大小球重点 —— 第 9 节大小球专题；定稿优先在大小球候选里选
  score        波胆重点   —— 第 9 节波胆专题（代码算比分概率表）；不影响定稿
  aggressive   激进       —— 价值注 k ×FOCUS_AGGRESSIVE_K_MULT（上限 FOCUS_K_MAX）
  conservative 保守       —— 价值注 k ×FOCUS_CONSERVATIVE_K_MULT，且不下探索注

任何模式都不改变方向判定规则，数据质量 <40 一律空仓。
本模块是纯函数：无 IO、无 LLM。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

MODES: dict[str, dict] = {
    "ou": {"label": "📊 大小球", "name": "大小球重点",
           "keywords": ("大小球", "大球", "小球", "总进球")},
    "score": {"label": "🎯 波胆", "name": "波胆重点",
              "keywords": ("波胆", "比分", "正确比分")},
    "aggressive": {"label": "🔥 激进", "name": "激进",
                   "keywords": ("激进",)},
    "conservative": {"label": "🛡 保守", "name": "保守",
                     "keywords": ("保守", "稳健", "谨慎")},
}
EXCLUSIVE = ("aggressive", "conservative")

_MARKER_RE = re.compile(r"^\s*\[\[focus:([^\]]*)\]\]\s*")
_STAMP_PREFIX = "> 本场侧重："
_SECTION_9_RE = re.compile(r"(?m)^###\s*9\s*[.．、]")


@dataclass(frozen=True)
class Focus:
    modes: tuple[str, ...]      # 按 MODES 的顺序排好，已去重
    text: str                   # 去掉标记后的自由文字，已 strip
    conflict: bool = False      # 文字里同时出现激进与保守

    @property
    def empty(self) -> bool:
        """无模式且无文字（预设精算）。"""
        return not self.modes and not self.text


def _ordered(modes: Iterable[str]) -> tuple[str, ...]:
    wanted = set(modes)
    return tuple(key for key in MODES if key in wanted)


def encode(modes: Iterable[str], text: str = "") -> str:
    """无模式时原样返回 text.strip()；有模式时返回 "[[focus:a,b]] text"。"""
    keys = _ordered(m for m in (modes or ()) if m in MODES)
    body = (text or "").strip()
    if not keys:
        return body
    return f"[[focus:{','.join(keys)}]] {body}".rstrip()


def _text_modes(text: str) -> set[str]:
    return {key for key, spec in MODES.items()
            if any(word in text for word in spec["keywords"])}


def parse(extra_instruction: str) -> Focus:
    """1) 读前缀标记里的模式（未知 key 丢弃）；2) 对自由文字按 keywords 检测并入；
    3) 激进与保守同时出现：都来自文字 → 两个都去掉且 conflict=True；
       一个来自标记一个来自文字 → 保留标记里的那个。"""
    raw = extra_instruction or ""
    marked: set[str] = set()
    match = _MARKER_RE.match(raw)
    if match:
        marked = {part.strip() for part in match.group(1).split(",")
                  if part.strip() in MODES}
        raw = raw[match.end():]
    text = raw.strip()
    found = _text_modes(text)
    modes = marked | found
    conflict = False
    if all(key in modes for key in EXCLUSIVE):
        in_marker = [key for key in EXCLUSIVE if key in marked]
        if len(in_marker) == 1:
            other = next(key for key in EXCLUSIVE if key != in_marker[0])
            modes.discard(other)
        else:
            # 都来自文字（或标记里本身就互斥）：按常规口径
            modes.difference_update(EXCLUSIVE)
            conflict = True
    return Focus(modes=_ordered(modes), text=text, conflict=conflict)


def label(focus: Focus) -> str:
    """例："📊 大小球 + 🛡 保守；补充：主队主力前锋确认缺阵"；空侧重返回 ""。"""
    if focus.empty:
        return ""
    parts = []
    if focus.modes:
        parts.append(" + ".join(MODES[key]["label"] for key in focus.modes))
    if focus.text:
        parts.append(f"补充：{focus.text}")
    return "；".join(parts)


def is_ou_play(play: str) -> bool:
    """候选玩法是否为大小球（含全场、主队、客队大小球）。"""
    return "大小球" in (play or "")


def stamp(report: str, focus: Focus) -> str:
    """在报告的「## 赛事：」那一行之后插入 "> 本场侧重：{label}"；
    找不到该行就插在最前面；focus.empty 或已存在该行时原样返回。"""
    if focus.empty or not report:
        return report
    if re.search(rf"(?m)^{re.escape(_STAMP_PREFIX)}", report):
        return report
    line = f"{_STAMP_PREFIX}{label(focus)}"
    match = re.search(r"(?m)^##\s*赛事[：:][^\r\n]*$", report)
    if match is None:
        return line + "\n\n" + report
    return report[:match.end()] + "\n" + line + report[match.end():]


def has_section_9(report: str) -> bool:
    return bool(_SECTION_9_RE.search(report or ""))
