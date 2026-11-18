"""影子倾向（docs/置信度影子倾向计划.md）的离线测试：解析、剥离、零影响、
decision_final 落盘、开关、提示词缓存不变性、发布剥离、回测统计。

运行：python -m unittest tests.test_shadow_lean -v
"""

import html
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("APIFOOTBALL_KEY", "offline-test-key")

from offline_support import prepare_publish_stubs  # noqa: E402
prepare_publish_stubs()

from bot import (analyzer, candidates, config, llm_client,  # noqa: E402
                 multi_analyzer, typesafe_decisions as decisions)
from scripts import backtest_reports as bt  # noqa: E402
from test_market_facts import make_csv  # noqa: E402


LEAN_LINE = ("- **影子倾向**：C01 让球 Arsenal −0.5｜置信度 58 / 100｜"
             "依据：主队主场近 5 场 4 胜（仅记录，不影响结论与注额）\n")

REPORT_TEMPLATE = """## 比赛：Arsenal vs Chelsea
## 赛事：英超  开球时间：2026-11-20 03:00
### 1. 数据提取
主队近况稳定。
### 7. 最终精算结论
- **四层判断**：市场=主 52% ｜ 基本面=无 ｜ 假设=无 ｜ 质量=85
- **亚盘判定**：无倾向 conclusion_private_marker
- **信号强度**：无
- **数据质量**：85 / 100（扣分构成：时效 5）
{lean}- **关键依据**：市场价格为基准
- **风险提示**：报告距开球 2 小时
### 8. 投注决策
| ID | 玩法 | 赔率 | 门槛=1/赔率 | p_市场 | p_最终 | edge | 资格 |
|----|------|------|-------------|--------|--------|------|------|
| C01 | 让球 Arsenal -0.5 | 1.93 | 51.8% | 52.0% | 52.0% | +0.4% | eligible |
| C02 | 让球 Chelsea +0.5 | 1.95 | 51.3% | 48.0% | 48.0% | −6.4% | eligible |
| C03 | 让球 Arsenal -0.75 | 2.20 | 45.5% | 44.0% | 44.0% | −3.0% | eligible |
| C04 | 胜平负 Arsenal 胜 | 2.10 | 47.6% | 50.0% | 50.0% | +5.0% | eligible |
| C05 | 胜平负 平局 | 3.40 | 29.4% | 27.0% | 27.0% | −8.2% | eligible |
- **选中候选 ID**：C04
- **选中项**：胜平负 Arsenal 胜
- **信号强度与凯利分数**：基础 k 1/8 × 质量系数 1.0 = 1/8
- **注额**：$0.6
"""

LEAN_REPORT = REPORT_TEMPLATE.format(lean=LEAN_LINE)
PLAIN_REPORT = REPORT_TEMPLATE.format(lean="")


def _pool(report):
    candidates_, _evidence = decisions._selection_pool(report)
    return candidates_


def _lean(report):
    return decisions.parse_shadow_lean(report, _pool(report))


def _with_lean(text):
    return REPORT_TEMPLATE.format(lean=f"- **影子倾向**：{text}\n")


def _judgment(choice="C04", confidence=0.9):
    return decisions.ChoiceJudgment(
        choice=choice, confidence=confidence,
        probabilities={"C01": 0.05, "C04": 0.05, "PASS": 0.05, choice: confidence},
        requested_model="jev-offline", actual_model="jev-offline",
        request_id="offline", latency_ms=1)


def _final_payload(report):
    match = decisions._DECISION_FINAL_RE.search(report)
    return json.loads(match.group(1))


