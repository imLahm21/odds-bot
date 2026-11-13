"""Offline persistence, validation and Telegram configuration coverage."""

import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("APIFOOTBALL_KEY", "offline-test-key")

from offline_support import prepare_publish_stubs  # noqa: E402
prepare_publish_stubs()

from bot import config, db, full_model_settings as settings, full_model_ui, tgbot  # noqa: E402


_APPROVED = {
    "data_audit": "gpt-6-luna/low glm-5.3-flash/low deepseek-v4.1-flash/low",
    "fundamentals": "claude-sonnet-5-5-high/high gpt-6.1-sol-cpa/high gemini-4-argon-high/high",
    "fundamentals_review": "gemini-4-argon-high/high deepseek-v4-pro/high gpt-6.1-sol-cpa/high",
    "market_primary": "gpt-6.1-sol-cpa/xhigh deepseek-v4-pro/max gemini-4-argon-high/high",
    "market_challenge": "grok-4.7/high claude-opus-5-5-high/high gpt-6.1-sol-cpa/high",
    "numeric_summary": "deepseek-v4.1-flash/high glm-5.3-flash/high gpt-6-luna/medium",
    "risk_review": "deepseek-v4-pro/max claude-opus-5-5-high/high gemini-4-argon-high/high",
    "cross_market": "gemini-4-argon-high/high deepseek-v4-pro/max gpt-6.1-sol-cpa/xhigh",
    "lesson_match": "claude-sonnet-5-5-high/high gpt-6.1-sol-cpa/high gemini-4-argon-high/high",
    "synthesis": "gpt-6-astra-cpa/xhigh claude-opus-5-5-high/high deepseek-v4-pro/max",
    "audit": "claude-opus-5-5-high/high gemini-4-argon-high/high deepseek-v4-pro/max",
}


