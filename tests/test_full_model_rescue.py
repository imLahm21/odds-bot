"""Offline tests for provider-pinned, configurable full-model rescue."""

from contextlib import ExitStack
from copy import deepcopy
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from threading import Event
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("APIFOOTBALL_KEY", "offline-test-key")
from offline_support import prepare_publish_stubs  # noqa: E402
prepare_publish_stubs()
from bot import analyzer, config, db, full_model_settings as settings, full_model_ui as ui, llm_client, multi_analyzer as multi, tgbot  # noqa: E402


_RESCUE = {"model": "gpt-6.1-sol-cpa", "effort": "high", "route_group": "cpa_gpt"}
_OFF = {"model": "", "effort": "", "route_group": ""}
_JSON = '{"module":"offline","summary":"ok","findings":[]}'


class _RescueFixture(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(db.SCHEMA)
        self.addCleanup(self.conn.close)


class TestRescueStorage(_RescueFixture):
    def test_defaults_are_independent_cpa_gpt61_high(self):
        for profile in settings.PROFILES:
            self.assertEqual(settings.rescue_defaults(profile), _RESCUE)
            self.assertEqual(db.get_full_model_rescue(self.conn, profile), _RESCUE)
        pair = settings.rescue_defaults("analyze")
        pair["model"] = "changed"
        self.assertEqual(settings.rescue_defaults("review"), _RESCUE)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM full_model_rescue").fetchone()[0], 0)

    def test_manual_model_group_effort_and_disable_are_profile_scoped(self):
        ok, error = db.set_full_model_rescue(self.conn, "analyze", "gpt-6-astra", "low", "ik_gpt")
        self.assertTrue(ok, error)
        self.assertEqual(db.get_full_model_rescue(self.conn, "analyze"),
                         {"model": "gpt-6-astra", "effort": "low", "route_group": "ik_gpt"})
        self.assertEqual(db.get_full_model_rescue(self.conn, "review"), _RESCUE)
        self.assertTrue(db.set_full_model_rescue(self.conn, "review", "", "", "")[0])
        self.assertEqual(db.get_full_model_rescue(self.conn, "review"), _OFF)
        db.reset_full_model_rescue(self.conn, "review")
        self.assertEqual(db.get_full_model_rescue(self.conn, "review"), _RESCUE)

    def test_invalid_rescue_edits_are_rejected_without_changes(self):
        self.assertTrue(db.set_full_model_rescue(self.conn, "analyze", "", "", "")[0])
        invalid = [("missing", "gpt-6.1-sol-cpa", "high", "cpa_gpt"),
                   ("analyze", "missing-model", "high", "cpa_gpt"),
                   ("analyze", "gpt-6.1-sol-cpa", "ultra", "cpa_gpt"),
                   ("analyze", "gpt-6.1-sol-cpa", "high", "ik_claude"),
                   ("analyze", "gpt-6.1-sol-cpa", "high", ""),
                   ("analyze", "", "high", "cpa_gpt"),
                   ("analyze", None, "high", "cpa_gpt")]
        for args in invalid:
            with self.subTest(args=args):
                self.assertFalse(db.set_full_model_rescue(self.conn, *args)[0])
                self.assertEqual(db.get_full_model_rescue(self.conn, "analyze"), _OFF)

    def test_bad_saved_group_uses_pinned_default_without_overwriting_row(self):
        self.conn.execute("INSERT INTO full_model_rescue VALUES (?,?,?,?,?)",
                          ("analyze", "gpt-6.1-sol-cpa", "high", "ik_claude", "offline"))
        with self.assertLogs(db.log, level="WARNING"):
            self.assertEqual(db.get_full_model_rescue(self.conn, "analyze"), _RESCUE)
        self.assertEqual(self.conn.execute("SELECT route_group FROM full_model_rescue").fetchone()[0], "ik_claude")

    def test_role_reset_keeps_rescue_but_profile_reset_restores_it(self):
        self.assertTrue(db.set_full_model_rescue(self.conn, "analyze", "", "", "")[0])
        self.assertTrue(db.set_full_model_rescue(self.conn, "review", "", "", "")[0])
        db.reset_full_model_profile(self.conn, "analyze", "market_primary")
        self.assertEqual(db.get_full_model_rescue(self.conn, "analyze"), _OFF)
        db.reset_full_model_profile(self.conn, "analyze")
        self.assertEqual(db.get_full_model_rescue(self.conn, "analyze"), _RESCUE)
        self.assertEqual(db.get_full_model_rescue(self.conn, "review"), _OFF)

    def test_upgrade_adds_rescue_table_without_losing_existing_role_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "legacy.db")
            db.init_db(path)
            conn = sqlite3.connect(path)
            try:
                self.assertTrue(db.set_full_model_slot(conn, "analyze", "market_primary", 0, "grok-4.7", "high")[0])
                conn.execute("DROP TABLE full_model_rescue")
                conn.commit()
            finally:
                conn.close()
            db.init_db(path)
            conn = sqlite3.connect(path)
            try:
                self.assertEqual(db.get_full_model_profile(conn, "analyze")["market_primary"]["model"], "grok-4.7")
                self.assertEqual(db.get_full_model_rescue(conn, "analyze"), _RESCUE)
                self.assertTrue(db.set_full_model_rescue(conn, "review", "gpt-6-astra", "low", "ik_gpt")[0])
            finally:
                conn.close()
            db.init_db(path)
            conn = sqlite3.connect(path)
            try:
                self.assertEqual(db.get_full_model_rescue(conn, "review")["model"], "gpt-6-astra")
            finally:
                conn.close()

    def test_snapshot_contains_the_saved_rescue_and_existing_roles(self):
        self.assertTrue(db.set_full_model_rescue(self.conn, "analyze", "gpt-6-astra", "low", "ik_gpt")[0])
        self.assertTrue(db.set_full_model_slot(self.conn, "analyze", "market_primary", 0, "grok-4.7", "high")[0])
        with patch.object(db, "get_conn", return_value=self.conn):
            snapshot = multi.profile_snapshot("analyze")
        self.assertEqual(snapshot["_rescue"]["model"], "gpt-6-astra")
        self.assertEqual(snapshot["market_primary"]["model"], "grok-4.7")


