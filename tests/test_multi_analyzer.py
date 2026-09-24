"""全模型交叉会诊的离线单元测试。"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("APIFOOTBALL_KEY", "offline-test-key")

from bot import config, llm_client, multi_analyzer  # noqa: E402


def _card(module_id: str) -> str:
    return (
        '{"module":"%s","summary":"ok","findings":[],'
        '"direction":"neutral","missing_data":[],"risks":[],'
        '"disagreements":[]}'
    ) % module_id


class TestMultiAnalyzer(unittest.TestCase):
    def test_explicit_chat_uses_requested_model_and_effort(self):
        class Breaker:
            def allow(self):
                return True

            def record(self, ok):
                self.ok = ok

        endpoint = {"label": "test", "route_group": "ik_grok",
                    "base_url": "https://example.invalid", "key": "secret"}
        seen = {}

        def fake_do_chat(ep, payload, *args):
            seen.update(payload)
            return "ok", True, "", False, False, False

        with (
            patch.object(llm_client, "available", return_value=True),
            patch.object(llm_client, "get_settings", return_value={
                "non_stream_timeout": 5, "max_retries": 0,
            }),
            patch.object(llm_client, "_endpoints_by_priority",
                         return_value=[(0, endpoint)]),
            patch.object(llm_client, "_breakers", [Breaker()]),
            patch.object(llm_client, "_do_chat", side_effect=fake_do_chat),
        ):
            result = llm_client.chat_model(
                "system", "user", "grok-4.6", effort="high",
                max_tokens=123,
            )

        self.assertEqual(result, "ok")
        self.assertEqual(seen["model"], "grok-4.6")
        self.assertEqual(seen["reasoning_effort"], "high")
        self.assertEqual(seen["max_tokens"], 123)

    def test_zero_budget_uses_per_model_environment_budget(self):
        class Breaker:
            def allow(self):
                return True

            def record(self, ok):
                self.ok = ok

        endpoint = {"label": "test", "route_group": "ik_grok",
                    "base_url": "https://example.invalid", "key": "secret"}
        seen = {}

        def fake_do_chat(ep, payload, *args):
            seen.update(payload)
            return "ok", True, "", False, False, False

        with (
            patch.object(llm_client, "available", return_value=True),
            patch.object(llm_client, "get_settings", return_value={
                "non_stream_timeout": 5, "max_retries": 0,
            }),
            patch.object(llm_client, "_endpoints_by_priority",
                         return_value=[(0, endpoint)]),
            patch.object(llm_client, "_breakers", [Breaker()]),
            patch.object(llm_client, "_do_chat", side_effect=fake_do_chat),
            patch.object(config, "LLM_MAX_TOKENS", 64000),
            patch.object(config, "LLM_MODEL_MAX_TOKENS",
                         {"grok-4.6": 128000}),
        ):
            result = llm_client.chat_model(
                "system", "user", "grok-4.6", effort="high",
                max_tokens=0,
            )

        self.assertEqual(result, "ok")
        self.assertEqual(seen["max_tokens"], 128000)

    def test_model_pool_replacement_and_full_task_coverage(self):
        self.assertNotIn("deepseek-v4-flash", config.LLM_MODELS)
        self.assertIn("deepseek-v4.1-flash", config.LLM_MODELS)
        self.assertEqual(
            set(config.llm_tier_eligible_models("balanced")),
            {"gpt-6-sol", "grok-4.6", "gpt-5.6-terra", "deepseek-v4.1-flash",
             "glm-5.3-flash", "grok-4.5"},
        )
        models = {task["model"] for task in config.FULL_CONSULT_TASKS}
        models.add(config.FULL_CONSULT_SYNTHESIS["model"])
        models.add(config.FULL_CONSULT_AUDIT["model"])
        self.assertEqual(len(models), 11)
        self.assertIn("deepseek-v4.1-flash", models)

    def test_full_consult_routes_keep_grade_and_supported_effort(self):
        self.assertEqual(multi_analyzer.validate_full_consult_routes(), [])
        for task in config.FULL_CONSULT_TASKS:
            candidates = multi_analyzer._route_candidates(task)
            grades = {
                config.FULL_CONSULT_MODEL_GRADES[item["model"]]
                for item in candidates
            }
            self.assertEqual(len(grades), 1, task["id"])
            for item in candidates:
                self.assertTrue(
                    config.llm_model_supports_effort(
                        item["model"], item["effort"]),
                    (task["id"], item),
                )
            self.assertEqual(task["max_tokens"], 0)
        self.assertEqual(config.FULL_CONSULT_AUDIT["max_tokens"], 0)

    def test_module_fallback_uses_its_own_model_and_effort(self):
        task = next(t for t in config.FULL_CONSULT_TASKS
                    if t["id"] == "market_primary")
        calls = []

        def fake_chat(system, user, model, **kwargs):
            calls.append((model, kwargs))
            if model == task["model"]:
                return "LLM 请求失败（测试故障）"
            return _card(task["id"])

        with patch.object(multi_analyzer.llm_client, "chat_model",
                          side_effect=fake_chat):
            result = multi_analyzer._run_one(
                task, {"home": "h", "away": "a", "league": "l",
                       "csv_text": "csv", "fundamentals": "fund"})

        self.assertEqual([item[0] for item in calls], [
            "grok-4.6", "gpt-6-astra",
        ])
        self.assertEqual(calls[0][1]["effort"], "xhigh")
        self.assertEqual(calls[1][1]["effort"], "xhigh")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["model"], "gpt-6-astra")
        self.assertTrue(result["fallback_used"])
        self.assertEqual(result["attempts"][0]["status"], "error")

    def test_module_invalid_json_also_uses_fallback(self):
        task = next(t for t in config.FULL_CONSULT_TASKS
                    if t["id"] == "numeric_summary")
        calls = []

        def fake_chat(system, user, model, **kwargs):
            calls.append(model)
            return "not-json" if len(calls) == 1 else _card(task["id"])

        with patch.object(multi_analyzer.llm_client, "chat_model",
                          side_effect=fake_chat):
            result = multi_analyzer._run_one(
                task, {"home": "h", "away": "a", "league": "l",
                       "csv_text": "csv", "fundamentals": "fund"})

        self.assertEqual(calls, ["deepseek-v4.1-flash", "glm-5.3-flash"])
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["fallback_used"])
        self.assertIn("JSON", result["attempts"][0]["error"])

    def test_synthesis_passes_model_specific_fallback_efforts(self):
        seen = {}

        def fake_stream(system, user, model, **kwargs):
            seen.update({"model": model, **kwargs})
            yield ("done", "report")

        with patch.object(multi_analyzer.llm_client,
                          "stream_chat_model", side_effect=fake_stream):
            result = multi_analyzer._synthesize(
                {"csv_text": "csv", "fundamentals": "fund",
                 "home": "h", "away": "a", "league": "l"}, [])

        fallbacks = config.FULL_CONSULT_SYNTHESIS["fallbacks"]
        self.assertEqual(result, "report")
        self.assertEqual(
            seen["fallback_models"],
            tuple(item["model"] for item in fallbacks),
        )
        self.assertEqual(
            seen["fallback_efforts"],
            tuple(item["effort"] for item in fallbacks),
        )

    def test_audit_transfers_to_same_grade_model_and_uses_zero_budget(self):
        report = (
            "\n".join(f"### {n}. section" for n in range(1, 9))
            + "\n### 8. 投注决策\n" + ("x" * 600)
        )
        calls = []

        def fake_chat(system, user, model, **kwargs):
            calls.append((model, kwargs))
            if model == config.FULL_CONSULT_AUDIT["model"]:
                return "LLM 请求失败（审计主模型故障）"
            return '{"ok":true,"issues":[]}'

        with patch.object(multi_analyzer.llm_client, "chat_model",
                          side_effect=fake_chat):
            audit = multi_analyzer._audit_report(
                {"home": "h", "away": "a", "league": "l",
                 "extra_instruction": ""}, report, [])

        self.assertTrue(audit["ok"])
        self.assertEqual(calls[0][0], "gpt-5.6-sol")
        self.assertEqual(calls[1][0], "deepseek-v4-pro")
        self.assertEqual(calls[1][1]["max_tokens"], 0)
        self.assertTrue(audit["route"]["fallback_used"])

    def test_parse_json_accepts_fenced_object(self):
        fenced = "\x60\x60\x60json\n" + _card("x") + "\n\x60\x60\x60"
        value, error = multi_analyzer._parse_json(fenced)
        self.assertEqual(error, "")
        self.assertEqual(value["module"], "x")

    def test_run_passes_custom_focus_to_all_models_and_audits(self):
        called = []
        focus = "重点看临场异动和大小球"

        def fake_chat(system, user, model, **kwargs):
            called.append((model, system, user))
            if "最终报告审计员" in system:
                return '{"ok":true,"issues":[]}'
            module_id = next(
                task["id"] for task in config.FULL_CONSULT_TASKS
                if task["model"] == model
            )
            return _card(module_id)

        report = (
            "### 1. 数据提取\nx\n### 2. 盘口定性\nx\n"
            "### 3. 资金流向与热度\nx\n### 4. 操盘手法匹配\nx\n"
            "### 5. 风控验证\nx\n### 6. 缺失节点预测\nx\n"
            "### 7. 最终精算结论\nx\n### 8. 投注决策\nx\n"
            + ("x" * 600)
        )

        def fake_stream(system, user, model, **kwargs):
            called.append((model, system, user))
            yield ("done", report)

        with (
            patch.object(multi_analyzer.llm_client, "chat_model",
                         side_effect=fake_chat),
            patch.object(multi_analyzer.llm_client, "stream_chat_model",
                         side_effect=fake_stream),
        ):
            result, summary = multi_analyzer.run(
                "盘口CSV", "基本面", "主队", "客队", "联赛",
                goals_block="进球状态", extra_instruction=focus,
            )

        self.assertIn("### 8. 投注决策", result)
        self.assertTrue(summary["audit"]["ok"])
        self.assertEqual(
            {model for model, _, _ in called},
            {task["model"] for task in config.FULL_CONSULT_TASKS}
            | {config.FULL_CONSULT_SYNTHESIS["model"],
               config.FULL_CONSULT_AUDIT["model"]},
        )
        self.assertTrue(all(focus in system or focus in user
                            for _, system, user in called))

    def test_failed_expert_transfers_and_does_not_block_synthesis(self):
        def fake_chat(system, user, model, **kwargs):
            if model == "grok-4.5":
                return "LLM 请求失败（测试失败）"
            if "最终报告审计员" in system:
                return '{"ok":true,"issues":[]}'
            module_id = next(
                task["id"] for task in config.FULL_CONSULT_TASKS
                if f"模块ID={task['id']}" in system
            )
            return _card(module_id)

        report = "### 1. x\n### 2. x\n### 3. x\n### 4. x\n### 5. x\n### 6. x\n### 7. x\n### 8. 投注决策\n" + ("x" * 600)

        with (
            patch.object(multi_analyzer.llm_client, "chat_model",
                         side_effect=fake_chat),
            patch.object(
                multi_analyzer.llm_client, "stream_chat_model",
                return_value=iter([("done", report)]),
            ),
        ):
            result, summary = multi_analyzer.run(
                "csv", "fund", "h", "a", "league")

        self.assertIn("### 模型会诊记录", result)
        transferred = [item for item in summary["results"]
                       if item["id"] == "market_challenge"]
        self.assertEqual(len(transferred), 1)
        self.assertEqual(transferred[0]["status"], "ok")
        self.assertEqual(transferred[0]["model"], "gpt-5.6-terra")
        self.assertTrue(transferred[0]["fallback_used"])

    def test_full_review_uses_all_models_and_preserves_custom_focus(self):
        called = []
        focus = "重点解释临场降盘为何没有兑现"

        def fake_chat(system, user, model, **kwargs):
            called.append((model, system, user))
            if "全模型赛后复盘审计员" in system:
                return '{"ok":true,"issues":[]}'
            module_id = next(
                task["id"] for task in config.FULL_CONSULT_TASKS
                if task["model"] == model
            )
            return _card(module_id)

        review_report = (
            "### 1. 实际结果\nx\n### 2. 盘口结算回放\nx\n"
            "### 3. 盲推预判 vs 实际对照\nx\n"
            "### 4. 信号有效性复盘\nx\n### 5. 经验教训\nx\n"
            "### 6. 盘口指示强度评分\nx\n" + ("x" * 500)
        )

        def fake_stream(system, user, model, **kwargs):
            called.append((model, system, user))
            self.assertIn("第一阶段盲推", user)
            self.assertIn("实际结果", user)
            yield ("done", review_report)

        with (
            patch.object(multi_analyzer.llm_client, "chat_model",
                         side_effect=fake_chat),
            patch.object(multi_analyzer.llm_client, "stream_chat_model",
                         side_effect=fake_stream),
        ):
            result, summary = multi_analyzer.run_review(
                "csv", "第一阶段盲推", "实际结果", "基本面",
                "主队", "客队", "联赛", goals_block="goals",
                extra_instruction=focus,
            )

        self.assertIn("### 6. 盘口指示强度评分", result)
        self.assertTrue(summary["audit"]["ok"])
        self.assertEqual(
            {model for model, _, _ in called},
            {task["model"] for task in config.FULL_CONSULT_TASKS}
            | {config.FULL_CONSULT_SYNTHESIS["model"],
               config.FULL_CONSULT_AUDIT["model"]},
        )
        self.assertTrue(all(focus in system or focus in user
                            for _, system, user in called))


if __name__ == "__main__":
    unittest.main()
