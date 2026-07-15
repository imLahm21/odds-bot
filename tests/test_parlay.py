"""parlay 纯逻辑测试（3串1 准入闸门 + 合并 EV×1.15 + 注额裁剪），不碰 LLM。

对齐 reference_staking_kelly.md §5.2：三腿全过准入(edge>0 且证据≥中)才可串；
任一负 edge / 证据不足 / 同场腿 → 拆单关。
运行：python -m pytest tests/test_parlay.py  或  python tests/test_parlay.py
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import parlay, config   # noqa: E402


def _leg(fid, edge, odds=2.0, p_final=0.55, evidence="medium", passed=False):
    return {"fid": fid, "home": f"H{fid}", "away": f"A{fid}", "play": "主胜",
            "odds": odds, "edge": edge, "p_final": p_final,
            "evidence": evidence, "pass": passed}


class TestEvidenceNorm(unittest.TestCase):
    def test_aliases(self):
        self.assertEqual(parlay.norm_evidence("强"), "strong")
        self.assertEqual(parlay.norm_evidence("MEDIUM"), "medium")
        self.assertEqual(parlay.norm_evidence("弱"), "weak")
        self.assertEqual(parlay.norm_evidence(""), "none")
        self.assertEqual(parlay.norm_evidence(None), "none")
        self.assertEqual(parlay.norm_evidence("看不懂"), "none")  # 未知→最保守


class TestGate(unittest.TestCase):
    def test_all_positive_can_parlay(self):
        legs = [_leg(1, 0.06), _leg(2, 0.08), _leg(3, 0.05)]
        v = parlay.evaluate(legs)
        self.assertTrue(v["can_parlay"])
        self.assertGreater(v["ev"], 0)
        self.assertGreater(v["stake"], 0)

    def test_one_negative_edge_rejected(self):
        legs = [_leg(1, 0.06), _leg(2, -0.02), _leg(3, 0.05)]
        v = parlay.evaluate(legs)
        self.assertFalse(v["can_parlay"])
        self.assertIn("腿2", v["reason"])

    def test_weak_evidence_rejected(self):
        legs = [_leg(1, 0.06), _leg(2, 0.08), _leg(3, 0.05, evidence="weak")]
        v = parlay.evaluate(legs)
        self.assertFalse(v["can_parlay"])
        self.assertIn("腿3", v["reason"])

    def test_pass_leg_rejected(self):
        legs = [_leg(1, 0.06), _leg(2, 0.08), _leg(3, 0.0, passed=True)]
        v = parlay.evaluate(legs)
        self.assertFalse(v["can_parlay"])

    def test_same_fixture_rejected(self):
        legs = [_leg(1, 0.06), _leg(1, 0.08), _leg(3, 0.05)]  # 两腿同 fid
        v = parlay.evaluate(legs)
        self.assertFalse(v["can_parlay"])
        self.assertIn("同场", v["reason"])

    def test_wrong_leg_count(self):
        v = parlay.evaluate([_leg(1, 0.06), _leg(2, 0.08)])
        self.assertFalse(v["can_parlay"])


class TestMath(unittest.TestCase):
    def test_combined_ev_and_boost(self):
        # 三腿各 p=0.55、odds=2.0 → p=0.166, O=8.0, boosted=9.2, EV=0.166*9.2-1≈+0.53
        legs = [_leg(1, 0.06, odds=2.0, p_final=0.55),
                _leg(2, 0.06, odds=2.0, p_final=0.55),
                _leg(3, 0.06, odds=2.0, p_final=0.55)]
        v = parlay.evaluate(legs)
        self.assertAlmostEqual(v["combined_p"], 0.55 ** 3, places=6)
        self.assertAlmostEqual(v["combined_odds"], 8.0, places=6)
        self.assertAlmostEqual(v["boosted_odds"],
                               8.0 * config.PARLAY_BCG_MULTIPLIER, places=6)
        self.assertAlmostEqual(v["ev"], 0.55 ** 3 * 9.2 - 1, places=6)

    def test_stake_capped(self):
        # 极端高 edge/赔率下注额应被单注上限裁到 PARLAY_STAKE_CAP
        legs = [_leg(1, 0.5, odds=3.0, p_final=0.9),
                _leg(2, 0.5, odds=3.0, p_final=0.9),
                _leg(3, 0.5, odds=3.0, p_final=0.9)]
        v = parlay.evaluate(legs)
        self.assertTrue(v["can_parlay"])
        self.assertLessEqual(v["stake"], config.PARLAY_STAKE_CAP)

    def test_weakest_evidence_sets_k(self):
        # 一腿 strong 两腿 medium → 最弱 medium → k=0.25
        legs = [_leg(1, 0.06, evidence="strong"),
                _leg(2, 0.06, evidence="medium"),
                _leg(3, 0.06, evidence="medium")]
        v = parlay.evaluate(legs)
        self.assertEqual(v["weakest_evidence"], "medium")
        self.assertEqual(v["k"], config.PARLAY_EVIDENCE_K["medium"])


class TestRender(unittest.TestCase):
    def test_render_contains_beta_and_verdict(self):
        legs = [_leg(1, 0.06), _leg(2, 0.08), _leg(3, 0.05)]
        md = parlay.render_report(parlay.evaluate(legs))
        self.assertIn("Beta", md)
        self.assertIn("可串", md)

    def test_render_reject(self):
        legs = [_leg(1, 0.06), _leg(2, -0.02), _leg(3, 0.05)]
        md = parlay.render_report(parlay.evaluate(legs))
        self.assertIn("拆单关", md)


if __name__ == "__main__":
    unittest.main(verbosity=2)
