"""
3串1 串关裁判（纯确定性，不碰 LLM）—— Beta

对齐 rules/风控验证/reference_staking_kelly.md 第五章 §5.2：
  - 只做 3串1（2串1 无 ×1.15 增益，默认拆单关）。
  - 准入门槛（全满足才可串）：三腿【每腿】edge>0 且证据达「中」以上。
  - ×1.15 是乘法，只放大已有 edge 的方向：一腿 −EV，×1.15 也救不回。
  - 同场腿属相关性套利、另算——调用方须保证三腿是不同 fixture。

输入：三条腿的结构化决策（由 analyzer.extract_decision 从各腿精算报告抽出）。
输出：可串/拆判定 + 合并 EV + 注额 + 逐腿明细 + 渲染好的 Markdown 结论。

本模块只做算术与规则判定，便于单测；LLM 抽取与 TG 编排在别处。
"""

import logging

from . import config

log = logging.getLogger("odds_bot.parlay")

# 证据档位规范化：把 extract_decision 可能给的中英文/别名收敛到四档 key。
_EVIDENCE_ALIASES = {
    "strong": "strong", "强": "strong", "high": "strong",
    "medium": "medium", "中": "medium", "mid": "medium", "moderate": "medium",
    "weak": "weak", "弱": "weak", "low": "weak",
    "none": "none", "无": "none", "": "none", None: "none",
}
_EVIDENCE_RANK = {"none": 0, "weak": 1, "medium": 2, "strong": 3}


def norm_evidence(ev) -> str:
    """把任意证据表述规范化为 strong/medium/weak/none。未知值按最保守的 none。"""
    if isinstance(ev, str):
        ev = ev.strip().lower()
    return _EVIDENCE_ALIASES.get(ev, "none")


def _leg_ok(leg: dict) -> tuple[bool, str]:
    """单腿是否满足准入（edge>0 且证据≥PARLAY_MIN_EVIDENCE）。返回 (通过, 原因)。"""
    edge = leg.get("edge")
    if edge is None:
        return False, "缺 edge（抽取失败）"
    if leg.get("pass"):
        return False, "该腿单场判定为 pass（无正 edge 玩法）"
    if edge <= 0:
        return False, f"edge={edge:+.1%} ≤ 0"
    ev = norm_evidence(leg.get("evidence"))
    if _EVIDENCE_RANK[ev] < _EVIDENCE_RANK[config.PARLAY_MIN_EVIDENCE]:
        return False, f"证据={ev} < 门槛{config.PARLAY_MIN_EVIDENCE}"
    return True, "通过"


def _weakest_evidence(legs: list[dict]) -> str:
    """三腿里最弱的证据档（木桶效应定凯利 k）。"""
    ranks = [_EVIDENCE_RANK[norm_evidence(l.get("evidence"))] for l in legs]
    worst = min(ranks) if ranks else 0
    for k, v in _EVIDENCE_RANK.items():
        if v == worst:
            return k
    return "none"


