"""LLM 主模型、回退模型与既有 odds.db 一次性迁移测试。"""

import os
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 本地最小测试环境可能未安装 bot 运行依赖；这些测试不发网络请求。
try:
    import dotenv  # noqa: F401
except ModuleNotFoundError:
    dotenv_stub = types.ModuleType("dotenv")
    dotenv_stub.load_dotenv = lambda *args, **kwargs: None
    sys.modules["dotenv"] = dotenv_stub

try:
    import requests  # noqa: F401
except ModuleNotFoundError:
    requests_stub = types.ModuleType("requests")
    requests_stub.exceptions = types.SimpleNamespace(
        Timeout=TimeoutError, RequestException=Exception)
    requests_stub.post = lambda *args, **kwargs: None
    sys.modules["requests"] = requests_stub

from bot import config, db, llm_client   # noqa: E402


PRIMARY = {
    "model_heavy": "gpt-6-astra",
    "model_heavy_visitor": "deepseek-v4-flash",
    "model_balanced": "gpt-5.6-sol",
    "model_balanced_visitor": "deepseek-v4-flash",
    "model_light": "gpt-5.6-luna",
    "model_light_visitor": "gpt-5.6-luna",
}

OLD_PRIMARY = {
    "model_heavy": "gpt-5.6-sol",
    "model_heavy_visitor": "gpt-5.6-terra",
    "model_balanced": "gpt-5.6-terra",
    "model_balanced_visitor": "gpt-5.6-terra",
    "model_light": "gpt-5.6-luna",
    "model_light_visitor": "gpt-5.6-luna",
}

OLD_FALLBACK = {
    "model_heavy": "gpt-5.5",
    "model_heavy_visitor": "gpt-5.5",
    "model_balanced": "gpt-5.4-mini",
    "model_balanced_visitor": "gpt-5.4-mini",
    "model_light": "gpt-5.4-mini",
    "model_light_visitor": "gpt-5.4-mini",
}

FALLBACK = {
    "model_heavy": "grok-4.6",
    "model_heavy_visitor": "deepseek-v4-flash",
    "model_balanced": "grok-4.6",
    "model_balanced_visitor": "deepseek-v4-flash",
    "model_light": "glm-5.3-flash",
    "model_light_visitor": "glm-5.3-flash",
}

V1_PRIMARY = {
    **PRIMARY,
    "model_heavy_visitor": "grok-4.6",
    "model_balanced_visitor": "grok-4.6",
    "model_light": "deepseek-v4-flash",
    "model_light_visitor": "deepseek-v4-flash",
}

V1_FALLBACK = {
    **FALLBACK,
    "model_heavy_visitor": "grok-4.6",
    "model_balanced_visitor": "grok-4.6",
    "model_light": "deepseek-v4-flash",
    "model_light_visitor": "deepseek-v4-flash",
}


class _FakeBreaker:
    def __init__(self):
        self.records = []
        self.state = "CLOSED"

    def allow(self):
        return True

    def record(self, ok):
        self.records.append(ok)


