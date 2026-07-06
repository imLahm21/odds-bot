# 实战教训总览（防错路由）

> 本文件是 `rules/实战教训/` 的**唯一入口**，功能是路由：告诉你本场需要读哪些主题防错文件。
> 每条规则的完整论证都在各主题文件（`feedback_*.md`）内，本文件不重复规则细节。
> `20260xxx_case_*.md` 为原始盘口数据存档，仅在需要复算/核对历史数据时读取。

## 读取协议（步骤 7 输出前执行）

1. **全场必查**：执行下方「通用防错清单」。
2. **场景路由**：对照「主题路由表」，判断本场命中哪些触发条件，**仅加载命中的主题文件**，执行其规则与文件末尾的「本主题检查项」。
3. **盘型比对**：若本场盘型与「案例索引」中某场历史错误一致，必须在报告「风险提示」中写明该案例及教训。
4. 实战教训属于**参考性较低的修正因素**，用于扣减置信度与增列备选，不单独推翻三算法主逻辑（碾压级基本面、保级死战等明确覆盖场景除外）。

## 主题路由表

| # | 触发条件（步骤 1~5 中检测到） | 主题文件 | 核心要点 |
|---|------------------------------|---------|---------|
| 1 | **每场必读**：判热度/形态前 | [feedback_heat_direction.md](feedback_heat_direction.md) | 水位下降方=热门、欧赔下降方=热门，双重验证一致后才可匹配军规；Betfair 按交易所逻辑 |
| 2 | 任一阶段出现升盘或降盘 | [feedback_sync_pricing.md](feedback_sync_pricing.md) | 早中期欧亚同步升盘=真实定价、连续降盘=修正走军规#3；临场④升盘须三重验证 |
| 3 | 全程不变盘（≥8 节点）/ 初盘开小后升盘 / 升盘后回降 / 降盘后主水反升 | [feedback_lure_variants.md](feedback_lure_variants.md) | 阻上严格判定；诱上型变体；浅盘升盘诱上五要件；临场诱下组合 |
| 4 | 临场③④/即时出现变盘、水位/欧赔/凯利跳变 | [feedback_late_stage_shift.md](feedback_late_stage_shift.md) | 超大异动=反向定价；剧烈欧亚降盘须验凯利；分层诱导陷阱凯利红灯优先 |
| 5 | 任一公司凯利极端值（>1.05 / <0.95）、方向反转、或初盘开小（浅盘）读凯利 | [feedback_kelly_signals.md](feedback_kelly_signals.md) | Pinnacle 双重极端强制反转；凯利反转方向>绝对值；浅盘灰色地带；平局凯利信号 |
| 6 | 中超赛事 | [feedback_csl_fundamentals.md](feedback_csl_fundamentals.md) | 主场反弹潜力；单败非崩溃；「排名差+主场不败」上限 25%；保级死战关闭凯利反转；置信度上限 75 |
| 7 | 有 H2H 交锋数据，或两队积分差 ≤3 分 | [feedback_h2h_weight.md](feedback_h2h_weight.md) | 平局率 ≥60% 强制进首选；H2H 权重随本赛季实力差双向调整（35% ↔ 20%） |
| 8 | 有近 10 场近况数据 | [feedback_home_away_quality.md](feedback_home_away_quality.md) | 近况按对手质量分级；主场输强队≠崩溃；客场优于主场须验证稳定性 |
| 9 | 基本面与盘口矛盾 / 基本面一边倒（疑似碾压）/ 战意动力差异（保级、争冠、德比、疲态） | [feedback_fundamentals_weight.md](feedback_fundamentals_weight.md) | 矛盾时盘口 80%；碾压级五要素过滤诱上；冠军压力客场=保平争胜 |
| 10 | 强队让 -1.5 及以上深盘 / 热门浅让（-0.5）拒绝加深 / 世界杯及杯赛强弱对话 / 多家客胜凯利偏低 | [feedback_strong_team_deep.md](feedback_strong_team_deep.md) | 深盘降赔式诱上不可过滤；高平赔冷平保权重；受让方 +1 赢盘可直接升级为反胜；浅让不升盘+客胜低凯利=受让方不败优先 |

