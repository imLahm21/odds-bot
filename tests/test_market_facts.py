"""bot/market_facts + bot/hypotheses + bot/candidates：用手工构造、可心算核对的盘口验证
事实（主线、上盘、欧亚差、变盘、热门方、单向升水）、假设触发与代码候选表。"""

import csv
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import candidates, hypotheses, market_facts  # noqa: E402

HEADER = ["快照时间(CST)", "联赛", "开球时间(CST)", "主队", "客队", "博彩公司", "盘口类型",
          "主胜赔率", "平局赔率", "客胜赔率", "凯利(主胜)", "凯利(平局)", "凯利(客胜)",
          "让球", "主队水位", "客队水位", "凯利(主)", "凯利(客)", "数据更新(CST)"]


def make_csv(ah_path, euro_path, *, book="Pinnacle", extra=()):
    """ah_path: [(节点, 让球, 主水, 客水)]；euro_path: [(节点, 主, 平, 客)]。"""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(HEADER)
    for node, hc, hw, aw in ah_path:
        w.writerow([f"2026-10-17 12:00（{node}）", "Serie A", "", "H", "A", book, "亚盘",
                    "", "", "", "", "", "", hc, hw, aw, "", "", ""])
    for node, ho, do, ao in euro_path:
        w.writerow([f"2026-10-17 12:00（{node}）", "Serie A", "", "H", "A", book, "欧指",
                    ho, do, ao, "", "", "", "", "", "", "", "", ""])
    for row in extra:
        w.writerow(row)
    return buf.getvalue()


def facts_of(text):
    return market_facts.build_facts(market_facts.rows_from_csv(text), "Serie A")


def codes(facts):
    return {t.code: t for t in hypotheses.evaluate(facts)}


class TestFacts(unittest.TestCase):
    def test_main_line_is_closest_to_even(self):
        text = make_csv([("初盘①", -0.25, 1.70, 2.20), ("初盘①", -0.5, 1.95, 1.95),
                         ("初盘①", -0.75, 2.30, 1.65)], [("初盘①", 1.90, 3.50, 4.20)])
        f = facts_of(text)
        self.assertEqual(f.lines[0].handicap, -0.5)
        self.assertEqual(f.upper, "home")

    def test_gap_against_conversion_table(self):
        # 主胜 1.90、上盘净水位 0.90 → 换算表半球（0.5）；实际 -1 → 深于换算 +0.5
        text = make_csv([("初盘①", -1.0, 1.90, 2.00), ("即时", -1.0, 1.90, 2.00)],
                        [("初盘①", 1.90, 3.50, 4.20), ("即时", 1.90, 3.50, 4.20)])
        f = facts_of(text)
        self.assertEqual(f.theory_depth, 0.5)
        self.assertEqual(f.gap_class, "deep")
        self.assertAlmostEqual(f.gap, 0.5)

    def test_moves_phase_heat_and_away_upper(self):
        # 客队让球（主队 +0.5 → +0.75），临场升盘；客胜概率上升 > 3pp → 上盘大热
        text = make_csv([("初盘①", 0.5, 1.95, 1.95), ("中盘③", 0.5, 2.00, 1.90),
                         ("临场②", 0.75, 1.85, 2.05)],
                        [("初盘①", 4.0, 3.6, 1.95), ("临场②", 4.6, 3.8, 1.75)])
        f = facts_of(text)
        self.assertEqual(f.upper, "away")
        self.assertEqual(len(f.moves), 1)
        self.assertEqual(f.moves[0].phase, "late")
        self.assertGreater(f.moves[0].delta, 0)            # 上盘让得更深 = 升盘
        self.assertEqual(f.heat_side, "away")
        self.assertTrue(f.big_heat and f.upper_heat)

    def test_monotone_rise(self):
        text = make_csv([("初盘①", -0.5, 1.85, 2.05), ("初盘②", -0.5, 1.90, 2.00),
                         ("中盘①", -0.5, 1.97, 1.93)], [("初盘①", 1.9, 3.5, 4.2)])
        self.assertTrue(facts_of(text).mono_rise)


