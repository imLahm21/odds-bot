"""bot/fundamentals：开球截止（F1/F4）、积分榜按 id 匹配（F2）、单块失败隔离（F3）、
结构化标志（F5）、事实表数字（F6）。API 响应为按 API-Football 结构录制的样例
（tests/fixtures/fundamentals/），不发真实请求。"""

import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import analyzer, db, fundamentals  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "fundamentals"
KICKOFF = "2026-10-17T14:00:00Z"
HOME_ID, AWAY_ID = 55, 40          # Brentford vs Liverpool（fixture 1557428）
CSL_HOME, CSL_AWAY = 830, 833       # 北京国安 vs 上海申花


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _dispatch(overrides: dict | None = None):
    """按 (endpoint, 关键参数) 返回录制响应；overrides 里的值若是异常则抛出。"""
    overrides = overrides or {}
    recorded = {
        ("/fixtures", "last", HOME_ID): _load("recent_brentford.json"),
        ("/fixtures", "last", AWAY_ID): _load("recent_liverpool.json"),
        ("/fixtures", "next", HOME_ID): _load("next_brentford.json"),
        ("/fixtures", "next", AWAY_ID): {"response": []},
        ("/fixtures/headtohead", None, None): _load("h2h.json"),
        ("/standings", None, None): _load("standings_epl.json"),
        ("/injuries", None, None): _load("injuries.json"),
        ("/teams/statistics", None, HOME_ID): _load("stats_brentford.json"),
        ("/teams/statistics", None, AWAY_ID): {"response": {}},
    }

    def fake(endpoint, params=None, max_retries=3):
        params = params or {}
        mode = "last" if "last" in params and endpoint == "/fixtures" else (
            "next" if "next" in params else None)
        team = params.get("team")
        key = (endpoint, mode, team)
        if key not in recorded:
            key = (endpoint, None, team) if (endpoint, None, team) in recorded else (endpoint, None, None)
        value = overrides.get(endpoint, recorded.get(key, {"response": []}))
        if isinstance(value, Exception):
            raise value
        return value
    return fake


def _build(meta, overrides=None, now=None):
    with (
        patch.object(db, "get_fixture_meta", return_value=meta),
        patch.object(fundamentals.api_client, "api_get", side_effect=_dispatch(overrides)),
    ):
        return fundamentals.build_fundamentals_ex(
            None, 1557428, now=now or datetime(2026, 11, 11, tzinfo=timezone.utc))


EPL_META = (1557428, 39, "Premier League", 2026, "Brentford", "Liverpool",
            HOME_ID, AWAY_ID, KICKOFF)


class TestKickoffCutoff(unittest.TestCase):
    def test_matches_after_kickoff_are_excluded(self):
        text, _ = _build(EPL_META)
        # 本场（10-17 1-2）及之后的比赛不得出现在近况里
        self.assertNotIn("2026-10-17 [Premier League] Brentford 1-2 Liverpool", text)
        recent_block = text.split("【Brentford 近 ", 1)[1].split("\n【", 1)[0]
        self.assertNotIn("2026-10-25", recent_block)
        self.assertNotIn("2026-10-17", recent_block)
        first = recent_block.split("\n")[1]
        self.assertIn("2026-10-10 [Premier League] Aston Villa 2-2 Brentford", first)
        h2h_block = text.split("【历史交锋", 1)[1].split("\n【", 1)[0]
        self.assertNotIn("2026-10-17", h2h_block)

    def test_unfinished_matches_are_skipped(self):
        text, _ = _build(EPL_META)
        self.assertNotIn("Postponed FC", text)

    def test_upcoming_only_after_kickoff(self):
        text, _ = _build(EPL_META)
        block = text.split("【Brentford 未来", 1)[1]
        self.assertNotIn("2026-10-17", block.split("\n【", 1)[0])
        self.assertIn("2026-10-25", block)


