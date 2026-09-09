"""微信公众号合规提示与源稿保留的纯逻辑回归测试。

运行：python -m unittest tests.test_wechat_publish -v
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import wechat_publish as wp   # noqa: E402
from bot import analyzer   # noqa: E402


class TestComplianceScan(unittest.TestCase):
    def test_normal_football_phrases_are_allowed(self):
        samples = [
            "主队最终一球小胜",
            "客队净胜两球",
            "这是一场足球半决赛",
            "加拿大球员状态出色",
            "这名小球员完成破门",
            "他在场上盘带突破",
            "球队维持一球领先优势",
        ]
        for text in samples:
            with self.subTest(text=text):
                self.assertEqual(wp._compliance_scan(text), [])

    def test_real_market_context_is_reported_without_exception(self):
        samples = [
            "主让一球",
            "一球盘低水",
            "一球/球半",
            "看好大球",
            "上盘方向",
            "临场盘口变化",
            "赔率持续下调",
        ]
        for text in samples:
            with self.subTest(text=text):
                findings = wp._compliance_scan(("正文第1节内容", text))
                self.assertTrue(findings)
                self.assertEqual(findings[0]["location"], "正文第1节内容")
                self.assertGreaterEqual(findings[0]["column"], 1)

    def test_scan_returns_term_line_column_and_context(self):
        findings = wp._compliance_scan(
            ("正文第2节内容", "首行正常\n随后提到平博赔率变化"))
        self.assertEqual(
            [(item["term"], item["line"], item["column"])
             for item in findings],
            [("平博", 2, 5), ("赔率", 2, 7)])
        self.assertTrue(all("平博赔率" in item["context"] for item in findings))

        warning = wp.compliance_warning_text(findings)
        self.assertIn("不拦截，草稿已保存", warning)
        self.assertIn("正文第2节内容 第2行第5-6字：平博", warning)


class TestWechatArticleJson(unittest.TestCase):
    @staticmethod
    def _valid_json() -> str:
        return (
            '{"title":"测试文章","subtitle":"副标题","lead":"导语",'
            '"body":[{"heading":"比赛看点","text":"第一段\\n\\n第二段"}],'
            '"compare":[],"highlights":[],'
            '"prediction":{"score":"1-0","note":"主队状态更稳"}}')

    def test_parser_repairs_raw_newline_trailing_commas_and_extra_text(self):
        raw = (
            "以下是结果：\n```json\n"
            '{"title":"测试文章","body":[{"heading":"看点",'
            '"text":"第一段\n第二段",}],}\n```\n完成')
        obj, error = analyzer._parse_wx_article_json(raw)
        self.assertEqual(error, "")
        self.assertEqual(obj["title"], "测试文章")
        self.assertEqual(obj["body"][0]["text"], "第一段\n第二段")

    def test_invalid_json_is_repaired_once_by_llm(self):
        malformed = '{"title":"测试文章","body":[{"text":"未闭合'
        errors = []
        with (
            patch.object(analyzer, "available", return_value=True),
            patch.object(analyzer, "_call_llm",
                         side_effect=[malformed, self._valid_json()]) as call_llm,
        ):
            result = analyzer.wx_compliant_article(
                "基本面正文", "主队", "客队", "测试联赛",
                error_out=errors)
        self.assertEqual(call_llm.call_count, 2)
        self.assertEqual(errors, [])
        self.assertEqual(result["title"], "测试文章")
        self.assertEqual(result["sections"][0]["text"], "第一段\n\n第二段")

    def test_repair_failure_returns_precise_reason(self):
        malformed = '{"title":"测试文章","body":['
        errors = []
        with (
            patch.object(analyzer, "available", return_value=True),
            patch.object(analyzer, "_call_llm",
                         side_effect=[malformed, "仍然不是 JSON"]),
        ):
            result = analyzer.wx_compliant_article(
                "基本面正文", "主队", "客队", "测试联赛",
                error_out=errors)
        self.assertIsNone(result)
        self.assertEqual(len(errors), 1)
        self.assertIn("JSON 格式不合法", errors[0])
        self.assertIn("自动修复后仍无法解析", errors[0])


class TestGeneratedArticleRecovery(unittest.TestCase):
    @staticmethod
    def _result(title: str, body: str) -> dict:
        return {
            "title": title,
            "subtitle": "",
            "lead": "",
            "sections": [{"heading": "比赛看点", "text": body}],
            "compare": [],
            "highlights": [],
            "prediction": {"score": "1-0", "note": "主队或以一球优势取胜"},
        }

    def test_one_goal_article_can_be_rendered(self):
        result = self._result("一球之差", "主队可能凭借一球小胜。")
        findings = []
        with patch("bot.analyzer.wx_compliant_article", return_value=result):
            title, content = wp.report_to_wx_article(
                "基本面正文", "主队", "客队", "测试联赛",
                compliance_findings=findings)
        self.assertEqual(title, "一球之差")
        self.assertIn("一球小胜", content)
        self.assertEqual(findings, [])

    def test_flagged_article_is_returned_for_draft_with_findings(self):
        result = self._result("盘口变化", "正文已经生成，但含有明确盘口术语。")
        findings = []
        with patch("bot.analyzer.wx_compliant_article", return_value=result):
            title, content = wp.report_to_wx_article(
                "基本面正文", "主队", "客队", "测试联赛",
                compliance_findings=findings)
        self.assertEqual(title, "盘口变化")
        self.assertIn("正文已经生成", content)
        self.assertIn("标题", {item["location"] for item in findings})
        self.assertIn("正文第1节内容",
                      {item["location"] for item in findings})
        self.assertIn("盘口", {item["term"] for item in findings})

    def test_generation_error_uses_precise_analyzer_reason(self):
        def failed_generation(*_args, error_out=None, **_kwargs):
            error_out.append(
                "LLM 返回的文章 JSON 格式不合法；自动修复后仍无法解析")
            return None

        with patch("bot.analyzer.wx_compliant_article",
                   side_effect=failed_generation):
            with self.assertRaises(wp.WechatError) as ctx:
                wp.report_to_wx_article(
                    "基本面正文", "主队", "客队", "测试联赛")
        message = str(ctx.exception)
        self.assertIn("JSON 格式不合法", message)
        self.assertIn("自动修复后仍无法解析", message)
        self.assertNotIn("未配置或返回空", message)

    def test_editable_source_is_versioned_and_utf8(self):
        with tempfile.TemporaryDirectory() as tmp:
            date_dir = os.path.join(
                tmp, "report", "2026", "08", "2026-08-31")
            os.makedirs(date_dir)
            report_path = os.path.join(date_dir, "A_vs_B_report.md")
            saved_path, payload = wp.save_editable_source(
                report_path, "A & B", "<section>一球小胜</section>")
            second_path, _ = wp.save_editable_source(
                report_path, "A & B", "<section>第二次生成</section>")
            self.assertTrue(os.path.isfile(saved_path))
            self.assertTrue(os.path.isfile(second_path))
            self.assertNotEqual(saved_path, second_path)
            self.assertEqual(
                os.path.dirname(saved_path),
                os.path.join(date_dir, "wechat_drafts"))
            with open(saved_path, "rb") as f:
                self.assertEqual(payload, f.read())
            text = payload.decode("utf-8")
            self.assertIn("<title>A &amp; B</title>", text)
            self.assertIn("一球小胜", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
