"""全模型精算与复盘补完（流式调用、四层权限、门槛、审计严重度、修复验收、
探索注额度、假设结算、比分缓存、/hypstats）的离线测试（不发网络请求）。"""

import json
import os
import sys
import tempfile
import types
import unittest
from contextlib import ExitStack
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
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

from bot import (analyzer, candidates, config, full_model_settings as settings,  # noqa: E402
                 hypothesis_settle, llm_client, multi_analyzer as multi, tgbot,
                 typesafe_decisions)
import backtest_hypotheses as bt  # noqa: E402
from test_market_facts import make_csv  # noqa: E402


SECTIONS_1_6 = "".join(f"### {n}. 段\n内容\n" for n in range(1, 7))
SECTION_7 = ("### 7. 最终精算结论\n- **亚盘判定**：无倾向\n- **信号强度**：无\n"
             "- **数据质量**：85 / 100\n")
TABLE_HEAD = ("| ID | 玩法 | 赔率 | 门槛=1/赔率 | p_市场 | p_最终 | edge | 资格 |\n"
              "|----|------|------|-------------|--------|--------|------|------|\n")
VALID_REPORT = (SECTIONS_1_6 + SECTION_7 + "### 8. 投注决策\n" + TABLE_HEAD
                + "| C01 | 主胜 | 2.00 | 50% | 54% | 54% | +8.0% | eligible |\n"
                "- **选中候选 ID**：C01\n" + "证据。" * 200)

# H-M07 场景（test_market_facts 同款）：浅于换算、全程不变盘、上盘大热 → 预测下盘
M07_CSV = make_csv([("初盘①", -0.25, 1.90, 2.00), ("即时", -0.25, 1.80, 2.10)],
                   [("初盘①", 1.60, 4.0, 5.5), ("即时", 1.45, 4.4, 6.5)])


def _expert_ok(task, bundle):
    return {"id": task["id"], "label": task["label"], "model": task["model"],
            "effort": task["effort"], "status": "ok", "card": {}}


# ─── WP-1：collect_model ─────────────────────────────────────────────────────
class TestCollectModel(unittest.TestCase):
    def _collect(self, events, **kwargs):
        state = {"closed": False}

        def fake_stream(*args, **kw):
            state["kwargs"] = kw
            try:
                yield from events
            finally:
                state["closed"] = True

        with patch.object(llm_client, "stream_chat_model", side_effect=fake_stream):
            result = llm_client.collect_model("system", "user", "gpt-6-astra", **kwargs)
        return result, state

    def test_delta_then_done_returns_text(self):
        result, state = self._collect([("delta", "部"), ("delta", "部分"), ("done", " 完整正文 ")])
        self.assertEqual(result, "完整正文")
        self.assertNotIn("fallback_models", state["kwargs"])   # 不做模型间回退

    def test_error_after_delta_is_failure_with_known_prefix(self):
        result, _ = self._collect([("delta", "部分正文"), ("error", "HTTP 524 中断")])
        self.assertTrue(result.startswith(multi._ERR_PREFIXES), result)
        self.assertIn("HTTP 524", result)
        result, _ = self._collect([("error", "LLM 超时（连接/读取 >300s）。")])
        self.assertEqual(result, "LLM 超时（连接/读取 >300s）。")

    def test_queued_and_warning_are_ignored(self):
        result, _ = self._collect([("queued", 2), ("warning", "输入较长"), ("queued", 4),
                                   ("done", "ok")])
        self.assertEqual(result, "ok")

    def test_cancel_closes_the_stream(self):
        cancel = Event()
        cancel.set()
        result, state = self._collect([("queued", 2), ("done", "never")], cancel=cancel)
        self.assertTrue(result.startswith("LLM 请求失败"))
        self.assertIn("已取消", result)
        self.assertTrue(state["closed"])