class TestStandingsById(unittest.TestCase):
    def test_csl_rows_match_by_team_id_with_chinese_labels(self):
        meta = (1, 169, "Super League", 2026, "Beijing Guoan", "Shanghai Shenhua",
                CSL_HOME, CSL_AWAY, KICKOFF)
        overrides = {"/standings": _load("standings_csl.json")}
        text, flags = _build(meta, overrides)
        self.assertTrue(flags["has_standings"])
        self.assertIn("北京国安 积分30", text)
        self.assertIn("上海申花 积分28", text)
        self.assertIn("积分榜：主队 第6 积30 / 客队 第7 积28，差 +2 分", text)

    def test_focus_rows_carry_home_away_split_and_form(self):
        text, _ = _build(EPL_META)
        line = next(l for l in text.splitlines() if "Brentford 积分" in l)
        self.assertIn("主场 3-1-0 进9失3", line)
        self.assertIn("form WDLWW", line)


class TestBlockIsolation(unittest.TestCase):
    def test_one_failing_block_keeps_the_others(self):
        text, flags = _build(EPL_META, {"/standings": RuntimeError("boom")})
        self.assertIn("【积分榜】拉取失败（RuntimeError）", text)
        self.assertFalse(flags["has_standings"])
        self.assertTrue(flags["has_form"])
        self.assertTrue(flags["has_h2h"])
        self.assertIn("【伤停】", text)
        self.assertIn("积分榜", flags["missing"])
        self.assertIn("基本面缺块：积分榜", text)


class TestFlagsAndFacts(unittest.TestCase):
    def test_flag_line_round_trips(self):
        text, flags = _build(EPL_META)
        parsed = fundamentals.parse_flags(text)
        self.assertEqual({k: parsed[k] for k in flags if k != "missing"},
                         {k: flags[k] for k in flags if k != "missing"})
        self.assertEqual(analyzer.fund_has(text), (True, True))

    def test_keyword_fallback_ignores_fixed_hints(self):
        # 无标志行、只有固定提示语的文本：旧判据会恒为真（F5）
        text = fundamentals._CONTEXT_HINT + "\n" + fundamentals._TAIL_NOTE
        self.assertEqual(analyzer.fund_has(text), (False, False))
        self.assertEqual(analyzer.fund_has("历史交锋 3胜 近况 2胜"), (True, True))

    def test_fact_table_numbers(self):
        text, _ = _build(EPL_META)
        facts = text.split("【事实表】", 1)[1].split("\n【", 1)[0]
        # Brentford 开球前 5 场：W2-1, D2-2, L0-1, W3-0, W1-0（自新到旧）
        self.assertIn("主队 Brentford 近5场 3胜1平1负 进8失4", facts)
        self.assertIn("主场 3场 2胜0平1负 进3失2", facts)   # 2-1 胜、0-1 负、1-0 胜
        self.assertIn("近5场 form WWLWD（左旧右新）", facts)
        self.assertIn("休息天数：主队 7 天", facts)
        self.assertIn("交锋 近3场（主队视角）0胜1平2负，场均总进球 3.0，平局率 33%", facts)
        self.assertIn("伤停：主队 2 人（含 Player A、Player B） / 客队 1 人（含 Player C）", facts)
        self.assertIn("积分榜：主队 第8 积15 / 客队 第2 积22，差 -7 分", facts)

    def test_lineups_only_requested_near_kickoff(self):
        calls = []

        def spy(endpoint, params=None, max_retries=3):
            calls.append(endpoint)
            return {"response": []}
        with (
            patch.object(db, "get_fixture_meta", return_value=EPL_META),
            patch.object(fundamentals.api_client, "api_get", side_effect=spy),
        ):
            fundamentals.build_fundamentals_ex(
                None, 1, now=datetime(2026, 10, 16, tzinfo=timezone.utc))
            self.assertNotIn("/fixtures/lineups", calls)
            fundamentals.build_fundamentals_ex(
                None, 1, now=datetime(2026, 10, 17, 13, 0, tzinfo=timezone.utc))
            self.assertIn("/fixtures/lineups", calls)


if __name__ == "__main__":
    unittest.main()