class TestParseShadowLean(unittest.TestCase):
    def test_01_standard_line(self):
        lean = _lean(LEAN_REPORT)
        self.assertEqual(lean.candidate_id, "C01")
        self.assertEqual(lean.conf, 58)
        self.assertEqual(lean.play, "让球 Arsenal -0.5")      # 取自候选表，不取自行内文字
        self.assertEqual(lean.odds, 1.93)

    def test_02_bold_variants(self):
        lean = _lean(_with_lean("**C02** 让球 Chelsea +0.5｜置信度 **62** / 100｜依据：x"))
        self.assertEqual((lean.candidate_id, lean.conf, lean.odds), ("C02", 62, 1.95))
        lean = _lean(_with_lean("c2 让球 Chelsea +0.5｜置信度：55/100"))
        self.assertEqual((lean.candidate_id, lean.conf), ("C02", 55))

    def test_03_secondary_line_or_1x2_is_rejected(self):
        for cid in ("C03", "C04", "C05", "C09"):
            with self.subTest(cid=cid):
                self.assertIsNone(_lean(_with_lean(f"{cid} 某玩法｜置信度 60 / 100")))

    def test_04_confidence_out_of_range(self):
        for conf in (45, 49, 101, 105):
            with self.subTest(conf=conf):
                self.assertIsNone(_lean(_with_lean(f"C01 让球 Arsenal -0.5｜置信度 {conf} / 100")))
        self.assertEqual(_lean(_with_lean("C01｜置信度 50 / 100")).conf, 50)
        self.assertEqual(_lean(_with_lean("C01｜置信度 100 / 100")).conf, 100)
        self.assertIsNone(_lean(_with_lean("C01 让球 Arsenal -0.5｜依据：没写置信度")))

    def test_05_none_or_missing_line(self):
        self.assertIsNone(_lean(_with_lean("无（缺亚盘主线）")))
        self.assertIsNone(_lean(_with_lean("[无]（缺亚盘主线，C01 不存在）｜置信度 60")))
        self.assertIsNone(_lean(PLAIN_REPORT))
        self.assertTrue(decisions.has_shadow_lean_line(_with_lean("无（缺亚盘主线）")))
        self.assertFalse(decisions.has_shadow_lean_line(PLAIN_REPORT))

    def test_06_no_handicap_rows_in_table(self):
        report = LEAN_REPORT.replace("让球 Arsenal -0.5", "大小球 大 2.5").replace(
            "让球 Chelsea +0.5", "大小球 小 2.5").replace("让球 Arsenal -0.75", "双进 Yes")
        self.assertIsNone(_lean(report))
        self.assertIsNone(decisions.parse_shadow_lean(LEAN_REPORT, ()))

    def test_07_lean_line_is_not_legacy_confidence(self):
        self.assertIsNone(decisions.parse_report_confidence(LEAN_REPORT))
        self.assertEqual(decisions.parse_signal_strength(LEAN_REPORT), "none")
        self.assertEqual(decisions.parse_data_quality(LEAN_REPORT), 85)


class TestStripShadowLean(unittest.TestCase):
    def test_08_only_the_lean_line_is_removed(self):
        stripped = decisions.strip_shadow_lean(LEAN_REPORT)
        self.assertEqual(stripped, PLAIN_REPORT)
        self.assertEqual(decisions.strip_shadow_lean(PLAIN_REPORT), PLAIN_REPORT)
        crlf = LEAN_REPORT.replace("\n", "\r\n")
        self.assertEqual(decisions.strip_shadow_lean(crlf), PLAIN_REPORT.replace("\n", "\r\n"))
        no_bold = LEAN_REPORT.replace("- **影子倾向**：", "* 影子倾向: ")
        self.assertEqual(decisions.strip_shadow_lean(no_bold), PLAIN_REPORT)
        self.assertEqual(decisions.strip_shadow_lean(""), "")


