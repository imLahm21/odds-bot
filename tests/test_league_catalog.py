"""国家队赛事目录回归测试：锁定实测 league_id 与 API season。"""

import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("APIFOOTBALL_KEY", "offline-test-key")

from bot import config, db, fundamentals  # noqa: E402


class TestNationalLeagueCatalog(unittest.TestCase):
    def test_afcon_qualification_is_enabled_as_season_2027(self):
        self.assertEqual(
            config.DEFAULT_ENABLED_LEAGUES[36],
            ("非洲杯预选赛 AFCON Qualifiers", 2027),
        )

    def test_uefa_nations_league_remains_season_2026(self):
        self.assertEqual(config.DEFAULT_ENABLED_LEAGUES[5][1], 2026)

    def test_afcon_qualification_has_search_and_display_names(self):
        self.assertEqual(
            config.LEAGUE_SEARCH_ALIASES["非洲杯预选赛"],
            "Africa Cup of Nations - Qualification",
        )
        self.assertEqual(config.LEAGUE_ZH_NAMES[36], "非洲杯预选赛")

    def test_afcon_qualification_uses_national_team_rules(self):
        self.assertTrue(fundamentals._is_national_team_event(
            36, "Africa Cup of Nations - Qualification"))

    def test_seed_config_adds_it_enabled_to_existing_database(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(db.SCHEMA)
            db.seed_config(conn)
            row = conn.execute(
                "SELECT league_name, season, enabled FROM watched_leagues "
                "WHERE league_id=36").fetchone()
        finally:
            conn.close()
        self.assertEqual(row, ("非洲杯预选赛 AFCON Qualifiers", 2027, 1))


if __name__ == "__main__":
    unittest.main()
