---
name: home-away-quality
description: 主客场状态按对手质量量化——主场输强队≠崩溃 / 客场优于主场须验证稳定性
metadata:
  type: feedback
aliases:
  - home-loss-to-strong-teams
  - away-better-than-home-stability
---

# 主客场状态按对手质量量化（速查卡）

> 本卡合并自：`home-loss-to-strong-teams`（主场输强队≠崩溃）、`away-better-than-home-stability`（客场优于主场须验证）。互为镜像。
> **完整论证见** [reference_case_lessons.md](reference_case_lessons.md) **第十七章规则 1/3**。

## A. 主场输强队 ≠ 主场能力崩溃

主场近期输球须按对手质量分级，不可简单统计战绩：

| 主场输球对手 | "主场崩溃"权重 |
|------------|--------------|
| 主要输前 3 强（≥60%） | **15%**（输强队正常） |
| 主要输同级别（≥60%） | 30%（真实低迷） |
| 主要输弱队（≥40%） | 40%（严重崩盘） |

## B. 客场"优于主场"须验证稳定性（镜像）

"客场战绩优于主场"不可直接判为客场战斗力强，验证三项：

| 验证项 | 真实客场强 | 虚假信号 |
|--------|----------|---------|
| 对手质量 | 赢同级/强队 | 赢弱队、输强队 |
| 稳定性 | 连续 3 场+ 不败 | 胜负交替 |
| 净胜球 | 平均 > 0 | 接近 0（小胜小负） |

连续不败+赢同级+净胜 >0 → 25%；胜负交替+小比分+净胜≈0 → 10%；主要赢弱队 → 5%。

## 相关

- [[feedback_h2h_weight]] — 配合使用：量化近况对手质量后上调 H2H 权重
- [[feedback_csl_fundamentals]] — 中超单场大败非崩溃（B 的中超版）
- 案例：Chelsea 2-1 Tottenham（Chelsea 主场输强队；Tottenham 客场胜负交替）