class TestZeroInfluence(unittest.TestCase):
    def _select(self, report, **kwargs):
        return decisions.select_decision(report, "Arsenal", "Chelsea", "英超",
                                         min_confidence=0.8, **kwargs)

    def test_09_selection_identical_with_and_without_lean(self):
        for kwargs in ({"use_jev": False}, {"use_jev": False, "modes": ("ou",)},
                       {"use_jev": False, "explore_allowed": False}):
            with self.subTest(kwargs=kwargs):
                a, b = self._select(PLAIN_REPORT, **kwargs), self._select(LEAN_REPORT, **kwargs)
                for field in ("selected_id", "tier", "k", "stake", "source", "positive_ids",
                              "signal", "quality", "fallback_reason"):
                    self.assertEqual(getattr(a, field), getattr(b, field), field)
                self.assertIsNone(a.lean)
                self.assertEqual(b.lean.candidate_id, "C01")
        # 影子倾向选 C01（edge +0.4%），定稿仍按 edge 最高选 C04
        self.assertEqual(self._select(LEAN_REPORT, use_jev=False).selected_id, "C04")
        # 无正 edge 的探索档同样不受影响
        negative = lambda r: r.replace("+0.4%", "−1.0%").replace("+5.0%", "−2.0%")  # noqa: E731
        a = self._select(negative(PLAIN_REPORT), use_jev=False)
        b = self._select(negative(LEAN_REPORT), use_jev=False)
        self.assertEqual((a.tier, a.selected_id, a.stake), (b.tier, b.selected_id, b.stake))

    def test_10_jev_never_sees_the_lean_line(self):
        with patch.object(decisions, "_request_choice_result",
                          return_value=(_judgment(), None)) as request:
            selection = self._select(LEAN_REPORT)
        self.assertEqual(selection.source, "jev")
        state_text = json.dumps(request.call_args.args[0], ensure_ascii=False)
        self.assertNotIn("影子倾向", state_text)
        self.assertNotIn("置信度 58", state_text)
        self.assertIn("conclusion_private_marker", state_text)   # 第 7 节其余内容照常送达
        # 影子模式入口走同一条路径
        with patch.object(decisions, "_request_choice_result",
                          return_value=(_judgment(), None)) as request, \
             patch.object(decisions, "_log_shadow"):
            decisions.log_selection_shadow(LEAN_REPORT, "Arsenal", "Chelsea", "英超")
        self.assertNotIn("影子倾向", json.dumps(request.call_args.args[0], ensure_ascii=False))

    def test_10b_parlay_extraction_llm_never_sees_the_lean_line(self):
        seen = {}

        def fake_llm(system, user, **kwargs):
            seen["user"] = user
            return '{"pass": true}'

        with patch.object(analyzer, "available", return_value=True), \
             patch.object(analyzer, "_call_llm", side_effect=fake_llm):
            analyzer._extract_decision_llm(LEAN_REPORT, "Arsenal", "Chelsea")
        self.assertNotIn("影子倾向", seen["user"])
        self.assertIn("conclusion_private_marker", seen["user"])


class TestDecisionFinalPayload(unittest.TestCase):
    def _apply(self, report, writer_model=""):
        selection = decisions.select_decision(report, min_confidence=None, use_jev=False,
                                              writer_model=writer_model)
        return selection, decisions.apply_selection(report, selection)

    def test_11_lean_and_writer_model_are_written(self):
        selection, output = self._apply(LEAN_REPORT, "gpt-6-astra-cpa")
        payload = _final_payload(output)
        self.assertEqual(payload["schema"], "decision_final/1")
        self.assertEqual(payload["lean"], {"id": "C01", "play": "让球 Arsenal -0.5",
                                           "odds": 1.93, "conf": 58})
        self.assertEqual(payload["writer_model"], "gpt-6-astra-cpa")
        # 可见的最终选定段不加任何影子倾向内容；串关契约不变
        block = "\n".join(decisions._selection_block(selection)[1:])
        self.assertNotIn("影子倾向", block)
        self.assertNotIn("lean", block)
        final = decisions.read_final_decision(output)
        self.assertEqual(final["play"], "胜平负 Arsenal 胜")
        # 原第 7 节影子倾向行保留在归档正文里
        self.assertIn("影子倾向", decisions.extract_conclusion_section(output))
        # 脚本端只读 decision_final 就能拿到同样的数据
        row = bt.extract_one("report/2026/11/2026-11-20/Arsenal_vs_Chelsea_report.md", output)
        self.assertEqual(row["lean"]["conf"], 58)
        self.assertEqual(row["writer_model"], "gpt-6-astra-cpa")
        self.assertEqual(row["lean_cls"], {"market": "ah", "side": "home", "line": -0.5})

    def test_11b_missing_lean_or_model_omits_the_keys(self):
        _, output = self._apply(PLAIN_REPORT)
        payload = _final_payload(output)
        self.assertNotIn("lean", payload)
        self.assertNotIn("writer_model", payload)
        _, invalid = self._apply(_with_lean("C03 让球 Arsenal -0.75｜置信度 70 / 100"), "m")
        self.assertNotIn("lean", _final_payload(invalid))

    def test_11c_selection_log_carries_lean_id_and_conf(self):
        selection = decisions.select_decision(LEAN_REPORT, min_confidence=None, use_jev=False)
        with self.assertLogs(decisions.log, level="INFO") as captured:
            decisions.log_selection(LEAN_REPORT, selection)
        record = json.loads("\n".join(captured.output).split("TYPESAFE_DECISION ", 1)[1])
        self.assertEqual((record["lean_id"], record["lean_conf"]), ("C01", 58))
        self.assertNotIn("影子倾向", "\n".join(captured.output))
        with self.assertLogs(decisions.log, level="INFO") as captured:
            decisions.log_selection(PLAIN_REPORT, decisions.select_decision(
                PLAIN_REPORT, min_confidence=None, use_jev=False))
        record = json.loads("\n".join(captured.output).split("TYPESAFE_DECISION ", 1)[1])
        self.assertEqual((record["lean_id"], record["lean_conf"]), ("", None))

    def test_11d_finalize_passes_writer_model_through(self):
        with patch.object(config, "TYPESAFE_DECISION_MODE", "active"), \
             patch.object(config, "TYPESAFE_DECISION_MIN_CONFIDENCE", 0.8), \
             patch.object(config, "EXPLORE_MODE", "off"), \
             patch.object(decisions, "_request_choice_result", return_value=(None, "missing_api_key")):
            output = analyzer._finalize_with_focus(LEAN_REPORT, "Arsenal", "Chelsea", "英超", "",
                                                   writer_model="claude-opus-5-5-high")
        payload = _final_payload(output)
        self.assertEqual(payload["writer_model"], "claude-opus-5-5-high")
        self.assertEqual(payload["lean"]["id"], "C01")


