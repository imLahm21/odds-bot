import json
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bot import analyzer, config
from bot import typesafe_decisions as decisions


STANDARD_REPORT = """## Match report contains private material
### 8. 投注决策
| ID | 玩法 | 赔率 | 门槛=1/赔率 | p_市场 | p_最终 | edge | 资格 |
|----|------|------|-------------|--------|--------|------|------|
| C01 | 主胜 | 2.00 | 50% | 52% | 54% | +8.0% | eligible |
| C02 | 客胜 | 1.90 | 52.6% | 49% | 48% | −8.8% | excluded |
- **选中候选 ID**：C01
- **选中项**：主胜
- **证据强度与凯利分数**：中，k=1/4
- **注额**：$2.5
### 9. 报告归档
not part of section 8
"""

LEGACY_REPORT = """### 8. 投注决策
| 玩法 | 赔率 | 门槛=1/赔率 | p_市场 | p_最终 | edge |
|------|------|-------------|--------|--------|------|
| 主胜 | 2.00 | 50% | 52% | 54% | +8.0% |
- **选中项**：主胜
- **证据强度与凯利分数**：弱，理论 k=1/8
- **注额**：$2.5
"""

AMBIGUOUS_REPORT = """### 8. 投注决策
| 玩法 | 赔率 | 门槛=1/赔率 | p_市场 | p_最终 | edge |
|------|------|-------------|--------|--------|------|
| 主胜 | 2.00 | 50% | 52% | 54% | +8.0% |
| 客胜 | 1.90 | 52.6% | 49% | 48% | −8.8% |
- **选中项**：理论比较后优先考虑其中一项
- **证据强度与凯利分数**：中
- **注额**：$2.5
"""