# ─── WP-1：_call_route / _stream_report ──────────────────────────────────────
class TestRouteChain(unittest.TestCase):
    def setUp(self):
        self.spec = settings.defaults("analyze")["market_primary"]
        self.chain = [item["model"] for item in settings.candidates(self.spec)]

    def test_second_model_success_marks_fallback(self):
        with patch.object(llm_client, "collect_model",
                          side_effect=["LLM 请求失败（offline）", '{"module":"x"}']) as call:
            value, route = multi._call_route("s", "u", self.spec, parse_json=True, phase="t")
        self.assertEqual(value, {"module": "x"})
        self.assertTrue(route["fallback_used"])
        self.assertEqual([c.kwargs["model"] for c in call.call_args_list], self.chain[:2])
        self.assertNotIn("timeout", call.call_args.kwargs)

    def test_role_deadline_skips_remaining_candidates(self):
        clock = iter([0.0, config.FULL_CONSULT_ROLE_DEADLINE + 1.0, 99999.0])
        fake_time = types.SimpleNamespace(monotonic=lambda: next(clock))
        with patch.object(multi, "time", fake_time), \
             patch.object(llm_client, "collect_model", return_value="LLM 请求失败（offline）") as call:
            value, route = multi._call_route("s", "u", self.spec, parse_json=True, phase="t",
                                             rescue=settings.rescue_defaults("analyze"))
        self.assertIsNone(value)
        call.assert_called_once()
        skipped = [a for a in route["attempts"] if a["status"] == "skipped"]
        self.assertTrue(skipped)
        self.assertTrue(all(a["error"] == "超过职责总时限" for a in skipped))
        self.assertTrue(route["deadline_skipped"])
        self.assertFalse(route["rescue_skipped"])                    # 不是防自审跳过
        self.assertIn("总时限", multi.execution_note({**route, "status": "error"}))

    def test_stream_report_regenerates_after_partial_output(self):
        spec = config.FULL_CONSULT_SYNTHESIS
        chain = settings.candidates(spec)

        def stream(system, user, model, **kwargs):
            if model == chain[0]["model"]:
                yield "delta", "前半份报告"
                yield "error", "stream interrupted"
            else:
                kwargs["on_model_used"](model, kwargs["effort"])
                yield "done", "第二个模型的完整报告"

        trace = {}
        with patch.object(llm_client, "stream_chat_model", side_effect=stream) as request:
            result = multi._stream_report("s", "u", {}, spec, rescue=None, trace=trace,
                                          cancel=None, progress=None, stage="synthesis")
        self.assertEqual(result, "第二个模型的完整报告")
        self.assertTrue(trace["partial_output"])
        self.assertEqual(trace["model"], chain[1]["model"])
        self.assertEqual(request.call_count, 2)