class TestPinnedRescueClient(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(llm_client, "available", return_value=True))
        self.stack.enter_context(patch.object(llm_client, "get_settings", return_value={
            "non_stream_timeout": 1, "max_retries": 0,
            "stream_first_byte_timeout": 1, "stream_idle_timeout": 1}))
        endpoints = [{"label": "offline-ik", "route_group": "ik_gpt", "key": "offline", "base_url": "https://offline.invalid"},
                     {"label": "offline-cpa", "route_group": "cpa_gpt", "key": "offline", "base_url": "https://offline.invalid"}]
        self.endpoints = self.stack.enter_context(patch.object(llm_client, "_endpoints_by_priority", return_value=list(enumerate(endpoints))))
        breakers = [Mock(allow=Mock(return_value=True)), Mock(allow=Mock(return_value=True))]
        self.stack.enter_context(patch.object(llm_client, "_breakers", breakers))

    def test_blocking_rescue_hits_only_cpa_even_when_ik_has_higher_priority(self):
        with patch.object(llm_client, "_do_chat", return_value=("ok", True, "", False, False, False)) as request:
            result = llm_client.chat_model("system", "user", "gpt-6.1-sol-cpa", effort="high", route_group="cpa_gpt")
        self.assertEqual(result, "ok")
        request.assert_called_once()
        self.assertEqual(request.call_args.args[0]["route_group"], "cpa_gpt")

    def test_cpa_failure_does_not_silently_use_ik(self):
        with patch.object(llm_client, "_do_chat", return_value=("", False, "HTTP 429", False, True, False)) as request:
            result = llm_client.chat_model("system", "user", "gpt-6.1-sol-cpa", route_group="cpa_gpt")
        self.assertTrue(result.startswith("LLM 请求失败"))
        request.assert_called_once()
        self.assertEqual(request.call_args.args[0]["route_group"], "cpa_gpt")

    def test_ordinary_explicit_call_keeps_existing_multi_group_behavior(self):
        with patch.object(llm_client, "_do_chat", return_value=("ok", True, "", False, False, False)) as request:
            self.assertEqual(llm_client.chat_model("system", "user", "gpt-6.1-sol-cpa"), "ok")
        self.assertEqual(request.call_args.args[0]["route_group"], "ik_gpt")

    def test_forbidden_group_never_requests_any_endpoint(self):
        with patch.object(llm_client, "_do_chat") as request:
            result = llm_client.chat_model("system", "user", "gpt-6.1-sol-cpa", route_group="cpa_claude")
        self.assertTrue(result.startswith("LLM 请求失败"))
        request.assert_not_called()
        self.endpoints.assert_not_called()

    def test_streaming_pin_and_real_model_trace(self):
        used = Mock()
        with patch.object(llm_client, "_stream_one", return_value=iter([("done", "report")])) as request:
            events = list(llm_client.stream_chat_model("system", "user", "gpt-6.1-sol-cpa",
                effort="high", route_group="cpa_gpt", on_model_used=used))
        self.assertEqual(events, [("done", "report")])
        request.assert_called_once()
        self.assertEqual(request.call_args.args[0]["route_group"], "cpa_gpt")
        used.assert_called_once_with("gpt-6.1-sol-cpa", "high")

    def test_streaming_cpa_failure_does_not_use_ik(self):
        with patch.object(llm_client, "_stream_one", return_value=iter([("error", "HTTP 524")])) as request:
            events = list(llm_client.stream_chat_model("system", "user", "gpt-6.1-sol-cpa", route_group="cpa_gpt"))
        self.assertEqual(events[-1][0], "error")
        request.assert_called_once()
        self.assertEqual(request.call_args.args[0]["route_group"], "cpa_gpt")