class TestHypotheses(unittest.TestCase):
    def test_m07_shallow_no_move_upper_big_heat(self):
        # 主胜 1.60 → 换算 半一(0.75)；实际 -0.25 → 浅于换算；全程不变盘；主胜概率大幅上升
        text = make_csv([("初盘①", -0.25, 1.90, 2.00), ("即时", -0.25, 1.80, 2.10)],
                        [("初盘①", 1.60, 4.0, 5.5), ("即时", 1.45, 4.4, 6.5)])
        hits = codes(facts_of(text))
        self.assertIn("H-M07", hits)
        self.assertEqual((hits["H-M07"].market, hits["H-M07"].side), ("ah", "lower"))
        self.assertNotIn("H-M09", hits)

    def test_m09_aligned_no_move_has_no_direction(self):
        text = make_csv([("初盘①", -0.5, 1.90, 2.00), ("即时", -0.5, 1.92, 1.98)],
                        [("初盘①", 1.90, 3.50, 4.20), ("即时", 1.90, 3.50, 4.20)])
        hits = codes(facts_of(text))
        self.assertIn("H-M09", hits)
        self.assertEqual(hits["H-M09"].market, "none")

    def test_m11_and_r3b_on_aligned_upgrade_with_water_rise(self):
        text = make_csv([("初盘①", -0.5, 1.90, 2.00), ("临场②", -0.75, 2.05, 1.85)],
                        [("初盘①", 1.90, 3.50, 4.20), ("临场②", 1.75, 3.60, 4.80)])
        hits = codes(facts_of(text))
        self.assertIn("H-M11", hits)
        self.assertIn("H-R3b", hits)
        self.assertIn("H-S4", hits)

    def test_registry_status_lookup_and_format(self):
        reg = hypotheses.registry_status()
        self.assertEqual(reg["H-M09"][0], "观察中")
        self.assertEqual(reg["H-K10"][0], "假设")          # 「H-K09~K11」区间展开
        text = hypotheses.format_triggers(
            [hypotheses.Trigger("H-M09", "none", "", "x")], reg)
        self.assertIn("| H-M09 |", text)
        self.assertIn("已验证假设给出的方向：无", text)

    def test_merged_row_prior_is_split_per_code(self):
        cell = "H-R3a 机判 39 / 33.3%（ROI −37.3%）；H-R3b 机判 299 / 49.1%；旧 LLM 自标 5 / 40%"
        self.assertEqual(hypotheses._prior_for("H-R3a", cell), "机判 39 / 33.3%（ROI −37.3%）")
        self.assertEqual(hypotheses._prior_for("H-R3b", cell), "机判 299 / 49.1%；旧 LLM 自标 5 / 40%")
        self.assertEqual(hypotheses._prior_for("H-D03", "H-D02 机判 214 / 47.2%"), "—")
        self.assertEqual(hypotheses._prior_for("H-OU01", "外部 32 场（未复核）"), "外部 32 场（未复核）")