def evaluate(legs: list[dict]) -> dict:
    """对三条腿做串关裁判。

    legs: 每项形如 {"fid","home","away","play","odds","edge","p_final",
                     "evidence","pass"}（edge/p_final 为小数，odds 为十进制赔率）。

    返回 dict：
      {"can_parlay": bool, "reason": str, "leg_checks": [(ok,reason)...],
       "combined_p","combined_odds","boosted_odds","ev","kelly_f","k",
       "stake","weakest_evidence","legs": legs}
    过闸时才有 EV/注额等数值；否则为 None。
    """
    n_expected = config.PARLAY_LEGS
    checks = [_leg_ok(l) for l in legs]
    result = {
        "can_parlay": False, "reason": "", "leg_checks": checks,
        "combined_p": None, "combined_odds": None, "boosted_odds": None,
        "ev": None, "kelly_f": None, "k": None, "stake": None,
        "weakest_evidence": _weakest_evidence(legs), "legs": legs,
    }

    if len(legs) != n_expected:
        result["reason"] = f"需恰好 {n_expected} 条腿，收到 {len(legs)}"
        return result

    # 同场腿相关性套利：调用方本应传不同 fixture，这里兜底再挡一次。
    fids = [l.get("fid") for l in legs if l.get("fid") is not None]
    if len(set(fids)) < len(fids):
        result["reason"] = "存在同场腿（相关性套利，须另算），不可作普通串关"
        return result

    failed = [i for i, (ok, _) in enumerate(checks) if not ok]
    if failed:
        idx = "、".join(f"腿{i+1}" for i in failed)
        result["reason"] = f"{idx} 未过准入 → 拆单关（§5.2：任一 −EV，×1.15 也救不回）"
        return result

    # 三腿全过闸 → 算合并 EV（BCG 3串1 ×1.15）
    p = 1.0
    o = 1.0
    for l in legs:
        p *= float(l["p_final"])
        o *= float(l["odds"])
    boosted = o * config.PARLAY_BCG_MULTIPLIER
    ev = p * boosted - 1.0

    result["combined_p"] = p
    result["combined_odds"] = o
    result["boosted_odds"] = boosted
    result["ev"] = ev

    if ev <= 0:
        # 理论上三腿全正 edge 后合并必正；此处兜底防抽取噪声/极端赔率。
        result["reason"] = f"合并 EV={ev:+.1%} ≤ 0（×1.15 未能翻正）→ 拆单关"
        return result

    weakest = result["weakest_evidence"]
    k = config.PARLAY_EVIDENCE_K.get(weakest, 0.0)
    # 凯利分数 f = EV / (净赔率)，净赔率 = boosted_odds − 1
    kelly_f = ev / (boosted - 1.0) if boosted > 1.0 else 0.0
    stake = config.PARLAY_STAKE_BANKROLL * k * kelly_f
    stake = min(stake, config.PARLAY_STAKE_CAP)
    stake = max(stake, 0.0)

    result["can_parlay"] = True
    result["reason"] = "三腿全过准入且 ×1.15 后 EV>0 → 可串"
    result["k"] = k
    result["kelly_f"] = kelly_f
    result["stake"] = round(stake, 1)
    return result


def _fmt_pct(x) -> str:
    return f"{x:+.1%}" if isinstance(x, (int, float)) else "—"


def render_report(verdict: dict) -> str:
    """把 evaluate() 的结果渲染成 Markdown 串关结论（含 Beta 提示）。"""
    legs = verdict.get("legs", [])
    checks = verdict.get("leg_checks", [])
    lines = [
        config.PARLAY_BETA_NOTICE,
        "",
        "## 3串1 串关裁判（Beta）",
        "",
        "### 逐腿明细",
        "",
        "| 腿 | 对阵 | 玩法 | 赔率 | edge | 证据 | 准入 |",
        "|----|------|------|------|------|------|------|",
    ]
    for i, leg in enumerate(legs):
        ok, why = checks[i] if i < len(checks) else (False, "—")
        mark = "✅" if ok else f"❌ {why}"
        lines.append(
            f"| {i+1} | {leg.get('home','?')} vs {leg.get('away','?')} "
            f"| {leg.get('play','?')} | {leg.get('odds','?')} "
            f"| {_fmt_pct(leg.get('edge'))} | {norm_evidence(leg.get('evidence'))} "
            f"| {mark} |")
    lines.append("")

    if verdict.get("can_parlay"):
        lines += [
            f"### 结论：✅ 可串（3串1）",
            "",
            f"- 合并命中概率 p = {verdict['combined_p']:.1%}",
            f"- 原始总赔率 = {verdict['combined_odds']:.3f}，"
            f"BCG ×{config.PARLAY_BCG_MULTIPLIER} 后 = {verdict['boosted_odds']:.3f}",
            f"- **合并 EV = {verdict['ev']:+.1%}**",
            f"- 最弱腿证据 = {verdict['weakest_evidence']} → 凯利分数 k = {verdict['k']}",
            f"- 凯利 f = {verdict['kelly_f']:.3f}，"
            f"**建议注额 = ${verdict['stake']:.1f}**"
            f"（本金 ${config.PARLAY_STAKE_BANKROLL:.0f}，"
            f"单注上限 ${config.PARLAY_STAKE_CAP:.0f}）",
            "",
            "> 提示：p 用大庄多庄去抽水算、实际下注在 BCG（抽水更高）；"
            "薄 EV 请更保守。3串1 需在 BCG 平台方享 ×1.15 增益。",
        ]
    else:
        lines += [
            f"### 结论：❌ 不可串 → 拆单关",
            "",
            f"- 原因：{verdict.get('reason','')}",
        ]
        if verdict.get("ev") is not None:
            lines.append(f"- （合并 EV = {verdict['ev']:+.1%}）")
        lines += [
            "",
            "> §5.2：3串1 的 ×1.15 增益是乘法，只放大已有 edge 的方向；"
            "任一腿 −EV 或证据不足，串起来必沉水下。按各腿单场结论分别决策。",
        ]
    return "\n".join(lines)

