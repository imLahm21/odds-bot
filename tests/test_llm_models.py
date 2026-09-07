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
    sys.modules["requests"] = requests_stub

from bot import config, db, llm_client   # noqa: E402


PRIMARY = {
    "model_heavy": "gpt-6-astra",
    "model_heavy_visitor": "grok-4.6",
    "model_balanced": "gpt-5.6-sol",
    "model_balanced_visitor": "grok-4.6",
    "model_light": "deepseek-v4-flash",
    "model_light_visitor": "deepseek-v4-flash",
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
    "model_heavy_visitor": "grok-4.6",
    "model_balanced": "grok-4.6",
    "model_balanced_visitor": "grok-4.6",
    "model_light": "deepseek-v4-flash",
    "model_light_visitor": "deepseek-v4-flash",
}


class TestModelProfile(unittest.TestCase):
    def test_primary_and_fallback_exactly_match_profile(self):
        actual = {}
        for tier in config.LLM_TIER_MODELS:
            actual[f"model_{tier}"] = config.llm_tier_default(tier, False)
            actual[f"model_{tier}_visitor"] = config.llm_tier_default(tier, True)
        self.assertEqual(actual, PRIMARY)
        self.assertEqual(config.LLM_FALLBACK_TIER_MODELS, {
            "heavy": "grok-4.6",
            "balanced": "grok-4.6",
            "light": "deepseek-v4-flash",
        })
        self.assertEqual(config.LLM_MODEL, "gpt-6-astra")
        self.assertEqual(config.FUND_ANALYZE_MODEL, "gpt-5.6-sol")
        self.assertEqual(config.LLM_LIVE_MODEL, "deepseek-v4-flash")

    def test_endpoint_pool_can_share_ikuncode_base_url_without_model_maps(self):
        env = {
            "LLM_BASE_URL": "https://api.ikuncode.cc/v1",
            "LLM_API_KEY": "test-key-0",
            "LLM_ENDPOINTS": (
                "test-key-1|https://api.ikuncode.cc/v1|Codex,"
                "test-key-2||Codex-Mixed"
            ),
        }
        with patch.dict(os.environ, env, clear=False):
            endpoints = llm_client._parse_endpoints()
        self.assertEqual(len(endpoints), 3)
        self.assertTrue(all(
            ep["base_url"] == "https://api.ikuncode.cc/v1"
            for ep in endpoints))
        self.assertTrue(all(ep["model_map"] == {} for ep in endpoints))

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

    def test_fallback_button_backend_and_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "odds.db")
            db.init_db(path)
            with patch.object(config, "DB_PATH", path):
                llm_client.apply_fallback_models()
                self.assertEqual(llm_client.get_tier_model("heavy"), "grok-4.6")
                self.assertEqual(
                    llm_client.get_tier_model("balanced", visitor=True),
                    "grok-4.6")
                self.assertEqual(
                    llm_client.get_tier_model("light"),
                    "deepseek-v4-flash")

                llm_client.reset_runtime_models()
                self.assertEqual(
                    llm_client.get_tier_model("heavy"), "gpt-6-astra")
                self.assertEqual(
                    llm_client.get_tier_model("heavy", visitor=True),
                    "grok-4.6")
                self.assertEqual(
                    llm_client.get_tier_model("balanced"), "gpt-5.6-sol")


if __name__ == "__main__":
    unittest.main(verbosity=2)
