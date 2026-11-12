"""
假设触发判定（纯函数，无 LLM）—— 只覆盖「机判 ✓」且触发条件能只用盘口事实判定的那部分

每条假设一个函数：输入 market_facts.MarketFacts，输出 Trigger 或 None。
预测 prediction 统一为 (market, side)：
  ("ah", "upper"/"lower")      亚盘上盘 / 下盘（上盘 = 初盘让球方）
  ("1x2", "home"/"draw"/"away") 胜平负
  ("1x2", "not_home"/…)       该项不出
  ("none", "")                 无方向（只记录，如 H-M09）
  ("transition", "")           场景转换类（H-M03/08/12/13），不预测赛果

状态、样本与「已验证」与否一律以 rules/假设登记表.md 为准（registry_status），
本模块不定方向；analyzer 把触发列表注入 prompt，LLM 只负责解释。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import config
from .market_facts import MarketFacts


@dataclass(frozen=True, slots=True)
class Trigger:
    code: str
    market: str
    side: str
    evidence: str


HypothesisFn = Callable[[MarketFacts], "Trigger | None"]
REGISTRY: dict[str, HypothesisFn] = {}


def hypothesis(code: str):
    def wrap(fn: HypothesisFn) -> HypothesisFn:
        REGISTRY[code] = fn
        return fn
    return wrap


def _t(code, market, side, evidence) -> Trigger:
    return Trigger(code, market, side, evidence)


def _no_move(f: MarketFacts) -> bool:
    return bool(f.lines) and not f.moves


def _moves(f: MarketFacts, *, up: bool | None = None, phases=("early", "mid", "late"),
           end: bool | None = None):
    return [m for m in f.moves
            if (up is None or (m.delta > 0) == up)
            and m.phase in phases and (end is None or m.end == end)]


def _ready(f: MarketFacts) -> bool:
    return bool(f.lines) and f.gap_class in ("deep", "shallow", "aligned")


# ─── 十三条军规 H-M01~13（reference_asian_handicap 第十二章）────────────────────

@hypothesis("H-M01")
def m01(f):
    if _ready(f) and f.gap_class == "deep" and _no_move(f) and f.lower_heat and f.big_heat:
        return _t("H-M01", "ah", "upper", "深于换算 + 不变盘 + 下盘大热")


@hypothesis("H-M02")
def m02(f):
    if _ready(f) and f.gap_class == "deep" and _moves(f, up=False, phases=("late",)) \
            and not _moves(f, up=True) and f.lower_heat and f.big_heat:
        return _t("H-M02", "ah", "upper", "深于换算 + 临场降盘 + 下盘大热")


@hypothesis("H-M03")
def m03(f):
    if _ready(f) and f.gap_class == "deep" and _moves(f, up=False, phases=("early", "mid")) \
            and f.theory_depth is not None and f.final_depth is not None \
            and abs(f.final_depth - f.theory_depth) < 0.25:
        return _t("H-M03", "transition", "", "深于换算 + 早中期降盘至一致")


@hypothesis("H-M04")
def m04(f):
    if (_ready(f) and f.gap_class == "deep" and _no_move(f) and f.upper_heat and f.big_heat
            and f.upper_water_final is not None and f.upper_water_final - 1 < 0.87):
        return _t("H-M04", "ah", "lower", "深于换算 + 不变盘 + 上盘降至低水 + 上盘大热")


@hypothesis("H-M05")
def m05(f):
    if _ready(f) and f.gap_class == "deep" and _moves(f, up=True, phases=("late",)) \
            and f.upper_heat and f.big_heat:
        return _t("H-M05", "ah", "lower", "深于换算 + 临场升盘 + 上盘大热")


@hypothesis("H-M06")
def m06(f):
    if _ready(f) and f.gap_class == "deep" and _moves(f, up=True, phases=("early", "mid")) \
            and f.upper_heat and f.big_heat:
        return _t("H-M06", "ah", "lower", "深于换算 + 早中期升盘 + 上盘大热")


@hypothesis("H-M07")
def m07(f):
    if _ready(f) and f.gap_class == "shallow" and _no_move(f) and f.upper_heat and f.big_heat:
        return _t("H-M07", "ah", "lower", "浅于换算 + 不变盘 + 上盘大热")


@hypothesis("H-M08")
def m08(f):
    if _ready(f) and f.gap_class == "shallow" and _moves(f, up=True, phases=("early", "mid")) \
            and f.theory_depth is not None and f.final_depth is not None \
            and abs(f.final_depth - f.theory_depth) < 0.25:
        return _t("H-M08", "transition", "", "浅于换算 + 早中期升盘至一致")


@hypothesis("H-M09")
def m09(f):
    if _ready(f) and f.gap_class == "aligned" and _no_move(f):
        heat = f"热门方 {f.heat_side}" if f.heat_side else "无明显热门"
        return _t("H-M09", "none", "", f"欧亚一致 + 不变盘（{heat}）")


@hypothesis("H-M10")
def m10(f):
    late_down = _moves(f, up=False, phases=("late",))
    if (_ready(f) and f.gap_class == "aligned" and late_down and not _moves(f, up=True)
            and all(m.upper_water_change <= 0.005 for m in late_down)):
        return _t("H-M10", "ah", "upper", "欧亚一致 + 临场降盘 + 主水降或平")


@hypothesis("H-M11")
def m11(f):
    if _ready(f) and f.gap_class == "aligned" and _moves(f, up=True) and f.upper_heat:
        return _t("H-M11", "ah", "lower", "欧亚一致 + 升盘 + 上盘热")


@hypothesis("H-M12")
def m12(f):
    if _ready(f) and f.gap_class == "aligned" and _moves(f, up=False, phases=("early",)):
        return _t("H-M12", "transition", "", "欧亚一致 + 早期降盘（原文：转场景一）")


@hypothesis("H-M13")
def m13(f):
    if _ready(f) and f.gap_class == "aligned" and _moves(f, up=True, phases=("early",)):
        return _t("H-M13", "transition", "", "欧亚一致 + 早期升盘（原文：转场景二）")


# ─── 盘口与水位四大规则 H-R（第二章）、大小盘升降 H-S（第三章）、蛙跳 H-T ─────────

@hypothesis("H-R1a")
def r1a(f):
    ups = _moves(f, up=True, phases=("early",))
    if ups and any(m.upper_water_change < 0 for m in ups):
        return _t("H-R1a", "ah", "upper", "早期升盘又降水")


@hypothesis("H-R1b")
def r1b(f):
    ups = _moves(f, up=True, phases=("late",), end=True)
    if ups and all(m.upper_water_change < 0 for m in ups):
        return _t("H-R1b", "ah", "lower", "末端升盘持续降水")


@hypothesis("H-R3a")
def r3a(f):
    ups = _moves(f, up=True)
    if ups and all(0 < m.upper_water_change < 0.10 for m in ups):
        return _t("H-R3a", "ah", "upper", "升盘升水 < 10 点")


@hypothesis("H-R3b")
def r3b(f):
    ups = _moves(f, up=True)
    if ups and any(m.upper_water_change > 0.10 for m in ups):
        return _t("H-R3b", "ah", "lower", "升盘升水 > 10 点")


@hypothesis("H-R4")
def r4(f):
    downs = _moves(f, up=False)
    if downs and any(m.upper_water_change > 0 for m in downs):
        return _t("H-R4", "ah", "upper", "降盘升水")


def _big(f) -> bool:
    return f.initial_depth is not None and f.initial_depth > 1.0 + 1e-9


@hypothesis("H-S1")
def s1(f):
    if f.lines and _big(f) and _moves(f, up=True, phases=("late",)):
        return _t("H-S1", "ah", "lower", "大盘（>一球）临场升盘")


@hypothesis("H-S2")
def s2(f):
    if f.lines and _big(f) and _moves(f, up=False):
        return _t("H-S2", "ah", "upper", "大盘降盘")


@hypothesis("H-S3")
def s3(f):
    if f.lines and not _big(f) and _moves(f, up=False, phases=("late",)):
        return _t("H-S3", "ah", "upper", "小盘（≤一球）临场降盘")


@hypothesis("H-S4")
def s4(f):
    if f.lines and not _big(f) and _moves(f, up=True):
        return _t("H-S4", "ah", "lower", "小盘升盘")


@hypothesis("H-T3")
def t3(f):
    if f.consecutive_moves >= 3:
        return _t("H-T3", "ah", "upper", f"连续 {f.consecutive_moves} 次升盘")


@hypothesis("H-T4")
def t4(f):
    if f.consecutive_moves <= -3:
        return _t("H-T4", "ah", "lower", f"连续 {-f.consecutive_moves} 次降盘")


# ─── 形态与欧亚联动 H-D / H-P ────────────────────────────────────────────────

@hypothesis("H-D02")
def d02(f):
    if f.mono_rise:
        return _t("H-D02", "ah", "upper", "阻上型：主线不变下上盘单向升水 ≥0.10")


def _euro_upper_change(f) -> float | None:
    if not f.euro_initial or not f.euro_final or not f.upper:
        return None
    a, b = f.euro_initial.get(f.upper), f.euro_final.get(f.upper)
    return (b - a) if a and b else None


@hypothesis("H-D13")
def d13(f):
    change = _euro_upper_change(f)
    downs = _moves(f, up=False)
    if change is not None and change > 0.30 and downs and not _moves(f, up=True):
        return _t("H-D13", "ah", "upper", f"上盘胜赔升 {change:+.2f} + 降盘（上盘小胜）")


@hypothesis("H-P-A1")
def pa1(f):
    ups = _moves(f, up=True, phases=("early", "mid"))
    if ups and not _moves(f, up=False) and f.upper_heat:
        return _t("H-P-A1", "ah", "upper", "早中期欧赔上盘方概率升 + 同步升盘")


@hypothesis("H-P-C3")
def pc3(f):
    change = _euro_upper_change(f)
    downs = _moves(f, up=False, phases=("early", "mid"))
    if change is not None and change > 0 and downs and not _moves(f, up=True):
        return _t("H-P-C3", "ah", "upper", "上盘胜赔初→终上升 + 早中期降盘（上盘小胜）")


# ─── 凯利与返还率 H-K ────────────────────────────────────────────────────────

@hypothesis("H-K02")
def k02(f):
    sides = f.kelly_flags.get("bet365_over") or []
    if sides:
        return _t("H-K02", "1x2", f"not_{sides[0]}", f"Bet365 {sides[0]} 凯利 > 1.03")


@hypothesis("H-K03")
def k03(f):
    sides = f.kelly_flags.get("pinnacle_under") or []
    if sides:
        return _t("H-K03", "1x2", sides[0], f"Pinnacle {sides[0]} 凯利 < 0.96")


@hypothesis("H-K20")
def k20(f):
    if f.return_initial and f.return_final and f.return_final - f.return_initial >= 0.015 \
            and f.euro_final:
        fav = min(("home", "draw", "away"), key=lambda k: f.euro_final.get(k) or 99)
        return _t("H-K20", "1x2", fav, f"返还率 {f.return_initial:.1%}→{f.return_final:.1%}，低赔方")


# ─── 评估与登记表 ────────────────────────────────────────────────────────────

def evaluate(facts: MarketFacts) -> list[Trigger]:
    out = []
    for fn in REGISTRY.values():
        try:
            hit = fn(facts)
        except (TypeError, ValueError, KeyError):
            hit = None
        if hit is not None:
            out.append(hit)
    return out


def registry_status(path: str | Path | None = None) -> dict[str, tuple[str, str]]:
    """登记表编号 → (状态, 前样本)。「H-R2a / H-R2b」这类合并行展开成各自编号。"""
    target = Path(path) if path else Path(__file__).resolve().parents[1] / config.HYPOTHESIS_REGISTRY
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return {}
    out: dict[str, tuple[str, str]] = {}
    for line in text.splitlines():
        if not line.startswith("| H-"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 8:
            continue
        status, prior = cells[-1], cells[5]
        for code in _expand_codes(cells[0]):
            out.setdefault(code, (status, _prior_for(code, prior)))
    return out


def _prior_for(code: str, prior: str) -> str:
    """前样本格按「H-XX 机判 …；旧 LLM 自标 …」分段：只取属于 code 的段（无编号段归前一编号）。
    格内没有任何编号段（旧格式 / 「—」）时原样返回。"""
    segments = [s.strip() for s in prior.split("；") if s.strip()]
    if not any(re.match(r"H-[A-Za-z0-9-]+\s", s) for s in segments):
        return prior
    owner, mine = None, []
    for seg in segments:
        m = re.match(r"(H-[A-Za-z0-9-]+)\s+(.*)", seg)
        if m:
            owner, seg = m.group(1), m.group(2)
        if owner == code:
            mine.append(seg)
    return "；".join(mine) or "—"


def _expand_codes(cell: str) -> list[str]:
    codes = []
    for part in re.split(r"\s*[/、]\s*", cell):
        m = re.fullmatch(r"(H-[A-Za-z-]*?)(\d+)[a-z]?~(?:H-)?[A-Za-z-]*?(\d+)", part)
        if m:
            prefix, lo, hi = m.group(1), int(m.group(2)), int(m.group(3))
            width = len(m.group(2))
            codes += [f"{prefix}{i:0{width}d}" for i in range(lo, hi + 1)]
        elif re.fullmatch(r"H-[A-Za-z0-9-]+", part):
            codes.append(part)
    return codes


_PRED_ZH = {("ah", "upper"): "上盘", ("ah", "lower"): "下盘", ("1x2", "home"): "主胜",
            ("1x2", "draw"): "平局", ("1x2", "away"): "客胜", ("1x2", "not_home"): "主胜不出",
            ("1x2", "not_draw"): "平局不出", ("1x2", "not_away"): "客胜不出",
            ("none", ""): "无方向", ("transition", ""): "场景转换（不预测赛果）"}


def format_triggers(triggers: list[Trigger], registry: dict | None = None) -> str:
    """注入 prompt 的【已触发假设】段落（第 4 节假设表以此为准）。"""
    registry = registry if registry is not None else registry_status()
    head = ["### 已触发假设（代码判定，第 4 节以此为准；不可机械判定的假设由你按规则文件补充并注明）"]
    if not triggers:
        return "\n".join(head + ["- 无（可机械判定的假设本场均未触发）"])
    head += ["| 假设 | 触发依据 | 预测 | 状态 | 登记前样本 |", "|------|---------|------|------|-----------|"]
    verified = []
    for t in triggers:
        status, prior = registry.get(t.code, ("假设", "—"))
        if status.startswith("已验证"):
            verified.append(t)
        head.append(f"| {t.code} | {t.evidence} | {_PRED_ZH.get((t.market, t.side), t.side)} "
                    f"| {status} | {prior} |")
    if verified:
        head.append("- 已验证假设给出的方向：" + "；".join(
            f"{t.code} {_PRED_ZH.get((t.market, t.side), t.side)}" for t in verified))
    else:
        head.append("- 已验证假设给出的方向：无（本场没有「已验证」假设触发）")
    return "\n".join(head)
