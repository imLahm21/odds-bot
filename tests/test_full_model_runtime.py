"""Offline runtime snapshots and actual-writer/auditor separation."""

from contextlib import contextmanager, ExitStack
from copy import deepcopy
import os
import sqlite3
import sys
from threading import Event
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("APIFOOTBALL_KEY", "offline-test-key")

from offline_support import prepare_publish_stubs  # noqa: E402
prepare_publish_stubs()

from bot import analyzer, config, db, full_model_settings as settings, llm_client, multi_analyzer, tgbot  # noqa: E402


_SECTIONS_7_8 = (
    "### 7. 最终精算结论\n- **信号强度**：无\n- **数据质量**：85 / 100\n"
    "### 8. 投注决策\n"
    "| ID | 玩法 | 赔率 | 门槛=1/赔率 | p_市场 | p_最终 | edge | 资格 |\n"
    "|----|------|------|-------------|--------|--------|------|------|\n"
    "| C01 | 主胜 | 2.00 | 50% | 54% | 54% | +8.0% | eligible |\n"
    "- **选中候选 ID**：C01\n"
)


def _report(review=False):
    if review:
        return "\n".join(f"### {n}. 分析" for n in range(1, 7)) + "\n" + "证据。" * 220
    # 精算报告须能通过确定性检查（第 7 节两行 + 可解析的第 8 节候选表）
    return ("\n".join(f"### {n}. 分析" for n in range(1, 7)) + "\n"
            + _SECTIONS_7_8 + "证据。" * 220)


def _expert(task, bundle):
    return {"id": task["id"], "label": task["label"], "model": task["model"],
            "effort": task["effort"], "status": "ok", "card": {}}


