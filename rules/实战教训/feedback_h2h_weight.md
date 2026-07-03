---
name: h2h-weight
description: H2H历史交锋权重双向调整——本赛季同向占优上调35% / 本赛季实力接近下调20%
metadata:
  type: feedback
aliases:
  - h2h-home-dominance-same-direction
  - h2h-home-dominance-close-current-season
---

# H2H 权重双向调整（速查卡）

> 本卡合并自：`h2h-home-dominance-same-direction`（同向占优上调）、`h2h-home-dominance-close-current-season`（实力接近下调）。
> **完整论证见** [reference_case_lessons.md](reference_case_lessons.md) **第十七章、第二十一章规则 2**。

## 核心：H2H 权重随本赛季实力差**双向**调整

| 场景 | H2H 权重 | 本赛季状态权重 | 平局备选 |
|------|---------|--------------|---------|
| H2H 主场不败率 ≥80% + 本赛季积分差**主队领先 ≥8 分**（同向） | **35%** | 20% | 弱备选 |
| H2H 主场不败率 ≥80% + 本赛季**积分差 ≤3 分**（接近） | **20%** | 30% | 强备选 |
| H2H 碾压 + 客队本赛季大胜强队 | 20% | 30% | 强备选 |
| H2H 碾压 + 主场平局率 ≥30% | 20% | 25% | 强备选 |

一句话：H2H 与本赛季**同向**时历史优势可信，上调至 35%；本赛季实力已**追平**（积分差 ≤3 分 / 客队状态上升）时历史优势被抵消，下调至 20%、平局转强备选。

## 联赛差异（同向占优 ≥80% 时）

中超 25% < 五大联赛（除英超）30% < **英超 35%**（主场氛围/草皮/裁判/球迷最强）。

## 相关

- [[feedback_h2h_vs_current_season]]（MEMORY，H2H 与本赛季反向）— 本卡为其两个补充方向
- [[feedback_csl_fundamentals]] — 中超主场反弹（积分差 ≤3 分时不触发 35% 上调）
- [[feedback_home_away_quality]] — 配合量化主队近况对手质量
- 案例：Chelsea 2-1 Tottenham（上调）、河南 0-2 浙江（下调）
