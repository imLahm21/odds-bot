"""用户展示不暴露资格字段，内部决策与原始归档保持完整。"""

import html
import importlib
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from bot import report_display


REPORT = """## 比赛：Home vs Away
### 1. 基本面
资格赛球队近况保持稳定。
### 7. 最终精算结论
- **置信度**：72 / 100
### 8. 投注决策
| ID | 玩法 | 赔率 | 门槛=1/赔率 | p_市场 | p_最终 | edge | 资格 |
|----|------|------|-------------|--------|--------|------|------|
| C01 | 主胜 | 2.00 | 50% | 52% | 54% | +8.0% | eligible |
| C02 | 客胜 | 3.00 | 33.3% | 40% | 40% | +20.0% | excluded |
- **选中候选 ID**：C01
- **选中项**：主胜
- **证据强度与凯利分数**：中，k=1/4
- **注额**：$2.0
"""


class TestEligibilityDisplay(unittest.TestCase):
    def test_hides_status_column_preserves_numbers_and_marks_excluded_row(self):
        visible = report_display.hide_eligibility_column(REPORT)
        self.assertNotIn("| 资格 |", visible)
        self.assertNotIn("eligible", visible)
        self.assertNotIn("excluded", visible)
        self.assertIn("| C01 | 主胜 | 2.00 | 50% | 52% | 54% | +8.0% |", visible)
        self.assertIn("| C02 | 客胜（已排除） | 3.00 | 33.3% | 40% | 40% | +20.0% |", visible)
        self.assertIn("资格赛球队近况保持稳定。", visible)
        self.assertIn("- **选中候选 ID**：C01", visible)
        self.assertEqual(report_display.hide_eligibility_column(visible), visible)

    def test_status_column_can_appear_at_any_position(self):
        base_headers = ["玩法", "赔率", "edge"]
        base_values = ["大球", "1.91", "−2.0%"]
        for position in range(4):
            headers, values = list(base_headers), list(base_values)
            headers.insert(position, "**Eligibility**")
            values.insert(position, "**ELIGIBLE**")
            report = "\n".join("| " + " | ".join(row) + " |" for row in (
                headers, ["---"] * 4, values))
            with self.subTest(position=position):
                self.assertEqual(report_display.hide_eligibility_column(report),
                                 "| 玩法 | 赔率 | edge |\n| --- | --- | --- |\n| 大球 | 1.91 | −2.0% |")

    def test_multiple_tables_and_escaped_pipes_keep_their_structure(self):
        extra = """\n```markdown
| Selection | Decimal odds | edge | eligibility |
| :--- | ---: | :---: | --- |
| Home \\| Draw | 1.90 | +1.0% | invalid |
```
"""
        visible = report_display.hide_eligibility_column(REPORT + extra)
        self.assertNotIn("eligible", visible)
        self.assertNotIn("eligibility", visible)
        self.assertIn("| Home \\| Draw（已排除） | 1.90 | +1.0% |", visible)
        self.assertIn("| :--- | ---: | :---: |", visible)
        self.assertIn("```markdown", visible)
        self.assertTrue(visible.endswith("```\n"))

    def test_legacy_tables_and_unrelated_qualification_tables_are_unchanged(self):
        for report in (
            "| 玩法 | 赔率 | edge |\n| --- | --- | --- |\n| 主胜 | 2.00 | +8.0% |\n",
            "| 球队 | 资格 |\n| --- | --- |\n| Home | eligible |\n",
            "这是一场资格赛，正文不应被重写。\n",
            "",
        ):
            with self.subTest(report=report):
                self.assertEqual(report_display.hide_eligibility_column(report), report)

    def test_malformed_table_is_not_truncated_or_reinterpreted(self):
        report = "| 玩法 | 赔率 | edge | 资格 |\n| --- | --- | --- | --- |\n| 主胜 | 2.00 | +8% | 54% | eligible |\n"
        self.assertEqual(report_display.hide_eligibility_column(report), report)

    def test_crlf_and_final_line_without_newline_are_preserved(self):
        report = "| 玩法 | 赔率 | edge | 资格 |\r\n| --- | --- | --- | --- |\r\n| 主胜 | 2.00 | +8% | eligible |"
        expected = "| 玩法 | 赔率 | edge |\r\n| --- | --- | --- |\r\n| 主胜 | 2.00 | +8% |"
        self.assertEqual(report_display.hide_eligibility_column(report), expected)


