"""Offline coverage of Jev finalization in the full-model pipeline.

Model transports are stubbed; candidate parsing, fallbacks, stake calculation
and the decision_final contract use the production implementations.
"""

from contextlib import ExitStack
import os
import sys
from threading import Event
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("APIFOOTBALL_KEY", "offline-test-key")

from bot import analyzer, config, full_model_settings, multi_analyzer, typesafe_decisions as decisions  # noqa: E402


_REPORT = (
    "".join(f"### {n}. 分析\n" + "逐项核对原始证据。" * 12 + "\n"
            for n in range(1, 7))
    + """### 7. 最终精算结论
- **亚盘判定**：上盘，赛前结论标记
- **置信度**：72 / 100
### 8. 投注决策
| ID | 玩法 | 赔率 | 门槛=1/赔率 | p_市场 | p_最终 | edge | 资格 |
|----|------|------|-------------|--------|--------|------|------|
| C01 | 主胜 | 2.00 | 50% | 52% | 54% | +8.0% | eligible |
| C02 | 大 2.5 | 1.90 | 52.6% | 55% | 57% | +8.3% | eligible |
| C03 | 客胜 | 4.00 | 25% | 22% | 22% | -12.0% | eligible |
| C04 | 双进 Yes | 3.00 | 33.3% | 40% | 40% | +20.0% | excluded |
- **选中候选 ID**：C02
- **选中项**：大 2.5
- **证据强度与凯利分数**：中，k=1/4
- **注额**：$2.0
"""
)


def _judgment(choice="C01", confidence=0.9):
    probabilities = dict.fromkeys(("C01", "C02", "PASS"), (1 - confidence) / 2)
    probabilities[choice] = confidence
    return decisions.ChoiceJudgment(
        choice=choice, confidence=confidence, probabilities=probabilities,
        requested_model="jev-offline-test", actual_model="jev-offline-test",
        request_id="offline", latency_ms=1,
    )