# ─── WP-3：模块提示词、输入与后处理 ──────────────────────────────────────────
class TestModulePermissions(unittest.TestCase):
    def _run(self, card, module_id="market_primary", bundle=None):
        task = {**settings.defaults("analyze")[module_id], "id": module_id, "label": "x"}
        with patch.object(multi, "_call_route", return_value=(dict(card), {})):
            return multi._run_one(task, bundle or {"csv_text": "", "fundamentals": ""})["card"]

    def test_direction_without_basis_is_downgraded(self):
        card = self._run({"direction": "home"})
        self.assertEqual(card["direction"], "neutral")
        self.assertEqual(card["direction_downgraded"], "home")
        self.assertEqual(card["direction_basis"], "none")

    def test_verified_basis_needs_a_verified_hypothesis(self):
        card = {"direction": "under", "direction_basis": "verified_hypothesis"}
        with patch.object(typesafe_decisions, "verified_hypothesis_count", return_value=0):
            self.assertEqual(self._run(card)["direction_downgraded"], "under")
        with patch.object(typesafe_decisions, "verified_hypothesis_count", return_value=1):
            kept = self._run(card)
        self.assertEqual(kept["direction"], "under")
        self.assertNotIn("direction_downgraded", kept)

    def test_fundamental_override_only_for_fundamental_modules(self):
        card = {"direction": "away", "direction_basis": "fundamental_override"}
        kept = self._run(card, "fundamentals")
        self.assertEqual(kept["direction"], "away")
        self.assertNotIn("direction_downgraded", kept)
        self.assertEqual(self._run(card, "market_primary")["direction"], "neutral")

    def test_neutral_is_untouched(self):
        self.assertNotIn("direction_downgraded", self._run({"direction": "neutral"}))

    def test_quotes_are_checked_against_their_source(self):
        bundle = {"csv_text": "快照时间,博彩公司\n2026-10-17 12:00（即时）,Pinnacle\n",
                  "fundamentals": "主队近 10 场 6胜 2平 2负"}
        card = self._run({"findings": [
            {"evidence": {"source": "csv", "quote": "2026-10-17  12:00（即时）, Pinnacle"}},
            {"evidence": [{"source": "fundamentals", "quote": "主队近10场 6胜2平2负"},
                          {"source": "rules", "quote": "规则原文不核对"}]},
        ]}, bundle=bundle)
        self.assertNotIn("unverified_quotes", card)
        card = self._run({"findings": [
            {"evidence": {"source": "csv", "quote": "Bet365 临场升盘"}}]}, bundle=bundle)
        self.assertEqual(card["unverified_quotes"], 1)
        self.assertIs(card["findings"][0]["evidence"]["verified"], False)

    def test_instruction_and_inputs_follow_the_four_layers(self):
        text = multi._json_instruction("market_primary", "市场", "")
        self.assertIn("【证据四层权限】", text)
        self.assertIn("direction_basis", text)
        self.assertIn("【证据四层权限】", multi._json_instruction("m", "复盘", "", review=True))
        bundle = {"csv_text": "CSV", "fundamentals": "FUND", "goals_block": "GOALS",
                  "market_block": "MARKET_BLOCK", "candidates_block": "CANDS_BLOCK"}
        inputs = {tid: multi._module_input({"id": tid}, bundle) for tid in (
            "data_audit", "market_primary", "numeric_summary", "cross_market", "lesson_match")}
        self.assertNotIn("MARKET_BLOCK", inputs["data_audit"])
        self.assertIn("MARKET_BLOCK", inputs["market_primary"])
        self.assertNotIn("CANDS_BLOCK", inputs["market_primary"])
        self.assertIn("CANDS_BLOCK", inputs["numeric_summary"])
        self.assertIn("CANDS_BLOCK", inputs["cross_market"])
        self.assertIn("MARKET_BLOCK", inputs["lesson_match"])
        self.assertNotIn("数据审计", inputs["lesson_match"])
        self.assertIn(config.HYPOTHESIS_REGISTRY, multi._RULE_FILES["market"])
        self.assertIn(config.HYPOTHESIS_REGISTRY, multi._RULE_FILES["lesson"])


# ─── WP-4：卡片压缩与最低成功数 ──────────────────────────────────────────────
class TestCompactAndGate(unittest.TestCase):
    def test_compact_cards_is_valid_json_and_truncates_findings(self):
        findings = [{"claim": "长" * 200, "evidence": {"source": "csv", "quote": "x"}}
                    for _ in range(40)]
        cards = [{"id": "m", "status": "ok", "attempts": [{"model": "a"}, {"model": "b"}],
                  "card": {"findings": findings, "summary": "s"}},
                 {"id": "f", "status": "error", "attempts": [{"model": "c"}], "card": None,
                  "error": "错" * 500}]
        with patch.object(config, "FULL_CONSULT_CARD_MAX_CHARS", 3000):
            text = multi._compact_cards(cards)
        data = json.loads(text)
        self.assertNotIn("attempts", text.replace("attempts_count", ""))
        self.assertEqual(data[0]["attempts_count"], 2)
        self.assertGreater(data[0]["findings_truncated"], 0)
        self.assertLessEqual(len(json.dumps(data[0], ensure_ascii=False, indent=2)), 3000)
        self.assertEqual(len(data[0]["card"]["findings"]) + data[0]["findings_truncated"], 40)
        self.assertEqual(len(data[1]["error"]), 200)
        self.assertEqual(len(cards[0]["card"]["findings"]), 40)      # 不改调用方的卡片

    def test_too_few_modules_stops_before_synthesis(self):
        failed = {"data_audit", "fundamentals", "fundamentals_review", "market_primary", "market_challenge"}

        def expert(task, bundle):
            result = _expert_ok(task, bundle)
            if task["id"] in failed:
                result.update(status="error", error="offline")
            return result

        for runner, args in ((multi.run, ("csv", "fund", "H", "A", "L")),
                             (multi.run_review, ("csv", "forecast", "result", "fund", "H", "A", "L"))):
            with self.subTest(runner=runner.__name__), \
                 patch.object(multi, "_run_one", side_effect=expert), \
                 patch.object(multi, "_synthesize") as synth, \
                 patch.object(multi, "_review_synthesize") as review_synth:
                report, summary = runner(*args, settings=settings.defaults(
                    "analyze" if runner is multi.run else "review"))
            self.assertEqual(report, "")
            self.assertIn("低于最低要求", summary["error"])
            self.assertIn("4/9", summary["error"])
            synth.assert_not_called()
            review_synth.assert_not_called()