class TestRescueExecution(unittest.TestCase):
    def setUp(self):
        self.spec = settings.defaults("analyze")["market_challenge"]

    def _call(self, rescue=_RESCUE, **kwargs):
        return multi._call_route("original system", "original input", self.spec,
                                 parse_json=True, phase="module:market_challenge", rescue=rescue, **kwargs)

    def test_normal_success_at_any_level_never_calls_rescue(self):
        for failures in range(3):
            with self.subTest(failures=failures), patch.object(llm_client, "chat_model",
                side_effect=["LLM 请求失败（offline）"] * failures + [_JSON]) as request:
                result, route = self._call()
                self.assertIsNotNone(result)
                self.assertFalse(route["rescue_attempted"])
                self.assertFalse(route["rescue_used"])
                self.assertEqual(request.call_count, failures + 1)
                self.assertTrue(all("route_group" not in call.kwargs for call in request.call_args_list))

    def test_three_model_failures_trigger_one_pinned_rescue_on_unchanged_input(self):
        with patch.object(llm_client, "chat_model", side_effect=["LLM 请求失败（offline）"] * 3 + [_JSON]) as request:
            result, route = self._call()
        self.assertIsNotNone(result)
        self.assertEqual([call.kwargs["model"] for call in request.call_args_list],
                         ["grok-4.7", "claude-opus-5-5-high", "gpt-6.1-sol-cpa", "gpt-6.1-sol-cpa"])
        self.assertTrue(all(call.args == ("original system", "original input") for call in request.call_args_list))
        self.assertEqual(request.call_args.kwargs["route_group"], "cpa_gpt")
        self.assertEqual(request.call_args.kwargs["effort"], "high")
        self.assertTrue(route["rescue_used"])
        self.assertTrue(route["rescue_attempted"])
        self.assertIn("公共兜底成功", multi.execution_note({**route, "status": "ok"}))

    def test_invalid_json_exhaustion_also_uses_rescue(self):
        with patch.object(llm_client, "chat_model", side_effect=["not json"] * 3 + [_JSON]) as request:
            result, route = self._call()
        self.assertIsNotNone(result)
        self.assertTrue(route["rescue_used"])
        self.assertEqual(request.call_count, 4)

    def test_rescue_failure_is_terminal_and_distinct_from_primary_failure(self):
        with patch.object(llm_client, "chat_model", return_value="LLM 请求失败（offline）") as request:
            result, route = self._call()
        self.assertIsNone(result)
        self.assertEqual(request.call_count, 4)
        self.assertFalse(route["rescue_used"])
        self.assertTrue(route["rescue_attempted"])
        self.assertEqual(route["model"], "gpt-6.1-sol-cpa")
        self.assertIn("公共兜底也失败", multi.execution_note({**route, "status": "error"}))

    def test_disabled_rescue_stops_after_normal_chain(self):
        with patch.object(llm_client, "chat_model", return_value="LLM 请求失败（offline）") as request:
            result, route = self._call(rescue=_OFF)
        self.assertIsNone(result)
        self.assertEqual(request.call_count, 3)
        self.assertIn("未启用公共兜底", multi.execution_note({**route, "status": "error"}))

    def test_rescue_cannot_bypass_invalid_normal_configuration(self):
        self.spec["effort"] = "not-supported"
        with patch.object(llm_client, "chat_model") as request:
            result, route = self._call()
        self.assertIsNone(result)
        self.assertIn("配置错误", route["error"])
        request.assert_not_called()

    def test_rescue_is_also_excluded_when_it_wrote_the_report(self):
        excluded = {pair["model"] for pair in settings.candidates(self.spec)} | {"gpt-6.1-sol-cpa"}
        with patch.object(llm_client, "chat_model") as request:
            result, route = self._call(excluded_models=excluded)
        self.assertIsNone(result)
        self.assertTrue(route["rescue_skipped"])
        self.assertFalse(route["rescue_attempted"])
        request.assert_not_called()

    def test_cancellation_before_rescue_prevents_the_extra_call(self):
        cancel = Event()
        count = 0

        def fail(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 3:
                cancel.set()
            return "LLM 请求失败（offline）"

        with patch.object(llm_client, "chat_model", side_effect=fail) as request:
            _, route = self._call(cancel=cancel)
        self.assertEqual(request.call_count, 3)
        self.assertFalse(route["rescue_attempted"])

    def test_failed_expert_keeps_the_last_actual_attempt_not_the_primary_label(self):
        with patch.object(llm_client, "chat_model", return_value="LLM 请求失败（offline）"):
            result = multi._run_one(self.spec, {"rescue": _RESCUE, "csv_text": "csv"})
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["requested_model"], "grok-4.7")
        self.assertEqual(result["model"], "gpt-6.1-sol-cpa")
        self.assertEqual(result["route_group"], "cpa_gpt")
        text = multi._render_appendix([result], {"ok": True})
        self.assertIn("初始 grok-4.7；最后尝试 gpt-6.1-sol-cpa", text)
        self.assertIn("公共兜底也失败 · cpa_gpt", text)

    def test_same_model_can_be_retried_once_with_the_explicit_rescue_group_and_effort(self):
        self.spec = settings.defaults("analyze")["market_primary"]
        with patch.object(llm_client, "chat_model", side_effect=["LLM 请求失败（offline）"] * 3 + [_JSON]) as request:
            _, route = self._call()
        self.assertEqual(request.call_count, 4)
        self.assertEqual(request.call_args_list[0].kwargs["model"], "gpt-6.1-sol-cpa")
        self.assertNotIn("route_group", request.call_args_list[0].kwargs)
        self.assertEqual(request.call_args.kwargs["route_group"], "cpa_gpt")
        self.assertTrue(route["rescue_used"])


