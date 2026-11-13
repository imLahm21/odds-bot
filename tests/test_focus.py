"""自定义侧重（bot/focus.py 及其在提示词、定稿、Telegram 交互中的接入）的测试（不发网络请求）。"""

import os
import re
import sys
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

from bot import (analyzer, config, focus, goals_model, multi_analyzer,  # noqa: E402
                 tgbot)
from bot import typesafe_decisions as decisions  # noqa: E402

CSV_HEADER = ("快照时间(CST),联赛,开球时间(CST),主队,客队,博彩公司,盘口类型,"
              "主胜赔率,平局赔率,客胜赔率,凯利(主胜),凯利(平局),凯利(客胜),"
              "让球,主队水位,客队水位,凯利(主),凯利(客),数据更新(CST)")
_T = "2026-11-12 20:00（即时）,英超,2026-11-12 20:30,H,A"
OU_CSV = "\n".join([
    CSV_HEADER,
    f"{_T},Bet365,欧指,2.00,3.50,3.80,1,1,1,,,,,,2026-11-12 20:00",
    f"{_T},Pinnacle,欧指,2.05,3.45,3.75,1,1,1,,,,,,2026-11-12 20:00",
    f"{_T},Bet365,亚盘,,,,,,,-0.5,1.95,1.90,1,1,2026-11-12 20:00",
    f"{_T},Bet365,大小球,,,,,,,2.5,1.90,1.95,1,1,2026-11-12 20:00",
    f"{_T},Bet365,大小球,,,,,,,3.5,3.10,1.36,1,1,2026-11-12 20:00",
]) + "\n"


# ─── focus.py ────────────────────────────────────────────────────────────────
class TestFocusParse(unittest.TestCase):
    def test_encode_parse_round_trip(self):
        encoded = focus.encode(("conservative", "ou", "ou"), "  文字  ")
        self.assertEqual(encoded, "[[focus:ou,conservative]] 文字")
        parsed = focus.parse(encoded)
        self.assertEqual(parsed.modes, ("ou", "conservative"))
        self.assertEqual(parsed.text, "文字")
        self.assertFalse(parsed.conflict)

    def test_encode_without_modes_returns_text(self):
        self.assertEqual(focus.encode((), "  只有文字 "), "只有文字")
        self.assertEqual(focus.encode(("ou",)), "[[focus:ou]]")

    def test_keyword_detection(self):
        self.assertEqual(focus.parse("重点关注波胆").modes, ("score",))
        self.assertEqual(focus.parse("激进一点，看大小球").modes, ("ou", "aggressive"))

    def test_conflict_in_text_disables_both(self):
        parsed = focus.parse("激进但也要保守")
        self.assertEqual(parsed.modes, ())
        self.assertTrue(parsed.conflict)
        self.assertFalse(parsed.empty)

    def test_marker_wins_over_text_keyword(self):
        parsed = focus.parse(focus.encode(("aggressive",), "保守一点也行"))
        self.assertEqual(parsed.modes, ("aggressive",))
        self.assertFalse(parsed.conflict)

    def test_empty_and_unknown_keys(self):
        self.assertTrue(focus.parse("").empty)
        self.assertTrue(focus.parse("   ").empty)
        self.assertEqual(focus.parse("[[focus:foo,ou]] x").modes, ("ou",))
        self.assertEqual(focus.label(focus.parse("")), "")

    def test_label(self):
        parsed = focus.parse(focus.encode(("ou", "conservative"), "主队主力前锋确认缺阵"))
        self.assertEqual(focus.label(parsed),
                         "📊 大小球 + 🛡 保守；补充：主队主力前锋确认缺阵")

    def test_is_ou_play(self):
        self.assertTrue(focus.is_ou_play("大小球 大 2.5"))
        self.assertTrue(focus.is_ou_play("主队大小球 大 1.5"))
        self.assertFalse(focus.is_ou_play("让球 -0.5（主）"))

    def test_stamp_inserts_after_league_line_once(self):
        report = "## 比赛：H vs A\n## 赛事：英超  开球时间：20:30\n\n### 1. 数据提取\n"
        obj = focus.parse(focus.encode(("ou",)))
        stamped = focus.stamp(report, obj)
        self.assertEqual(
            stamped,
            "## 比赛：H vs A\n## 赛事：英超  开球时间：20:30\n"
            "> 本场侧重：📊 大小球\n\n### 1. 数据提取\n")
        self.assertEqual(focus.stamp(stamped, obj), stamped)
        self.assertIs(focus.stamp(report, focus.parse("")), report)

    def test_stamp_without_league_line_goes_first(self):
        stamped = focus.stamp("### 1. x\n", focus.parse("看大小球"))
        self.assertTrue(stamped.startswith("> 本场侧重：📊 大小球；补充：看大小球\n"))

    def test_has_section_9(self):
        self.assertTrue(focus.has_section_9("x\n### 9. 侧重专题\n"))
        self.assertTrue(focus.has_section_9("### 9、侧重专题"))
        self.assertFalse(focus.has_section_9("### 8. 投注决策\n"))