class TestDecisionParsing(unittest.TestCase):
    def test_extracts_only_section_8(self):
        section = decisions.extract_decision_section(STANDARD_REPORT)
        self.assertIsNotNone(section)
        self.assertIn("C01", section)
        self.assertNotIn("private material", section)
        self.assertNotIn("section 8", section)

    def test_standard_table_uses_explicit_id_and_copies_values(self):
        result = decisions.parse_decision_section(STANDARD_REPORT)
        self.assertIsNotNone(result)
        self.assertEqual(result.selected_id, "C01")
        self.assertEqual(result.source, "explicit_id")
        self.assertEqual(result.evidence, "medium")
        self.assertEqual(result.stake, 2.5)
        self.assertEqual(result.candidates[0].p_market, 0.52)
        self.assertEqual(result.candidates[0].p_final, 0.54)
        self.assertEqual(result.candidates[0].edge, 0.08)
        self.assertEqual(result.candidates[1].eligibility, "excluded")
        output = decisions.decision_to_extract_dict(result)
        self.assertEqual(
            output,
            {
                "pass": False,
                "play": "主胜",
                "odds": 2.0,
                "edge": 0.08,
                "p_final": 0.54,
                "evidence": "medium",
                "stake": 2.5,
                "warnings": [],
            },
        )

    def test_legacy_table_assigns_ids_and_exactly_matches_name(self):
        result = decisions.parse_decision_section(LEGACY_REPORT)
        self.assertEqual(result.source, "exact_name")
        self.assertEqual(result.selected_id, "C01")
        self.assertEqual(result.candidates[0].candidate_id, "C01")
        self.assertEqual(result.evidence, "weak")

    def test_unicode_percent_sign_and_minus_are_normalized(self):
        report = LEGACY_REPORT.replace(
            "+8.0%", "−2.5％"
        ).replace("52% | 54%", "５２％ | ５４％")
        result = decisions.parse_decision_section(report)
        self.assertEqual(result.candidates[0].p_market, 0.52)
        self.assertEqual(result.candidates[0].p_final, 0.54)
        self.assertEqual(result.candidates[0].edge, -0.025)

    def test_pass_is_normalized_to_existing_contract(self):
        report = STANDARD_REPORT.replace(
            "- **选中候选 ID**：C01", "- **选中候选 ID**：PASS"
        ).replace("- **选中项**：主胜", "- **选中项**：pass，空仓")
        result = decisions.parse_decision_section(report)
        self.assertTrue(result.is_pass)
        self.assertEqual(result.selected_id, "PASS")
        self.assertEqual(
            decisions.decision_to_extract_dict(result),
            {
                "pass": True,
                "play": "",
                "odds": 0.0,
                "edge": 0.0,
                "p_final": 0.0,
                "evidence": "medium",
                "stake": 0.0,
                "warnings": [],
            },
        )

    def test_no_positive_eligible_edge_is_deterministic_pass(self):
        report = LEGACY_REPORT.replace("+8.0%", "−2.0%").replace(
            "- **选中项**：主胜", "")
        result = decisions.parse_decision_section(report)
        self.assertEqual(result.source, "deterministic_pass")
        self.assertEqual(result.selected_id, "PASS")
        self.assertTrue(result.is_pass)

    def test_nonfinite_stake_forces_fallback(self):
        report = STANDARD_REPORT.replace("$2.5", "$Inf")
        result = decisions.parse_decision_section(report)
        self.assertIsNone(decisions.decision_to_extract_dict(result))
        self.assertIn("invalid_stake", result.warnings)

    def test_legacy_no_quote_row_is_excluded_without_inventing_numbers(self):
        report = LEGACY_REPORT.replace(
            "| 主胜 | 2.00 | 50% | 52% | 54% | +8.0% |",
            "| 主胜 | 2.00 | 50% | 52% | 54% | +8.0% |\n"
            "| 双重机会 | 无报价 | — | — | — | 无法计算，不编造 |",
        )
        result = decisions.parse_decision_section(report)
        self.assertEqual(result.source, "exact_name")
        self.assertEqual(result.candidates[1].eligibility, "excluded")
        self.assertIsNone(result.candidates[1].odds)
        self.assertFalse(any("invalid_candidate_numbers" in w
                             for w in result.warnings))
    def test_duplicate_or_nonsequential_ids_are_rejected(self):
        report = STANDARD_REPORT.replace("| C02 |", "| C01 |")
        result = decisions.parse_decision_section(report)
        self.assertEqual(result.source, "invalid")
        self.assertIn("invalid_or_nonsequential_candidate_id", result.warnings)

    def test_unknown_or_excluded_selected_id_is_rejected(self):
        unknown = STANDARD_REPORT.replace("选中候选 ID**：C01", "选中候选 ID**：C99")
        self.assertEqual(
            decisions.parse_decision_section(unknown).source, "invalid")
        excluded = STANDARD_REPORT.replace("选中候选 ID**：C01", "选中候选 ID**：C02")
        self.assertEqual(
            decisions.parse_decision_section(excluded).source, "invalid")

    def test_nonfinite_and_out_of_range_numbers_are_rejected(self):
        nonfinite = STANDARD_REPORT.replace("52% | 54%", "NaN | Inf")
        parsed = decisions.parse_decision_section(nonfinite)
        self.assertEqual(parsed.source, "invalid")
        self.assertTrue(any("invalid_candidate_numbers" in w for w in parsed.warnings))
        out_of_range = STANDARD_REPORT.replace("52% | 54%", "52% | 101%")
        self.assertEqual(
            decisions.parse_decision_section(out_of_range).source, "invalid")

    def test_string_false_is_not_truthy_in_legacy_fallback(self):
        response = (
            '{"pass":"false","play":"主胜","odds":2,"edge":0.08,'
            '"p_final":0.54,"evidence":"medium","stake":2.5}'
        )
        with (
            patch.object(config, "TYPESAFE_DECISION_MODE", "off"),
            patch.object(analyzer, "available", return_value=True),
            patch.object(analyzer, "_call_llm", return_value=response),
        ):
            result = analyzer.extract_decision("legacy report")
        self.assertFalse(result["pass"])