class TestCandidates(unittest.TestCase):
    def test_quarter_line_edges_follow_1x2_states(self):
        euro = [("即时", 2.00, 3.40, 3.80)]
        text = make_csv([("即时", -0.25, 1.95, 1.95)], euro,
                        extra=[["2026-10-17 12:00（即时）", "Serie A", "", "H", "A", "Bet365", "亚盘",
                                "", "", "", "", "", "", -0.25, 1.93, 1.97, "", "", ""],
                               ["2026-10-17 12:00（即时）", "Serie A", "", "H", "A", "Bet365", "欧指",
                                2.05, 3.30, 3.70, "", "", "", "", "", "", "", "", ""]])
        table = candidates.build_table(text, "Serie A", "H", "A")
        self.assertEqual(table.bookmaker, "Pinnacle")      # 意甲首要分析庄
        rows = {r.play: r for r in table.rows}
        home = rows["让球 H -0.25"]
        self.assertEqual(home.odds, 1.95)                  # 取首要分析庄报价，不取最高价
        # 胜平负两庄平均隐含概率去抽水 → 让 -0.25：win=p主, half_lose=p平
        inv = [(1 / 2.00 + 1 / 2.05) / 2, (1 / 3.40 + 1 / 3.30) / 2, (1 / 3.80 + 1 / 3.70) / 2]
        ph, pd, pa = (x / sum(inv) for x in inv)
        self.assertAlmostEqual(home.edge, ph * 0.95 - pd * 0.5 - pa, places=9)
        self.assertIn("胜平负 平局", rows)
        block = candidates.format_table(table)
        self.assertIn("| ID | 玩法 | 赔率 | 门槛=1/赔率 | p_市场 | p_最终 | edge | 资格 |", block)

    def test_deep_line_without_totals_cannot_be_priced(self):
        text = make_csv([("即时", -1.0, 1.95, 1.95)], [("即时", 1.40, 4.8, 7.5)])
        table = candidates.build_table(text, "Serie A")
        rows = [r for r in table.rows if r.play.startswith("让球")]
        self.assertTrue(rows and all(r.edge is None and not r.eligible for r in rows))


if __name__ == "__main__":
    unittest.main()


class TestConsistentLines(unittest.TestCase):
    def test_mispaired_line_is_dropped_before_main_line(self):
        # −0.5 主队约 42%，−1.5 却约 55%：后者自相矛盾（疑似配对错位），不得被选为主线
        text = make_csv([("即时", -0.5, 2.15, 1.60), ("即时", -1.5, 1.67, 2.04),
                         ("即时", 0.0, 1.75, 2.10)], [("即时", 2.6, 3.4, 2.7)])
        quotes = [q for q in market_facts.rows_from_csv(text) if q.market == "ah"]
        kept = {q.handicap for q in market_facts.consistent_lines(quotes)}
        self.assertEqual(kept, {-0.5, 0.0})
        self.assertNotEqual(market_facts.main_line(quotes).handicap, -1.5)


class TestLegacyAhSign(unittest.TestCase):
    """2026-09-06 16:50Z 之前入库的亚盘行让球符号相反（parser 旧 bug），读库时须翻回。"""

    def _conn(self, rows):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE odds_history (fixture_id INT, snapshot_utc TEXT, node_label TEXT, "
                     "bookmaker_id INT, bookmaker TEXT, market TEXT, home_odds REAL, draw_odds REAL, "
                     "away_odds REAL, kelly_home REAL, kelly_draw REAL, kelly_away REAL, handicap REAL, "
                     "home_water REAL, away_water REAL)")
        for snap, node, market, hc, hw, aw in rows:
            conn.execute("INSERT INTO odds_history VALUES (1, ?, ?, 4, 'Pinnacle', ?, NULL, NULL, NULL, "
                         "NULL, NULL, NULL, ?, ?, ?)", (snap, node, market, hc, hw, aw))
        return conn

    def test_pre_fix_rows_are_flipped_and_merged_with_post_fix(self):
        conn = self._conn([
            ("2026-09-06T10:00:00Z", "初盘①", "ah", 0.75, 1.90, 2.00),   # 旧 bug：实为主让 −0.75
            ("2026-09-06T10:00:00Z", "初盘①", "ou", 2.5, 1.90, 2.00),    # 大小球不受影响
            ("2026-09-06T17:00:00Z", "初盘①", "ah", -0.75, 1.95, 1.95),  # 修复后同一条线
        ])
        quotes = market_facts.rows_from_db(conn, 1)
        ah = [q for q in quotes if q.market == "ah"]
        self.assertEqual([(q.handicap, q.home_water) for q in ah], [(-0.75, 1.95)])
        self.assertEqual([q.handicap for q in quotes if q.market == "ou"], [2.5])

    def test_pre_fix_only_row_is_corrected(self):
        conn = self._conn([("2026-08-20T10:00:00Z", "初盘①", "ah", -0.5, 2.10, 1.80)])
        self.assertEqual([q.handicap for q in market_facts.rows_from_db(conn, 1)], [0.5])