# ─── goals_model 注入段 ───────────────────────────────────────────────────────
class TestGoalsBlocks(unittest.TestCase):
    def setUp(self):
        self.st = goals_model.states_from_csv(OU_CSV)
        self.assertIsNotNone(self.st)

    def test_score_block_rows_order_and_fair_odds(self):
        block = goals_model.format_score_block(self.st, 8, 0.15)
        self.assertIn("### 比分概率表（代码计算）", block)
        rows = re.findall(r"^\| (\d+):(\d+) \| ([\d.]+)% \| ([\d.]+) \| ([\d.]+) \|$",
                          block, re.M)
        self.assertEqual(len(rows), 8)
        probs = [float(r[2]) for r in rows]
        self.assertEqual(probs, sorted(probs, reverse=True))
        matrix = goals_model.score_matrix(self.st["lambda_home"], self.st["lambda_away"])
        for i, j, _, fair, floor in rows:
            p = matrix[(int(i), int(j))]
            self.assertAlmostEqual(float(fair), 1 / p, delta=0.01)
            self.assertAlmostEqual(float(floor), 1 / p * 1.15, delta=0.01)
        self.assertIn("不计 edge", block)

    def test_total_goals_block_sums_to_one(self):
        block = goals_model.format_total_goals_block(self.st)
        self.assertIn("### 总进球分布（代码计算）", block)
        line = next(l for l in block.splitlines() if "0球" in l)
        values = [float(v) for v in re.findall(r"([\d.]+)%", line)]
        self.assertEqual(len(values), 6)
        self.assertAlmostEqual(sum(values) / 100, 1.0, delta=0.01)
        self.assertIn("P(≤2)", block)
        self.assertIn("P(≥3)", block)

    def test_missing_state_says_cannot_compute(self):
        self.assertIn("无法计算", goals_model.format_score_block(None, 8, 0.15))
        self.assertIn("无法计算", goals_model.format_total_goals_block(None))


