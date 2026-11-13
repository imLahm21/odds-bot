"""重档并发上限、缓存预热错峰与复盘串行队列的测试（不发网络请求）。"""

import os
import sys
import threading
import time
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("APIFOOTBALL_KEY", "offline-test-key")

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

from offline_support import prepare_publish_stubs  # noqa: E402

prepare_publish_stubs()

from bot import analyzer, config, llm_client, multi_analyzer, tgbot  # noqa: E402

EP = {"key": "k", "base_url": "https://cpa.example/v1", "label": "CPA-GPT",
      "route_group": "cpa_gpt"}


class GroupLimitTests(unittest.TestCase):
    def setUp(self):
        patches = [
            patch.object(llm_client, "_group_slots", {}),
            patch.dict(config.LLM_GROUP_MAX_CONCURRENT, {"cpa_gpt": 1}),
            patch.object(llm_client, "_QUEUE_POLL_SECONDS", 0.01),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def test_limit_falls_back_to_keys_times_per_key(self):
        endpoints = [dict(EP, route_group="ik_grok", label=f"g{i}") for i in range(3)]
        with patch.object(llm_client, "_ENDPOINTS", endpoints):
            self.assertEqual(llm_client.group_concurrency_limit("ik_grok"),
                             3 * config.LLM_GROUP_CONCURRENT_PER_KEY)
        self.assertEqual(llm_client.group_concurrency_limit("cpa_gpt"), 1)

    def test_second_stream_queues_until_first_releases(self):
        gate = threading.Event()

        def fake_stream(ep, payload, first_byte_to, idle_to, max_retries=0):
            gate.wait(2)
            yield ("done", "ok")

        with patch.object(llm_client, "_stream_one", fake_stream):
            first = llm_client._limited_stream(True, EP, {}, 1, 1)
            holder = threading.Thread(target=lambda: list(first))
            holder.start()
            time.sleep(0.05)
            second = llm_client._limited_stream(True, EP, {}, 1, 1)
            self.assertEqual(next(second)[0], "queued")
            gate.set()
            holder.join(2)
            events = [event for event in second if event[0] != "queued"]
        self.assertEqual(events, [("done", "ok")])

    def test_abandoned_stream_returns_its_slot(self):
        def fake_stream(ep, payload, first_byte_to, idle_to, max_retries=0):
            yield ("delta", "a")
            yield ("done", "a")

        with patch.object(llm_client, "_stream_one", fake_stream):
            stream = llm_client._limited_stream(True, EP, {}, 1, 1)
            self.assertEqual(next(stream), ("delta", "a"))
            stream.close()                      # 用户点了停止，消费方提前退出
            self.assertEqual(list(llm_client._limited_stream(True, EP, {}, 1, 1))[-1],
                             ("done", "a"))

    def test_unlimited_calls_never_queue(self):
        slot = llm_client._group_slot("cpa_gpt")
        slot.acquire()                          # 重档名额已被占满
        try:
            with patch.object(llm_client, "_stream_one",
                              lambda *a, **k: iter([("done", "live")])), \
                    patch.object(llm_client, "_do_chat",
                                 return_value=("live", True, "", False, False, False)):
                self.assertEqual(list(llm_client._limited_stream(False, EP, {}, 1, 1)),
                                 [("done", "live")])
                self.assertEqual(llm_client._limited_chat(False, EP, {}, 1, 0)[0], "live")
        finally:
            slot.release()

    def test_light_models_are_exempt(self):
        self.assertTrue(config.llm_model_is_light("gpt-6-luna"))
        self.assertFalse(config.llm_model_is_light("gpt-6-astra-cpa"))


class StaggerTests(unittest.TestCase):
    def setUp(self):
        patches = [
            patch.object(tgbot, "_warmup_leader_at", {}),
            patch.object(tgbot, "_analysis_tasks", {}),
            patch.object(config, "LLM_CACHE_WARMUP_SECONDS", 25),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def test_cpa_batch_waits_once_for_cache_warmup(self):
        with patch.object(tgbot, "_heavy_route_group", return_value="cpa_gpt"):
            self.assertEqual(tgbot._stagger_delay(1, "精算", 0), 0)
            for running in (1, 2, 5):
                delay = tgbot._stagger_delay(1, "精算", running)
                self.assertTrue(24 < delay <= 25, delay)

    def test_cpa_followers_after_warmup_start_at_once(self):
        with patch.object(tgbot, "_heavy_route_group", return_value="cpa_gpt"):
            tgbot._warmup_leader_at["cpa_gpt"] = time.monotonic() - 60
            self.assertEqual(tgbot._stagger_delay(1, "精算", 1), 0)

    def test_warmup_can_be_disabled(self):
        with patch.object(tgbot, "_heavy_route_group", return_value="cpa_gpt"), \
                patch.object(config, "LLM_CACHE_WARMUP_SECONDS", 0):
            self.assertEqual(tgbot._stagger_delay(1, "精算", 0), 0)
            self.assertEqual(tgbot._stagger_delay(1, "精算", 3), 0)

    def test_ik_groups_keep_incremental_stagger(self):
        with patch.object(tgbot, "_heavy_route_group", return_value="ik_grok"):
            self.assertEqual(tgbot._stagger_delay(1, "精算", 0), 0)
            self.assertEqual(tgbot._stagger_delay(1, "精算", 2), 60)
            self.assertEqual(tgbot._stagger_delay(1, "精算", 9),
                             tgbot._ANALYSIS_STAGGER_MAX)

    def test_non_analysis_kinds_are_not_staggered(self):
        with patch.object(tgbot, "_heavy_route_group", return_value="ik_grok"):
            self.assertEqual(tgbot._stagger_delay(1, "复盘", 3), 0)
            self.assertEqual(tgbot._stagger_delay(1, "基本面预分析", 3), 0)


class ReviewQueueTests(unittest.TestCase):
    def setUp(self):
        patches = [
            patch.object(tgbot, "_analysis_tasks", {}),
            patch.object(tgbot, "_warmup_leader_at", {}),
            patch.object(tgbot, "_REVIEW_YIELD_POLL_SECONDS", 0.01),
            patch.object(tgbot, "send", return_value=1),
            patch.object(tgbot, "_heavy_route_group", return_value="cpa_gpt"),
            patch.object(config, "LLM_CACHE_WARMUP_SECONDS", 0),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def _wait_idle(self):
        deadline = time.monotonic() + 3
        while tgbot._analysis_tasks and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(tgbot._analysis_tasks)

    def test_reviews_run_one_at_a_time(self):
        active, peak, lock = [0], [0], threading.Lock()

        def review(task_id, cancel):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.05)
            with lock:
                active[0] -= 1

        for _ in range(3):
            self.assertTrue(tgbot._submit_analysis(1, "复盘", review))
        self._wait_idle()
        self.assertEqual(peak[0], 1)

    def test_reviews_do_not_use_analysis_quota(self):
        release = threading.Event()
        for _ in range(tgbot._ANALYSIS_MAX_PER_CHAT + 2):
            self.assertTrue(tgbot._submit_analysis(
                1, "复盘", lambda task_id, cancel: release.wait(2)))
        self.assertTrue(tgbot._submit_analysis(
            1, "精算", lambda task_id, cancel: None))
        release.set()
        self._wait_idle()

    def test_review_waits_while_an_analysis_is_running(self):
        order, release = [], threading.Event()

        def analysis(task_id, cancel):
            release.wait(2)
            order.append("精算")

        self.assertTrue(tgbot._submit_analysis(1, "精算", analysis))
        self.assertTrue(tgbot._submit_analysis(
            1, "复盘", lambda task_id, cancel: order.append("复盘")))
        time.sleep(0.1)
        self.assertEqual(order, [])
        release.set()
        self._wait_idle()
        self.assertEqual(order, ["精算", "复盘"])

    def test_queued_review_can_be_cancelled(self):
        ran, release = [], threading.Event()
        self.assertTrue(tgbot._submit_analysis(
            1, "复盘", lambda task_id, cancel: release.wait(2)))
        queued = tgbot._submit_analysis(
            1, "复盘", lambda task_id, cancel: ran.append(1))
        self.assertEqual(tgbot._analysis_cancel(queued), "复盘")
        release.set()
        self._wait_idle()
        self.assertEqual(ran, [])


class ReviewPromptTests(unittest.TestCase):
    def test_review_prompt_carries_code_triggers_and_layer_rules(self):
        with patch.object(analyzer, "load_rules", return_value="规则"), \
                patch.object(analyzer, "market_hypotheses_block",
                             return_value="【已触发假设】H-M07\n\n") as block:
            system, user = analyzer._review_prompts(
                "csv", "盲推", "2:1", "A", "B", "英超")
        block.assert_called_once_with("csv", "英超")
        self.assertIn("【已触发假设】H-M07", user)
        self.assertLess(user.index("【已触发假设】"), user.index("第一遍盲推预判"))
        for phrase in ("证据四层口径", "无倾向（不计对错）", "已触发假设逐条检验",
                       "假设样本记录"):
            self.assertIn(phrase, system)

    def test_full_model_prompts_no_longer_use_old_layer_names(self):
        bundle = {"csv_text": "csv", "league": "英超", "forecast": "盲推",
                  "fundamentals": "", "home": "A", "away": "B"}
        task = {"id": "data_audit", "label": "数据审计"}
        with patch.object(analyzer, "market_hypotheses_block",
                          return_value="【已触发假设】H-M07\n\n"):
            module_input = multi_analyzer._review_module_input(task, bundle)
        self.assertIn("【已触发假设】H-M07", module_input)
        system = multi_analyzer._review_module_system(task, bundle)
        for old in ("A层", "B层", "C层", "D层"):
            self.assertNotIn(old, system)
        self.assertIn("证据四层", system)


if __name__ == "__main__":
    unittest.main()