class TestChoiceAdapter(unittest.TestCase):
    def test_missing_key_returns_without_importing_sdk(self):
        with (
            patch.object(config, "TYPESAFE_API_KEY", ""),
            patch.dict(sys.modules, {"typesafe_sdk": None}),
        ):
            judgment, reason = decisions._request_choice_result(
                {"text": "x"}, {"a": None}, "choose", question_id="q")
        self.assertIsNone(judgment)
        self.assertEqual(reason, "missing_api_key")

    def test_sdk_response_is_validated_and_metadata_is_copied(self):
        captured = {}

        class FakeChoice:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        class FakeRetryPolicy:
            def __init__(self, **kwargs):
                captured["retry"] = kwargs

        class FakeClient:
            def __init__(self, **kwargs):
                captured["client"] = kwargs

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def system_one(self, **kwargs):
                captured["request"] = kwargs
                return SimpleNamespace(
                    answers={
                        "q": SimpleNamespace(
                            choice="a",
                            confidence=0.9,
                            probabilities={"a": 0.9, "b": 0.1},
                        )
                    },
                    model="jev-1.13.0",
                    request_id="req-test",
                    usage=SimpleNamespace(input_tokens=12, output_tokens=4),
                )

        sdk = types.ModuleType("typesafe_sdk")
        sdk.Choice = FakeChoice
        sdk.RetryPolicy = FakeRetryPolicy
        sdk.TypeSafeClient = FakeClient
        with (
            patch.object(config, "TYPESAFE_API_KEY", "test-key"),
            patch.object(config, "TYPESAFE_BASE_URL", ""),
            patch.dict(sys.modules, {"typesafe_sdk": sdk}),
        ):
            judgment, reason = decisions._request_choice_result(
                {"text": "short"}, {"a": None, "b": None},
                "choose one", question_id="q")

        self.assertIsNone(reason)
        self.assertEqual(judgment.choice, "a")
        self.assertEqual(judgment.request_id, "req-test")
        self.assertEqual(judgment.actual_model, "jev-1.13.0")
        self.assertEqual(dict(judgment.token_usage), {
            "input_tokens": 12, "output_tokens": 4,
        })
        self.assertEqual(captured["client"]["timeout"], 5.0)
        self.assertEqual(captured["retry"]["max_retries"], 2)
        self.assertEqual(captured["retry"]["timeout"], 12.0)
        self.assertIn(429, captured["retry"]["http_statuses"])
        self.assertIn(503, captured["retry"]["http_statuses"])

    def test_bad_choice_probability_shape_fails_closed(self):
        class FakeChoice:
            def __init__(self, **kwargs):
                pass

        class FakeRetryPolicy:
            def __init__(self, **kwargs):
                pass

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def system_one(self, **kwargs):
                return SimpleNamespace(
                    answers={
                        "q": SimpleNamespace(
                            choice="unknown",
                            confidence=0.99,
                            probabilities={"a": 1.0},
                        )
                    },
                    model="jev-1.13.0",
                    request_id="req",
                    usage=None,
                )

        sdk = types.ModuleType("typesafe_sdk")
        sdk.Choice = FakeChoice
        sdk.RetryPolicy = FakeRetryPolicy
        sdk.TypeSafeClient = FakeClient
        with (
            patch.object(config, "TYPESAFE_API_KEY", "test-key"),
            patch.dict(sys.modules, {"typesafe_sdk": sdk}),
        ):
            judgment, reason = decisions._request_choice_result(
                {}, {"a": None}, "choose", question_id="q")
        self.assertIsNone(judgment)
        self.assertEqual(reason, "unknown_choice")

    def test_request_exception_log_does_not_include_exception_body(self):
        marker = "private-report-marker"

        class FakeChoice:
            def __init__(self, **kwargs):
                pass

        class FakeRetryPolicy:
            def __init__(self, **kwargs):
                pass

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def system_one(self, **kwargs):
                raise RuntimeError(marker)

        sdk = types.ModuleType("typesafe_sdk")
        sdk.Choice = FakeChoice
        sdk.RetryPolicy = FakeRetryPolicy
        sdk.TypeSafeClient = FakeClient
        with (
            patch.object(config, "TYPESAFE_API_KEY", "test-key"),
            patch.dict(sys.modules, {"typesafe_sdk": sdk}),
            self.assertLogs(decisions.log, level="WARNING") as logs,
        ):
            judgment, reason = decisions._request_choice_result(
                {"text": marker}, {"a": None}, "choose", question_id="q")
        self.assertIsNone(judgment)
        self.assertNotIn(marker, "\n".join(logs.output))