# ─── analyzer._analyze_prompts ───────────────────────────────────────────────
class TestAnalyzePrompts(unittest.TestCase):
    def _prompts(self, extra=""):
        with (
            patch.object(analyzer, "load_rules", return_value="规则正文"),
            patch.object(analyzer, "market_hypotheses_block", return_value="【市场】\n"),
            patch.object(analyzer, "candidates_block", return_value="【候选】\n"),
        ):
            return analyzer._analyze_prompts(OU_CSV, "基本面原文", "H", "A", "英超", extra)

    def test_no_focus_matches_preset_format(self):
        system, user = self._prompts()
        st = goals_model.states_from_csv(OU_CSV)
        expected_user = (
            f"## 比赛：H vs A\n## 联赛：英超\n\n"
            f"### 盘口快照（CSV）\n{OU_CSV}\n\n"
            f"{goals_model.format_states_block(st)}\n"
            "【市场】\n【候选】\n"
            "### 基本面\n基本面原文\n"
        )
        self.assertEqual(user, expected_user)
        self.assertNotIn("本场侧重任务", system + user)
        self.assertNotIn("用户额外侧重", system)

    def test_focus_keeps_system_and_appends_task_last(self):
        base_system, base_user = self._prompts()
        extra = focus.encode(("ou", "score"), "主队主力前锋确认缺阵")
        system, user = self._prompts(extra)
        self.assertEqual(system, base_system)
        self.assertTrue(user.startswith(base_user))
        task = analyzer.focus_task_block(focus.parse(extra))
        self.assertTrue(user.endswith(task))
        self.assertIn("### 总进球分布（代码计算）", user)
        self.assertIn("### 比分概率表（代码计算）", user)
        self.assertLess(user.index("总进球分布"), user.index("比分概率表"))
        self.assertLess(user.index("比分概率表"), user.index("本场侧重任务"))
        self.assertIn("用户提供·未核实", task)
        self.assertIn("### 9. 侧重专题", task)

    def test_mode_specific_blocks(self):
        _, user = self._prompts(focus.encode(("score",)))
        self.assertIn("比分概率表", user)
        self.assertNotIn("总进球分布", user)
        self.assertNotIn("【用户补充文字的处理】", user)
        _, user = self._prompts(focus.encode(("ou",)))
        self.assertIn("总进球分布", user)
        self.assertNotIn("比分概率表", user)

    def test_conflict_line_in_task(self):
        task = analyzer.focus_task_block(focus.parse("激进但也要保守"))
        self.assertIn("激进与保守同时出现，按常规口径", task)
        self.assertEqual(analyzer.focus_task_block(focus.parse("")), "")


# ─── typesafe_decisions.select_decision ──────────────────────────────────────
FOCUS_REPORT = """## 比赛：Home vs Away
## 赛事：英超  开球时间：2026-11-12 20:30
### 7. 最终精算结论
- **亚盘判定**：无倾向
- **信号强度**：无
- **数据质量**：85 / 100（扣分构成：时效 5、不同步 5、临场附加 5）
### 8. 投注决策
| ID | 玩法 | 赔率 | 门槛=1/赔率 | p_市场 | p_最终 | edge | 资格 |
|----|------|------|-------------|--------|--------|------|------|
| C01 | 让球 -0.5（主） | 2.00 | 50% | 54% | 54% | +8.0% | eligible |
| C02 | 大小球 大 2.5 | 1.90 | 52.6% | 55% | 55% | +4.5% | eligible |
| C03 | 客胜 | 4.00 | 25% | 22% | 22% | −12.0% | eligible |
- **选中候选 ID**：C01
- **选中项**：让球 -0.5（主）
- **信号强度与凯利分数**：基础 k 1/8 × 质量系数 1.0 = 1/8
- **注额**：$1.0
"""
EXPLORE_ONLY = (FOCUS_REPORT.replace("+8.0%", "−1.0%").replace("54% | 54%", "49.5% | 49.5%")
                .replace("+4.5%", "−2.5%").replace("55% | 55%", "51.3% | 51.3%"))


def _select(report, **kwargs):
    with patch.object(config, "EXPLORE_MODE", "on"):
        return decisions.select_decision(report, "Home", "Away", "League",
                                         min_confidence=0.8, use_jev=False, **kwargs)


