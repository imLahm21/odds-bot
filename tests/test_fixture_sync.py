"""赛程同步回归测试：覆盖占位日期移出短窗口与队名更新。"""

import os
import sqlite3
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("APIFOOTBALL_KEY", "offline-test-key")

from bot import api_client, db, scheduler  # noqa: E402


def _fixture_row(*, commence="2026-09-08T19:00:00+00:00",
                 home="Old Home", away="Old Away"):
    return {
        "fixture_id": 1635606,
        "league_id": 2,
        "league_name": "欧冠 Champions Lg",
        "season": 2026,
        "home_team": home,
        "away_team": away,
        "home_team_id": 575,
        "away_team_id": 541,
        "commence_utc": commence,
        "status": "NS",
    }


class TestFixtureApiQuery(unittest.TestCase):
    @patch.object(api_client, "api_get")
    def test_full_season_query_omits_date_window(self, api_get):
        api_get.return_value = {"response": []}

        api_client.fetch_fixtures(2, 2026)

        api_get.assert_called_once_with(
            "/fixtures", {"league": 2, "season": 2026})

    @patch.object(api_client, "api_get")
    def test_date_window_remains_available(self, api_get):
        api_get.return_value = {"response": []}

        api_client.fetch_fixtures(2, 2026, "2026-09-01", "2026-09-15")

        api_get.assert_called_once_with(
            "/fixtures",
            {"league": 2, "season": 2026,
             "from": "2026-09-01", "to": "2026-09-15"})


class TestFixtureUpsert(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(db.SCHEMA)

    def tearDown(self):
        self.conn.close()

    def test_moved_fixture_updates_date_and_team_names(self):
        db.upsert_fixtures(self.conn, [_fixture_row()])

        db.upsert_fixtures(self.conn, [_fixture_row(
            commence="2026-11-04T17:45:00+00:00",
            home="AEK Athens FC", away="Real Madrid")])

        actual = self.conn.execute(
            "SELECT home_team, away_team, commence_utc FROM fixtures "
            "WHERE fixture_id=1635606").fetchone()
        self.assertEqual(actual, (
            "AEK Athens FC", "Real Madrid", "2026-11-04T17:45:00+00:00"))


class TestTaskAFixtureSync(unittest.TestCase):
    @patch.object(scheduler.time, "sleep")
    @patch.object(scheduler.db, "checkpoint_wal")
    @patch.object(scheduler.db, "cleanup_old", return_value=(0, 0))
    @patch.object(scheduler.db, "upsert_fixtures", return_value=1)
    @patch.object(scheduler.parser, "parse_fixtures_response")
    @patch.object(scheduler.api_client, "fetch_fixtures")
    @patch.object(scheduler.db, "get_enabled_leagues")
    @patch.object(scheduler.db, "get_conn")
    def test_task_a_fetches_whole_season(self, get_conn, get_enabled_leagues,
                                         fetch_fixtures, parse_fixtures,
                                         upsert_fixtures, cleanup_old,
                                         checkpoint_wal, sleep):
        conn = MagicMock()
        get_conn.return_value = conn
        get_enabled_leagues.return_value = {
            2: ("欧冠 Champions Lg", 2026),
        }
        response = [{"fixture": {"id": 1635606}}]
        parsed = [_fixture_row(
            commence="2026-11-04T17:45:00+00:00",
            home="AEK Athens FC", away="Real Madrid")]
        fetch_fixtures.return_value = response
        parse_fixtures.return_value = parsed

        result = scheduler.task_a_update_fixtures()

        self.assertEqual(result, 1)
        fetch_fixtures.assert_called_once_with(2, 2026)
        parse_fixtures.assert_called_once_with(
            response, 2, "欧冠 Champions Lg", 2026)
        upsert_fixtures.assert_called_once_with(conn, parsed)
        cleanup_old.assert_called_once_with(conn, scheduler.config.CLEANUP_DAYS)
        checkpoint_wal.assert_called_once_with(conn)
        sleep.assert_called_once_with(scheduler.config.REQUEST_THROTTLE_SEC)
        conn.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