# ─── WP-5：审计严重度、候选表一致性、修复验收 ────────────────────────────────
class TestAuditAndRepair(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(config, "TYPESAFE_DECISION_MODE", "off"))
        self.stack.enter_context(patch.object(multi, "_run_one", side_effect=_expert_ok))
        self.synth = self.stack.enter_context(
            patch.object(multi, "_synthesize", return_value=VALID_REPORT))

    def _run(self):
        return multi.run("csv", "fund", "H", "A", "L", settings=settings.defaults("analyze"))

    def test_minor_only_passes_without_repair(self):
        phases = []

        def route(system, user, spec, **kwargs):
            phases.append(kwargs["phase"])
            return {"ok": False, "issues": [{"section": "3", "severity": "minor",
                                             "problem": "措辞"}, "旧式字符串问题"]}, {}

        with patch.object(multi, "_call_route", side_effect=route):
            report, summary = self._run()
        self.assertEqual(phases, ["audit"])
        self.assertTrue(summary["audit"]["ok"])
        self.assertEqual(len(summary["audit"]["minors"]), 2)
        self.assertIn("通过（minor 2 项）", report)

    def test_ok_false_with_no_issues_counts_as_pass(self):
        with patch.object(multi, "_call_route", return_value=({"ok": False, "issues": []}, {})):
            audit = multi._audit_report({}, VALID_REPORT, [])
        self.assertTrue(audit["ok"])
        self.assertEqual(audit["blockers"], [])

    def test_deterministic_problems_are_blockers(self):
        broken = VALID_REPORT.replace("- **信号强度**：无\n", "")
        with patch.object(multi, "_call_route", return_value=({"ok": True, "issues": []}, {})):
            audit = multi._audit_report({}, broken, [])
        self.assertFalse(audit["ok"])
        self.assertEqual(audit["blockers"][0]["severity"], "blocker")
        self.assertIn("信号强度", audit["blockers"][0]["problem"])

    def test_signal_strength_overreach_is_flagged(self):
        strong = VALID_REPORT.replace("信号强度**：无", "信号强度**：强")
        with patch.object(typesafe_decisions, "verified_hypothesis_count", return_value=0):
            self.assertTrue(any("越权" in issue for issue in multi._basic_report_issues(strong)))
        with patch.object(typesafe_decisions, "verified_hypothesis_count", return_value=2):
            self.assertFalse(any("越权" in issue for issue in multi._basic_report_issues(strong)))

    def test_candidate_table_must_match_code_table(self):
        csv_text = make_csv([("即时", -0.25, 1.95, 1.95)], [("即时", 2.00, 3.40, 3.80)])
        bundle = {"csv_text": csv_text, "league": "Serie A", "home": "H", "away": "A"}
        table = candidates.build_table(csv_text, "Serie A", "H", "A")
        rows = [line for line in candidates.format_table(table).splitlines()
                if line.startswith("| C")]
        report = SECTIONS_1_6 + SECTION_7 + "### 8. 投注决策\n" + TABLE_HEAD + "\n".join(rows) + "\n"
        self.assertEqual(multi._candidate_mismatches(report, bundle), [])
        self.assertFalse(any("不一致" in i for i in multi._basic_report_issues(report, bundle=bundle)))
        # 模型在表末追加的行不比
        extra = report + "| C99 | 主队大小球 大 1.5 | 2.10 | 47.6% | 50% | 50% | +5.0% | eligible |\n"
        self.assertEqual(multi._candidate_mismatches(extra, bundle), [])
        first = table.rows[0]
        wrong = report.replace(f"| {first.odds:.2f} |", f"| {first.odds + 0.05:.2f} |", 1)
        mismatches = multi._candidate_mismatches(wrong, bundle)
        self.assertEqual(mismatches, [f"C01 赔率 {first.odds + 0.05:.2f}≠{first.odds:.2f}"])
        issues = multi._basic_report_issues(wrong, bundle=bundle)
        self.assertTrue(any(i.startswith("第 8 节候选表与代码候选表不一致：C01 赔率") for i in issues))
        short = SECTIONS_1_6 + SECTION_7 + "### 8. 投注决策\n" + TABLE_HEAD + rows[0] + "\n"
        self.assertIn(f"候选表缺 {len(rows) - 1} 行", multi._candidate_mismatches(short, bundle))

    def test_rejected_repair_keeps_original(self):
        blocker = {"section": "7", "severity": "blocker", "problem": "判小球却预测 3:2"}
        audit = {"ok": False, "issues": [blocker], "blockers": [blocker], "minors": [], "route": {}}
        no_table = VALID_REPORT.split("### 8.")[0] + "### 8. 投注决策\n没有表\n" + "证据。" * 400
        for repaired in ("太短的修复稿", no_table):
            with self.subTest(repaired=repaired[:6]), \
                 patch.object(multi, "_audit_report", side_effect=[dict(audit)]) as auditor, \
                 patch.object(multi, "_call_route", return_value=(repaired, {"model": "x"})) as route:
                report, summary = self._run()
            self.assertTrue(report.startswith(VALID_REPORT))
            self.assertTrue(summary["audit"]["repair_rejected"])
            auditor.assert_called_once()                              # 不做第二次审计
            self.assertEqual(json.loads(route.call_args.args[1].split("### 审计问题\n")[1]
                                        .split("\n### 代码计算")[0]), [blocker])
            self.assertIn("修复稿未通过验收，保留原稿", report)
            self.assertIn("判小球却预测 3:2", report)
        self.assertFalse(multi._accept_repair(VALID_REPORT, "短", {}))
        self.assertFalse(multi._accept_repair(VALID_REPORT, no_table, {}))
        self.assertTrue(multi._accept_repair(VALID_REPORT, VALID_REPORT + "补一句", {}))

    def test_accepted_repair_replaces_report_and_reaudits(self):
        blocker = {"section": "", "severity": "blocker", "problem": "x"}
        first = {"ok": False, "issues": [blocker], "blockers": [blocker], "minors": [], "route": {}}
        second = {"ok": True, "issues": [], "blockers": [], "minors": [], "route": {}}
        progress = []
        with patch.object(multi, "_audit_report", side_effect=[first, second]), \
             patch.object(multi, "_call_route",
                          return_value=(VALID_REPORT + "修复标记", {"model": "fixer"})):
            report, summary = multi.run("csv", "fund", "H", "A", "L",
                                        settings=settings.defaults("analyze"),
                                        progress=lambda kind, result: progress.append(kind))
        self.assertIn("修复标记", report)
        self.assertIn("fixer", summary["writers"])
        self.assertEqual([k for k in progress if k in ("audit", "repair")], ["audit", "repair", "audit"])