class TestFocusSelection(unittest.TestCase):
    def test_baseline_value_bet(self):
        selection = _select(FOCUS_REPORT)
        self.assertEqual(selection.selected_id, "C01")
        self.assertEqual(selection.k, 0.125)
        self.assertEqual(selection.focus_note, "")

    def test_empty_modes_identical_to_no_modes(self):
        for report in (FOCUS_REPORT, EXPLORE_ONLY):
            plain = _select(report)
            self.assertEqual(plain, _select(report, modes=()))
            output = decisions.apply_selection(report, plain)
            self.assertNotIn("侧重调整", output)
            self.assertNotIn('"focus"', output)

    def test_aggressive_doubles_k_with_cap(self):
        selection = _select(FOCUS_REPORT, modes=("aggressive",))
        self.assertEqual(selection.k, 0.25)
        self.assertEqual(selection.stake, 2.0)          # 100×0.25×0.08/1.0
        self.assertIn("激进", selection.focus_note)
        strong = (FOCUS_REPORT.replace("信号强度**：无", "信号强度**：强")
                  .replace("+8.0%", "+60.0%").replace("54% | 54%", "80% | 80%"))
        with patch.object(decisions, "verified_hypothesis_count", return_value=1):
            selection = _select(strong, modes=("aggressive",))
        self.assertLessEqual(selection.k, config.FOCUS_K_MAX)
        self.assertEqual(selection.k, 0.5)
        self.assertLessEqual(selection.stake, config.PARLAY_STAKE_CAP)

    def test_conservative_halves_k_and_blocks_explore(self):
        selection = _select(FOCUS_REPORT, modes=("conservative",))
        self.assertEqual(selection.k, 0.0625)
        self.assertEqual(selection.tier, "value")
        self.assertEqual(_select(EXPLORE_ONLY).tier, "explore")
        selection = _select(EXPLORE_ONLY, modes=("conservative",))
        self.assertEqual(selection.selected_id, "PASS")
        self.assertEqual(selection.source, "no_positive_edge")
        self.assertIn("不下探索注", selection.focus_note)

    def test_ou_prefers_over_under_candidates(self):
        selection = _select(FOCUS_REPORT, modes=("ou",))
        self.assertEqual(selection.selected_id, "C02")
        self.assertEqual(selection.positive_ids, ("C01", "C02"))
        self.assertIn("仅在大小球候选中选定", selection.focus_note)

    def test_ou_falls_back_when_no_positive_ou(self):
        report = FOCUS_REPORT.replace("+4.5%", "−5.0%").replace("55% | 55%", "50% | 50%")
        selection = _select(report, modes=("ou",))
        self.assertEqual(selection.selected_id, "C01")
        self.assertIn("按常规", selection.focus_note)

    def test_ou_explore_prefers_over_under(self):
        selection = _select(EXPLORE_ONLY, modes=("ou",))
        self.assertEqual(selection.tier, "explore")
        self.assertEqual(selection.selected_id, "C02")
        self.assertEqual(_select(EXPLORE_ONLY).selected_id, "C01")

    def test_ou_jev_only_sees_over_under_candidates(self):
        with patch.object(decisions, "_request_choice_result",
                          return_value=(None, "jev_disabled")) as request:
            decisions.select_decision(FOCUS_REPORT, min_confidence=0.8, modes=("ou",))
        self.assertEqual(set(request.call_args.args[1]), {"C02", "PASS"})

    def test_low_quality_cannot_be_bypassed(self):
        report = FOCUS_REPORT.replace("85 / 100", "30 / 100")
        selection = _select(report, modes=("aggressive",))
        self.assertEqual(selection.selected_id, "PASS")
        self.assertEqual(selection.source, "low_quality")

    def test_apply_selection_writes_focus_and_is_idempotent(self):
        selection = _select(FOCUS_REPORT, modes=("ou", "aggressive"))
        output = decisions.apply_selection(FOCUS_REPORT, selection)
        self.assertIn("- **侧重调整**：大小球重点：仅在大小球候选中选定；激进口径", output)
        self.assertIn('"focus":["ou","aggressive"]', output)
        self.assertIn("侧重口径 ×2", output)
        self.assertEqual(decisions.apply_selection(output, selection), output)
        final = decisions.read_final_decision(output)
        self.assertFalse(final["pass"])
        self.assertEqual(final["play"], "大小球 大 2.5")

    def test_finalize_decision_passes_modes_and_conservative_blocks_explore(self):
        with (
            patch.object(config, "TYPESAFE_DECISION_MODE", "active"),
            patch.object(analyzer, "_explore_budget_left", return_value=True),
            patch.object(analyzer.typesafe_decisions, "select_decision",
                         return_value=None) as select,
        ):
            analyzer.finalize_decision(FOCUS_REPORT, "H", "A", "L",
                                       modes=("conservative",))
        self.assertEqual(select.call_args.kwargs["modes"], ("conservative",))
        self.assertFalse(select.call_args.kwargs["explore_allowed"])