class TestMultiJev(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(
            multi_analyzer, "profile_snapshot", side_effect=full_model_settings.defaults))
        stack.enter_context(patch.object(config, "TYPESAFE_DECISION_MODE", "active"))
        stack.enter_context(patch.object(config, "TYPESAFE_DECISION_MIN_CONFIDENCE", 0.8))
        stack.enter_context(patch.object(
            multi_analyzer, "_run_one",
            side_effect=lambda task, bundle: {
                "id": task["id"], "label": task["label"],
                "model": task["model"], "effort": task["effort"],
                "status": "ok", "card": {},
            },
        ))
        self.synthesize = stack.enter_context(patch.object(
            multi_analyzer, "_synthesize", return_value=_REPORT))
        self.audit = stack.enter_context(patch.object(
            multi_analyzer, "_audit_report", return_value={"ok": True, "issues": []}))
        self.repair = stack.enter_context(patch.object(
            multi_analyzer, "_call_route", return_value=(None, {"error": "offline"})))
        self.request = stack.enter_context(patch.object(
            decisions, "_request_choice_result", return_value=(_judgment(), None)))
        self.shadow = stack.enter_context(patch.object(analyzer, "_start_typesafe_shadow"))
        self.finalize = stack.enter_context(patch.object(
            analyzer, "finalize_decision", wraps=analyzer.finalize_decision))

    def _run(self, report=_REPORT, *, fundamentals="fund", **kwargs):
        self.synthesize.return_value = report
        return multi_analyzer.run(
            "csv", fundamentals, "Home", "Away", "League", **kwargs)

    def test_active_finalizes_validated_candidates_before_appendix(self):
        report, summary = self._run()
        final = decisions.read_final_decision(report)
        self.assertTrue(summary["audit"]["ok"])
        self.assertIsNotNone(final)
        self.assertEqual(final["play"], "主胜")
        self.assertEqual(final["odds"], 2.0)
        self.assertAlmostEqual(final["edge"], 0.08)
        self.assertAlmostEqual(final["p_final"], 0.54)
        self.assertEqual(final["stake"], 2.0)
        self.assertTrue(report.startswith(_REPORT.split("### 8.", 1)[0]))
        self.assertIn("模型初选", report)
        self.assertLess(report.index("decision_final"), report.index("### 全模型执行记录"))
        self.finalize.assert_called_once_with(_REPORT, "Home", "Away", "League")
        self.request.assert_called_once()
        state, criteria, _ = self.request.call_args.args
        self.assertEqual(set(criteria), {"C01", "C02", "PASS"})
        self.assertEqual(state["match"], {"home": "Home", "away": "Away", "league": "League"})
        self.assertEqual(state["report_confidence"], 72)
        self.assertNotIn("全模型执行记录", state["section_7_conclusion"])

    def test_threshold_boundary_and_low_confidence_fallback(self):
        for confidence, expected in ((0.8, "主胜"), (0.799, "大 2.5")):
            with self.subTest(confidence=confidence):
                self.request.return_value = (_judgment(confidence=confidence), None)
                report, _ = self._run()
                self.assertEqual(decisions.read_final_decision(report)["play"], expected)
                if confidence < 0.8:
                    self.assertIn("Jev 未参与", report)

    def test_confident_jev_pass_has_zero_stake(self):
        self.request.return_value = (_judgment("PASS"), None)
        report, _ = self._run()
        final = decisions.read_final_decision(report)
        self.assertTrue(final["pass"])
        self.assertEqual(final["stake"], 0.0)

    def test_jev_failure_falls_back_to_highest_positive_edge(self):
        self.request.return_value = (None, "timeout")
        report, _ = self._run()
        self.assertEqual(decisions.read_final_decision(report)["play"], "大 2.5")
        self.assertIn("Jev 未参与", report)

    def test_jev_failure_preserves_explicit_original_pass(self):
        original = _REPORT.replace("选中候选 ID**：C02", "选中候选 ID**：PASS").replace(
            "选中项**：大 2.5", "选中项**：PASS")
        self.request.return_value = (None, "timeout")
        report, _ = self._run(original)
        self.assertTrue(decisions.read_final_decision(report)["pass"])
        self.assertIn("保留模型初选 PASS", report)

    def test_no_eligible_positive_edge_passes_without_jev(self):
        original = _REPORT.replace("52% | 54% | +8.0%", "52% | 48% | -4.0%").replace(
            "55% | 57% | +8.3%", "55% | 50% | -5.0%")
        report, _ = self._run(original)
        self.assertTrue(decisions.read_final_decision(report)["pass"])
        self.request.assert_not_called()

    def test_malformed_candidate_table_keeps_original_report(self):
        original = _REPORT.replace("| C01 | 主胜 | 2.00 |", "| C01 | 主胜 |")
        report, _ = self._run(original)
        self.assertTrue(report.startswith(original))
        self.assertIsNone(decisions.read_final_decision(report))
        self.request.assert_not_called()

    def test_off_mode_does_not_change_report_or_call_jev(self):
        with patch.object(config, "TYPESAFE_DECISION_MODE", "off"):
            report, _ = self._run()
        self.assertTrue(report.startswith(_REPORT))
        self.assertIsNone(decisions.read_final_decision(report))
        self.request.assert_not_called()
        self.shadow.assert_not_called()

    def test_shadow_mode_schedules_only_the_audited_report(self):
        with patch.object(config, "TYPESAFE_DECISION_MODE", "shadow"):
            report, _ = self._run()
        self.assertTrue(report.startswith(_REPORT))
        self.assertIsNone(decisions.read_final_decision(report))
        self.request.assert_not_called()
        self.shadow.assert_called_once_with(
            decisions.log_selection_shadow, _REPORT, "Home", "Away", "League",
            name="typesafe-select-shadow")

    def test_jev_runs_once_after_repair_and_reaudit_on_repaired_data(self):
        repaired = _REPORT.replace("赛前结论标记", "修复后的结论标记").replace(
            "52% | 54% | +8.0%", "52% | 60% | +20.0%").strip()
        events = []

        def audit(bundle, report, cards, **kwargs):
            events.append("audit")
            return {"ok": report == repaired, "issues": [] if report == repaired else ["fix"]}

        def repair(*args, **kwargs):
            events.append("repair")
            return repaired, {}

        def request(*args, **kwargs):
            events.append("decision")
            return _judgment(), None

        self.audit.side_effect = audit
        self.repair.side_effect = repair
        self.request.side_effect = request
        report, _ = self._run()
        self.assertEqual(events, ["audit", "repair", "audit", "decision"])
        self.finalize.assert_called_once_with(repaired, "Home", "Away", "League")
        self.request.assert_called_once()
        self.assertIn("修复后的结论标记", self.request.call_args.args[0]["section_7_conclusion"])
        final = decisions.read_final_decision(report)
        self.assertAlmostEqual(final["p_final"], 0.60)
        self.assertAlmostEqual(final["edge"], 0.20)
        self.assertEqual(final["stake"], 5.0)

    def test_cancellation_during_audit_skips_jev(self):
        cancel = Event()

        def audit(*args, **kwargs):
            cancel.set()
            return {"ok": True, "issues": []}

        self.audit.side_effect = audit
        report, summary = self._run(cancel=cancel)
        self.assertEqual(report, "")
        self.assertTrue(summary["cancelled"])
        self.finalize.assert_not_called()
        self.request.assert_not_called()

    def test_cancellation_during_jev_discards_report(self):
        cancel = Event()

        def request(*args, **kwargs):
            cancel.set()
            return _judgment(), None

        self.request.side_effect = request
        report, summary = self._run(cancel=cancel)
        self.assertEqual(report, "")
        self.assertTrue(summary["cancelled"])

    def test_parlay_reads_final_contract_without_reselecting(self):
        report, _ = self._run()
        with (
            patch.object(analyzer, "_extract_decision_llm") as fallback,
            patch.object(decisions, "resolve_decision") as resolve,
        ):
            extracted = analyzer.extract_decision(report, "Home", "Away")
        self.assertEqual(extracted, decisions.read_final_decision(report))
        self.assertEqual(extracted["play"], "主胜")
        fallback.assert_not_called()
        resolve.assert_not_called()
        self.request.assert_called_once()

    def test_second_stage_preserves_finalized_blind_forecast_without_reselecting(self):
        forecast, _ = self._run(fundamentals="", extra_instruction="第一阶段盲推")
        self.assertEqual(decisions.read_final_decision(forecast)["play"], "主胜")
        self.assertEqual(self.synthesize.call_args.args[0]["fundamentals"], "")
        first_state = self.request.call_args.args[0]
        self.assertNotIn("实际结果标记", str(first_state))
        self.request.reset_mock()
        self.finalize.reset_mock()
        review = "\n".join(f"### {n}. 复盘\n" + "复盘证据。" * 20 for n in range(1, 7))
        with (
            patch.object(multi_analyzer, "_review_synthesize", return_value=review) as synth,
            patch.object(multi_analyzer, "_review_audit", return_value={"ok": True, "issues": []}),
        ):
            report, summary = multi_analyzer.run_review(
                "csv", forecast, "实际结果标记", "post-match fundamentals",
                "Home", "Away", "League")
        self.assertTrue(summary["audit"]["ok"])
        self.assertTrue(report.startswith(review))
        self.assertEqual(synth.call_args.args[0]["forecast"], forecast)
        self.assertEqual(synth.call_args.args[0]["result_text"], "实际结果标记")
        self.finalize.assert_not_called()
        self.request.assert_not_called()
        self.shadow.assert_not_called()


if __name__ == "__main__":
    unittest.main()