class TestLessonRouting(unittest.TestCase):
    def test_only_sections_four_and_five_reach_choice(self):
        report = """private prefix
### 1. Original analysis
do not send
## Review
### 4. 信号有效性复盘
section four
### 5. 经验教训
section five
### 6. Other review text
do not send
"""
        probabilities = {
            "topic_a": 0.7,
            "topic_b": 0.1,
            "topic_c": 0.1,
            "__new_topic__": 0.1,
        }
        answer = decisions.ChoiceJudgment(
            choice="topic_a", confidence=0.8,
            probabilities=probabilities,
            requested_model="jev-1.13.0",
            actual_model="jev-1.13.0",
            request_id="req",
            latency_ms=5,
        )
        with patch.object(
            decisions, "_request_choice_result", return_value=(answer, None)
        ) as request:
            result = decisions.route_lesson_topic(
                report,
                {
                    "topic_a": {"trigger": "trigger A", "exclusions": "exclude A"},
                    "topic_b": {"trigger": "trigger B", "exclusions": ""},
                    "topic_c": {"trigger": "trigger C", "exclusions": ""},
                },
                "Home", "Away", "League",
            )
        state = request.call_args.args[0]
        state_text = json.dumps(state, ensure_ascii=False)
        self.assertEqual(result.recommended, "topic_a")
        self.assertEqual(result.candidates, ("topic_a", "topic_b", "topic_c"))
        self.assertIn("section four", state_text)
        self.assertIn("section five", state_text)
        self.assertIn("exclude A", state_text)
        self.assertNotIn("private prefix", state_text)
        self.assertNotIn("Original analysis", state_text)
        self.assertNotIn("Other review text", state_text)

    def test_active_high_confidence_route_returns_top_three(self):
        judgment = decisions.LessonRouteJudgment(
            recommended="topic_a",
            candidates=("topic_a", "topic_b", "topic_c"),
            need_new_topic=False,
            confidence=0.93,
            probabilities={"topic_a": 0.6, "topic_b": 0.2,
                           "topic_c": 0.1, "__new_topic__": 0.1},
        )
        with (
            patch.object(config, "TYPESAFE_LESSON_MODE", "active"),
            patch.object(config, "TYPESAFE_LESSON_MIN_CONFIDENCE", 0.9),
            patch.object(decisions, "load_lesson_topics", return_value={}),
            patch.object(decisions, "route_lesson_topic", return_value=judgment),
            patch.object(analyzer, "_route_lesson_llm") as fallback,
        ):
            route, error = analyzer.route_lesson("review", "H", "A", "L")
        self.assertIsNone(error)
        self.assertEqual(route["recommended"], "topic_a")
        self.assertEqual(route["candidates"], ["topic_a", "topic_b", "topic_c"])
        fallback.assert_not_called()

    def test_active_new_topic_judgment_uses_existing_llm_fallback(self):
        judgment = decisions.LessonRouteJudgment(
            recommended="",
            candidates=("topic_a",),
            need_new_topic=True,
            confidence=0.95,
            probabilities={"topic_a": 0.05, "__new_topic__": 0.95},
        )
        baseline = ({"recommended": "legacy", "candidates": ["legacy"]}, None)
        with (
            patch.object(config, "TYPESAFE_LESSON_MODE", "active"),
            patch.object(config, "TYPESAFE_LESSON_MIN_CONFIDENCE", 0.9),
            patch.object(decisions, "load_lesson_topics", return_value={}),
            patch.object(decisions, "route_lesson_topic", return_value=judgment),
            patch.object(analyzer, "_route_lesson_llm",
                         return_value=baseline) as fallback,
        ):
            result = analyzer.route_lesson("review", "H", "A", "L")
        self.assertIs(result, baseline)
        fallback.assert_called_once()

    def test_new_topic_is_a_no_match_outcome_without_generated_slug(self):
        answer = decisions.ChoiceJudgment(
            choice="__new_topic__", confidence=0.9,
            probabilities={"topic_a": 0.05, "__new_topic__": 0.95},
            requested_model="jev-1.13.0",
            actual_model="jev-1.13.0",
            request_id="req",
            latency_ms=4,
        )
        report = "### 4. 信号有效性复盘\nA\n### 5. 经验教训\nB"
        with patch.object(
            decisions, "_request_choice_result", return_value=(answer, None)
        ):
            result = decisions.route_lesson_topic(
                report, {"topic_a": "trigger"}, "", "", "")
        self.assertTrue(result.need_new_topic)
        self.assertEqual(result.recommended, "")
        self.assertEqual(result.candidates, ("topic_a",))