# ─── WP-6：复盘盲推不占探索注额度 ────────────────────────────────────────────
class TestExploreBudget(unittest.TestCase):
    def _finalize(self, **kwargs):
        selection = Mock(tier="explore")
        with patch.object(config, "TYPESAFE_DECISION_MODE", "active"), \
             patch.object(config, "EXPLORE_MODE", "on"), \
             patch.object(typesafe_decisions, "select_decision", return_value=selection) as select, \
             patch.object(typesafe_decisions, "log_selection"), \
             patch.object(typesafe_decisions, "apply_selection", return_value="final"), \
             patch.object(analyzer, "_explore_budget_left", return_value=True) as budget, \
             patch.object(analyzer, "_count_explore") as count:
            result = analyzer.finalize_decision("report", "H", "A", "L", **kwargs)
        return result, select, budget, count

    def test_blind_finalize_does_not_touch_daily_budget(self):
        result, select, budget, count = self._finalize(count_explore=False)
        self.assertEqual(result, "final")
        budget.assert_not_called()
        count.assert_not_called()
        self.assertTrue(select.call_args.kwargs["explore_allowed"])

    def test_default_finalize_still_counts(self):
        _, _, budget, count = self._finalize()
        budget.assert_called_once()
        count.assert_called_once()

    def test_conservative_still_blocks_explore_without_budget(self):
        _, select, _, _ = self._finalize(count_explore=False, modes=("conservative",))
        self.assertFalse(select.call_args.kwargs["explore_allowed"])

    def test_review_blind_stream_passes_count_explore_false(self):
        with patch.object(analyzer, "analyze_stream", return_value=iter([])) as stream:
            list(analyzer.review_blind_stream("csv", "H", "A", "L"))
        self.assertIs(stream.call_args.kwargs["count_explore"], False)

    def test_multi_run_passes_count_explore_only_when_false(self):
        with patch.object(config, "TYPESAFE_DECISION_MODE", "off"), \
             patch.object(multi, "_run_one", side_effect=_expert_ok), \
             patch.object(multi, "_synthesize", return_value=VALID_REPORT), \
             patch.object(multi, "_audit_report", return_value={"ok": True, "issues": []}), \
             patch.object(analyzer, "finalize_decision", side_effect=lambda r, *a, **k: r) as fin:
            multi.run("csv", "", "H", "A", "L", settings=settings.defaults("review"),
                      profile="review", apply_focus_modes=False, count_explore=False)
            self.assertEqual(fin.call_args.kwargs, {"count_explore": False})
            multi.run("csv", "", "H", "A", "L", settings=settings.defaults("analyze"))
            self.assertEqual(fin.call_args.kwargs, {})


