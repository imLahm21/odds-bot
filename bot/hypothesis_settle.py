"""假设按比分结算（从 scripts/backtest_hypotheses.py 移入，逻辑不变）。

回测脚本、登记后样本统计（scripts/build_hypothesis_stats.py）与复盘（settlement_block）
共用同一套结算口径：亚盘按基准庄终盘主线五态结算，胜平负按基准庄终盘欧赔。
"""

from __future__ import annotations

import logging
import math

from . import hypotheses, market_facts

log = logging.getLogger("odds_bot.hypothesis_settle")


def settle_home(margin: int, line: float) -> float:
    """主队让球 line（负 = 让出）在主队净胜 margin 下的结算：1 / .5 / 0 / −.5 / −1。"""
    def one(l: float) -> float:
        adj = margin + l
        return 1.0 if adj > 1e-9 else (-1.0 if adj < -1e-9 else 0.0)
    if abs(line * 4) % 2 == 1:
        return (one(line - 0.25) + one(line + 0.25)) / 2
    return one(line)


def pnl(result: float, odds: float) -> float:
    if result > 0:
        return result * (odds - 1)
    return result


class Tally:
    __slots__ = ("n", "hits", "misses", "push", "stake", "ret", "pnls", "dated")

    def __init__(self):
        self.n = self.hits = self.misses = self.push = 0
        self.stake = self.ret = 0.0
        self.pnls: list[float] = []
        self.dated: list[tuple[str, float]] = []

    def add(self, result: float, odds: float | None, when: str):
        self.n += 1
        if result > 0:
            self.hits += 1
        elif result < 0:
            self.misses += 1
        else:
            self.push += 1
        if odds:
            p = pnl(result, odds)
            self.stake += 1
            self.ret += p
            self.pnls.append(p)
            self.dated.append((when, p))

    @property
    def hit_rate(self) -> float | None:
        d = self.hits + self.misses
        return self.hits / d if d else None

    @property
    def roi(self) -> float | None:
        return self.ret / self.stake if self.stake else None

    def ci95(self) -> tuple[float, float] | None:
        d = self.hits + self.misses
        if not d:
            return None
        p, z = self.hits / d, 1.96
        denom = 1 + z * z / d
        center = (p + z * z / (2 * d)) / denom
        half = z * math.sqrt(p * (1 - p) / d + z * z / (4 * d * d)) / denom
        return center - half, center + half

    def halves_same_sign(self) -> bool | None:
        if len(self.dated) < 2:
            return None
        rows = sorted(self.dated)
        mid = len(rows) // 2
        a, b = sum(p for _, p in rows[:mid]), sum(p for _, p in rows[mid:])
        return (a > 0) == (b > 0)

    def robust_sign(self) -> bool | None:
        if len(self.pnls) < 2:
            return None
        drop = max(self.pnls, key=abs)
        rest = list(self.pnls)
        rest.remove(drop)
        return (sum(rest) > 0) == (self.ret > 0)


def settle_trigger(t: hypotheses.Trigger, facts: market_facts.MarketFacts,
                   score: tuple[int, int]) -> tuple[float, float | None] | None:
    gh, ga = score
    if t.market == "ah" and facts.lines:
        final = facts.lines[-1]
        r_home = settle_home(gh - ga, final.handicap)
        upper_is_home = facts.upper == "home"
        r_upper = r_home if upper_is_home else -r_home
        if t.side == "upper":
            return r_upper, final.upper_water
        return -r_upper, final.lower_water
    if t.market == "1x2":
        outcome = "home" if gh > ga else ("away" if gh < ga else "draw")
        if t.side.startswith("not_"):
            return (1.0 if outcome != t.side[4:] else -1.0), None
        odds = (facts.euro_final or {}).get(t.side)
        return (1.0 if outcome == t.side else -1.0), odds
    return None


def _verdict(settled: tuple[float, float | None] | None) -> str:
    if settled is None:
        return "无法结算"
    result = settled[0]
    if result > 0:
        return "符合"
    if result < 0:
        return "不符合"
    return "走盘"


def settlement_block(csv_text: str, league: str, score: tuple[int, int]) -> str:
    """复盘用【假设结算】表：本场已触发的可机械判定假设按全场比分由代码结算。
    任何异常返回空串并记 warning（不能阻断复盘）。"""
    try:
        facts = market_facts.build_facts(market_facts.rows_from_csv(csv_text), league)
        triggers = hypotheses.evaluate(facts)
        head = ["### 假设结算（代码按比分计算）"]
        if not triggers:
            return "\n".join(head + ["- 本场未触发可机械判定的假设"])
        registry = hypotheses.registry_status()
        out = head + ["| 假设 | 状态 | 预测 | 结算 |", "|------|------|------|------|"]
        for t in triggers:
            status = registry.get(t.code, ("假设", "—"))[0]
            prediction = hypotheses._PRED_ZH.get((t.market, t.side), t.side or "无方向")
            out.append(f"| {t.code} | {status} | {prediction} | "
                       f"{_verdict(settle_trigger(t, facts, score))} |")
        return "\n".join(out)
    except Exception as exc:  # noqa: BLE001
        log.warning("假设结算失败（%s），本场复盘不注入结算表", type(exc).__name__)
        return ""
