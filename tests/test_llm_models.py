"""LLM 主模型、回退模型与既有 odds.db 一次性迁移测试。"""

import os
import json
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("APIFOOTBALL_KEY", "offline-test-key")

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

try:
    import jwt  # noqa: F401
except ModuleNotFoundError:
    jwt_stub = types.ModuleType("jwt")
    jwt_stub.encode = lambda *args, **kwargs: "token"
    sys.modules["jwt"] = jwt_stub

try:
    import markdown  # noqa: F401
except ModuleNotFoundError:
    markdown_stub = types.ModuleType("markdown")
    markdown_stub.markdown = lambda text, **kwargs: text
    sys.modules["markdown"] = markdown_stub

from bot import analyzer, config, db, llm_client, tgbot   # noqa: E402


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

# 回退槽（fallback_*）的 seed 默认值。db 现在存 12 个键 = 6 主 + 6 回退。
FALLBACK_SLOTS = {
    "fallback_heavy": "grok-4.6",
    "fallback_heavy_visitor": "deepseek-v4-flash",
    "fallback_balanced": "grok-4.6",
    "fallback_balanced_visitor": "deepseek-v4-flash",
    "fallback_light": "glm-5.3-flash",
    "fallback_light_visitor": "glm-5.3-flash",
}


def full_state(primary: dict) -> dict:
    """把 6 个主模型期望值补成 get_llm_runtime_state 返回的完整 12 键。"""
    return {**primary, **FALLBACK_SLOTS}


# 供 patch llm_client._runtime_models 用：绕开 DB，锁定 12 槽位取值。
RUNTIME_MODELS = full_state(PRIMARY)


class _FakeBreaker:
    def __init__(self):
        self.records = []
        self.state = "CLOSED"

    def allow(self):
        return True

    def record(self, ok):
        self.records.append(ok)

    def stats(self):
        return {
            "state": self.state,
            "consecutive": 0,
            "total": len(self.records),
            "fails": sum(1 for ok in self.records if not ok),
            "error_rate": 0.0,
            "half_ok": 0,
            "open_remain": 0,
        }