class TestDecisionResolution(unittest.TestCase):
    def test_ambiguous_legacy_report_selects_id_then_copies_row_values(self):
        probabilities = {
            "C01": 0.1, "C02": 0.8, "PASS": 0.05, "NONE": 0.05,
        }
        answer = decisions.ChoiceJudgment(
            choice="C02", confidence=0.8,
            probabilities=probabilities,
            requested_model="jev-1.13.0",
            actual_model="jev-1.13.0",
            request_id="req",
            latency_ms=7,
        )
        with patch.object(
            decisions, "_request_choice_result", return_value=(answer, None)
        ) as request:
            result = decisions.resolve_decision(AMBIGUOUS_REPORT)
        state = request.call_args.args[0]
        self.assertEqual(set(state["candidates"]), {"C01", "C02", "PASS", "NONE"})
        self.assertEqual(result.selected_id, "C02")
        output = decisions.decision_to_extract_dict(result)
        self.assertEqual(output["play"], "客胜")
        self.assertEqual(output["odds"], 1.9)
        self.assertAlmostEqual(output["edge"], -0.088)

    def test_none_or_unknown_candidate_requires_fallback(self):
        answer = decisions.ChoiceJudgment(
            choice="NONE", confidence=0.95,
            probabilities={"C01": 0.02, "C02": 0.02, "PASS": 0.01, "NONE": 0.95},
            requested_model="jev-1.13.0", actual_model="jev-1.13.0",
            request_id="req", latency_ms=1,
        )
        with patch.object(
            decisions, "_request_choice_result", return_value=(answer, None)
        ):
            result = decisions.resolve_decision(AMBIGUOUS_REPORT)
        self.assertEqual(result.source, "jev_none")
        self.assertIsNone(decisions.decision_to_extract_dict(result))

    def test_active_explicit_decision_uses_deterministic_parse(self):
        parsed = decisions.parse_decision_section(STANDARD_REPORT)
        expected = decisions.decision_to_extract_dict(parsed)
        with (
            patch.object(config, "TYPESAFE_DECISION_MODE", "active"),
            patch.object(config, "TYPESAFE_DECISION_MIN_CONFIDENCE", None),
            patch.object(decisions, "resolve_decision", return_value=parsed),
            patch.object(analyzer, "_extract_decision_llm") as fallback,
        ):
            result = analyzer.extract_decision(STANDARD_REPORT)
        self.assertEqual(result, expected)
        fallback.assert_not_called()

    def test_decision_shadow_returns_baseline_and_schedules_comparison(self):
        baseline = {
            "pass": False, "play": "baseline", "odds": 2.0, "edge": 0.1,
            "p_final": 0.55, "evidence": "medium", "stake": 1.0,
        }
        with (
            patch.object(config, "TYPESAFE_DECISION_MODE", "shadow"),
            patch.object(analyzer, "_extract_decision_llm",
                         return_value=baseline),
            patch.object(analyzer, "_start_typesafe_shadow") as schedule,
        ):
            result = analyzer.extract_decision(STANDARD_REPORT, visitor=True)
        self.assertIs(result, baseline)
        schedule.assert_called_once()
        self.assertIs(schedule.call_args.args[0], decisions.log_decision_shadow)
    def test_active_low_confidence_jev_falls_back_with_visitor_role(self):
        result = decisions.DecisionParseResult(
            selected_id="C01", is_pass=False, evidence="medium", stake=1.0,
            candidates=(
                decisions.DecisionCandidate(
                    "C01", "主胜", 2.0, 0.52, 0.54, 0.08, "eligible", "| C01 |"
                ),
            ),
            source="jev", confidence=0.4,
        )
        response = (
            '{"pass":false,"play":"主胜","odds":2,"edge":0.08,'
            '"p_final":0.54,"evidence":"medium","stake":1}'
        )
        with (
            patch.object(config, "TYPESAFE_DECISION_MODE", "active"),
            patch.object(config, "TYPESAFE_DECISION_MIN_CONFIDENCE", 0.9),
            patch.object(decisions, "resolve_decision", return_value=result),
            patch.object(analyzer, "available", return_value=True),
            patch.object(analyzer, "_call_llm", return_value=response) as call,
        ):
            output = analyzer.extract_decision(
                AMBIGUOUS_REPORT, visitor=True)
        self.assertEqual(output["play"], "主胜")
        self.assertTrue(call.call_args.kwargs["visitor"])


