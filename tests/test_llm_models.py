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
    "model_heavy_visitor": "deepseek-v4-pro",
    "model_balanced": "gpt-5.6-terra",
    "model_balanced_visitor": "deepseek-v4-flash",
    "model_light": "gpt-5.6-luna",
    "model_light_visitor": "gpt-5.6-luna",
}

# 上一版（v1）的主模型方案，被 PROFILE_UPGRADES 第一条精确识别。
OLD_PRIMARY = {
    "model_heavy": "gpt-6-astra",
    "model_heavy_visitor": "grok-4.6",
    "model_balanced": "gpt-5.6-sol",
    "model_balanced_visitor": "grok-4.6",
    "model_light": "deepseek-v4-flash",
    "model_light_visitor": "deepseek-v4-flash",
}

# 已彻底下线的旧模型名（不在 LLM_MODELS 里），由 UPGRADE_MAP 单值改写。
RETIRED_VALUES = {
    "model_heavy": "gpt-5.5",
    "model_heavy_visitor": "gpt-5.5",
    "model_balanced": "gpt-5.4-mini",
    "model_balanced_visitor": "gpt-5.4-mini",
    "model_light": "gpt-5.4-mini",
    "model_light_visitor": "gpt-5.4-mini",
}

# RETIRED_VALUES 经单值映射后应得到的主模型值。
RETIRED_UPGRADED = {
    "model_heavy": "grok-4.6",
    "model_heavy_visitor": "deepseek-v4-pro",
    "model_balanced": "grok-4.5",
    "model_balanced_visitor": "deepseek-v4-flash",
    "model_light": "gpt-5.6-luna",
    "model_light_visitor": "gpt-5.6-luna",
}

# 上一版（v1）的回退方案，被 PROFILE_UPGRADES 第二条精确识别。
V1_FALLBACK_PROFILE = {
    "model_heavy": "grok-4.6",
    "model_heavy_visitor": "grok-4.6",
    "model_balanced": "grok-4.6",
    "model_balanced_visitor": "grok-4.6",
    "model_light": "deepseek-v4-flash",
    "model_light_visitor": "deepseek-v4-flash",
}

V1_FALLBACK_UPGRADED = {
    "model_heavy": "grok-4.6",
    "model_heavy_visitor": "deepseek-v4-pro",
    "model_balanced": "grok-4.5",
    "model_balanced_visitor": "deepseek-v4-flash",
    "model_light": "gpt-5.6-luna",
    "model_light_visitor": "gpt-5.6-luna",
}

