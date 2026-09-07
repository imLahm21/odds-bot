"""report/<年>/<月>/<日期> 路径规则的纯逻辑测试。"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import report_paths as rp   # noqa: E402


class TestReportPaths(unittest.TestCase):
    def test_canonical_year_month_date_layout(self):
        expected = os.path.join("report", "2026", "08", "2026-08-31")
        self.assertEqual(rp.canonical_date_dir("report", "2026-08-31"),
                         expected)

    def test_non_date_falls_back_to_direct_child(self):
        self.assertEqual(rp.canonical_date_dir("report", "未知日期"),
                         os.path.join("report", "未知日期"))

    def test_resolve_supports_legacy_and_prefers_canonical(self):
        with tempfile.TemporaryDirectory() as tmp:
            legacy = os.path.join(tmp, "2026-08-31")
            canonical = os.path.join(tmp, "2026", "08", "2026-08-31")
            os.makedirs(legacy)
            self.assertEqual(rp.resolve_date_dir(tmp, "2026-08-31"), legacy)
            os.makedirs(canonical)
            self.assertEqual(rp.resolve_date_dir(tmp, "2026-08-31"),
                             canonical)

    def test_iter_finds_nested_and_legacy_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.path.join(tmp, "2026-06-12")
            nested = os.path.join(tmp, "2026", "08", "2026-08-31")
            os.makedirs(old)
            os.makedirs(nested)
            os.makedirs(os.path.join(tmp, "visitors", "123"))
            self.assertEqual(
                rp.iter_date_dirs(tmp),
                [("2026-08-31", nested), ("2026-06-12", old)])

    def test_iter_ignores_mismatched_year_month_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            wrong = os.path.join(tmp, "2025", "08", "2026-08-31")
            os.makedirs(wrong)
            self.assertEqual(rp.iter_date_dirs(tmp), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