class _ProfileFixture(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.executescript(db.SCHEMA)

    def _set(self, role="market_primary", slot=0, model="grok-4.7", effort="high", profile="analyze"):
        ok, error = db.set_full_model_slot(self.conn, profile, role, slot, model, effort)
        self.assertTrue(ok, error)


class TestFullModelSettings(_ProfileFixture):
    def test_approved_defaults_and_all_registered_model_grades(self):
        self.assertEqual(set(config.LLM_MODELS) - config.FULL_CONSULT_EXCLUDED_MODELS,
                         set(config.FULL_CONSULT_MODEL_GRADES))
        for profile in settings.PROFILES:
            roles = settings.defaults(profile)
            self.assertEqual(list(roles), list(_APPROVED))
            for role, chain in _APPROVED.items():
                with self.subTest(profile=profile, role=role):
                    actual = " ".join(f"{p['model']}/{p['effort']}" for p in settings.slots(roles[role]))
                    self.assertEqual(actual, chain)
                    self.assertEqual(settings.route_problems(roles[role]), [])

    def test_fresh_profiles_use_code_defaults_without_seeding_overrides(self):
        for profile in settings.PROFILES:
            self.assertEqual(db.get_full_model_profile(self.conn, profile), settings.defaults(profile))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM full_model_routes").fetchone()[0], 0)

    def test_saving_all_default_pairs_roundtrips_the_code_profiles_exactly(self):
        pairs_checked = 0
        for profile in settings.PROFILES:
            for role, spec in settings.defaults(profile).items():
                for position, pair in enumerate(settings.slots(spec)):
                    self._set(role, position, pair["model"], pair["effort"], profile)
                    pairs_checked += 1
            self.assertEqual(db.get_full_model_profile(self.conn, profile), settings.defaults(profile))
        self.assertEqual(pairs_checked, 66)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM full_model_routes").fetchone()[0], 22)

    def test_profiles_and_returned_snapshots_do_not_share_mutable_state(self):
        first = db.get_full_model_profile(self.conn, "analyze")
        first["market_primary"]["fallbacks"][0]["model"] = "modified"
        self.assertEqual(db.get_full_model_profile(self.conn, "review"), settings.defaults("review"))
        self.assertEqual(db.get_full_model_profile(self.conn, "analyze"), settings.defaults("analyze"))

    def test_primary_and_both_fallback_pairs_persist_independently(self):
        self._set()
        self._set(slot=1, model="gpt-6-astra-cpa", effort="low")
        self._set(slot=2, model="claude-sonnet-5-5-high", effort="medium")
        pairs = settings.slots(db.get_full_model_profile(self.conn, "analyze")["market_primary"])
        self.assertEqual(pairs, [
            {"model": "grok-4.7", "effort": "high"},
            {"model": "gpt-6-astra-cpa", "effort": "low"},
            {"model": "claude-sonnet-5-5-high", "effort": "medium"},
        ])
        self.assertEqual(db.get_full_model_profile(self.conn, "review"), settings.defaults("review"))

    def test_explicit_provider_default_effort_is_distinct_from_none(self):
        self._set(effort="")
        self.assertEqual(db.get_full_model_profile(self.conn, "analyze")["market_primary"]["effort"], "")

    def test_invalid_edits_leave_the_saved_profile_unchanged(self):
        self._set()
        before = list(self.conn.execute("SELECT * FROM full_model_routes"))
        invalid = [
            ("analyze", "missing", 0, "grok-4.7", "high"),
            ("missing", "market_primary", 0, "grok-4.7", "high"),
            ("analyze", "market_primary", -1, "grok-4.7", "high"),
            ("analyze", "market_primary", True, "grok-4.7", "high"),
            ("analyze", "market_primary", 0, "unknown-model", "high"),
            ("analyze", "market_primary", 0, "grok-4.7", "ultra"),
            ("analyze", "market_primary", 0, "", ""),
            ("analyze", "market_primary", 1, "grok-4.6", "high"),
            ("analyze", "market_primary", 1, "deepseek-v4.1-flash", "high"),
            ("analyze", "market_primary", 2, "deepseek-v4-pro", "high"),
        ]
        for args in invalid:
            with self.subTest(args=args):
                ok, error = db.set_full_model_slot(self.conn, *args)
                self.assertFalse(ok)
                self.assertTrue(error)
                self.assertEqual(list(self.conn.execute("SELECT * FROM full_model_routes")), before)

    def test_clearing_first_fallback_revalidates_the_effective_chain(self):
        self._set(role="data_audit", slot=2, model="gpt-5.6-luna", effort="low")
        ok, _ = db.set_full_model_slot(self.conn, "analyze", "data_audit", 1, "", "")
        self.assertFalse(ok)  # Second Luna would become a same-group first fallback.
        self._set(role="data_audit", slot=2, model="", effort="")
        self._set(role="data_audit", slot=1, model="", effort="")
        role = db.get_full_model_profile(self.conn, "analyze")["data_audit"]
        self.assertEqual(len(settings.candidates(role)), 1)

    def test_reset_role_and_profile_are_scoped_and_keep_ordinary_tiers(self):
        db.seed_config(self.conn)
        original_tiers = db.get_llm_runtime_state(self.conn)
        self._set()
        self._set(role="synthesis", model="grok-4.7")
        self._set(profile="review")
        db.reset_full_model_profile(self.conn, "analyze", "market_primary")
        current = db.get_full_model_profile(self.conn, "analyze")
        self.assertEqual(current["market_primary"], settings.defaults("analyze")["market_primary"])
        self.assertEqual(current["synthesis"]["model"], "grok-4.7")
        db.reset_full_model_profile(self.conn, "analyze")
        self.assertEqual(db.get_full_model_profile(self.conn, "analyze"), settings.defaults("analyze"))
        self.assertEqual(db.get_full_model_profile(self.conn, "review")["market_primary"]["model"], "grok-4.7")
        self.assertEqual(db.get_llm_runtime_state(self.conn), original_tiers)

    def test_corrupt_overrides_fall_back_without_destroying_the_saved_value(self):
        for raw in ("not-json", "null", "[]", '[1,2,3]', '[{"model":"missing"},{},{}]'):
            with self.subTest(raw=raw):
                self.conn.execute("INSERT OR REPLACE INTO full_model_routes VALUES (?,?,?,?)",
                                  ("analyze", "market_primary", raw, "offline"))
                self.conn.commit()
                with self.assertLogs(db.log, level="WARNING"):
                    current = db.get_full_model_profile(self.conn, "analyze")
                self.assertEqual(current["market_primary"], settings.defaults("analyze")["market_primary"])
                self.assertEqual(self.conn.execute("SELECT slots_json FROM full_model_routes").fetchone()[0], raw)

    def test_saved_choices_survive_restart_and_idempotent_schema_initialization(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "profiles.db")
            db.init_db(path)
            with db.get_conn(path) as conn:
                ok, error = db.set_full_model_slot(conn, "review", "market_primary", 0, "grok-4.7", "high")
                self.assertTrue(ok, error)
            conn.close()
            db.init_db(path)
            with db.get_conn(path) as conn:
                db.seed_config(conn)
                self.assertEqual(db.get_full_model_profile(conn, "review")["market_primary"]["model"], "grok-4.7")
                self.assertEqual(db.get_full_model_profile(conn, "analyze"), settings.defaults("analyze"))
            conn.close()

    def test_role_labels_and_budgets_cannot_be_overridden_by_stored_pairs(self):
        pairs = settings.slots(settings.defaults("analyze")["market_primary"])
        pairs[0].update(label="injected", id="audit", max_tokens=1)
        self.conn.execute("INSERT INTO full_model_routes VALUES (?,?,?,?)",
                          ("analyze", "market_primary", json.dumps(pairs), "offline"))
        role = db.get_full_model_profile(self.conn, "analyze")["market_primary"]
        self.assertEqual(role["id"], "market_primary")
        self.assertEqual(role["label"], "盘口主分析")
        self.assertEqual(role["max_tokens"], 0)


class TestFullModelUI(_ProfileFixture):
    def test_ui_model_then_strength_saves_atomically(self):
        model_index = list(config.LLM_MODELS).index("grok-4.7")
        text, keyboard, _ = full_model_ui.handle_callback(self.conn, f"fmc:c:a:3:0:{model_index}")
        self.assertIn("grok-4.7", text)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM full_model_routes").fetchone()[0], 0)
        save = next(button["callback_data"] for row in keyboard["inline_keyboard"] for button in row
                    if button["text"] == config.LLM_EFFORT_LABELS["high"])
        text, _, notice = full_model_ui.handle_callback(self.conn, save)
        self.assertIn("已保存", notice)
        self.assertIn("grok-4.7", text)
        self.assertEqual(db.get_full_model_profile(self.conn, "analyze")["market_primary"]["effort"], "high")

    def test_effort_menu_only_contains_model_registered_options_and_provider_default(self):
        index = list(config.LLM_MODELS).index("gpt-6.1-sol-cpa")
        _, keyboard, _ = full_model_ui.handle_callback(self.conn, f"fmc:c:r:3:0:{index}")
        labels = [button["text"] for row in keyboard["inline_keyboard"] for button in row]
        self.assertIn("模型默认（不发送参数）", labels)
        self.assertNotIn(config.LLM_EFFORT_LABELS["ultra"], labels)
        self.assertNotIn(config.LLM_EFFORT_LABELS["none"], labels)

    def test_ui_reset_needs_confirmation_and_is_profile_scoped(self):
        self._set()
        self._set(profile="review")
        full_model_ui.handle_callback(self.conn, "fmc:z:a")
        self.assertEqual(db.get_full_model_profile(self.conn, "analyze")["market_primary"]["model"], "grok-4.7")
        full_model_ui.handle_callback(self.conn, "fmc:zd:a")
        self.assertEqual(db.get_full_model_profile(self.conn, "analyze"), settings.defaults("analyze"))
        self.assertEqual(db.get_full_model_profile(self.conn, "review")["market_primary"]["model"], "grok-4.7")

    def test_ui_invalid_callbacks_never_mutate_settings(self):
        for data in ("fmc:no:a", "fmc:p:missing", "fmc:r:a:-1", "fmc:r:a:99", "fmc:m:r:3:3",
                     "fmc:c:a:3:0:-1", "fmc:s:a:3:0:999:0", "fmc:s:a:3:0:1:999",
                     "fmc:x:a:3:0", "fmc:p:a:extra"):
            with self.subTest(data=data), self.assertRaises(ValueError):
                full_model_ui.handle_callback(self.conn, data)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM full_model_routes").fetchone()[0], 0)

    def test_menu_text_and_callback_sizes_fit_telegram_limits(self):
        screens = ["fmc:", "fmc:p:a", "fmc:p:r"]
        for i in range(11):
            screens += [f"fmc:r:a:{i}", f"fmc:r:r:{i}", f"fmc:m:a:{i}:0"]
        for data in screens:
            text, keyboard, _ = full_model_ui.handle_callback(self.conn, data)
            self.assertLess(len(text), 4096)
            self.assertNotIn("会诊", text)
            for row in keyboard["inline_keyboard"]:
                for button in row:
                    self.assertLessEqual(len(button["callback_data"].encode()), 64)
                    self.assertFalse(button["callback_data"].startswith("fx:"))

    def test_visitors_cannot_open_or_mutate_full_model_settings(self):
        for data in ("fmc:", "fmc:p:a", "fmc:s:a:3:0:1:0", "fmc:zd:r"):
            with patch.object(tgbot, "_is_admin", return_value=False), \
                 patch.object(tgbot.db, "get_conn") as connection, \
                 patch.object(tgbot, "answer_callback") as answer:
                tgbot._handle_full_model_callback("callback", data, 123, 456)
                connection.assert_not_called()
                answer.assert_called_once_with("callback", "仅管理员可操作")

    def test_full_model_menu_exists_even_without_provider_credentials(self):
        with patch.object(tgbot.llm_client, "available", return_value=False):
            keyboard = tgbot._llm_panel_keyboard()
        callbacks = {button["callback_data"] for row in keyboard["inline_keyboard"] for button in row}
        self.assertEqual(callbacks, {"fmc:p:a", "fmc:p:r"})

    def test_admin_callback_dispatch_saves_and_renders_the_selected_profile(self):
        model_index = list(config.LLM_MODELS).index("grok-4.7")
        effort_index = ("", *config.llm_model_efforts("grok-4.7")).index("high")
        callback = {"id": "offline", "data": f"fmc:s:a:3:0:{model_index}:{effort_index}",
                    "message": {"chat": {"id": 123}, "message_id": 456}}
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "callbacks.db")
            db.init_db(path)
            with patch.object(tgbot, "_authorized", return_value=True), \
                 patch.object(tgbot, "_is_admin", return_value=True), \
                 patch.object(db, "get_conn", side_effect=lambda: sqlite3.connect(path)), \
                 patch.object(tgbot, "answer_callback") as answer, \
                 patch.object(tgbot, "edit_text") as edit:
                tgbot.handle_callback(callback)
            answer.assert_called_once_with("offline", "已保存，下次任务生效")
            self.assertIn("grok-4.7", edit.call_args.args[2])
            conn = sqlite3.connect(path)
            try:
                self.assertEqual(db.get_full_model_profile(conn, "analyze")["market_primary"]["model"], "grok-4.7")
                self.assertEqual(db.get_full_model_profile(conn, "review"), settings.defaults("review"))
            finally:
                conn.close()

    def test_callback_dispatch_checks_whitelist_and_admin_before_database(self):
        callback = {"id": "offline", "data": "fmc:zd:a",
                    "message": {"chat": {"id": 123}, "message_id": 456}}
        for allowed, expected in ((False, "未授权"), (True, "仅管理员可操作")):
            with patch.object(tgbot, "_authorized", return_value=allowed), \
                 patch.object(tgbot, "_is_admin", return_value=False), \
                 patch.object(db, "get_conn") as connection, \
                 patch.object(tgbot, "answer_callback") as answer:
                tgbot.handle_callback(callback)
                connection.assert_not_called()
                answer.assert_called_once_with("offline", expected)

    def test_controller_reports_database_failure_without_exposing_internal_error(self):
        with patch.object(tgbot, "_is_admin", return_value=True), \
             patch.object(db, "get_conn", side_effect=sqlite3.OperationalError("private-error-marker")), \
             patch.object(tgbot, "answer_callback") as answer, \
             patch.object(tgbot, "edit_text") as edit:
            with self.assertLogs(tgbot.log, level="WARNING"):
                tgbot._handle_full_model_callback("offline", "fmc:p:a", 123, 456)
        self.assertNotIn("private-error-marker", answer.call_args.args[1])
        edit.assert_not_called()

    def test_old_fixtures_pagination_and_refresh_callbacks_remain_available_to_visitors(self):
        cases = [("fx:future:0", "future", 0, False),
                 ("fx:past:1", "past", 1, False),
                 ("fx:future:2:r", "future", 2, True)]
        for admin in (False, True):
            for data, view, page, refresh in cases:
                with self.subTest(admin=admin, data=data), \
                     patch.object(tgbot, "_authorized", return_value=True), \
                     patch.object(tgbot, "_is_admin", return_value=admin), \
                     patch.object(tgbot, "_handle_full_model_callback") as configuration, \
                     patch.object(tgbot, "_render_fixtures", return_value=("fixtures", {})) as render, \
                     patch.object(tgbot, "answer_callback"), \
                     patch.object(tgbot, "edit_text") as edit:
                    tgbot.handle_callback({"id": "offline", "data": data,
                        "message": {"chat": {"id": 123}, "message_id": 456}})
                    configuration.assert_not_called()
                    render.assert_called_once_with(view, page, refresh=refresh)
                    self.assertEqual(edit.call_args.args[2], "fixtures")


if __name__ == "__main__":
    unittest.main()