class TestFinalizeWithFocus(unittest.TestCase):
    def test_stamp_and_missing_section_9_note(self):
        extra = focus.encode(("ou",))
        with patch.object(analyzer, "finalize_decision",
                          side_effect=lambda r, *a, **k: r) as fin:
            out = analyzer._finalize_with_focus(FOCUS_REPORT, "H", "A", "L", extra)
        self.assertEqual(fin.call_args.kwargs["modes"], ("ou",))
        self.assertIn("> 本场侧重：📊 大小球", out)
        self.assertTrue(out.endswith(analyzer.FOCUS_MISSING_SECTION_9_NOTE))
        with patch.object(analyzer, "finalize_decision",
                          side_effect=lambda r, *a, **k: r):
            out = analyzer._finalize_with_focus(
                FOCUS_REPORT + "### 9. 侧重专题\nx\n", "H", "A", "L", extra)
        self.assertNotIn("模型未输出", out)

    def test_no_focus_is_plain_finalize(self):
        with patch.object(analyzer, "finalize_decision",
                          side_effect=lambda r, *a, **k: r) as fin:
            out = analyzer._finalize_with_focus(FOCUS_REPORT, "H", "A", "L", "")
        fin.assert_called_once_with(FOCUS_REPORT, "H", "A", "L")
        self.assertEqual(out, FOCUS_REPORT)


# ─── multi_analyzer ──────────────────────────────────────────────────────────
class TestMultiAnalyzerFocus(unittest.TestCase):
    REPORT = "".join(f"### {n}. 段\nx\n" for n in range(1, 8)) + "### 8. 投注决策\n" + "x" * 600

    def test_section_9_required_only_with_focus(self):
        self.assertNotIn("缺少 ### 9. 侧重专题",
                         multi_analyzer._basic_report_issues(self.REPORT))
        self.assertIn("缺少 ### 9. 侧重专题",
                      multi_analyzer._basic_report_issues(self.REPORT, focus_required=True))
        with_9 = self.REPORT + "\n### 9. 侧重专题\nx\n"
        # 该夹具没有第 7 节两行与候选表，另有阻断项；这里只看第 9 节这一条
        self.assertNotIn("缺少 ### 9. 侧重专题",
                         multi_analyzer._basic_report_issues(with_9, focus_required=True))

    def test_json_instruction_uses_label_for_analyze_only(self):
        text = multi_analyzer._json_instruction("m", "角色", "激进一点")
        self.assertIn("用户自定义侧重：🔥 激进；补充：激进一点", text)
        review = multi_analyzer._json_instruction("m", "角色", "激进一点", review=True)
        self.assertIn("用户自定义侧重：激进一点\n", review)