# ─── WP-7：假设结算 ──────────────────────────────────────────────────────────
class TestHypothesisSettlement(unittest.TestCase):
    def test_script_reexports_moved_objects(self):
        self.assertIs(bt.Tally, hypothesis_settle.Tally)
        self.assertIs(bt.settle_trigger, hypothesis_settle.settle_trigger)
        self.assertIs(bt.settle_home, hypothesis_settle.settle_home)
        self.assertIs(bt.pnl, hypothesis_settle.pnl)
        self.assertEqual(bt.settle_home(1, -0.75), 0.5)

    def test_settlement_block_table(self):
        text = hypothesis_settle.settlement_block(M07_CSV, "Serie A", (0, 1))
        self.assertIn("### 假设结算（代码按比分计算）", text)
        self.assertIn("| 假设 | 状态 | 预测 | 结算 |", text)
        row = next(line for line in text.splitlines() if line.startswith("| H-M07 |"))
        self.assertIn("| 下盘 | 符合 |", row)                       # 主让 -0.25 输球 → 下盘赢
        lost = hypothesis_settle.settlement_block(M07_CSV, "Serie A", (2, 0))
        self.assertIn("| 下盘 | 不符合 |", lost)

    def test_settlement_block_without_triggers_and_on_error(self):
        empty = hypothesis_settle.settlement_block("", "Serie A", (1, 1))
        self.assertIn("本场未触发可机械判定的假设", empty)
        with patch.object(hypothesis_settle.market_facts, "build_facts", side_effect=RuntimeError):
            with self.assertLogs(hypothesis_settle.log, level="WARNING"):
                self.assertEqual(hypothesis_settle.settlement_block(M07_CSV, "Serie A", (1, 0)), "")

    def test_review_prompts_inject_settlement_after_result(self):
        system, user = analyzer._review_prompts(M07_CSV, "盲推", "主 2-1 客", "H", "A", "Serie A",
                                                score=(2, 1))
        self.assertIn("假设结算", user)
        self.assertGreater(user.index("### 假设结算"), user.index("### 实际结果"))
        self.assertIn("照抄该表", system)
        _, plain = analyzer._review_prompts(M07_CSV, "盲推", "主 2-1 客", "H", "A", "Serie A")
        self.assertNotIn("### 假设结算", plain)

    def test_full_review_puts_settlement_in_modules_and_synthesis(self):
        seen = {}

        def expert(task, bundle):
            seen.setdefault("module_input", multi._module_input(task, bundle))
            return _expert_ok(task, bundle)

        with patch.object(multi, "_run_one", side_effect=expert), \
             patch.object(multi, "_review_synthesize", return_value="review") as synth, \
             patch.object(multi, "_review_audit", return_value={"ok": True, "issues": []}):
            multi.run_review(M07_CSV, "forecast", "result", "fund", "H", "A", "Serie A",
                             settings=settings.defaults("review"), score=(0, 1))
        bundle = synth.call_args.args[0]
        self.assertIn("| H-M07 |", bundle["settlement_block"])
        self.assertGreater(seen["module_input"].index("### 假设结算"),
                           seen["module_input"].index("### 实际结果"))
        with patch.object(analyzer, "_review_prompts", return_value=("s", "u")) as prompts:
            multi._review_synthesis_prompt(bundle, [])
        self.assertEqual(prompts.call_args.kwargs["score"], (0, 1))

    def test_tgbot_reads_full_time_score_like_fmt_result(self):
        self.assertEqual(tgbot._full_time_score({"goals": {"home": 2, "away": 1}}), (2, 1))
        self.assertIsNone(tgbot._full_time_score({"goals": {"home": None, "away": 1}}))
        self.assertIsNone(tgbot._full_time_score({"result": "offline"}))