> 典型加载量：一场常规联赛命中 4~6 个文件；触发条件不满足的文件不读。

## 通用防错清单（所有比赛必查）

- [ ] **热度方向双重验证**：亚盘水位下降方=热门、欧赔下降方=热门，两者一致；军规匹配前写明「热门方=X，证据」（→ 主题 1）
- [ ] **Betfair 按交易所逻辑**解读：赔率/凯利下降=散户大热，非庄家看好（→ 主题 1）
- [ ] **Pinnacle 全面偏移**（三项凯利同侧）且间距 < 0.05 → 忽略该公司凯利信号（→ 主题 5）
- [ ] **即时节点凯利异动**区分趋势延续型/突变反转型：单节点变化 > 前序累计 = 反向解读（→ 主题 5）
- [ ] **近况按对手质量量化**：输给强队 ≠ 状态/主场崩溃（→ 主题 8）
- [ ] **基本面与盘口矛盾**时，下调基本面权重至 20%（→ 主题 9）
- [ ] **杯赛独立评估**：杯赛状态与联赛脱节，不可用联赛近况直接定杯赛（→ 主题 9）
- [ ] **H2H 平局率 ≥ 60%** → 平局必须首选或并列首选，不可仅列风险提示（→ 主题 7）
- [ ] **任一公司平局凯利 < 0.96** → 判 B/C 冲突（非"基本一致"），平局至少次选（→ 主题 5/7）
- [ ] **积分差 ≤ 3 分且排名相邻** → 平局基础概率上调（→ 主题 7）
- [ ] **临场④急剧变盘 ≥ 0.25 球** → 置信度额外扣减 10 分（→ 主题 4）
- [ ] **置信度封顶速查**：
  - 平手/平半盘默认 ≤ 65；半球盘 ≤ 75
  - 平手/平半**放宽至 ≤ 72** 须三要件全满：(a) 初盘定性「合适」或「开小」（非开大）；(b) ≥3 家公司协同确认同一热度/盘路方向；(c) 全部 10 节点完整且最新快照距开球 ≤ 3h。缺一仍按 ≤ 65；放宽后 B/C 冲突仍须正常扣减
  - 中超「主场+排名差」组合 ≤ 75（→ 主题 6）
  - 深盘/杯赛/缺终指/B-C 分歧任意 ≥ 2 项 ≤ 65~70（→ 主题 10）

## 案例索引（按时间排序）

> 编号沿用旧索引库；案例 23~25 为补登，编号在 21~22 之后但日期更早。新案例在末尾追加，并在对应主题文件内补规则。