class TestStreamingRescue(unittest.TestCase):
    def _synthesize(self, stream, review=False):
        trace = {"model": "gpt-6-astra-cpa", "effort": "xhigh"}
        prompt = "_review_synthesis_prompt" if review else "_synthesis_prompt"
        fn = multi._review_synthesize if review else multi._synthesize
        with patch.object(multi, prompt, return_value=("original system", "original input", {})), \
             patch.object(llm_client, "stream_chat_model", side_effect=stream) as request:
            result = fn({}, [], rescue=_RESCUE, trace=trace)
        return result, trace, request

    def test_zero_content_chain_failure_then_cpa_rescue_for_both_modes(self):
        for review in (False, True):
            def stream(system, user, **kwargs):
                if kwargs.get("route_group"):
                    kwargs["on_model_used"](kwargs["model"], kwargs["effort"])
                    yield "done", "complete report"
                else:
                    yield "error", "all normal models unavailable"
            result, trace, request = self._synthesize(stream, review)
            self.assertEqual(result, "complete report")
            self.assertEqual(request.call_count, 2)
            self.assertEqual(request.call_args.kwargs["route_group"], "cpa_gpt")
            self.assertEqual(request.call_args.kwargs["fallback_models"], ())
            self.assertTrue(trace["rescue_used"])
            self.assertEqual(trace["model"], "gpt-6.1-sol-cpa")

    def test_partial_normal_output_does_not_regenerate_with_rescue(self):
        def stream(*args, **kwargs):
            yield "delta", "partial original report"
            yield "error", "stream interrupted"
        result, trace, request = self._synthesize(stream)
        self.assertEqual(result, "")
        self.assertEqual(request.call_count, 1)
        self.assertEqual(trace["rescue_skipped_reason"], "partial_output")
        self.assertFalse(trace.get("rescue_attempted", False))

    def test_rescue_failure_does_not_loop_back_to_normal_models(self):
        result, trace, request = self._synthesize(lambda *args, **kwargs: iter([("error", "offline")]))
        self.assertEqual(result, "")
        self.assertEqual(request.call_count, 2)
        self.assertTrue(trace["rescue_attempted"])
        self.assertFalse(trace["rescue_used"])
        self.assertEqual(trace["model"], "gpt-6.1-sol-cpa")
        self.assertEqual(trace["route_group"], "cpa_gpt")

    def test_normal_stream_success_never_calls_rescue(self):
        result, trace, request = self._synthesize(lambda *args, **kwargs: iter([("done", "complete")]))
        self.assertEqual(result, "complete")
        request.assert_called_once()
        self.assertFalse(trace.get("rescue_attempted", False))

    def test_partial_rescue_output_is_not_regenerated_or_marked_successful(self):
        def stream(*args, **kwargs):
            if not kwargs.get("route_group"):
                yield "error", "normal exhausted"
            else:
                yield "delta", "partial rescue"
                yield "error", "interrupted"
        result, trace, request = self._synthesize(stream)
        self.assertEqual(result, "")
        self.assertEqual(request.call_count, 2)
        self.assertTrue(trace["rescue_attempted"])
        self.assertFalse(trace["rescue_used"])
        self.assertNotIn("rescue_skipped_reason", trace)


