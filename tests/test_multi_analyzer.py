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

    def test_model_pool_replacement_and_full_task_coverage(self):
        self.assertNotIn("deepseek-v4-flash", config.LLM_MODELS)
        self.assertIn("deepseek-v4.1-flash", config.LLM_MODELS)
        self.assertEqual(
            set(config.llm_tier_eligible_models("balanced")),
            {"gpt-5.6-terra", "deepseek-v4.1-flash",
             "glm-5.3-flash", "grok-4.5"},
        )
        models = {task["model"] for task in config.FULL_CONSULT_TASKS}
        models.add(config.FULL_CONSULT_SYNTHESIS["model"])
        models.add(config.FULL_CONSULT_AUDIT["model"])
        self.assertEqual(len(models), 11)
        self.assertIn("deepseek-v4.1-flash", models)

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

    def test_failed_expert_is_recorded_and_does_not_block_synthesis(self):
        def fake_chat(system, user, model, **kwargs):
            if model == "grok-4.5":
                return "LLM 请求失败（测试失败）"
            if "最终报告审计员" in system:
                return '{"ok":true,"issues":[]}'
            module_id = next(
                task["id"] for task in config.FULL_CONSULT_TASKS
                if task["model"] == model
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
        failed = [item for item in summary["results"]
                  if item["model"] == "grok-4.5"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["status"], "error")


if __name__ == "__main__":
    unittest.main()