| # | 比赛 | 日期 | 赛事 | 赛果 | 教训主题（文件 × 节） | 数据存档 |
|---|------|------|------|------|---------------------|---------|
| 11 | Chelsea 1-0 Leeds | 2026-04-26 | 足总杯半决赛 | 上盘赢球赢盘 | sync_pricing A | — |
| 12 | 浙江 2-1 深圳新鹏城 | 2026-05-02 | 中超 | 上盘赢球赢盘 | sync_pricing C；csl A；h2h C | — |
| 13 | 霍芬海姆 3-3 斯图加特 | 2026-05-02 | 德甲 | 平局，上盘输盘 | late_stage A；h2h A；kelly G | — |
| 14 | Newcastle 3-1 Brighton | 2026-05-02 | 英超 | 上盘大胜 | heat_direction；fundamentals D1 | — |
| 15 | 云南玉昆 1-2 浙江 | 2026-05-06 | 中超 | 上盘输盘 | sync_pricing B；kelly D1 | — |
| 16 | 上海海港 2-2 浙江 | 2026-05-15 | 中超 | 平局走盘 | lure_variants B；kelly D2；h2h B | — |
| 17 | AS Roma 2-0 Lazio | 2026-05-17 | 意甲（罗马德比） | 上盘赢球赢盘 | fundamentals B/C | — |
| 18 | 浙江 4-1 山东泰山 | 2026-05-20 | 中超 | 上盘大胜 | lure_variants C；kelly C；csl B | [20260520_case_01_zhejiang_vs_shandong.md](20260520_case_01_zhejiang_vs_shandong.md) |
| 19 | Chelsea 2-1 Tottenham | 2026-05-20 | 英超 | 上盘赢球赢盘 | lure_variants D/E；h2h B；home_away A/B；kelly C | — |
| 20 | Bournemouth 1-1 Man City | 2026-05-20 | 英超（冠军争夺战） | 平局走盘 | fundamentals D2/D3；kelly G；lure_variants D；h2h E | [20260520_case_02_bournemouth_vs_man_city.md](20260520_case_02_bournemouth_vs_man_city.md) |
| 21 | England 0-0 Ghana | 2026-06-24 | 世界杯小组赛 | 深盘上盘全输，冷平 | strong_team_deep A | [England_vs_Ghana_review.md](../../report/2026-06-24/England_vs_Ghana_review.md) |
| 22 | Ecuador 2-1 Germany | 2026-06-26 | 世界杯小组赛 | 受让方 +1 赢盘且直接主胜 | strong_team_deep B | [Ecuador_vs_Germany_review.md](../../report/2026-06-26/Ecuador_vs_Germany_review.md) |
| 23 | 河南 0-2 浙江 | 2026-05-30 | 中超 | 下盘客队赢球赢盘 | late_stage B；kelly E；csl C；h2h B | [20260530_case_05_henan_vs_zhejiang.md](20260530_case_05_henan_vs_zhejiang.md) |
| 24 | 重庆铜梁龙 2-3 北京国安 | 2026-05-30 | 中超 | 客队赢球赢盘 | kelly F；late_stage C/D；csl D | [20260531_case_03_chongqing_vs_beijing.md](20260531_case_03_chongqing_vs_beijing.md) |
| 25 | 天津津门虎 1-0 大连英博 | 2026-05-31 | 中超 | 主队赢球赢盘 | csl E（kelly F 的例外） | [20260531_case_04_tianjin_vs_dalian.md](20260531_case_04_tianjin_vs_dalian.md) |
| 26 | Brazil 1-2 Norway | 2026-07-06 | 世界杯 | 客胜，受让方 +0.5 赢盘 | strong_team_deep C | [20260706_case_06_brazil_vs_norway.md](20260706_case_06_brazil_vs_norway.md) |

> 表中主题缩写对应文件：sync_pricing=[feedback_sync_pricing.md](feedback_sync_pricing.md)、lure_variants=[feedback_lure_variants.md](feedback_lure_variants.md)、late_stage=[feedback_late_stage_shift.md](feedback_late_stage_shift.md)、kelly=[feedback_kelly_signals.md](feedback_kelly_signals.md)、csl=[feedback_csl_fundamentals.md](feedback_csl_fundamentals.md)、h2h=[feedback_h2h_weight.md](feedback_h2h_weight.md)、home_away=[feedback_home_away_quality.md](feedback_home_away_quality.md)、fundamentals=[feedback_fundamentals_weight.md](feedback_fundamentals_weight.md)、heat_direction=[feedback_heat_direction.md](feedback_heat_direction.md)、strong_team_deep=[feedback_strong_team_deep.md](feedback_strong_team_deep.md)

## 新教训的归档规则

> **机器人自动归档**：完整可执行步骤见 [ARCHIVING_PROTOCOL.md](ARCHIVING_PROTOCOL.md)（前置编号 → 路由 → 三写一改 → 自检 → 提交）。下列为要点摘录。

1. 新复盘教训**先找可归入的现有主题文件**，在对应节内追加规则（含来源案例、错误判断、阈值表格），并更新其「本主题检查项」
2. 无法归入现有主题时才新建 `feedback_<主题>.md`，并在本文件「主题路由表」加一行触发条件
3. 有原始盘口数据需存档时，新建 `20260xxx_case_xx_<对阵>.md` 数据卡，并在「案例索引」末尾追加一行；**案例号与 case_xx 号是两套独立序列，分别取当前最大值 +1**
4. 全场通用、不依赖场景触发的检查项，才可加入本文件「通用防错清单」
5. 若扩展了某主题的**触发边界**，须同步更新该主题文件顶部「触发条件」与本文件「主题路由表」对应行（两处都改）