class TestRescueUI(_RescueFixture):
    def test_model_group_effort_are_saved_together_only_on_final_selection(self):
        # 供应商按模型名分离后每个模型只有一个授权组，选完模型直接进强度页（组随模型确定）
        index = list(config.LLM_MODELS).index("gpt-6-astra")
        self.assertEqual(config.llm_route_groups_for_model("gpt-6-astra"), ("ik_gpt",))
        text, keyboard, _ = ui.handle_callback(self.conn, f"fmc:gc:a:{index}")
        self.assertIn("ik_gpt", text)
        self.assertEqual(db.get_full_model_rescue(self.conn, "analyze"), _RESCUE)
        save = next(button["callback_data"] for row in keyboard["inline_keyboard"] for button in row
                    if button["text"] == config.LLM_EFFORT_LABELS["low"])
        _, _, notice = ui.handle_callback(self.conn, save)
        self.assertIn("已保存", notice)
        self.assertEqual(db.get_full_model_rescue(self.conn, "analyze"),
                         {"model": "gpt-6-astra", "effort": "low", "route_group": "ik_gpt"})
        self.assertEqual(db.get_full_model_rescue(self.conn, "review"), _RESCUE)

    def test_rescue_disable_and_confirmed_reset(self):
        ui.handle_callback(self.conn, "fmc:gx:a")
        self.assertEqual(db.get_full_model_rescue(self.conn, "analyze"), _OFF)
        ui.handle_callback(self.conn, "fmc:gz:a")
        self.assertEqual(db.get_full_model_rescue(self.conn, "analyze"), _OFF)
        ui.handle_callback(self.conn, "fmc:gzd:a")
        self.assertEqual(db.get_full_model_rescue(self.conn, "analyze"), _RESCUE)

    def test_new_callbacks_are_bounded_and_do_not_use_fixture_namespace(self):
        for data in ["fmc:p:a", "fmc:g:a", "fmc:gm:r", "fmc:ge:r", "fmc:gr:r"]:
            text, keyboard, _ = ui.handle_callback(self.conn, data)
            self.assertLess(len(text), 4096)
            for row in keyboard["inline_keyboard"]:
                for button in row:
                    self.assertLessEqual(len(button["callback_data"].encode()), 64)
                    self.assertFalse(button["callback_data"].startswith("fx:"))

    def test_visitors_cannot_modify_rescue(self):
        for data in ("fmc:g:a", "fmc:gx:a", "fmc:gs:a:2:1:3", "fmc:gzd:r"):
            with patch.object(tgbot, "_is_admin", return_value=False), \
                 patch.object(db, "get_conn") as connection, \
                 patch.object(tgbot, "answer_callback"):
                tgbot._handle_full_model_callback("offline", data, 123, 456)
                connection.assert_not_called()

    def test_invalid_rescue_callbacks_do_not_modify_the_profile(self):
        for data in ("fmc:g:unknown", "fmc:gc:a:-1", "fmc:gg:a:999:0",
                     "fmc:gs:a:2:999:0", "fmc:gs:a:2:0:999", "fmc:gx:a:extra"):
            with self.subTest(data=data), self.assertRaises(ValueError):
                ui.handle_callback(self.conn, data)
        self.assertEqual(db.get_full_model_rescue(self.conn, "analyze"), _RESCUE)