class TestReportDisplayOutlets(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 与现有 bot 测试一致：无可选发布依赖时，仅补导入桩，不请求外部服务。
        stubs = {}
        for name in ("jwt", "markdown"):
            try:
                importlib.import_module(name)
            except ModuleNotFoundError:
                stubs[name] = types.ModuleType(name)
        with patch.dict(sys.modules, stubs):
            from bot import analyzer, tgbot, ghost_publish, wechat_publish, typesafe_decisions
        cls.analyzer = analyzer
        cls.tgbot = tgbot
        cls.ghost = ghost_publish
        cls.wechat = wechat_publish
        cls.decisions = typesafe_decisions

    def test_display_hides_internal_comments_but_raw_eligibility_is_preserved(self):
        report = REPORT + '<!-- rules_manifest {"files":[]} -->\n'
        parsed_before = self.decisions.parse_decision_section(report)
        visible = self.analyzer.prepare_report_for_display(report)
        self.assertNotIn("eligible", visible)
        self.assertNotIn("rules_manifest", visible)
        self.assertEqual(self.decisions.parse_decision_section(report), parsed_before)
        self.assertEqual(parsed_before.candidates[1].eligibility, "excluded")
        self.assertIn("| 资格 |", self.analyzer.strip_rules_manifest(report))
        # 即使用户复制展示版再解析，中文排除提示仍可被现有解析器识别。
        parsed_visible = self.decisions.parse_decision_section(visible)
        self.assertEqual(parsed_visible.selected_id, parsed_before.selected_id)
        self.assertEqual(parsed_visible.candidates[1].eligibility, "excluded")
        selected_visible = self.decisions.select_decision(
            visible, min_confidence=None, use_jev=False)
        self.assertEqual(selected_visible.positive_ids, ("C01",))

    def test_telegram_hides_column_on_existing_archived_report_text(self):
        visible = self.tgbot._md_to_tg(REPORT)
        self.assertNotIn("eligible", visible)
        self.assertNotIn("| 资格 |", visible)
        self.assertIn("客胜（已排除）", visible)
        self.assertIn("选中候选 ID：C01", visible)

    def test_ghost_renders_only_the_cleaned_report(self):
        with (
            patch.object(self.ghost, "_render",
                         side_effect=lambda text: "<section>" + html.escape(text) + "</section>") as render,
            patch.object(self.analyzer, "fan_fundamentals_brief", return_value=""),
            patch.object(self.analyzer, "seo_summarize", return_value=(None, None)),
        ):
            _, body, *_ = self.ghost.report_to_post(REPORT, title="测试报告")
        rendered_text = "\n".join(call.args[0] for call in render.call_args_list)
        self.assertNotIn("eligible", rendered_text)
        self.assertNotIn("| 资格 |", rendered_text)
        self.assertIn("客胜（已排除）", body)
        self.assertIn("+8.0%", body)

    def test_wechat_generation_receives_cleaned_legacy_report(self):
        legacy = REPORT.replace("### 7. 最终精算结论", "### 6. 研判结论")
        article = {"title": "赛前看点", "sections": [
            {"heading": "球队近况", "text": "主队状态稳定。"}]}
        with patch.object(self.analyzer, "wx_compliant_article", return_value=article) as generate:
            title, _ = self.wechat.report_to_wx_article(legacy, "Home", "Away", "联赛")
        self.assertEqual(title, "赛前看点")
        self.assertNotIn("eligible", generate.call_args.args[0])
        self.assertNotIn("| 资格 |", generate.call_args.args[0])
        self.assertIn("+8.0%", generate.call_args.args[0])

    def test_raw_archive_and_parlay_contract_keep_full_machine_data(self):
        selection = self.decisions.select_decision(REPORT, min_confidence=None, use_jev=False)
        self.assertEqual(selection.positive_ids, ("C01",))
        final_report = self.decisions.apply_selection(REPORT, selection)
        expected = self.decisions.read_final_decision(final_report)
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(self.tgbot, "_REPORT_ROOT", directory):
                path = self.tgbot._archive_report(
                    {"home": "Home", "away": "Away", "kick_cst": "2026-10-24 20:00"},
                    final_report)
            archived = Path(path).read_text(encoding="utf-8")
        self.assertEqual(archived, final_report)
        self.assertIn("eligible", archived)
        self.assertIn("decision_final", archived)
        with patch.object(self.analyzer, "_extract_decision_llm") as llm:
            self.assertEqual(self.analyzer.extract_decision(archived), expected)
        llm.assert_not_called()
        visible = self.tgbot._md_to_tg(archived)
        self.assertNotIn("eligible", visible)
        self.assertNotIn("decision_final", visible)
        self.assertIn("选中候选 ID：C01", visible)

    def test_generated_no_positive_edge_reason_is_human_readable(self):
        report = REPORT.replace("+8.0%", "−1.0%")
        selection = self.decisions.select_decision(report, min_confidence=None, use_jev=False)
        self.assertEqual(selection.selected_id, "PASS")
        visible = self.tgbot._md_to_tg(self.decisions.apply_selection(report, selection))
        self.assertNotIn("eligible", visible)
        self.assertIn("无符合条件且 edge>0 的候选", visible)


if __name__ == "__main__":
    unittest.main()
