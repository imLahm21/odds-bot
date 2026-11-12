"""db.archive_old：清理前按「每节点最后一行 + 每庄每线最早一行」归档，且可重复执行。"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import db  # noqa: E402


class TestArchiveOld(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(self.path)
        self.conn = db.get_conn(self.path)
        self.conn.execute(
            "INSERT INTO fixtures (fixture_id, league_id, league_name, season, home_team, "
            "away_team, home_team_id, away_team_id, commence_utc) VALUES "
            "(1, 39, 'PL', 2026, 'A', 'B', 10, 20, '2020-01-01T15:00:00Z'),"
            "(2, 39, 'PL', 2026, 'C', 'D', 30, 40, '2999-01-01T15:00:00Z')")
        rows = [
            # fixture 1：同节点同庄同线 3 行（取最后一行），跨两个节点
            (1, "2019-12-29T10:00:00Z", "初盘①", 8, "Bet365", "ah", -0.5, 1.90, 1.95),
            (1, "2019-12-29T12:00:00Z", "初盘①", 8, "Bet365", "ah", -0.5, 1.88, 1.97),
            (1, "2020-01-01T14:50:00Z", "即时", 8, "Bet365", "ah", -0.5, 1.80, 2.05),
            (1, "2019-12-29T10:00:00Z", "初盘①", 8, "Bet365", "btts", None, 1.70, 2.10),
            (2, "2998-12-31T10:00:00Z", "初盘②", 8, "Bet365", "ah", -1.0, 1.90, 1.95),
        ]
        self.conn.executemany(
            "INSERT INTO odds_history (fixture_id, snapshot_utc, node_label, bookmaker_id, "
            "bookmaker, market, handicap, home_water, away_water) VALUES (?,?,?,?,?,?,?,?,?)",
            rows)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + suffix)
            except OSError:
                pass

    def test_archive_keeps_last_per_node_and_first_per_line(self):
        fixtures, _ = db.archive_old(self.conn, days=30)
        self.assertEqual(fixtures, 1)
        got = self.conn.execute(
            "SELECT kind, node_label, market, home_water FROM odds_archive "
            "WHERE fixture_id=1 ORDER BY kind, market, node_label").fetchall()
        self.assertIn(("node_last", "初盘①", "ah", 1.88), got)
        self.assertIn(("node_last", "即时", "ah", 1.80), got)
        self.assertIn(("first", "初盘①", "ah", 1.90), got)
        self.assertIn(("node_last", "初盘①", "btts", 1.70), got)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM odds_archive WHERE fixture_id=2").fetchone()[0], 0)
        meta = self.conn.execute(
            "SELECT home_team, commence_utc FROM fixtures_archive WHERE fixture_id=1").fetchone()
        self.assertEqual(meta, ("A", "2020-01-01T15:00:00Z"))

    def test_archive_is_idempotent_and_survives_cleanup(self):
        db.archive_old(self.conn, days=30)
        before = self.conn.execute("SELECT COUNT(*) FROM odds_archive").fetchone()[0]
        db.archive_old(self.conn, days=30)
        after = self.conn.execute("SELECT COUNT(*) FROM odds_archive").fetchone()[0]
        self.assertEqual(before, after)                   # NULL 盘口线（btts）也不重复
        db.cleanup_old(self.conn, days=30)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM odds_history WHERE fixture_id=1").fetchone()[0], 0)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM odds_archive").fetchone()[0], after)


if __name__ == "__main__":
    unittest.main()
