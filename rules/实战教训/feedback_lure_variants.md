---
name: lure-variants
description: 诱盘变体速查——浅盘升盘诱上 / 升盘后回降 / 临场诱下的触发阈值与反向结论
metadata:
  type: feedback
aliases:
  - shallow-opening-upgrade-lure
  - upgrade-then-downgrade-pricing-correction
  - lure-down-variant
---

# 诱盘变体识别（速查卡）

> 本卡合并自：`shallow-opening-upgrade-lure`（浅盘升盘诱上）、`upgrade-then-downgrade-pricing-correction`（升盘后回降）、`lure-down-variant`（临场诱下）。
> **完整论证见** [reference_case_lessons.md](reference_case_lessons.md) **第十三章、第十六章**。

## A. 浅盘升盘诱上组合（→ 十三章）

初盘开小 + 临场升盘 + 主水大幅反升 = 双重诱上，**真实看好下盘（主队）**。五项全满触发：

| 要件 | 阈值 |
|------|------|
| ① 初盘开小 | 实际盘口 < 理论 0.25 球+ |
| ② 临场②~③升盘 | 升幅 ≥ 0.25 球 |
| ③ 升盘后主水持续反升 | ≥ 15 点（含临场③延续） |
| ④ 即时节点欧赔回调 | ≥ 0.05 |
| ⑤ 升盘后亚盘主凯利虚高 | 持续 > 1.08 |

置信度：5 项 75~85 / 4 项 65~75 / 3 项 55~65 / ≤2 项不触发。比分须含**主队 2 球+ 净胜**（2:0、3:1）。
与"欧亚同步升盘=真实定价"的区分：真实定价升盘后三维度（盘口/水位/欧赔）同步收紧上盘赔付；诱上则水位维度扩大敞口 + 欧赔即时回调。

## B. 升盘后回降 ≠ 全程不变盘（→ 十六章规则 1）

盘口经历升盘→回降（如 -0.25→-0.5→-0.25）**不属于"全程不变盘"**，不可套用诱上变体（[[feedback_lure_up_variant]]），应走军规 #3（定价修正→转场景三）或军规 #10 变体。

## C. 临场诱下组合（→ 十六章规则 2~3，与诱上变体镜像）

降盘 + 主水先降后反升 = 临场诱下，封盘前诱导下盘资金，**真实看好上盘**。全部满足触发：

| 要件 | 阈值 |
|------|------|
| 初盘②~中盘升盘 | ≥ 0.25 球 |
| 临场②降回初盘水平或更低 | — |
| 临场②主水压低 | 降幅 ≥ 10 点 |
| 临场④主水反升 + 欧赔主胜跳升 | 反升 ≥ 8 点 且 欧主胜 ≥ 0.10 |

## 相关

- [[feedback_kelly_signals]]、[[feedback_late_stage_shift]] — 临场异动/凯利配合验证
- [[feedback_lure_up_variant]]（诱上型变体，MEMORY 记忆库）— C 为其镜像
- 案例：浙江 4-1 山东泰山（A）、Chelsea 2-1 Tottenham（B、C）