class TestShadowBehavior(unittest.TestCase):
    def test_lesson_shadow_returns_baseline_route_for_admin_buttons(self):
        jwt_stub = types.ModuleType("jwt")
        jwt_stub.encode = lambda *args, **kwargs: "token"
        markdown_stub = types.ModuleType("markdown")
        markdown_stub.markdown = lambda text, **kwargs: text
        with patch.dict(sys.modules, {
            "jwt": jwt_stub,
            "markdown": markdown_stub,
        }):
            from bot import tgbot

        baseline = {
            "recommended": "topic_a",
            "candidates": ["topic_a", "topic_b"],
            "reason": "baseline",
            "need_new_topic": False,
            "new_topic_slug": None,
        }
        with (
            patch.object(config, "TYPESAFE_LESSON_MODE", "shadow"),
            patch.object(analyzer, "_route_lesson_llm",
                         return_value=(baseline, None)),
            patch.object(analyzer, "_start_typesafe_shadow") as schedule,
        ):
            result = analyzer.route_lesson("report", "Home", "Away", "League")
        self.assertIs(result[0], baseline)
        schedule.assert_called_once()
        keyboard = tgbot._lesson_topic_keyboard("token", result[0])
        callbacks = [
            row[0]["callback_data"] for row in keyboard["inline_keyboard"]
        ]
        self.assertEqual(callbacks[:2], [
            "la:token:topic_a", "la:token:topic_b",
        ])
        self.assertIn("la:token:__new__", callbacks)

    def test_decision_shadow_log_contains_hash_not_report_or_raw_row(self):
        report = """private-outside-marker
### 8. 投注决策
| ID | 玩法 | 赔率 | 门槛=1/赔率 | p_市场 | p_最终 | edge | 资格 |
|----|------|------|-------------|--------|--------|------|------|
| C01 | row_private_marker | 2.00 | 50% | 52% | 54% | +8.0% | eligible |
- **选中候选 ID**：C01
- **选中项**：主胜
- **注额**：$2.5
"""
        baseline = {
            "pass": False, "play": "baseline_play", "odds": 2.0,
            "edge": 0.08, "p_final": 0.54, "evidence": "medium",
            "stake": 2.5,
        }
        with self.assertLogs(decisions.log, level="INFO") as captured:
            decisions.log_decision_shadow(report, baseline)
        line = "\n".join(captured.output)
        self.assertIn("TYPESAFE_SHADOW", line)
        self.assertNotIn("private-outside-marker", line)
        self.assertNotIn("row_private_marker", line)
        record = json.loads(line.split("TYPESAFE_SHADOW ", 1)[1])
        self.assertEqual(record["feature"], "decision")
        self.assertEqual(record["typesafe_choice"], "C01")
        self.assertEqual(len(record["case_hash"]), 20)

if __name__ == "__main__":
    unittest.main()