# ─── tgbot 侧重键盘 ──────────────────────────────────────────────────────────
class TestFocusKeyboard(unittest.TestCase):
    CHAT = 4242

    def setUp(self):
        tgbot._pending_focus[self.CHAT] = {"fid": 99, "effort": "high", "modes": set()}
        self.addCleanup(tgbot._pending_focus.pop, self.CHAT, None)
        self.addCleanup(tgbot._pending_custom.pop, self.CHAT, None)
        for name in ("answer_callback", "edit_markup", "send"):
            patcher = patch.object(tgbot, name)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)
        patcher = patch.object(tgbot, "_authorized", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _click(self, data):
        tgbot.handle_callback({"id": "cb", "data": data,
                               "message": {"chat": {"id": self.CHAT}, "message_id": 7}})

    def test_aggressive_then_conservative_keeps_only_last(self):
        self._click("fo:t:aggressive")
        self._click("fo:t:conservative")
        self.assertEqual(tgbot._pending_focus[self.CHAT]["modes"], {"conservative"})
        keyboard = self.edit_markup.call_args.args[2]
        texts = [b["text"] for row in keyboard["inline_keyboard"] for b in row]
        self.assertIn("✅ 🛡 保守", texts)
        self.assertIn("🔥 激进", texts)

    def test_go_submits_encoded_modes(self):
        self._click("fo:t:ou")
        with patch.object(tgbot, "_submit_analysis", return_value="tid") as submit:
            self._click("fo:go")
        submit.assert_called_once_with(self.CHAT, "精算", tgbot._run_sop, self.CHAT, 99,
                                       focus.encode(("ou",)), "high")
        self.assertNotIn(self.CHAT, tgbot._pending_focus)

    def test_go_without_modes_is_rejected(self):
        with patch.object(tgbot, "_submit_analysis") as submit:
            self._click("fo:go")
        submit.assert_not_called()
        self.answer_callback.assert_called_with("cb", "请至少选一个侧重，或点补充文字")

    def test_extra_text_moves_to_pending_custom(self):
        self._click("fo:t:score")
        self._click("fo:x")
        self.assertEqual(tgbot._pending_custom[self.CHAT], (99, "high", ("score",)))
        self.assertNotIn(self.CHAT, tgbot._pending_focus)

    def test_expired_state(self):
        tgbot._pending_focus.pop(self.CHAT)
        self._click("fo:t:ou")
        self.answer_callback.assert_called_with("cb", "已失效，请重新 /analyze")


if __name__ == "__main__":
    unittest.main()


class BlindReviewInstructionTests(unittest.TestCase):
    """复盘盲推说明是系统约束，不能被当成用户侧重。"""

    def _bundle(self, **extra):
        return {"csv_text": "", "fundamentals": "", "home": "A", "away": "B",
                "league": "英超", **extra}

    def test_blind_note_stays_out_of_focus_machinery(self):
        from bot import analyzer, multi_analyzer
        note = "这是赛后复盘的第一阶段盲推：当前严禁使用或猜测实际比分。\n用户复盘侧重：保守"
        with patch.object(analyzer, "load_rules", return_value="规则"):
            system, user, _ = multi_analyzer._synthesis_prompt(
                self._bundle(extra_instruction=note, focus_enabled=False), [])
            plain_system, _, _ = multi_analyzer._synthesis_prompt(self._bundle(), [])
        self.assertNotIn("本场侧重任务", user)
        self.assertNotIn("比分概率表", user)
        self.assertIn("本次运行说明", system)
        self.assertIn(note, system)
        self.assertTrue(system.startswith(plain_system))
        self.assertEqual(
            multi_analyzer._basic_report_issues("x" * 600, focus_required=False),
            multi_analyzer._basic_report_issues("x" * 600))

    def test_user_focus_still_goes_last_in_full_model(self):
        from bot import analyzer, multi_analyzer
        with patch.object(analyzer, "load_rules", return_value="规则"):
            system, user, _ = multi_analyzer._synthesis_prompt(
                self._bundle(extra_instruction="重点关注波胆"), [])
        self.assertNotIn("本次运行说明", system)
        self.assertTrue(user.rstrip().endswith(
            "- **对第 7、8 节的影响**：[逐条写改了什么；按四层规则不能改的写明原因]"))
