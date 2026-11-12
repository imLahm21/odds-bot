"""scripts/build_hypothesis_stats.write_back：「后」列写回，含合并行按编号分段。"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import backtest_hypotheses as bt  # noqa: E402
import build_hypothesis_stats as stats  # noqa: E402

TABLE = "\n".join([
    "| 编号 | 触发摘要 | 预测 | 来源 | 机判 | 前样本 | 后 | 状态 |",
    "|------|---------|------|------|------|--------|----|------|",
    "| H-M09 | x | 无方向 | AH | ✓ | 机判触发 493 | 0 | 观察中 |",
    "| H-R3a / H-R3b | x | 上 / 下 | AH | ✓ | — | 0 | 假设 |",
    "| H-S1~S4 | x | 下 / 上 / 上 / 下 | AH | ✓ | — | 0 | 假设 |",
    "| H-T3 / H-T4 | x | 上 / 下 | AH | ✓ | — | 0 | 假设 |",
])


def tally(wins, losses):
    t = bt.Tally()
    for i in range(wins):
        t.add(1, 1.9, f"2026-12-{i + 1:02d}")
    for i in range(losses):
        t.add(-1, 1.9, f"2026-12-{i + 1:02d}")
    return t


def post_column(text, code):
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells[0] == code:
            return cells[6]
    raise KeyError(code)


class TestWriteBack(unittest.TestCase):
    def test_single_and_merged_rows(self):
        out = stats.write_back(TABLE, {
            "H-M09": tally(3, 1), "H-R3b": tally(1, 1), "H-S1": tally(2, 0), "H-S4": tally(0, 1)})
        self.assertEqual(post_column(out, "H-M09"), "4 / 75.0%")
        self.assertEqual(post_column(out, "H-R3a / H-R3b"), "H-R3b 2 / 50.0%")
        self.assertEqual(post_column(out, "H-S1~S4"), "H-S1 2 / 100.0%；H-S4 1 / 0.0%")
        self.assertEqual(post_column(out, "H-T3 / H-T4"), "0")      # 无后样本 → 不动
        for line in out.splitlines()[2:]:
            self.assertEqual(line.count("|"), 9)                     # 列数不变


if __name__ == "__main__":
    unittest.main()
