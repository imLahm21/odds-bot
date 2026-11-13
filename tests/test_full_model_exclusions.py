"""Exact-ID exclusions apply only to full-model analysis/review, including rescue."""

import json
import os
import sqlite3
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("APIFOOTBALL_KEY", "offline-test-key")

from bot import config, db, full_model_settings as settings, full_model_ui as ui, llm_client, multi_analyzer as multi  # noqa: E402


_BLOCKED = "glm-5.3"
_FLASH = "glm-5.3-flash"
_EMPTY = {"model": "", "effort": ""}


class TestFullModelExclusions(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(db.SCHEMA)
        self.addCleanup(self.conn.close)

    def _save_legacy(self, role, pairs, profile="analyze"):
        raw = json.dumps(pairs)
        self.conn.execute("INSERT OR REPLACE INTO full_model_routes VALUES (?,?,?,?)",
                          (profile, role, raw, "offline"))
        self.conn.commit()
        return raw

    def test_defaults_exclude_exact_model_but_keep_flash_and_ordinary_registration(self):
        self.assertFalse(settings.model_allowed(_BLOCKED))
        self.assertTrue(settings.model_allowed(_FLASH))
        self.assertIn(_BLOCKED, config.LLM_MODELS)
        self.assertIn(_BLOCKED, config.llm_tier_eligible_models("heavy"))
        self.assertEqual(config.LLM_FALLBACK_TIER_MODELS["heavy"]["visitor"], _BLOCKED)
        for profile in settings.PROFILES:
            models = {pair["model"] for spec in settings.defaults(profile).values()
                      for pair in settings.slots(spec)}
            self.assertNotIn(_BLOCKED, models)
            self.assertIn(_FLASH, models)
            self.assertEqual(multi.validate_full_consult_routes(settings.defaults(profile)), [])

    def test_model_menus_hide_only_blocked_id_without_reindexing_callbacks(self):
        blocked_index = list(config.LLM_MODELS).index(_BLOCKED)
        flash_index = list(config.LLM_MODELS).index(_FLASH)
        for code in ("a", "r"):
            for role in range(11):
                for slot in range(3):
                    _, keyboard, _ = ui.handle_callback(self.conn, f"fmc:m:{code}:{role}:{slot}")
                    callbacks = {b["callback_data"] for row in keyboard["inline_keyboard"] for b in row}
                    self.assertNotIn(f"fmc:c:{code}:{role}:{slot}:{blocked_index}", callbacks)
                    if slot == 0:
                        self.assertIn(f"fmc:c:{code}:{role}:{slot}:{flash_index}", callbacks)
            _, keyboard, _ = ui.handle_callback(self.conn, f"fmc:gm:{code}")
            callbacks = {b["callback_data"] for row in keyboard["inline_keyboard"] for b in row}
            self.assertNotIn(f"fmc:gc:{code}:{blocked_index}", callbacks)
            self.assertIn(f"fmc:gc:{code}:{flash_index}", callbacks)

    def test_stale_model_and_rescue_buttons_cannot_restore_excluded_model(self):
        index = list(config.LLM_MODELS).index(_BLOCKED)
        for code in ("a", "r"):
            for callback in (f"fmc:c:{code}:2:0:{index}", f"fmc:s:{code}:2:0:{index}:1",
                             f"fmc:gc:{code}:{index}", f"fmc:gg:{code}:{index}:0",
                             f"fmc:gs:{code}:{index}:0:1"):
                with self.subTest(callback=callback), self.assertRaises(ValueError):
                    ui.handle_callback(self.conn, callback)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM full_model_routes").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM full_model_rescue").fetchone()[0], 0)

    def test_backend_rejects_excluded_primary_fallback_and_rescue(self):
        for profile in settings.PROFILES:
            for position in range(3):
                ok, error = db.set_full_model_slot(self.conn, profile, "fundamentals_review", position, _BLOCKED, "high")
                self.assertFalse(ok)
                self.assertIn("排除", error)
            self.assertFalse(db.set_full_model_rescue(self.conn, profile, _BLOCKED, "high", "ik_glm")[0])

    def test_legacy_blocked_primary_promotes_existing_fallback_and_preserves_its_effort(self):
        pairs = [{"model": _BLOCKED, "effort": "high"},
                 {"model": "gemini-4-argon-high", "effort": "low"},
                 {"model": "deepseek-v4-pro", "effort": "high"}]
        for profile in settings.PROFILES:
            raw = self._save_legacy("fundamentals_review", pairs, profile)
            with self.assertLogs(db.log, level="WARNING"):
                role = db.get_full_model_profile(self.conn, profile)["fundamentals_review"]
            self.assertEqual(settings.slots(role), [pairs[1], _EMPTY, pairs[2]])
            saved = self.conn.execute("SELECT slots_json FROM full_model_routes WHERE profile=?", (profile,)).fetchone()[0]
            self.assertEqual(saved, raw)

    def test_legacy_blocked_fallback_is_cleared_without_resetting_other_manual_choices(self):
        pairs = [{"model": "gpt-6-astra-cpa", "effort": "xhigh"},
                 {"model": _BLOCKED, "effort": "high"},
                 {"model": "claude-sonnet-5-5-high", "effort": "medium"}]
        raw = self._save_legacy("market_primary", pairs)
        with self.assertLogs(db.log, level="WARNING"):
            role = db.get_full_model_profile(self.conn, "analyze")["market_primary"]
        self.assertEqual(settings.slots(role), [pairs[0], _EMPTY, pairs[2]])
        self.assertEqual(settings.route_problems(role), [])
        self.assertEqual(self.conn.execute("SELECT slots_json FROM full_model_routes").fetchone()[0], raw)

    def test_legacy_promotion_does_not_leave_same_group_as_first_effective_fallback(self):
        pairs = [{"model": _BLOCKED, "effort": "high"},
                 {"model": "gpt-6.1-sol", "effort": "xhigh"},
                 {"model": "gpt-6-sol", "effort": "high"}]
        self._save_legacy("lesson_match", pairs)
        with self.assertLogs(db.log, level="WARNING"):
            role = db.get_full_model_profile(self.conn, "analyze")["lesson_match"]
        self.assertEqual(settings.slots(role), [pairs[1], _EMPTY, _EMPTY])
        self.assertEqual(settings.route_problems(role), [])

    def test_legacy_excluded_only_primary_uses_new_default_without_enabling_empty_fallbacks(self):
        self._save_legacy("fundamentals_review", [{"model": _BLOCKED, "effort": "high"}, _EMPTY, _EMPTY])
        with self.assertLogs(db.log, level="WARNING"):
            role = db.get_full_model_profile(self.conn, "analyze")["fundamentals_review"]
        self.assertEqual(role["model"], "gemini-4-argon-high")
        self.assertEqual(settings.slots(role)[1:], [_EMPTY, _EMPTY])

    def test_legacy_excluded_rescue_uses_safe_default_and_retains_original_archive(self):
        self.conn.execute("INSERT INTO full_model_rescue VALUES (?,?,?,?,?)",
                          ("analyze", _BLOCKED, "high", "ik_glm", "offline"))
        with self.assertLogs(db.log, level="WARNING"):
            rescue = db.get_full_model_rescue(self.conn, "analyze")
        self.assertEqual(rescue, settings.rescue_defaults("analyze"))
        self.assertEqual(self.conn.execute("SELECT model FROM full_model_rescue").fetchone()[0], _BLOCKED)

    def test_excluded_snapshot_and_direct_routes_never_request_models(self):
        routes = settings.defaults("analyze")
        routes["fundamentals_review"].update(model=_BLOCKED, fallbacks=())
        with patch.object(multi, "_run_one") as expert, patch.object(llm_client, "chat_model") as chat:
            report, summary = multi.run("csv", "fund", "H", "A", "L", settings=routes)
            self.assertEqual(report, "")
            self.assertIn("配置错误", summary["error"])
            result, _ = multi._call_route("system", "input", routes["fundamentals_review"],
                parse_json=True, phase="offline", rescue=settings.rescue_defaults("analyze"))
            self.assertIsNone(result)
            expert.assert_not_called()
            chat.assert_not_called()

    def test_direct_stream_cannot_bypass_full_model_exclusion(self):
        spec = {"model": _BLOCKED, "effort": "high", "max_tokens": 0, "fallbacks": ()}
        with patch.object(multi, "_synthesis_prompt", return_value=("system", "input", {})), \
             patch.object(llm_client, "stream_chat_model") as stream:
            self.assertEqual(multi._synthesize({}, [], spec=spec, rescue=settings.rescue_defaults("analyze")), "")
        stream.assert_not_called()

    def test_ordinary_explicit_client_can_still_use_glm53(self):
        endpoint = {"label": "offline", "route_group": "ik_glm", "key": "offline", "base_url": "https://offline.invalid"}
        with patch.object(llm_client, "available", return_value=True), \
             patch.object(llm_client, "get_settings", return_value={"non_stream_timeout": 1, "max_retries": 0}), \
             patch.object(llm_client, "_endpoints_by_priority", return_value=[(0, endpoint)]), \
             patch.object(llm_client, "_breakers", [Mock(allow=Mock(return_value=True))]), \
             patch.object(llm_client, "_do_chat", return_value=("ok", True, "", False, False, False)) as request:
            self.assertEqual(llm_client.chat_model("system", "input", _BLOCKED, effort="high"), "ok")
        self.assertEqual(request.call_args.args[1]["model"], _BLOCKED)


if __name__ == "__main__":
    unittest.main()