def _table(plays):
    rows = [candidates.Candidate(play, 1.9, 0.5, 0.01, True) for play in plays]
    return candidates.CandidateTable(node="即时", bookmaker="Pinnacle", snapshot="12:00",
                                     rows=rows, lam_total=2.6)


def _prompt_system(extra=""):
    with patch.object(analyzer, "load_rules", return_value="规则"):
        system, _user = analyzer._analyze_prompts("", "基本面", "Arsenal", "Chelsea", "英超", extra)
    return system


class TestSwitchAndPrompts(unittest.TestCase):
    def test_12_mode_off_disables_everything(self):
        with patch.object(config, "SHADOW_LEAN_MODE", "off"):
            selection = decisions.select_decision(LEAN_REPORT, min_confidence=None, use_jev=False)
            self.assertIsNone(selection.lean)
            self.assertNotIn("lean", _final_payload(decisions.apply_selection(LEAN_REPORT, selection)))
            self.assertNotIn("影子倾向", _prompt_system())
            self.assertNotIn("影子倾向", candidates.format_table(_table(["让球 H -0.5", "让球 A +0.5"])))
            self.assertNotIn(multi_analyzer.SHADOW_LEAN_MISSING,
                             multi_analyzer._basic_report_issues(PLAIN_REPORT))
            self.assertEqual(multi_analyzer._shadow_lean_note(), "")
        self.assertIn("影子倾向", _prompt_system())

    def test_13_format_table_hint(self):
        text = candidates.format_table(_table(["让球 H -0.5", "让球 A +0.5", "胜平负 H 胜"]))
        self.assertIn("- 影子倾向（第 7 节）只能在 C01 / C02 中选一个（让球主线两侧）", text)
        self.assertLess(text.index("影子倾向"), text.index("可在表末追加"))
        missing = candidates.format_table(_table(["胜平负 H 胜", "胜平负 平局", "大小球 大 2.5"]))
        self.assertIn("- 影子倾向（第 7 节）：本场缺亚盘主线，写「无（缺亚盘主线）」", missing)
        self.assertNotIn("C01 / C02", missing)

    def test_13b_build_table_puts_main_handicap_first(self):
        csv_text = make_csv(
            [("即时", -0.5, 1.93, 1.95), ("即时", -0.75, 2.20, 1.70)],
            [("即时", 2.10, 3.40, 3.60)])
        table = candidates.build_table(csv_text, "Serie A", "H", "A")
        self.assertTrue(all(row.play.startswith("让球 ") for row in table.rows[:2]))
        self.assertIn("只能在 C01 / C02 中选一个", candidates.format_table(table))

    def test_14_missing_line_is_minor_only(self):
        issues = multi_analyzer._basic_report_issues(PLAIN_REPORT)
        self.assertIn("第 7 节缺「影子倾向」行", issues)
        self.assertNotIn("第 7 节缺「影子倾向」行", multi_analyzer._basic_report_issues(LEAN_REPORT))
        split = multi_analyzer._split_severity(issues, [])
        self.assertNotIn(multi_analyzer.SHADOW_LEAN_MISSING,
                         [b["problem"] for b in split["blockers"]])
        self.assertIn(multi_analyzer.SHADOW_LEAN_MISSING,
                      [m["problem"] for m in split["minors"]])
        audit = multi_analyzer._audit_result(
            [multi_analyzer.SHADOW_LEAN_MISSING], {"ok": True, "issues": []}, {}, "x")
        self.assertTrue(audit["ok"])
        self.assertEqual(multi_analyzer._audit_blockers(audit), [])

    def test_14b_dropping_the_lean_line_does_not_reject_a_repair(self):
        long_original = LEAN_REPORT + "证据。" * 200
        repaired = PLAIN_REPORT + "证据。" * 200
        self.assertTrue(multi_analyzer._accept_repair(long_original, repaired, {}))

    def test_14c_audit_and_synthesis_prompts_explain_the_lean_line(self):
        with patch.object(multi_analyzer, "_call_route",
                          return_value=({"ok": True, "issues": []}, {})) as route:
            multi_analyzer._audit_report({}, LEAN_REPORT, [])
        audit_system = route.call_args.args[0]
        self.assertIn("影子倾向", audit_system)
        self.assertIn("不属于方向越权", audit_system)
        bundle = {"csv_text": "", "fundamentals": "", "home": "Arsenal", "away": "Chelsea",
                  "league": "英超", "extra_instruction": ""}
        with patch.object(analyzer, "load_rules", return_value="规则"):
            _system, user, _ = multi_analyzer._synthesis_prompt(bundle, [])
        self.assertIn("不属于方向越权", user.split("===== 精算合并要求 =====", 1)[1])

    def test_18_system_is_identical_with_and_without_focus(self):
        plain = _prompt_system()
        focused = _prompt_system("激进一点，重点看大小球")
        self.assertEqual(plain, focused)
        self.assertIn(analyzer.SHADOW_LEAN_INSTRUCTION, plain)