class TestFullModelRuntime(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(config, "TYPESAFE_DECISION_MODE", "off"))
        self.snapshot = self.stack.enter_context(patch.object(
            multi_analyzer, "profile_snapshot", side_effect=settings.defaults))
        self.expert = self.stack.enter_context(patch.object(multi_analyzer, "_run_one", side_effect=_expert))

    @contextmanager
    def _stream_transport(self, fallback=False, review=False):
        actual = "claude-opus-5-5-high" if fallback else "gpt-6-astra-cpa"
        endpoint = {"label": "offline", "route_group": "cpa_claude" if fallback else "ik_gpt",
                    "base_url": "https://offline.invalid/v1", "key": "offline"}

        class Breaker:
            def allow(self):
                return True

            def record(self, ok):
                pass

        with ExitStack() as stack:
            stack.enter_context(patch.object(llm_client, "available", return_value=True))
            stack.enter_context(patch.object(llm_client, "get_settings", return_value={
                "stream_first_byte_timeout": 1, "stream_idle_timeout": 1, "max_retries": 0}))
            stack.enter_context(patch.object(llm_client, "_endpoints_by_priority",
                side_effect=lambda model: [(0, endpoint)] if model == actual else []))
            stack.enter_context(patch.object(llm_client, "_breakers", [Breaker()]))
            stack.enter_context(patch.object(llm_client, "_stream_one",
                side_effect=lambda *args: iter([("delta", _report(review)), ("done", _report(review))])))
            stack.enter_context(patch.object(multi_analyzer, "_synthesis_prompt", return_value=("system", "user", {})))
            stack.enter_context(patch.object(multi_analyzer, "_review_synthesis_prompt", return_value=("system", "user", {})))
            yield

    def test_actual_stream_fallback_cannot_audit_its_own_output(self):
        with self._stream_transport(fallback=True), patch.object(
            llm_client, "collect_model", return_value='{"ok":true,"issues":[]}'
        ) as chat:
            report, summary = multi_analyzer.run("csv", "fund", "Home", "Away", "League")
        self.assertTrue(report)
        self.assertEqual(summary["synthesis"], {"model": "claude-opus-5-5-high", "effort": "high"})
        self.assertEqual(summary["audit"]["route"]["model"], "gemini-4-argon-high")
        self.assertTrue(summary["audit"]["route"]["fallback_used"])
        self.assertEqual(chat.call_args.kwargs["model"], "gemini-4-argon-high")
        self.assertEqual(chat.call_args.kwargs["effort"], "high")
        chat.assert_called_once()

    def test_stream_trace_reports_only_actual_content_model_without_extra_events(self):
        used = Mock()
        with self._stream_transport(fallback=True):
            events = list(llm_client.stream_chat_model(
                "system", "user", "gpt-6-astra-cpa", effort="xhigh",
                fallback_models=("claude-opus-5-5-high",), fallback_efforts=("high",),
                on_model_used=used))
        self.assertEqual([event[0] for event in events], ["delta", "done"])
        used.assert_called_once_with("claude-opus-5-5-high", "high")

    def test_done_only_stream_still_tracks_actual_writer(self):
        used = Mock()
        with self._stream_transport(fallback=True), patch.object(
            llm_client, "_stream_one", return_value=iter([("done", _report())])
        ):
            events = list(llm_client.stream_chat_model(
                "system", "user", "gpt-6-astra-cpa", effort="xhigh",
                fallback_models=("claude-opus-5-5-high",), fallback_efforts=("high",),
                on_model_used=used))
        self.assertEqual([event[0] for event in events], ["done"])
        used.assert_called_once_with("claude-opus-5-5-high", "high")

    def test_post_match_review_uses_same_actual_writer_exclusion(self):
        with self._stream_transport(fallback=True, review=True), patch.object(
            llm_client, "collect_model", return_value='{"ok":true,"issues":[]}'
        ) as chat:
            report, summary = multi_analyzer.run_review(
                "csv", "immutable forecast", "actual result", "fund", "Home", "Away", "League")
        self.assertTrue(report)
        self.assertEqual(summary["profile"], "review")
        self.assertEqual(summary["synthesis"]["model"], "claude-opus-5-5-high")
        self.assertEqual(summary["audit"]["route"]["model"], "gemini-4-argon-high")
        chat.assert_called_once()

    def test_repair_writers_are_excluded_in_addition_to_initial_writer(self):
        calls = []

        def chat(system, user, model, **kwargs):
            if "最终报告审计员" in system:
                calls.append(("audit", model))
                return ('{"ok":false,"issues":[{"severity":"blocker","problem":"fix"}]}'
                        if model == "claude-opus-5-5-high" else '{"ok":true,"issues":[]}')
            calls.append(("repair", model))
            return "LLM 请求失败（offline）" if model == "gpt-6-astra-cpa" else _report() + "\n修复标记"

        with self._stream_transport(), patch.object(llm_client, "collect_model", side_effect=chat):
            report, summary = multi_analyzer.run("csv", "fund", "Home", "Away", "League")
        self.assertIn("修复标记", report)
        self.assertEqual(calls, [
            ("audit", "claude-opus-5-5-high"), ("repair", "gpt-6-astra-cpa"),
            ("repair", "claude-opus-5-5-high"), ("audit", "gemini-4-argon-high"),
        ])
        self.assertEqual(set(summary["writers"]), {"gpt-6-astra-cpa", "claude-opus-5-5-high"})

    def test_cpa_and_ik_names_of_same_model_are_not_independent_auditors(self):
        with patch.object(llm_client, "collect_model", return_value='{"ok":true}') as chat:
            result, route = multi_analyzer._call_route(
                "audit", "report", config.FULL_CONSULT_AUDIT, parse_json=True,
                phase="audit", excluded_models={"claude-opus-5-5"})
        self.assertTrue(result["ok"])
        self.assertEqual(route["model"], "gemini-4-argon-high")
        self.assertEqual(chat.call_args.kwargs["model"], "gemini-4-argon-high")

    def test_no_independent_auditor_is_explicit_failure_without_self_request(self):
        spec = deepcopy(config.FULL_CONSULT_AUDIT)
        spec["fallbacks"] = ({"model": "claude-opus-5-5", "effort": "high"},)
        with patch.object(llm_client, "collect_model") as chat:
            result, route = multi_analyzer._call_route(
                "audit", "report", spec, parse_json=True, phase="audit",
                excluded_models={"claude-opus-5-5-high"})
        self.assertIsNone(result)
        self.assertIn("无独立审计模型", route["error"])
        self.assertEqual(route["model"], "")
        chat.assert_not_called()

    def test_filtering_writers_does_not_invalidate_original_cross_group_chain(self):
        spec = deepcopy(config.FULL_CONSULT_AUDIT)
        spec["fallbacks"] = ({"model": "gpt-6.1-sol-cpa", "effort": "high"},
                             {"model": "gpt-6-sol", "effort": "high"})
        with patch.object(llm_client, "collect_model", side_effect=["invalid json", '{"ok":true}']) as chat:
            result, route = multi_analyzer._call_route(
                "audit", "report", spec, parse_json=True, phase="audit",
                excluded_models={"claude-opus-5-5-high"})
        self.assertTrue(result["ok"])
        self.assertEqual(route["model"], "gpt-6-sol")
        self.assertEqual([call.kwargs["model"] for call in chat.call_args_list], ["gpt-6.1-sol-cpa", "gpt-6-sol"])

    def test_job_snapshots_are_not_affected_by_later_database_edits(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.executescript(db.SCHEMA)
        snapshot = db.get_full_model_profile(conn, "analyze")
        edited = False

        def progress(kind, result):
            nonlocal edited
            if kind == "module" and not edited:
                edited = True
                ok, error = db.set_full_model_slot(conn, "analyze", "market_primary", 0, "grok-4.7", "high")
                self.assertTrue(ok, error)

        with self._stream_transport(), patch.object(llm_client, "collect_model", return_value='{"ok":true}'):
            _, summary = multi_analyzer.run("csv", "fund", "Home", "Away", "League",
                                             settings=snapshot, progress=progress)
        actual = next(result for result in summary["results"] if result["id"] == "market_primary")
        self.assertEqual(actual["model"], "gpt-6.1-sol-cpa")
        self.assertEqual(db.get_full_model_profile(conn, "analyze")["market_primary"]["model"], "grok-4.7")
        self.snapshot.assert_not_called()

    def test_review_first_stage_loads_review_not_analyze_profile(self):
        routes = settings.defaults("review")
        routes["market_primary"] = settings.replace_slot(routes["market_primary"], 0, "grok-4.7", "high")
        self.snapshot.return_value = routes
        self.snapshot.side_effect = None
        with self._stream_transport(), patch.object(llm_client, "collect_model", return_value='{"ok":true}'):
            _, summary = multi_analyzer.run("csv", "", "Home", "Away", "League", profile="review")
        self.snapshot.assert_called_once_with("review")
        actual = next(result for result in summary["results"] if result["id"] == "market_primary")
        self.assertEqual(actual["model"], "grok-4.7")

    def test_invalid_snapshot_rejected_before_model_requests(self):
        routes = settings.defaults("analyze")
        routes["synthesis"]["effort"] = "not-supported"
        report, summary = multi_analyzer.run("csv", "fund", "Home", "Away", "League", settings=routes)
        self.assertEqual(report, "")
        self.assertIn("配置错误", summary["error"])
        self.expert.assert_not_called()

    def test_cancelled_jobs_do_not_start_any_model(self):
        cancel = Event()
        cancel.set()
        report, summary = multi_analyzer.run("csv", "fund", "Home", "Away", "League", cancel=cancel)
        self.assertEqual(report, "")
        self.assertTrue(summary["cancelled"])
        report, summary = multi_analyzer.run_review(
            "csv", "forecast", "result", "fund", "Home", "Away", "League", cancel=cancel)
        self.assertEqual(report, "")
        self.assertTrue(summary["cancelled"])
        self.expert.assert_not_called()
        self.snapshot.assert_not_called()


class TestFullModelTelegram(unittest.TestCase):
    def test_review_both_stages_share_one_review_snapshot(self):
        snapshot = settings.defaults("review")
        meta = {"home": "Home", "away": "Away", "league": "League"}
        from bot import fundamentals, goals_model
        with ExitStack() as stack:
            stack.enter_context(patch.object(tgbot, "_is_admin", return_value=True))
            stack.enter_context(patch.object(analyzer, "available", return_value=True))
            stack.enter_context(patch.object(tgbot, "_build_csv", return_value=("csv", meta)))
            stack.enter_context(patch.object(tgbot.api_client, "fetch_fixture_result", return_value={"result": "offline"}))
            stack.enter_context(patch.object(tgbot, "_fmt_result", return_value=("result marker", "FT")))
            stack.enter_context(patch.object(goals_model, "states_from_csv", return_value=[]))
            stack.enter_context(patch.object(goals_model, "format_states_block", return_value="goals"))
            stack.enter_context(patch.object(fundamentals, "build_fundamentals", return_value="post-match fundamentals"))
            stack.enter_context(patch.object(db, "get_conn", return_value=Mock()))
            profile = stack.enter_context(patch.object(multi_analyzer, "profile_snapshot", return_value=snapshot))
            blind = stack.enter_context(patch.object(multi_analyzer, "run", return_value=("blind forecast", {})))
            review = stack.enter_context(patch.object(multi_analyzer, "run_review", return_value=("review report", {})))
            for name in ("send", "edit_text", "_send_long", "_llm_audit"):
                stack.enter_context(patch.object(tgbot, name))
            stack.enter_context(patch.object(tgbot, "_archive_report", return_value=None))
            stack.enter_context(patch.object(tgbot, "_lesson_pending", {}))
            tgbot._run_multi_review(123, 456)
        profile.assert_called_once_with("review")
        self.assertEqual(blind.call_args.args[1], "")
        self.assertNotIn("result marker", str(blind.call_args))
        self.assertEqual(blind.call_args.kwargs["profile"], "review")
        self.assertIs(blind.call_args.kwargs["settings"], snapshot)
        self.assertIs(review.call_args.kwargs["settings"], snapshot)
        self.assertEqual(review.call_args.args[1:4], ("blind forecast", "result marker", "post-match fundamentals"))


if __name__ == "__main__":
    unittest.main()
