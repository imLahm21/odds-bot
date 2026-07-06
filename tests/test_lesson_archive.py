"""lesson_archive 纯逻辑测试（采番/字母节/文本编辑/原子落盘/自检），不碰 LLM。

用临时目录搭一个最小教训库骨架，验证 build_changeset + apply_changeset 的四处联动
与原子性。运行：python -m pytest tests/test_lesson_archive.py  或  python tests/test_lesson_archive.py
"""

import os
import sys
import shutil
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import lesson_archive as la   # noqa: E402


OVERVIEW_SEED = """# 实战教训总览（防错路由）

## 主题路由表

| # | 触发条件 | 主题文件 | 核心要点 |
|---|---------|---------|---------|
| 1 | 每场必读 | [feedback_heat_direction.md](feedback_heat_direction.md) | 热度方向 |
| 10 | 强队深盘 | [feedback_strong_team_deep.md](feedback_strong_team_deep.md) | 深盘诱上 |

> 典型加载量：4~6 个文件。

## 通用防错清单

- [ ] 某项

## 案例索引（按时间排序）

| # | 比赛 | 日期 | 赛事 | 赛果 | 教训主题 | 数据存档 |
|---|------|------|------|------|---------|---------|
| 24 | A 1-0 B | 2026-05-30 | 中超 | 主胜 | csl D | — |
| 25 | C 2-1 D | 2026-05-31 | 中超 | 主胜 | csl E | [x.md](x.md) |

> 表中主题缩写对应文件：heat_direction=[feedback_heat_direction.md](feedback_heat_direction.md)、strong_team_deep=[feedback_strong_team_deep.md](feedback_strong_team_deep.md)

## 新教训的归档规则

1. 略
"""

TOPIC_SEED = """---
name: strong-team-deep
description: 强队深盘
metadata:
  type: feedback
---

# 强队深盘 / 杯赛冷平

> **触发条件**：强队让 -1.5 及以上深盘；世界杯强弱对话。
> 配套说明。

## A. 强队深盘冷平

> 来源：某场。

**规则 A1：略**。

## B. 升盘后降温

> 来源：另一场。

**规则 B1：略**。

## 本主题检查项

- [ ] 深盘冷平？→ 保权重
- [ ] 升盘降温？→ 看受让方

## 相关

- 案例：England 0-0 Ghana（A）、Ecuador 2-1 Germany（B）
"""


def _seed_lib(d: str) -> None:
    with open(os.path.join(d, "reference_case_lessons.md"), "w",
              encoding="utf-8") as f:
        f.write(OVERVIEW_SEED)
    with open(os.path.join(d, "feedback_strong_team_deep.md"), "w",
              encoding="utf-8") as f:
        f.write(TOPIC_SEED)
    with open(os.path.join(d, "feedback_heat_direction.md"), "w",
              encoding="utf-8") as f:
        f.write("---\nname: heat-direction\n---\n\n# 热度\n\n"
                "> **触发条件**：每场必读。\n\n## A. 略\n\n**规则 A1**\n\n"
                "## 本主题检查项\n\n- [ ] 略\n\n## 相关\n\n- 案例：略\n")
    # 一张既有数据卡（case_06），用于验证 next_data_card_no
    with open(os.path.join(d, "20260706_case_06_e_vs_f.md"), "w",
              encoding="utf-8") as f:
        f.write("# 案例：E vs F\n")


def _sample_plan(letter="C"):
    return {
        "card_md": ("# 案例（数据存档）：X 1-2 Y（2026-07-10 世界杯）\n\n"
                    "> 存档卡。案例索引见案例 {CASE_NO}。\n\n"
                    "## 结果\n- 预测：略\n\n## 关键信号\n- 略\n\n"
                    "## 教训与规则\n- 略\n\n## 衍生规则\n- 略\n"),
        "rule_section_md": (f"## {letter}. 新场景标题\n\n"
                            "> 来源：X 1-2 Y（2026-07-10 世界杯）。原判偏差。"
                            "存档：{CARD_LINK}。\n\n"
                            f"**规则 {letter}1：新规则内容**。\n"),
        "checklist_items": ["- [ ] 新触发？→ 新动作", "另一条无前缀的检查"],
        "index_row": {"teams": "X 1-2 Y", "date": "2026-07-10",
                      "league": "世界杯", "result": "客胜",
                      "topic_ref": f"strong_team_deep {letter}"},
        "trigger_extension": None,
        "related_case_append": "X 1-2 Y（C，存档新卡）",
    }