class TestClassifyPlay(unittest.TestCase):
    def test_15_candidate_handicap_formats(self):
        cases = [
            ("让球 Arsenal -0.5", "Arsenal", "Chelsea", "home", -0.5),
            ("让球 Chelsea +0.25", "Arsenal", "Chelsea", "away", 0.25),
            ("让球 Arsenal 0", "Arsenal", "Chelsea", "home", 0.0),
            ("让球 Arsenal −0.75", "Arsenal", "Chelsea", "home", -0.75),
            ("让球 Schalke 04 -1", "Schalke 04", "Hannover 96", "home", -1.0),
            ("让球 Hannover 96 +1", "Schalke 04", "Hannover 96", "away", 1.0),
            ("让球 Manchester City -0.5", "Manchester United", "Manchester City", "away", -0.5),
        ]
        for play, home, away, side, line in cases:
            with self.subTest(play=play):
                self.assertEqual(bt.classify_play(play, home, away),
                                 {"market": "ah", "side": side, "line": line})


def _sample(result, odds, conf=60, date="2026-12-01", model="m"):
    return {"date": date, "path": date, "model": model, "conf": conf,
            "odds": odds, "result": result}


class TestLeanBacktest(unittest.TestCase):
    def test_16_core_stats_match_hand_calculation(self):
        samples = [_sample(1.0, 1.90), _sample(-0.5, 1.95), _sample(0.0, 2.00),
                   _sample(-1.0, 1.85)]
        s = bt.lean_summary(samples)
        self.assertEqual((s["n"], s["win"], s["lose"], s["push"]), (4, 1, 2, 1))
        self.assertAlmostEqual(s["hit"], 1 / 3)
        self.assertAlmostEqual(s["profit"], 0.9 - 0.5 + 0.0 - 1.0)
        self.assertAlmostEqual(s["roi"], -0.15)
        self.assertAlmostEqual(bt.lean_summary([_sample(0.5, 1.90)])["roi"], 0.45)   # 半赢

    def test_16b_samples_settle_from_the_lean_side(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            (cache / "2026-12-01.json").write_text(json.dumps([{
                "id": 1, "date": "2026-12-01", "status": "FT", "home": "Arsenal",
                "away": "Chelsea", "gh": 1, "ga": 1, "ft": {"home": 1, "away": 1}}]),
                encoding="utf-8")
            base = {"kickoff_date": "2026-12-01", "home": "Arsenal", "away": "Chelsea",
                    "writer_model": "m"}
            rows = [
                {**base, "path": "report/visitors/1/x_report.md",
                 "lean": {"id": "C02", "play": "让球 Chelsea +0.25", "odds": 1.90, "conf": 60},
                 "lean_cls": {"market": "ah", "side": "away", "line": 0.25}},
                {**base, "path": "report/2026/12/2026-12-01/x_report.md",
                 "lean": {"id": "C02", "play": "让球 Chelsea +0.25", "odds": 1.90, "conf": 60},
                 "lean_cls": {"market": "ah", "side": "away", "line": 0.25}},
            ]
            samples, info = bt.lean_samples(rows, cache)
        self.assertEqual(info["dup"], 1)                       # 管理员 + 访客同场只算一份
        self.assertEqual(len(samples), 1)
        self.assertNotIn("visitors", samples[0]["path"])
        self.assertEqual(samples[0]["result"], 0.5)            # 1:1 受让 +0.25 → 半赢
        self.assertAlmostEqual(bt.lean_summary(samples)["roi"], 0.45)

    def test_16c_threshold_suggestion_and_verdict(self):
        self.assertEqual(bt.suggest_threshold([50] * 40 + [60] * 18 + [70] * 2), 60)
        self.assertEqual(bt.suggest_threshold([50] * 30 + [65] * 12 + [70] * 18), 70)
        self.assertEqual(bt.suggest_threshold([50] * 60), 60)
        wins = [_sample(1.0, 1.90, date=f"2026-12-{i % 28 + 1:02d}") for i in range(100)]
        self.assertEqual(bt.hconf01_verdict(wins)[0], "达标")
        losses = [_sample(-1.0, 1.90) for _ in range(60)]
        self.assertEqual(bt.hconf01_verdict(losses)[0], "否决")
        verdict, unmet, _ = bt.hconf01_verdict(wins[:30])
        self.assertEqual(verdict, "继续观察")
        self.assertIn("达标·≥T 组 ≥100 场", unmet)
        # 99 输 1 大赢：ROI 为正，但命中率上限 < 50% → 否决
        lucky = [_sample(-1.0, 1.90) for _ in range(99)] + [_sample(1.0, 200.0)]
        self.assertEqual(bt.hconf01_verdict(lucky)[0], "否决")
        # 命中 48%、靠一笔 @20 的大赢把 ROI 拉正：去掉最大一笔后为负 → 不能达标
        carried = ([_sample(1.0, 20.0)]
                   + [_sample(1.0 if i % 2 else -1.0, 1.90) for i in range(94)]
                   + [_sample(-1.0, 1.90) for _ in range(5)])
        verdict, unmet, detail = bt.hconf01_verdict(carried)
        self.assertGreater(detail["summary"]["roi"], 0)
        self.assertEqual(verdict, "继续观察")
        self.assertIn("达标·去掉盈利最大一笔后 ROI > 0", unmet)

    def _rows_and_cache(self, tmp, n, conf=lambda i: 50 + i % 30):
        from datetime import date, timedelta
        cache = Path(tmp)
        rows = []
        for i in range(n):
            day = (date(2026, 12, 1) + timedelta(days=i)).isoformat()
            (cache / f"{day}.json").write_text(json.dumps([{
                "id": i, "date": day, "status": "FT", "home": "Arsenal", "away": "Chelsea",
                "gh": i % 3, "ga": 1, "ft": {"home": i % 3, "away": 1}}]), encoding="utf-8")
            rows.append({"path": f"report/{day}/Arsenal_vs_Chelsea_report.md",
                         "kickoff_date": day, "home": "Arsenal", "away": "Chelsea",
                         "has_final": True, "writer_model": "gpt-6-astra-cpa",
                         "lean": {"id": "C01", "play": "让球 Arsenal -0.5", "odds": 1.93,
                                  "conf": conf(i)},
                         "lean_cls": {"market": "ah", "side": "home", "line": -0.5},
                         "lean_line": "pick"})
        return rows, cache

    def test_17_calibration_output_hides_hit_rate_and_roi(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows, cache = self._rows_and_cache(tmp, 5)
            md = "\n".join(bt.build_lean_md(rows, cache, threshold=0))
            self.assertIn("定线期：还差 55 场", md)
            rows, cache = self._rows_and_cache(tmp, 70)
            md_full = "\n".join(bt.build_lean_md(rows, cache, threshold=0))
            self.assertIn("建议检验线 T =", md_full)
            tested = "\n".join(bt.build_lean_md(rows, cache, threshold=65, since="2026-12-20"))
        for text in (md, md_full):
            self.assertNotIn("命中率", text)
            self.assertNotIn("ROI", text)
            self.assertNotIn("roi", text.lower())
        self.assertIn("H-CONF01", tested)
        self.assertIn("**结论：", tested)
        self.assertIn("仅作描述，不据此调整检验线", tested)

    def test_17b_no_samples_finishes_normally(self):
        md = "\n".join(bt.build_lean_md([{"path": "p", "has_final": False, "lean": None,
                                           "lean_line": None, "writer_model": ""}],
                                         Path("missing-cache-dir"), threshold=0))
        self.assertIn("暂无样本", md)

    def test_17c_extract_reads_only_decision_final(self):
        # 正文里有影子倾向行、但没有 decision_final（非 active 定稿）→ 不入样本，只计诊断
        row = bt.extract_one("report/x/Arsenal_vs_Chelsea_report.md", LEAN_REPORT)
        self.assertIsNone(row["lean"])
        self.assertEqual(row["lean_line"], "pick")
        self.assertFalse(row["has_final"])


REVIEW_TEXT = LEAN_REPORT.replace("### 7. 最终精算结论", "### 6. 研判结论")


class TestPublishStripping(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from bot import ghost_publish, tgbot, wechat_publish
        cls.ghost, cls.tgbot, cls.wechat = ghost_publish, tgbot, wechat_publish

    def _final(self):
        selection = decisions.select_decision(LEAN_REPORT, min_confidence=None, use_jev=False,
                                              writer_model="m")
        return decisions.apply_selection(LEAN_REPORT, selection)

    def test_19_publish_strips_but_display_keeps(self):
        final = self._final()
        shown = analyzer.prepare_report_for_display(final)
        published = analyzer.prepare_report_for_publish(final)
        self.assertIn("影子倾向", shown)
        self.assertNotIn("影子倾向", published)
        for text in (shown, published):
            self.assertNotIn("decision_final", text)
            self.assertNotIn('"lean"', text)
        self.assertIn("影子倾向", self.tgbot._md_to_tg(final))   # TG 照常显示

    def test_20_ghost_and_wechat_bodies_have_no_lean_line(self):
        with patch.object(self.ghost, "_render",
                          side_effect=lambda text: "<p>" + html.escape(text) + "</p>") as render, \
             patch.object(analyzer, "fan_fundamentals_brief", return_value=""), \
             patch.object(analyzer, "seo_summarize", return_value=(None, None)):
            _title, body, *_ = self.ghost.report_to_post(self._final(), title="测试")
        rendered = "\n".join(call.args[0] for call in render.call_args_list)
        self.assertIn("关键依据", rendered)                     # 第 7 节其余内容仍在付费区
        self.assertNotIn("影子倾向", rendered)
        self.assertNotIn("影子倾向", body)
        article = {"title": "赛前看点", "sections": [{"heading": "近况", "text": "主队稳定。"}]}
        with patch.object(analyzer, "wx_compliant_article", return_value=article) as generate:
            self.wechat.report_to_wx_article(REVIEW_TEXT, "Arsenal", "Chelsea", "英超")
        self.assertIn("关键依据", generate.call_args.args[0])   # 无第 7 节标题时整篇喂入
        self.assertNotIn("影子倾向", generate.call_args.args[0])


class _Breaker:
    def allow(self):
        return True

    def record(self, ok):
        self.ok = ok


_EP = {"label": "test", "route_group": "g", "base_url": "https://example.invalid", "key": "k"}


class TestActualModel(unittest.TestCase):
    def _common(self):
        return [
            patch.object(llm_client, "available", return_value=True),
            patch.object(llm_client, "get_settings", return_value={
                "non_stream_timeout": 5, "max_retries": 0,
                "stream_first_byte_timeout": 5, "stream_idle_timeout": 5}),
            patch.object(llm_client, "_endpoints_by_priority", return_value=[(0, _EP)]),
            patch.object(llm_client, "_breakers", [_Breaker()]),
            patch.object(llm_client, "_log_usage"),
        ]

    def _run(self, patches, fn):
        from contextlib import ExitStack
        with ExitStack() as stack:
            for item in self._common() + patches:
                stack.enter_context(item)
            return fn()

    def test_chat_records_model_after_failover(self):
        metrics = {}
        results = iter([("", False, "boom", False, False, False),
                        ("正文", True, "", False, False, False)])
        out = self._run([
            patch.object(llm_client, "resolve_model_chain", return_value=["m1", "m2"]),
            patch.object(llm_client, "_limited_chat", side_effect=lambda *a, **k: next(results)),
        ], lambda: llm_client.chat("s", "u", input_metrics=metrics))
        self.assertEqual(out, "正文")
        self.assertEqual(metrics["actual_model"], "m2")

    def test_chat_failure_does_not_record(self):
        metrics = {}
        self._run([
            patch.object(llm_client, "resolve_model_chain", return_value=["m1"]),
            patch.object(llm_client, "_limited_chat",
                         return_value=("", False, "boom", False, False, False)),
        ], lambda: llm_client.chat("s", "u", input_metrics=metrics))
        self.assertNotIn("actual_model", metrics)

    def test_chat_model_records_model(self):
        metrics = {}
        self._run([patch.object(llm_client, "_limited_chat",
                                return_value=("正文", True, "", False, False, False))],
                  lambda: llm_client.chat_model("s", "u", "grok-4.6", input_metrics=metrics))
        self.assertEqual(metrics["actual_model"], "grok-4.6")

    def test_streams_record_model_before_done_is_yielded(self):
        seen = {}

        def stream(*args, **kwargs):
            return iter([("delta", "正"), ("done", "正文")])

        def consume(gen, metrics):
            for kind, _payload in gen:
                if kind == "done":
                    seen.setdefault("at_done", []).append(metrics.get("actual_model"))

        metrics_a, metrics_b = {}, {}
        self._run([
            patch.object(llm_client, "resolve_model_chain", return_value=["m-stream"]),
            patch.object(llm_client, "_limited_stream", side_effect=stream),
        ], lambda: (consume(llm_client.stream_chat("s", "u", input_metrics=metrics_a), metrics_a),
                    consume(llm_client.stream_chat_model("s", "u", "m-explicit",
                                                         input_metrics=metrics_b), metrics_b)))
        self.assertEqual(seen["at_done"], ["m-stream", "m-explicit"])

    def test_analyze_passes_actual_model_to_finalize(self):
        def fake_call(system, user, effort, **kwargs):
            kwargs["input_metrics"]["actual_model"] = "claude-opus-5-5-high"
            return LEAN_REPORT

        with patch.object(analyzer, "available", return_value=True), \
             patch.object(analyzer, "load_rules", return_value="规则"), \
             patch.object(analyzer, "_call_llm", side_effect=fake_call), \
             patch.object(analyzer, "finalize_decision", side_effect=lambda r, *a, **k: r) as fin:
            analyzer.analyze("", "基本面", "Arsenal", "Chelsea", "英超")
        self.assertEqual(fin.call_args.kwargs.get("writer_model"), "claude-opus-5-5-high")

    def test_analyze_stream_passes_actual_model_to_finalize(self):
        def fake_stream(system, user, effort="", **kwargs):
            kwargs["input_metrics"]["actual_model"] = "gpt-6-astra-cpa"
            yield ("done", LEAN_REPORT)

        with patch.object(analyzer, "available", return_value=True), \
             patch.object(analyzer, "load_rules", return_value="规则"), \
             patch.object(analyzer, "_stream_llm", side_effect=fake_stream), \
             patch.object(analyzer, "finalize_decision", side_effect=lambda r, *a, **k: r) as fin:
            list(analyzer.analyze_stream("", "基本面", "Arsenal", "Chelsea", "英超"))
        self.assertEqual(fin.call_args.kwargs.get("writer_model"), "gpt-6-astra-cpa")


if __name__ == "__main__":
    unittest.main()