class TestRescuePipeline(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(config, "TYPESAFE_DECISION_MODE", "off"))
        self.expert = self.stack.enter_context(patch.object(multi, "_run_one", side_effect=lambda task, bundle: {
            "id": task["id"], "label": task["label"], "model": task["model"], "effort": task["effort"],
            "status": "ok", "card": {}}))
        self.synth = self.stack.enter_context(patch.object(multi, "_synthesize", return_value="report"))
        self.review_synth = self.stack.enter_context(patch.object(multi, "_review_synthesize", return_value="review"))
        self.audit = self.stack.enter_context(patch.object(multi, "_audit_report", return_value={"ok": True, "issues": []}))
        self.review_audit = self.stack.enter_context(patch.object(multi, "_review_audit", return_value={"ok": True, "issues": []}))

    def test_each_profile_passes_the_frozen_custom_rescue_to_all_stages(self):
        custom = {"model": "gpt-6-astra", "effort": "low", "route_group": "ik_gpt"}
        for profile in settings.PROFILES:
            routes = settings.defaults(profile)
            routes["_rescue"] = deepcopy(custom)
            self.expert.reset_mock()
            if profile == "analyze":
                report, _ = multi.run("csv", "fund", "H", "A", "League", settings=routes)
                synth, audit = self.synth, self.audit
            else:
                report, _ = multi.run_review("csv", "forecast", "result", "fund", "H", "A", "League", settings=routes)
                synth, audit = self.review_synth, self.review_audit
            self.assertTrue(report)
            self.assertEqual(synth.call_args.kwargs["rescue"], custom)
            self.assertEqual(audit.call_args.kwargs["rescue"], custom)
            self.assertTrue(all(call.args[1]["rescue"] == custom for call in self.expert.call_args_list))
            self.assertIsNot(synth.call_args.kwargs["rescue"], routes["_rescue"])

    def test_repair_rescue_is_tracked_and_excluded_from_the_next_audit(self):
        self.audit.side_effect = [{"ok": False, "issues": ["fix"]}, {"ok": True, "issues": []}]
        route = {**_RESCUE, "rescue_used": True, "rescue_attempted": True, "rescue_configured": True}
        with patch.object(multi, "_call_route", return_value=("repaired", route)) as repair:
            report, summary = multi.run("csv", "fund", "H", "A", "League", settings=settings.defaults("analyze"))
        self.assertEqual(repair.call_args.kwargs["rescue"], _RESCUE)
        self.assertTrue(summary["synthesis"]["rescue_used"])
        self.assertEqual(summary["synthesis"]["route_group"], "cpa_gpt")
        self.assertIn("gpt-6.1-sol-cpa", self.audit.call_args.kwargs["excluded_models"])
        self.assertIn("公共兜底", report)

    def test_phase_two_does_not_reselect_even_when_rescue_is_available(self):
        with patch.object(analyzer, "finalize_decision") as finalize:
            _, summary = multi.run_review("csv", "fixed forecast", "result", "fund", "H", "A", "League",
                                           settings=settings.defaults("review"))
        self.assertEqual(summary["profile"], "review")
        finalize.assert_not_called()