class TestModelProfile(unittest.TestCase):
    def test_primary_and_fallback_exactly_match_profile(self):
        actual = {}
        for tier in config.LLM_TIER_MODELS:
            actual[f"model_{tier}"] = config.llm_tier_default(tier, False)
            actual[f"model_{tier}_visitor"] = config.llm_tier_default(tier, True)
        self.assertEqual(actual, PRIMARY)
        self.assertEqual(config.LLM_FALLBACK_TIER_MODELS, {
            "heavy": {
                "admin": "grok-4.6",
                "visitor": "deepseek-v4-flash",
            },
            "balanced": {
                "admin": "grok-4.6",
                "visitor": "deepseek-v4-flash",
            },
            "light": {
                "admin": "glm-5.3-flash",
                "visitor": "glm-5.3-flash",
            },
        })
        self.assertEqual(config.LLM_MODEL, "gpt-6-astra")
        self.assertEqual(config.FUND_ANALYZE_MODEL, "gpt-5.6-sol")
        self.assertEqual(config.LLM_LIVE_MODEL, "gpt-5.6-luna")

    def test_mixed_openai_luna_and_ikuncode_capabilities_parse(self):
        env = {
            "LLM_BASE_URL": "https://api.openai.com/v1",
            "LLM_API_KEY": "test-openai-key",
            "LLM_SUPPORTED_MODELS": "gpt-5.6-luna",
            "LLM_ENDPOINTS": (
                "test-key-1|https://api.ikuncode.cc/v1|Codex||"
                "gpt-6-astra:gpt-5.6-sol:deepseek-v4-flash:grok-4.6:"
                "glm-5.3-flash,"
                "test-key-2|https://api.ikuncode.cc/v1|Codex-Mixed||"
                "gpt-6-astra:gpt-5.6-sol:deepseek-v4-flash:grok-4.6:"
                "glm-5.3-flash"
            ),
        }
        with patch.dict(os.environ, env, clear=False):
            endpoints = llm_client._parse_endpoints()
        self.assertEqual(len(endpoints), 3)
        self.assertTrue(all(ep["model_map"] == {} for ep in endpoints))
        self.assertEqual(endpoints[0]["base_url"], "https://api.openai.com/v1")
        self.assertEqual(endpoints[0]["supported_models"], {"gpt-5.6-luna"})
        self.assertTrue(all(
            ep["base_url"] == "https://api.ikuncode.cc/v1"
            for ep in endpoints[1:]))
        self.assertTrue(all("gpt-5.6-luna" not in ep["supported_models"]
                            for ep in endpoints[1:]))

    @staticmethod
    def _endpoint(label, supported):
        return {
            "key": f"key-{label}",
            "base_url": f"https://{label.lower()}.example/v1",
            "label": label,
            "model_map": {},
            "supported_models": set(supported),
        }

    def test_non_stream_luna_skips_ikuncode_without_breaker_penalty(self):
        ikuncode = self._endpoint("IKuncode", {
            "gpt-6-astra", "gpt-5.6-sol", "deepseek-v4-flash",
            "grok-4.6", "glm-5.3-flash",
        })
        official = self._endpoint("OpenAI", {"gpt-5.6-luna"})
        breakers = [_FakeBreaker(), _FakeBreaker()]
        settings = {key: spec["default"]
                    for key, spec in config.LLM_SETTING_SPECS.items()}
        calls = []

        def fake_chat(ep, payload, read_to, retries):
            calls.append((ep["label"], payload["model"]))
            return "ok", True, "", False, False

        with (
            patch.object(llm_client, "_ENDPOINTS", [ikuncode, official]),
            patch.object(llm_client, "_breakers", breakers),
            patch.object(llm_client, "_endpoints_by_priority",
                         return_value=[(0, ikuncode), (1, official)]),
            patch.object(llm_client, "get_settings", return_value=settings),
            patch.object(llm_client, "get_tier_model",
                         return_value="gpt-5.6-luna"),
            patch.object(llm_client, "_do_chat", side_effect=fake_chat),
        ):
            result = llm_client.chat("system", "user", tier="light")

        self.assertEqual(result, "ok")
        self.assertEqual(calls, [("OpenAI", "gpt-5.6-luna")])
        self.assertEqual(breakers[0].records, [])
        self.assertEqual(breakers[1].records, [True])

    def test_stream_visitor_deepseek_skips_openai_and_uses_ikuncode(self):
        official = self._endpoint("OpenAI", {"gpt-5.6-luna"})
        ikuncode = self._endpoint("IKuncode", {
            "gpt-6-astra", "gpt-5.6-sol", "deepseek-v4-flash",
            "grok-4.6", "glm-5.3-flash",
        })
        breakers = [_FakeBreaker(), _FakeBreaker()]
        settings = {key: spec["default"]
                    for key, spec in config.LLM_SETTING_SPECS.items()}
        routed = []

        def fake_stream(ep, payload, first_byte_to, idle_to, retries):
            routed.append((ep["label"], payload["model"]))
            yield ("done", "ok")

        with (
            patch.object(llm_client, "_ENDPOINTS", [official, ikuncode]),
            patch.object(llm_client, "_breakers", breakers),
            patch.object(llm_client, "_endpoints_by_priority",
                         return_value=[(0, official), (1, ikuncode)]),
            patch.object(llm_client, "get_settings", return_value=settings),
            patch.object(llm_client, "get_tier_model",
                         return_value="deepseek-v4-flash"),
            patch.object(llm_client, "_stream_one",
                         side_effect=fake_stream),
        ):
            events = list(llm_client.stream_chat(
                "system", "user", tier="heavy", visitor=True))

        self.assertEqual(events, [("done", "ok")])
        self.assertEqual(routed, [("IKuncode", "deepseek-v4-flash")])
        self.assertEqual(breakers[0].records, [])
        self.assertEqual(breakers[1].records, [True])

    def test_probe_skips_unsupported_model_without_http(self):
        official = self._endpoint("OpenAI", {"gpt-5.6-luna"})
        breaker = _FakeBreaker()
        with (
            patch.object(llm_client, "_ENDPOINTS", [official]),
            patch.object(llm_client, "_breakers", [breaker]),
            patch.object(llm_client, "get_tier_model",
                         return_value="gpt-6-astra"),
            patch.object(llm_client.requests, "post") as post,
        ):
            result = llm_client.probe(0, "heavy")
        self.assertTrue(result["skipped"])
        self.assertEqual(result["req_model"], "gpt-6-astra")
        post.assert_not_called()
        self.assertEqual(breaker.records, [])

    def test_old_primary_values_migrate_once(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(db.SCHEMA)
            conn.executemany(
                "INSERT INTO llm_runtime_state (key, value) VALUES (?,?)",
                list(OLD_PRIMARY.items()))
            db.seed_config(conn)
            self.assertEqual(db.get_llm_runtime_state(conn), PRIMARY)
            version = conn.execute(
                "SELECT value FROM llm_runtime_state WHERE key=?",
                (db._LLM_MODEL_PROFILE_KEY,)).fetchone()
            self.assertEqual(version[0], config.LLM_TIER_MODEL_PROFILE_VERSION)

            # 版本已记后不再重复迁移，保护之后的人工选择。
            conn.execute(
                "UPDATE llm_runtime_state SET value='gpt-5.6-terra' "
                "WHERE key='model_balanced'")
            conn.commit()
            db.seed_config(conn)
            self.assertEqual(
                db.get_llm_runtime_state(conn)["model_balanced"],
                "gpt-5.6-terra")
        finally:
            conn.close()

    def test_unknown_custom_model_is_preserved(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(db.SCHEMA)
            rows = dict(OLD_PRIMARY)
            rows["model_heavy"] = "custom-private-model"
            conn.executemany(
                "INSERT INTO llm_runtime_state (key, value) VALUES (?,?)",
                list(rows.items()))
            db.seed_config(conn)
            state = db.get_llm_runtime_state(conn)
            self.assertEqual(state["model_heavy"], "custom-private-model")
            self.assertEqual(state["model_balanced"], "gpt-5.6-sol")
        finally:
            conn.close()

    def test_old_fallback_values_migrate_to_new_fallback(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(db.SCHEMA)
            conn.executemany(
                "INSERT INTO llm_runtime_state (key, value) VALUES (?,?)",
                list(OLD_FALLBACK.items()))
            db.seed_config(conn)
            self.assertEqual(db.get_llm_runtime_state(conn), FALLBACK)
        finally:
            conn.close()

    def test_previous_primary_and_fallback_profiles_resolve_luna_ambiguity(self):
        for old_profile, expected in (
                (V1_PRIMARY, PRIMARY), (V1_FALLBACK, FALLBACK)):
            with self.subTest(old_profile=old_profile):
                conn = sqlite3.connect(":memory:")
                try:
                    conn.executescript(db.SCHEMA)
                    conn.executemany(
                        "INSERT INTO llm_runtime_state (key, value) VALUES (?,?)",
                        list(old_profile.items()))
                    db.seed_config(conn)
                    self.assertEqual(db.get_llm_runtime_state(conn), expected)
                finally:
                    conn.close()

    def test_fallback_button_backend_and_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "odds.db")
            db.init_db(path)
            with patch.object(config, "DB_PATH", path):
                llm_client.apply_fallback_models()
                self.assertEqual(llm_client.get_tier_model("heavy"), "grok-4.6")
                self.assertEqual(
                    llm_client.get_tier_model("balanced", visitor=True),
                    "deepseek-v4-flash")
                self.assertEqual(
                    llm_client.get_tier_model("light"),
                    "glm-5.3-flash")

                llm_client.reset_runtime_models()
                self.assertEqual(
                    llm_client.get_tier_model("heavy"), "gpt-6-astra")
                self.assertEqual(
                    llm_client.get_tier_model("heavy", visitor=True),
                    "deepseek-v4-flash")
                self.assertEqual(
                    llm_client.get_tier_model("balanced"), "gpt-5.6-sol")
                self.assertEqual(
                    llm_client.get_tier_model("light"), "gpt-5.6-luna")


if __name__ == "__main__":
    unittest.main(verbosity=2)