class TestNumbering(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        _seed_lib(self.d)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_next_case_no(self):
        # 索引表末行 25 → 26
        self.assertEqual(la.next_case_no(self.d), 26)

    def test_next_data_card_no_independent(self):
        # 文件名 case_06 → 07（与案例号 26 是两套序列）
        self.assertEqual(la.next_data_card_no(self.d), 7)

    def test_next_section_letter(self):
        with open(os.path.join(self.d, "feedback_strong_team_deep.md"),
                  encoding="utf-8") as f:
            txt = f.read()
        self.assertEqual(la.next_section_letter(txt), "C")
        self.assertEqual(la.next_section_letter("# 无节\n"), "A")


class TestChangeset(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        _seed_lib(self.d)
        self.meta = {"home": "X", "away": "Y", "league": "世界杯",
                     "kick_cst": "2026-07-10"}

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _build(self, plan=None):
        plan = plan or _sample_plan("C")
        plan["_section_letter"] = "C"
        return la.build_changeset(plan, self.meta, "strong_team_deep",
                                  is_new_topic=False, lessons_dir=self.d)

    def test_numbers_backfilled(self):
        cs = self._build()
        self.assertEqual(cs["case_no"], 26)
        self.assertEqual(cs["card_no"], 7)
        self.assertEqual(cs["section_letter"], "C")
        # 数据卡文件名用 card_no（07），内容里 {CASE_NO} 回填为 26
        self.assertIn("case_07", os.path.basename(cs["card_path"]))
        card_txt = cs["files"][cs["card_path"]]
        self.assertIn("案例 26", card_txt)
        self.assertNotIn("{CASE_NO}", card_txt)

    def test_rule_section_inserted_before_checklist(self):
        cs = self._build()
        topic_path = os.path.join(self.d, "feedback_strong_team_deep.md")
        t = cs["files"][topic_path]
        self.assertIn("## C. 新场景标题", t)
        # C 节必须在「## 本主题检查项」之前
        self.assertLess(t.index("## C. 新场景标题"), t.index("## 本主题检查项"))
        # B 节仍在 C 节之前
        self.assertLess(t.index("## B. 升盘后降温"), t.index("## C. 新场景标题"))
        # {CARD_LINK} 已回填
        self.assertNotIn("{CARD_LINK}", t)
        self.assertIn("case_07", t)

    def test_checklist_appended_with_prefix(self):
        cs = self._build()
        t = cs["files"][os.path.join(self.d, "feedback_strong_team_deep.md")]
        seg = t[t.index("## 本主题检查项"):]
        self.assertIn("- [ ] 新触发？→ 新动作", seg)
        # 无前缀的那条被补上 "- [ ] "
        self.assertIn("- [ ] 另一条无前缀的检查", seg)

    def test_index_row_appended(self):
        cs = self._build()
        ov = cs["files"][os.path.join(self.d, "reference_case_lessons.md")]
        self.assertIn("| 26 | X 1-2 Y | 2026-07-10 | 世界杯 | 客胜 |", ov)
        # 旧行仍在
        self.assertIn("| 25 | C 2-1 D |", ov)

    def test_trigger_extension_updates_two_places(self):
        plan = _sample_plan("C")
        plan["trigger_extension"] = "或浅让不升盘"
        cs = self._build(plan)
        topic = cs["files"][os.path.join(self.d, "feedback_strong_team_deep.md")]
        ov = cs["files"][os.path.join(self.d, "reference_case_lessons.md")]
        self.assertIn("或浅让不升盘", topic)   # 主题文件顶部触发条件
        self.assertIn("或浅让不升盘", ov)       # 路由表核心要点列

    def test_atomic_apply_and_selfcheck(self):
        cs = self._build()
        la.apply_changeset(cs)
        # 三处都真落盘
        self.assertTrue(os.path.exists(cs["card_path"]))
        with open(os.path.join(self.d, "reference_case_lessons.md"),
                  encoding="utf-8") as f:
            self.assertIn("| 26 |", f.read())
        problems = la.run_self_check(cs, "strong_team_deep", lessons_dir=self.d)
        self.assertEqual(problems, [], f"自检不应报错：{problems}")
        # 无残留 .tmp
        leftover = [n for n in os.listdir(self.d) if n.endswith(".tmp")]
        self.assertEqual(leftover, [])

    def test_new_topic_branch(self):
        plan = _sample_plan("A")
        plan["_section_letter"] = "A"
        plan["trigger_extension"] = "新主题触发条件"
        cs = la.build_changeset(plan, self.meta, "corner_flow",
                                is_new_topic=True, lessons_dir=self.d)
        topic_path = os.path.join(self.d, "feedback_corner_flow.md")
        self.assertIn(topic_path, cs["files"])
        newf = cs["files"][topic_path]
        self.assertIn("name: corner-flow", newf)         # frontmatter slug
        self.assertIn("## A. 新场景标题", newf)
        self.assertIn("## 本主题检查项", newf)
        ov = cs["files"][os.path.join(self.d, "reference_case_lessons.md")]
        # 路由表加了新行（# 应为 11 = 现有最大 10 +1）
        self.assertIn("feedback_corner_flow.md", ov)
        self.assertIn("| 11 |", ov)
        # 缩写注释补了新项
        self.assertIn("corner_flow=[feedback_corner_flow.md]", ov)


if __name__ == "__main__":
    unittest.main(verbosity=2)