# ─── WP-8：比分缓存陈旧与 /hypstats ──────────────────────────────────────────
class TestScoreCache(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        patcher = patch.object(bt, "SCORES", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write(self, day, statuses):
        rows = [{"id": i, "status": s, "home": 1, "away": 0} for i, s in enumerate(statuses)]
        (self.dir / f"{day}.json").write_text(json.dumps(rows), encoding="utf-8")

    def test_stale_recent_cache_is_refetched(self):
        self._write("2026-11-10", ["FT", "NS"])
        self.assertTrue(bt._needs_fetch("2026-11-10", "2026-11-12"))

    def test_finished_cache_is_kept(self):
        self._write("2026-11-10", ["FT", "AET", "PEN", "CANC", "PST", "ABD", "AWD", "WO"])
        self.assertFalse(bt._needs_fetch("2026-11-10", "2026-11-12"))

    def test_old_unfinished_cache_is_not_refetched(self):
        self._write("2026-11-01", ["1H"])
        self.assertFalse(bt._needs_fetch("2026-11-01", "2026-11-12"))
        self.assertTrue(bt._needs_fetch("2026-11-05", "2026-11-12"))   # 无缓存照常取


class TestHypstatsCommand(unittest.TestCase):
    def test_admin_only_and_help(self):
        self.assertIn("hypstats", tgbot._ADMIN_ONLY_CMDS)
        self.assertIn("/hypstats", tgbot._HELP_ADMIN_EXTRA)
        self.assertNotIn("/hypstats", tgbot._HELP_VISITOR)

    def _stubs(self, registered="2026-10-01"):
        bt_stub = types.SimpleNamespace(fetch_scores=Mock(return_value=3))
        stats_stub = types.SimpleNamespace(
            REGISTRY=Mock(read_text=Mock(return_value="表头")),
            registration_date=Mock(return_value=registered),
            NO_REGISTRATION_DATE="登记表表头尚未填写登记日期",
            build_report=Mock(return_value=("# 假设登记后样本\n| H-M09 |", {})))
        return bt_stub, stats_stub

    def test_reports_back_without_writing_registry(self):
        bt_stub, stats_stub = self._stubs()
        with patch.object(tgbot, "_import_hypothesis_scripts", return_value=(bt_stub, stats_stub)), \
             patch.object(tgbot, "_send_long") as send_long, patch.object(tgbot, "send"):
            tgbot._run_hypstats(1)
        bt_stub.fetch_scores.assert_called_once()
        stats_stub.build_report.assert_called_once()
        text = send_long.call_args.args[1]
        self.assertIn("| H-M09 |", text)
        self.assertTrue(text.endswith(tgbot.HYPSTATS_FOOTER))

    def test_missing_registration_date_returns_script_error(self):
        bt_stub, stats_stub = self._stubs(registered=None)
        with patch.object(tgbot, "_import_hypothesis_scripts", return_value=(bt_stub, stats_stub)), \
             patch.object(tgbot, "send") as send:
            tgbot._run_hypstats(1)
        self.assertEqual(send.call_args.args[1], "登记表表头尚未填写登记日期")
        bt_stub.fetch_scores.assert_not_called()

    def test_real_scripts_import_and_expose_entry_points(self):
        real_bt, real_stats = tgbot._import_hypothesis_scripts()
        self.assertTrue(callable(real_bt.fetch_scores))
        self.assertTrue(callable(real_stats.build_report))


# ─── WP-9：审计结论直接告诉用户 ──────────────────────────────────────────────
class TestAuditNotice(unittest.TestCase):
    def test_notice_text(self):
        self.assertEqual(tgbot._audit_notice({"ok": True}), "")
        self.assertEqual(tgbot._audit_notice(None), "")
        blockers = [{"problem": "a"}, {"problem": "b"}]
        text = tgbot._audit_notice({"ok": False, "blockers": blockers})
        self.assertEqual(text, "⚠️ 审计未通过：2 项阻断问题（详见报告末尾的执行记录）")
        text = tgbot._audit_notice({"ok": False, "blockers": blockers, "repair_rejected": True})
        self.assertTrue(text.endswith("修复稿未通过验收，以下为原稿"))

    def test_appendix_lists_blockers(self):
        audit = {"ok": False, "route": {"model": "auditor"},
                 "blockers": [{"section": "7", "problem": "问题" * 200}] * 6,
                 "minors": ["m1"], "repair_rejected": True}
        text = multi._render_appendix([], audit)
        self.assertIn("未通过：6 项阻断问题", text)
        self.assertEqual(text.count("  - §7 "), 5)
        self.assertIn("minor 1 项", text)
        self.assertIn("修复稿未通过验收，保留原稿", text)


if __name__ == "__main__":
    unittest.main()


class CandidateOddsRoundingTests(unittest.TestCase):
    """三位小数的报价（如 1.925）在报告里写成两位，不能被判成与代码候选表不一致。"""

    def test_two_decimal_rounding_is_not_a_mismatch(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from bot import multi_analyzer
        code = SimpleNamespace(rows=[SimpleNamespace(odds=1.925, edge=0.01)])
        for shown in (1.92, 1.93):
            report_rows = (SimpleNamespace(odds=shown, edge=0.01),)
            with patch.object(multi_analyzer.candidates, "build_table", return_value=code), \
                    patch.object(multi_analyzer.typesafe_decisions, "_selection_pool",
                                 return_value=(report_rows, "unknown")):
                self.assertEqual(multi_analyzer._candidate_mismatches("r", {"csv_text": "c"}), [])
        with patch.object(multi_analyzer.candidates, "build_table", return_value=code), \
                patch.object(multi_analyzer.typesafe_decisions, "_selection_pool",
                             return_value=((SimpleNamespace(odds=1.94, edge=0.01),), "unknown")):
            self.assertEqual(len(multi_analyzer._candidate_mismatches("r", {"csv_text": "c"})), 1)