class TestModelProfile(unittest.TestCase):
    def test_primary_and_fallback_exactly_match_profile(self):
        actual = {}
        for tier in config.LLM_TIER_MODELS:
            actual[f"model_{tier}"] = config.llm_tier_default(tier, False)
            actual[f"model_{tier}_visitor"] = config.llm_tier_default(tier, True)
        self.assertEqual(actual, PRIMARY)
        self.assertEqual(config.LLM_FALLBACK_TIER_MODELS, {
            "heavy": {"admin": "grok-4.6", "visitor": "deepseek-v4-flash"},
            "balanced": {"admin": "grok-4.6", "visitor": "deepseek-v4-flash"},
            "light": {"admin": "glm-5.3-flash", "visitor": "glm-5.3-flash"},
        })
        self.assertEqual(config.LLM_MODEL, "gpt-6-astra")
        self.assertEqual(
            {model: config.llm_route_groups_for_model(model)
             for model in config.LLM_MODELS},
            {
                "gpt-6-astra": ("ik_gpt",),
                "gpt-5.6-sol": ("ik_gpt",),
                "gpt-5.6-luna": ("openai_gpt",),
                "grok-4.6": ("ik_grok",),
                "deepseek-v4-flash": ("ik_deepseek",),
                "glm-5.3-flash": ("ik_glm",),
            })

    def test_group_inferred_from_model_prefix(self):
        """同系列新模型无需登记路由表即可归组；只有走官方的才需显式覆盖。"""
        self.assertEqual(
            config.llm_route_groups_for_model("grok-4.7"), ("ik_grok",))
        self.assertEqual(
            config.llm_route_groups_for_model("deepseek-v5"),
            ("ik_deepseek",))
        self.assertEqual(
            config.llm_route_groups_for_model("glm-6"), ("ik_glm",))
        self.assertEqual(
            config.llm_route_groups_for_model("gpt-7-nova"), ("ik_gpt",))
        self.assertEqual(
            config.llm_route_groups_for_model("gpt-5.6-luna"),
            ("openai_gpt",))
        self.assertEqual(config.llm_route_groups_for_model("llama-4"), ())
        self.assertEqual(config.llm_route_groups_for_model(""), ())
        self.assertTrue(config.llm_model_registered("gpt-6-astra"))
        self.assertFalse(config.llm_model_registered("gpt-5.6-terra"))
        self.assertEqual(
            config.llm_route_groups_for_model("gpt-5.6-terra"), ("ik_gpt",))

    def test_light_tier_pool_excludes_heavy_reasoning_models(self):
        """走地 1min 循环受不了重档推理，轻档池必须只有快模型。"""
        light = config.llm_tier_eligible_models("light")
        for slow in ("gpt-6-astra", "gpt-5.6-sol", "grok-4.6"):
            self.assertNotIn(slow, light)
        self.assertIn("gpt-5.6-luna", light)
        self.assertIn("glm-5.3-flash", light)
        self.assertIn("gpt-6-astra", config.llm_tier_eligible_models("heavy"))

    def test_grouped_endpoint_parser_separates_each_credential_family(self):
        raw = (
            "ik_gpt|key-gpt-1|https://api.ikuncode.cc/v1|IK-GPT-1,"
            "ik_gpt|key-gpt-2|https://api.ikuncode.cc/v1|IK-GPT-2,"
            "ik_grok|key-grok|https://api.ikuncode.cc/v1|IK-Grok,"
            "ik_deepseek|key-deepseek|https://api.ikuncode.cc/v1|IK-DeepSeek,"
            "ik_glm|key-glm|https://api.ikuncode.cc/v1|IK-GLM,"
            "openai_gpt|key-openai|https://api.openai.com/v1|OpenAI-Luna"
        )
        endpoints = llm_client._parse_route_endpoints(raw)
        self.assertEqual(len(endpoints), 6)
        self.assertEqual(
            [ep["route_group"] for ep in endpoints].count("ik_gpt"), 2)
        self.assertEqual(
            {frozenset(ep) for ep in endpoints},
            {frozenset({"key", "base_url", "label", "route_group"})})

    def test_legacy_env_vars_no_longer_produce_endpoints(self):
        """旧变量彻底不参与路由，但必须被显式报出来，不能沉默。"""
        env = {
            "LLM_BASE_URL": "https://api.openai.com/v1",
            "LLM_API_KEY": "test-openai-key",
            "LLM_ENDPOINTS": "k|https://api.ikuncode.cc/v1|Codex",
            "LLM_ROUTE_ENDPOINTS": "",
        }
        with patch.dict(os.environ, env, clear=False):
            self.assertEqual(llm_client._parse_route_endpoints(), [])
            self.assertEqual(
                set(llm_client._legacy_env_present()),
                {"LLM_BASE_URL", "LLM_API_KEY", "LLM_ENDPOINTS"})

    def test_same_key_cannot_be_assigned_to_two_groups(self):
        raw = (
            "ik_gpt|same-key|https://api.ikuncode.cc/v1|IK-GPT,"
            "ik_grok|same-key|https://api.ikuncode.cc/v1|IK-Grok"
        )
        with self.assertLogs("odds_bot.llm", level="ERROR"):
            endpoints = llm_client._parse_route_endpoints(raw)
        self.assertEqual(len(endpoints), 1)
        self.assertEqual(endpoints[0]["route_group"], "ik_gpt")

    @staticmethod
    def _group_endpoint(label, group):
        """构造与 _parse_route_endpoints 输出同形的假端点（四个键，不多不少）。"""
        return {
            "key": f"key-{label}",
            "base_url": ("https://api.openai.com/v1" if group == "openai_gpt"
                         else "https://api.ikuncode.cc/v1"),
            "label": label,
            "route_group": group,
        }

    def _all_group_endpoints(self):
        """五个组各一条端点，供「全组齐备」场景复用。"""
        return [self._group_endpoint(group, group)
                for group in config.LLM_ROUTE_GROUPS]

    def test_group_first_selection_and_independent_round_robin(self):
        endpoints = [
            self._group_endpoint("GPT-1", "ik_gpt"),
            self._group_endpoint("GPT-2", "ik_gpt"),
            self._group_endpoint("Grok", "ik_grok"),
            self._group_endpoint("DeepSeek", "ik_deepseek"),
            self._group_endpoint("GLM", "ik_glm"),
            self._group_endpoint("OpenAI", "openai_gpt"),
        ]
        breakers = [_FakeBreaker() for _ in endpoints]
        with (
            patch.object(llm_client, "_ENDPOINTS", endpoints),
            patch.object(llm_client, "_breakers", breakers),
            patch.object(llm_client, "_runtime_models", dict(RUNTIME_MODELS)),
            patch.object(llm_client, "_rr_counters", {}),
            patch.object(llm_client, "_get_disabled", return_value=set()),
            patch.object(llm_client, "_legacy_env_present", return_value=[]),
        ):
            grok = llm_client._endpoints_by_priority("grok-4.6")
            luna = llm_client._endpoints_by_priority("gpt-5.6-luna")
            first_gpt = llm_client._endpoints_by_priority("gpt-6-astra")
            second_gpt = llm_client._endpoints_by_priority("gpt-6-astra")
            self.assertEqual(llm_client.routing_issues(), [])

        self.assertEqual([ep["label"] for _, ep in grok], ["Grok"])
        self.assertEqual([ep["label"] for _, ep in luna], ["OpenAI"])
        self.assertEqual(
            {ep["label"] for _, ep in first_gpt}, {"GPT-1", "GPT-2"})
        self.assertNotEqual(
            first_gpt[0][1]["label"], second_gpt[0][1]["label"])

    def test_routing_issues_reports_missing_required_group(self):
        endpoints = [
            self._group_endpoint("GPT", "ik_gpt"),
            self._group_endpoint("Grok", "ik_grok"),
            self._group_endpoint("DeepSeek", "ik_deepseek"),
            self._group_endpoint("OpenAI", "openai_gpt"),
        ]
        with (
            patch.object(llm_client, "_ENDPOINTS", endpoints),
            patch.object(llm_client, "_runtime_models", dict(RUNTIME_MODELS)),
            patch.object(llm_client, "_legacy_env_present", return_value=[]),
        ):
            issues = llm_client.routing_issues()
        # 缺 ik_glm，命中的是轻档两个角色的【回退】槽（glm-5.3-flash）
        self.assertEqual(len(issues), 2)
        for item in issues:
            self.assertIn("glm-5.3-flash", item)
            self.assertIn("ik_glm", item)
            self.assertIn("回退", item)

    def test_routing_issues_flags_leftover_legacy_env(self):
        with (
            patch.object(llm_client, "_ENDPOINTS", self._all_group_endpoints()),
            patch.object(llm_client, "_runtime_models", dict(RUNTIME_MODELS)),
            patch.object(llm_client, "_legacy_env_present",
                         return_value=["LLM_API_KEY"]),
        ):
            issues = llm_client.routing_issues()
        self.assertEqual(len(issues), 1)
        self.assertIn("LLM_API_KEY", issues[0])
        self.assertIn("已不参与路由", issues[0])

    def test_routing_issues_flags_unregistered_model_without_blocking(self):
        runtime = dict(RUNTIME_MODELS)
        runtime["model_heavy"] = "gpt-5.6-terra"      # 已下线的旧名字
        with (
            patch.object(llm_client, "_ENDPOINTS", self._all_group_endpoints()),
            patch.object(llm_client, "_runtime_models", runtime),
            patch.object(llm_client, "_legacy_env_present", return_value=[]),
        ):
            issues = llm_client.routing_issues()
            snapshot = {(s["tier"], s["role"]): s
                        for s in llm_client.slot_snapshot()}
            chain = llm_client.resolve_model_chain("heavy", visitor=False)
        self.assertEqual(len(issues), 1)
        self.assertIn("gpt-5.6-terra", issues[0])
        self.assertIn("404", issues[0])
        slot = snapshot[("heavy", "admin")]
        self.assertTrue(slot["primary_ready"])
        self.assertTrue(slot["primary_retired"])
        self.assertEqual(chain[0], "gpt-5.6-terra")

    def test_group_signature_prevents_cross_group_switch_collision(self):
        left = self._group_endpoint("Shared", "ik_gpt")
        right = self._group_endpoint("Shared", "ik_grok")
        self.assertNotEqual(llm_client._sig(left), llm_client._sig(right))
        self.assertEqual(llm_client._legacy_sig(left),
                         llm_client._legacy_sig(right))

    def test_routing_snapshot_never_exposes_keys(self):
        endpoint = self._group_endpoint("SecretEndpoint", "ik_gpt")
        endpoint["key"] = "never-print-this-secret"
        with (
            patch.object(llm_client, "_ENDPOINTS", [endpoint]),
            patch.object(llm_client, "_runtime_models", dict(RUNTIME_MODELS)),
            patch.object(llm_client, "_legacy_env_present", return_value=[]),
        ):
            snapshot = llm_client.routing_snapshot()
        rendered = json.dumps(snapshot, ensure_ascii=False)
        self.assertNotIn("never-print-this-secret", rendered)
        self.assertEqual(snapshot["endpoints"][0]["route_group"], "ik_gpt")
        # 6 槽位（3 档 × 2 角色），每槽位内含 primary+fallback 两个模型值
        # （12 个 runtime_state 键，但 slot_snapshot 按槽位分组，不是按键铺平）。
        self.assertEqual(len(snapshot["slots"]), 6)
        self.assertNotIn("mode", snapshot)

    def test_llm_panel_and_keyboard_are_group_based(self):
        groups = list(config.LLM_ROUTE_GROUPS)
        endpoints = [self._group_endpoint(group, group) for group in groups]
        breakers = [_FakeBreaker() for _ in endpoints]
        settings = {key: spec["default"]
                    for key, spec in config.LLM_SETTING_SPECS.items()}

        def current_model(tier, visitor=False):
            return config.llm_tier_default(tier, visitor)

        def current_fallback(tier, visitor=False):
            return config.llm_tier_fallback(tier, visitor)

        with (
            patch.object(llm_client, "_ENDPOINTS", endpoints),
            patch.object(llm_client, "_breakers", breakers),
            patch.object(llm_client, "_get_disabled", return_value=set()),
            patch.object(llm_client, "get_settings", return_value=settings),
            patch.object(llm_client, "get_tier_model",
                         side_effect=current_model),
            patch.object(llm_client, "get_fallback_model",
                         side_effect=current_fallback),
            patch.object(llm_client, "configured_groups",
                         return_value=set(groups)),
            patch.object(llm_client, "slot_snapshot", return_value=[]),
        ):
            panel = tgbot._llm_panel_text()
            keyboard = tgbot._llm_panel_keyboard()

        for group in groups:
            self.assertIn(group, panel)
        self.assertNotIn("key-ik_gpt", panel)
        callbacks = [button["callback_data"]
                     for row in keyboard["inline_keyboard"]
                     for button in row]
        self.assertTrue(all(f"ltg:{group}" in callbacks for group in groups))
        self.assertTrue(any(cb.startswith("lms:") for cb in callbacks))
        self.assertNotIn("lt:all:heavy", callbacks)

    def test_group_probe_tests_only_models_bound_to_that_group(self):
        endpoints = [
            {"label": "GPT-1", "route_group": "ik_gpt"},
            {"label": "GPT-2", "route_group": "ik_gpt"},
            {"label": "Grok", "route_group": "ik_grok"},
        ]
        calls = []

        def fake_probe(idx, model):
            calls.append((idx, model))
            return {
                "ok": True, "http_status": 200, "latency_ms": 1,
                "model": model, "req_model": model, "which": "model",
                "error": "", "breaker_state": "CLOSED",
            }

        with (
            patch.object(llm_client, "endpoints", return_value=endpoints),
            patch.object(llm_client, "probe_model", side_effect=fake_probe),
            patch.object(tgbot, "_llm_panel_text", return_value="panel"),
            patch.object(tgbot, "_llm_panel_keyboard",
                         return_value={"inline_keyboard": []}),
            patch.object(tgbot, "edit_text") as edit,
        ):
            tgbot._llm_run_group_probe(1, 2, "ik_gpt")

        expected_models = {"gpt-6-astra", "gpt-5.6-sol"}
        self.assertEqual({model for _, model in calls}, expected_models)
        self.assertEqual({idx for idx, _ in calls}, {0, 1})
        self.assertNotIn("grok-4.6", {model for _, model in calls})
        edit.assert_called_once()

    def test_resolve_model_chain_drops_model_with_no_configured_group(self):
        """访客重档默认 deepseek，只买了 GPT 密钥时——链应该跳过它、退到回退模型，
        而不是把请求硬发给没有 key 的组（这正是本次要修的故障模式）。"""
        gpt_only = [self._group_endpoint("GPT", "ik_gpt")]
        runtime = dict(RUNTIME_MODELS)
        runtime["model_heavy_visitor"] = "deepseek-v4-flash"
        runtime["fallback_heavy_visitor"] = "gpt-6-astra"
        with (
            patch.object(llm_client, "_ENDPOINTS", gpt_only),
            patch.object(llm_client, "_runtime_models", runtime),
        ):
            chain = llm_client.resolve_model_chain("heavy", visitor=True)
        self.assertEqual(chain, ["gpt-6-astra"])

    def test_resolve_model_chain_empty_when_nothing_configured(self):
        with (
            patch.object(llm_client, "_ENDPOINTS", []),
            patch.object(llm_client, "_runtime_models", dict(RUNTIME_MODELS)),
        ):
            chain = llm_client.resolve_model_chain("heavy", visitor=False)
        self.assertEqual(chain, [])

    def test_chat_escalates_to_fallback_group_when_primary_group_absent(self):
        """核心场景：只买了 GPT 密钥，访客重档默认 deepseek——
        chat() 必须自动升级到回退模型（grok 会被跳过，因为 grok 组同样没配密钥；
        这里回退直接指向 GPT 组内的模型，验证真正打进请求的是回退模型名）。"""
        gpt_ep = self._group_endpoint("GPT", "ik_gpt")
        breaker = _FakeBreaker()
        runtime = dict(RUNTIME_MODELS)
        runtime["model_heavy_visitor"] = "deepseek-v4-flash"
        runtime["fallback_heavy_visitor"] = "gpt-6-astra"
        calls = []

        def fake_do_chat(ep, payload, read_to, retries):
            calls.append(payload["model"])
            return "ok", True, "", False, False

        with (
            patch.object(llm_client, "_ENDPOINTS", [gpt_ep]),
            patch.object(llm_client, "_breakers", [breaker]),
            patch.object(llm_client, "_runtime_models", runtime),
            patch.object(llm_client, "_get_disabled", return_value=set()),
            patch.object(llm_client, "get_settings", return_value={
                key: spec["default"]
                for key, spec in config.LLM_SETTING_SPECS.items()}),
            patch.object(llm_client, "_do_chat", side_effect=fake_do_chat),
        ):
            result = llm_client.chat(
                "system", "user", tier="heavy", visitor=True)

        self.assertEqual(result, "ok")
        self.assertEqual(calls, ["gpt-6-astra"])

    def test_chat_reports_precise_error_when_chain_empty(self):
        """两个候选都缺组时，错误串必须点名具体缺哪个组——不能只说「失败」。
        端点池非空（否则 available() 为 False，走的是另一条「未配置」文案），
        但唯一配置的组（ik_gpt）覆盖不了这个槽位的主/回退模型。"""
        gpt_only = [self._group_endpoint("GPT", "ik_gpt")]
        runtime = dict(RUNTIME_MODELS)
        runtime["model_heavy"] = "deepseek-v4-flash"
        runtime["fallback_heavy"] = "grok-4.6"
        with (
            patch.object(llm_client, "_ENDPOINTS", gpt_only),
            patch.object(llm_client, "_runtime_models", runtime),
        ):
            result = llm_client.chat("system", "user", tier="heavy")
        self.assertIn("deepseek-v4-flash", result)
        self.assertIn("ik_deepseek", result)
        self.assertIn("grok-4.6", result)
        self.assertIn("ik_grok", result)

    def test_stream_chat_escalates_before_first_byte(self):
        """流式：主模型组无端点时应静默升级到回退模型，用户全程只看到一次输出。"""
        gpt_ep = self._group_endpoint("GPT", "ik_gpt")
        breaker = _FakeBreaker()
        runtime = dict(RUNTIME_MODELS)
        runtime["model_heavy_visitor"] = "deepseek-v4-flash"
        runtime["fallback_heavy_visitor"] = "gpt-6-astra"
        routed = []

        def fake_stream(ep, payload, first_byte_to, idle_to, retries):
            routed.append(payload["model"])
            yield ("done", "ok")

        with (
            patch.object(llm_client, "_ENDPOINTS", [gpt_ep]),
            patch.object(llm_client, "_breakers", [breaker]),
            patch.object(llm_client, "_runtime_models", runtime),
            patch.object(llm_client, "_get_disabled", return_value=set()),
            patch.object(llm_client, "get_settings", return_value={
                key: spec["default"]
                for key, spec in config.LLM_SETTING_SPECS.items()}),
            patch.object(llm_client, "_stream_one", side_effect=fake_stream),
        ):
            events = list(llm_client.stream_chat(
                "system", "user", tier="heavy", visitor=True))
        self.assertEqual(events, [("done", "ok")])
        self.assertEqual(routed, ["gpt-6-astra"])

    def test_stream_chat_does_not_switch_model_after_content_emitted(self):
        """已经吐过正文的流断了，不能静默换模型重来——那会产生重复可见输出。"""
        gpt_ep = self._group_endpoint("GPT", "ik_gpt")
        breaker = _FakeBreaker()
        runtime = dict(RUNTIME_MODELS)
        runtime["model_heavy"] = "gpt-6-astra"
        runtime["fallback_heavy"] = "gpt-5.6-sol"

        def fake_stream(ep, payload, first_byte_to, idle_to, retries):
            yield ("delta", "已经吐出的正文")
            yield ("error", "读取中断")

        with (
            patch.object(llm_client, "_ENDPOINTS", [gpt_ep]),
            patch.object(llm_client, "_breakers", [breaker]),
            patch.object(llm_client, "_runtime_models", runtime),
            patch.object(llm_client, "_get_disabled", return_value=set()),
            patch.object(llm_client, "get_settings", return_value={
                key: spec["default"]
                for key, spec in config.LLM_SETTING_SPECS.items()}),
            patch.object(llm_client, "_stream_one", side_effect=fake_stream),
        ):
            events = list(llm_client.stream_chat("system", "user", tier="heavy"))
        kinds = [ev[0] for ev in events]
        self.assertEqual(kinds, ["delta", "error"])
        self.assertEqual(breaker.records, [False])

    def test_probe_skips_unsupported_model_without_http(self):
        official = self._group_endpoint("OpenAI", "openai_gpt")
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

    def test_chain_error_names_missing_groups_for_every_candidate(self):
        runtime = dict(RUNTIME_MODELS)
        runtime["model_heavy_visitor"] = "deepseek-v4-flash"
        runtime["fallback_heavy_visitor"] = "grok-4.6"
        with (
            patch.object(llm_client, "_ENDPOINTS", []),
            patch.object(llm_client, "_runtime_models", runtime),
        ):
            msg = llm_client.chain_error("heavy", visitor=True)
        self.assertIn("deepseek-v4-flash", msg)
        self.assertIn("ik_deepseek", msg)
        self.assertIn("grok-4.6", msg)
        self.assertIn("ik_grok", msg)

    def test_parlay_decision_extraction_forwards_visitor_role(self):
        response = (
            '{"pass":true,"play":"","odds":0,"edge":0,'
            '"p_final":0,"evidence":"none","stake":null}'
        )
        with (
            patch.object(analyzer, "available", return_value=True),
            patch.object(analyzer, "_call_llm",
                         return_value=response) as call,
        ):
            result = analyzer.extract_decision(
                "### 8. decision\npass", visitor=True)
        self.assertTrue(result["pass"])
        self.assertTrue(call.call_args.kwargs["visitor"])
        self.assertEqual(call.call_args.kwargs["tier"], "balanced")

    def test_old_primary_values_migrate_once(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(db.SCHEMA)
            conn.executemany(
                "INSERT INTO llm_runtime_state (key, value) VALUES (?,?)",
                list(OLD_PRIMARY.items()))
            db.seed_config(conn)
            self.assertEqual(
                db.get_llm_runtime_state(conn), full_state(PRIMARY))
            version = conn.execute(
                "SELECT value FROM llm_runtime_state WHERE key=?",
                (db._LLM_MODEL_PROFILE_KEY,)).fetchone()
            self.assertEqual(version[0], config.LLM_TIER_MODEL_PROFILE_VERSION)

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
        """用户可能把某槽位指向网关的私有模型——迁移不能替他决定，只能改
        它认得的旧名字。"""
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
            self.assertEqual(
                db.get_llm_runtime_state(conn), full_state(FALLBACK))
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
                        "INSERT INTO llm_runtime_state (key, value) "
                        "VALUES (?,?)", list(old_profile.items()))
                    db.seed_config(conn)
                    self.assertEqual(
                        db.get_llm_runtime_state(conn), full_state(expected))
                finally:
                    conn.close()

    def test_seed_writes_all_twelve_slots_on_fresh_db(self):
        """全新库（无任何旧值）也要 seed 出完整 12 键——这是本次从 6 键扩到
        12 键最容易漏的地方：只 seed 主模型、忘了回退槽。"""
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(db.SCHEMA)
            db.seed_config(conn)
            state = db.get_llm_runtime_state(conn)
            self.assertEqual(len(state), 12)
            self.assertEqual(state, full_state(PRIMARY))
        finally:
            conn.close()

    def test_fallback_button_backend_and_reset(self):
        """「一键切到回退模型」把 6 个回退值写进主槽；恢复默认把 12 槽位复原。"""
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
                    llm_client.get_tier_model("light"), "glm-5.3-flash")
                # 回退槽本身不动（幂等，可反复点）
                self.assertEqual(
                    llm_client.get_fallback_model("heavy"), "grok-4.6")

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
                self.assertEqual(
                    llm_client.get_fallback_model("heavy"), "grok-4.6")


if __name__ == "__main__":
    unittest.main(verbosity=2)