# 回退槽（fallback_*）的 seed 默认值。db 现在存 12 个键 = 6 主 + 6 回退。
# 轻型池只有 luna 一个模型，故轻档无同档回退可选，初值为空串。
FALLBACK_SLOTS = {
    "fallback_heavy": "grok-4.6",
    "fallback_heavy_visitor": "glm-5.3",
    "fallback_balanced": "grok-4.5",
    "fallback_balanced_visitor": "glm-5.3-flash",
    "fallback_light": "",
    "fallback_light_visitor": "",
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
            "heavy": {"admin": "grok-4.6", "visitor": "glm-5.3"},
            "balanced": {"admin": "grok-4.5", "visitor": "glm-5.3-flash"},
            "light": {"admin": "", "visitor": ""},
        })
        self.assertEqual(config.LLM_MODEL, "gpt-6-astra")
        self.assertEqual(
            {model: config.llm_route_groups_for_model(model)
             for model in config.LLM_MODELS},
            {
                "gpt-6-astra": ("ik_gpt",),
                "gpt-5.6-sol": ("ik_gpt",),
                "glm-5.3": ("ik_glm",),
                "grok-4.6": ("ik_grok",),
                "deepseek-v4-pro": ("ik_deepseek",),
                "gpt-5.6-terra": ("ik_gpt",),
                "deepseek-v4-flash": ("ik_deepseek",),
                "glm-5.3-flash": ("ik_glm",),
                "grok-4.5": ("ik_grok",),
                "gpt-5.6-luna": ("openai_gpt",),
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
        # gpt-5.6-terra 已重新登记为中型池成员（用户方案），不再算「下线」
        self.assertTrue(config.llm_model_registered("gpt-5.6-terra"))
        # gpt-5.5 才是彻底下线的名字：前缀仍能推出组，但 registered 为 False——
        # 这正是「能推出组 ≠ 仍在服役」的场景。
        self.assertFalse(config.llm_model_registered("gpt-5.5"))
        self.assertEqual(
            config.llm_route_groups_for_model("gpt-5.5"), ("ik_gpt",))

    def test_tier_pools_are_strictly_partitioned(self):
        """三池严格分区：每个模型只属于一个档位，不会跨档出现在可选池里。"""
        heavy = set(config.llm_tier_eligible_models("heavy"))
        balanced = set(config.llm_tier_eligible_models("balanced"))
        light = set(config.llm_tier_eligible_models("light"))
        self.assertEqual(heavy, {"gpt-6-astra", "gpt-5.6-sol", "glm-5.3",
                                 "grok-4.6", "deepseek-v4-pro"})
        self.assertEqual(balanced, {"gpt-5.6-terra", "deepseek-v4-flash",
                                    "glm-5.3-flash", "grok-4.5"})
        self.assertEqual(light, {"gpt-5.6-luna"})
        self.assertEqual(heavy & balanced, set())
        self.assertEqual(heavy & light, set())
        self.assertEqual(balanced & light, set())

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
        # 缺 ik_glm → 命中两个回退槽：重档访客回退 glm-5.3、平衡访客回退 glm-5.3-flash
        self.assertEqual(len(issues), 2)
        for item in issues:
            self.assertIn("ik_glm", item)
            self.assertIn("回退", item)
        joined = "\n".join(issues)
        self.assertIn("glm-5.3", joined)
        self.assertIn("glm-5.3-flash", joined)

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

    def test_routing_issues_flags_fallback_equal_to_primary(self):
        """回退与主模型相同 = 没有回退（同组同挂），必须报出来但不静默改值。"""
        runtime = dict(RUNTIME_MODELS)
        runtime["fallback_heavy"] = runtime["model_heavy"]
        with (
            patch.object(llm_client, "_ENDPOINTS", self._all_group_endpoints()),
            patch.object(llm_client, "_runtime_models", runtime),
            patch.object(llm_client, "_legacy_env_present", return_value=[]),
        ):
            issues = llm_client.routing_issues()
            fallback = llm_client.get_fallback_model("heavy", visitor=False)
        self.assertEqual(len(issues), 1)
        self.assertIn("等于没有回退", issues[0])
        self.assertIn(runtime["model_heavy"], issues[0])
        # 只报告，不改值
        self.assertEqual(fallback, runtime["model_heavy"])

    def test_routing_issues_flags_unregistered_model_without_blocking(self):
        runtime = dict(RUNTIME_MODELS)
        runtime["model_heavy"] = "gpt-5.5"      # 已彻底下线的旧名字
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
        self.assertIn("gpt-5.5", issues[0])
        self.assertIn("404", issues[0])
        slot = snapshot[("heavy", "admin")]
        self.assertTrue(slot["primary_ready"])
        self.assertTrue(slot["primary_retired"])
        self.assertEqual(chain[0], "gpt-5.5")

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

        # ik_gpt 组承载的全部已登记模型（跨档位：重档两个 + 中型 terra）
        expected_models = {"gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra"}
        self.assertEqual({model for _, model in calls}, expected_models)
        self.assertEqual({idx for idx, _ in calls}, {0, 1})
        # 别组的模型一个都不该被拿去测（拿 GPT key 测 grok 必然 403）
        probed = {model for _, model in calls}
        self.assertNotIn("grok-4.6", probed)
        self.assertNotIn("glm-5.3", probed)
        self.assertNotIn("gpt-5.6-luna", probed)   # 走官方组，不属 ik_gpt
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
            # 未登记的私有模型原样保留（不因档位校验被抹掉）
            self.assertEqual(state["model_heavy"], "custom-private-model")
            # 同批里已登记但跨档的值（gpt-5.6-sol 现属重档）被拉回本档默认
            self.assertEqual(state["model_balanced"], "gpt-5.6-terra")
        finally:
            conn.close()

    def test_retired_model_names_upgrade_by_single_value_map(self):
        """已彻底下线的旧名字按单值映射改写到当前档位的合法模型。"""
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(db.SCHEMA)
            conn.executemany(
                "INSERT INTO llm_runtime_state (key, value) VALUES (?,?)",
                list(RETIRED_VALUES.items()))
            db.seed_config(conn)
            self.assertEqual(
                db.get_llm_runtime_state(conn), full_state(RETIRED_UPGRADED))
        finally:
            conn.close()

    def test_cross_tier_value_is_pulled_back_to_tier_default(self):
        """模型换档后的自愈：库里存着「已登记但不属于该档」的值时重置为本档默认。
        面板产生不了这种组合，只可能来自配置演进，故重置不会覆盖有效的人工选择。"""
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(db.SCHEMA)
            db.seed_config(conn)
            # 绕过校验直接塞一个跨档值：luna 是轻档模型，塞进重档主槽
            conn.execute("UPDATE llm_runtime_state SET value='gpt-5.6-luna' "
                         "WHERE key='model_heavy'")
            conn.commit()
            db.seed_config(conn)
            self.assertEqual(
                db.get_llm_runtime_state(conn)["model_heavy"], "gpt-6-astra")
        finally:
            conn.close()

    def test_previous_primary_and_fallback_profiles_resolve_luna_ambiguity(self):
        """两条完整旧方案各自被精确识别（解决 deepseek-v4-flash 同时可能表示
        主轻档或回退轻档的歧义），分别迁到主方案 / 回退方案的新值。"""
        for old_profile, expected in (
                (OLD_PRIMARY, PRIMARY),
                (V1_FALLBACK_PROFILE, V1_FALLBACK_UPGRADED)):
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
        """「一键切到回退模型」把 6 个回退值写进主槽；恢复默认把 12 槽位复原。
        轻档回退留空（池内无第二模型），不做主/回退切换。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "odds.db")
            db.init_db(path)
            with patch.object(config, "DB_PATH", path):
                llm_client.apply_fallback_models()
                self.assertEqual(llm_client.get_tier_model("heavy"), "grok-4.6")
                self.assertEqual(
                    llm_client.get_tier_model("heavy", visitor=True), "glm-5.3")
                self.assertEqual(
                    llm_client.get_tier_model("balanced"), "grok-4.5")
                self.assertEqual(
                    llm_client.get_tier_model("balanced", visitor=True),
                    "glm-5.3-flash")
                # 轻档回退为空，apply_fallback 仍写入但写的是空串，
                # get_tier_model 空串等价于未设置、返回档位默认 luna
                self.assertEqual(
                    llm_client.get_tier_model("light"), "gpt-5.6-luna")
                # 回退槽本身不动（幂等，可反复点）
                self.assertEqual(
                    llm_client.get_fallback_model("heavy"), "grok-4.6")
                self.assertEqual(
                    llm_client.get_fallback_model("light"), "")

                llm_client.reset_runtime_models()
                self.assertEqual(
                    llm_client.get_tier_model("heavy"), "gpt-6-astra")
                self.assertEqual(
                    llm_client.get_tier_model("heavy", visitor=True),
                    "deepseek-v4-pro")
                self.assertEqual(
                    llm_client.get_tier_model("balanced"), "gpt-5.6-terra")
                self.assertEqual(
                    llm_client.get_tier_model("light"), "gpt-5.6-luna")
                self.assertEqual(
                    llm_client.get_fallback_model("heavy"), "grok-4.6")


if __name__ == "__main__":
    unittest.main(verbosity=2)
