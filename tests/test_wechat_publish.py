"""微信公众号合规扫描与源稿保留的纯逻辑回归测试。

运行：python -m unittest tests.test_wechat_publish -v
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import wechat_publish as wp   # noqa: E402


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
                wp._compliance_scan(text)

    def test_real_market_context_is_still_blocked(self):
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
                with self.assertRaises(wp.ComplianceError):
                    wp._compliance_scan(text)


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
        with patch("bot.analyzer.wx_compliant_article", return_value=result):
            title, content = wp.report_to_wx_article(
                "基本面正文", "主队", "客队", "测试联赛")
        self.assertEqual(title, "一球之差")
        self.assertIn("一球小胜", content)

    def test_blocked_article_is_attached_to_error(self):
        result = self._result("盘口变化", "正文已经生成，但含有明确盘口术语。")
        with patch("bot.analyzer.wx_compliant_article", return_value=result):
            with self.assertRaises(wp.ComplianceError) as ctx:
                wp.report_to_wx_article(
                    "基本面正文", "主队", "客队", "测试联赛")
        self.assertEqual(ctx.exception.title, "盘口变化")
        self.assertIn("正文已经生成", ctx.exception.content_html)

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