class TestRescueProgress(unittest.TestCase):
    def test_telegram_failure_names_last_model_and_exhaustion_instead_of_primary_only(self):
        from bot import fundamentals, goals_model
        snapshot = settings.defaults("analyze")
        snapshot["_rescue"] = deepcopy(_RESCUE)

        def run(*args, **kwargs):
            kwargs["progress"]("module", {
                "id": "risk_review", "model": "gpt-6.1-sol-cpa", "effort": "high", "status": "error",
                "requested_model": "deepseek-v4-pro", "route_group": "cpa_gpt",
                "rescue_configured": True, "rescue_attempted": True, "rescue_used": False,
                "attempts": [{"status": "error"}],
            })
            return "report", {"audit": {"ok": True}}

        with ExitStack() as stack:
            stack.enter_context(patch.object(tgbot, "_is_admin", return_value=True))
            stack.enter_context(patch.object(analyzer, "available", return_value=True))
            stack.enter_context(patch.object(tgbot, "_build_csv", return_value=("csv", {"home": "H", "away": "A", "league": "L"})))
            stack.enter_context(patch.object(multi, "profile_snapshot", return_value=snapshot))
            stack.enter_context(patch.object(multi, "run", side_effect=run))
            stack.enter_context(patch.object(db, "get_conn", return_value=Mock()))
            stack.enter_context(patch.object(fundamentals, "build_fundamentals", return_value="fund"))
            stack.enter_context(patch.object(goals_model, "states_from_csv", return_value=[]))
            stack.enter_context(patch.object(goals_model, "format_states_block", return_value=""))
            stack.enter_context(patch.object(tgbot, "send", return_value=1))
            edits = stack.enter_context(patch.object(tgbot, "edit_text"))
            for name in ("_send_long", "_llm_audit"):
                stack.enter_context(patch.object(tgbot, name))
            stack.enter_context(patch.object(tgbot, "_archive_report", return_value=None))
            tgbot._run_multi_sop(123, 456)
        texts = [call.args[2] for call in edits.call_args_list]
        self.assertTrue(any("最后尝试 gpt-6.1-sol-cpa / high" in text
                            and "公共兜底也失败 · cpa_gpt" in text for text in texts))


if __name__ == "__main__":
    unittest.main()
