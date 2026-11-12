# 实战教训总览（防错路由）

> 本文件是 `rules/实战教训/` 的**唯一入口**，功能是路由：告诉你本场需要读哪些主题防错文件。
> 每条规则的完整论证都在各主题文件（`feedback_*.md`）内，本文件不重复规则细节。
> `20260xxx_case_*.md` 为原始盘口数据存档，仅在需要复算/核对历史数据时读取。

## 证据四层（先读这段）

| 层 | 内容 | 产出 | 方向权 |
|----|------|------|--------|
| 市场层 | 报价及其变换：去抽水概率、盘口线、变盘事实、热门方、离散度、凯利、返还率 | p_市场 + 事实描述 | 无 |
| 基本面层 | 伤停、战意、赛程、近况、交锋、排名 | 相对市场的偏离 | 仅明文触发条件 |
| 假设层 | 全部「盘路 → 赛果」规则，见 [假设登记表.md](../假设登记表.md) | 命中的假设及状态 | 仅「已验证」 |
| 质量层 | 节点完整度、时效、报价同步性、基本面完整度 | 质量分 | 无，只调注额 |

### 方向怎么定
1. 默认无倾向。市场价格就是基准。
2. 「已验证」假设可提出方向；已验证假设互相矛盾则抵消。
3. 基本面仅在碾压级五要素 ≥3 项或极端动力差异 ≥3 项时可提出方向；与已验证假设矛盾则无倾向。
4. 「假设」「观察中」状态的规则只记录命中，不影响结论。
5. 凯利、返还率是报价的变换，属市场层事实，不单独构成证据。

### 两个指标
信号强度（无/弱/中/强）决定 p_最终 是否偏离 p_市场；数据质量（0~100）只决定凯利分数。
不再有权重百分比，不再有置信度上限。定义与扣分表见 CLAUDE.md 步骤 7。

> 本目录各主题文件（`feedback_*.md`）里的「盘路 → 赛果」条文都是**假设**，已标编号并登记；
> 文件中残留的「首选 / 打出 / 优先」等措辞只是该假设的**预测**。读到时按步骤 4 记录命中，不据此改方向。

---

## 读取协议（分两段执行）

规则必须在**用到它的那一步开始前**就位；输出前的复核只是兜底，不能代替前置加载。
各主题文件顶部已标注「最早使用步骤」与「命中后须重算」，按其执行。

### 第一段：前置加载（分析过程中，按步骤推进随时生效）

| 时点 | 须已加载的主题 |
|------|---------------|
| 步骤 1b~1d 读基本面前 | fundamentals_weight、home_away_quality、csl_fundamentals（中超）、h2h_weight（有 H2H） |
| 步骤 2 欧亚换算前 | strong_team_deep |
| 步骤 3 记录热门方/水位走势前 | **heat_direction（每场必读）**、sync_pricing、lure_variants、late_stage_shift |
| 步骤 5 市场层读数前 | kelly_signals |

> 机器人环境下，主题文件由 `bot/config.py` 的 `LESSON_TOPIC_RULES` 按赛事/基本面条件
> 随核心规则一次性注入 prompt（联赛类与基本面数据类条件加载，盘型类每场加载——
> 因其触发条件须读完 CSV 才知道，而规则必须在分析前就位）。
> 手工分析时按上表在对应步骤前自行读取。

### 第二段：输出前复核（步骤 7 输出前）

1. **全场必查**：执行下方「通用防错清单」。
2. **补漏重算**：若此时才发现某主题命中而前置未生效，**必须回头重算该主题「命中后须重算」列出的步骤**，不可只在「风险提示」里加一句了事。
3. **盘型比对**：若本场盘型与「案例索引」中某场历史错误一致，必须在报告「风险提示」中写明该案例及教训。
4. 实战教训中的「盘路 → 赛果」条文属**假设层**：只记录命中，**不改方向、不加减分**（见本文开头「证据四层」）。基本面类的「条件覆盖」场景（碾压级五要素、极端动力差异 ≥3 项）属基本面层，须满足明文触发条件才可提出方向。

## 主题路由表

| # | 触发条件（步骤 1~5 中检测到） | 主题文件 | 核心要点 |
|---|------------------------------|---------|---------|
| 1 | **每场必读**：记录热门方/水位走势前 | [feedback_heat_direction.md](feedback_heat_direction.md) | 市场层读数规范：水位下降方、欧赔下降方 → 热门方的事实定义；Betfair 按交易所逻辑 |
| 2 | 任一阶段出现升盘或降盘 | [feedback_sync_pricing.md](feedback_sync_pricing.md) | 假设层 H-P 系列：欧亚同步升/降盘、临场④升盘、两端价格跨节点反向移动视为有效变盘、淘汰赛次回合追分边界 |
| 3 | 全程不变盘（≥8 节点）/ 初盘浅于换算后升盘 / 升盘后回降 / 降盘后主水反升 | [feedback_lure_variants.md](feedback_lure_variants.md) | 阻上的机械定义；H-V 系列：诱上型变体、浅盘升盘五要件、临场诱下组合 |
| 4 | 临场③④/即时出现变盘、水位/欧赔/凯利跳变 | [feedback_late_stage_shift.md](feedback_late_stage_shift.md) | H-L 系列：超大异动、剧烈欧亚降盘、分层诱导 |
| 5 | 任一公司凯利极端值（>1.05 / <0.95）、方向反转、或初盘浅于换算读凯利 | [feedback_kelly_signals.md](feedback_kelly_signals.md) | 凯利读数规范（A/B/C/H 节）；H-K-D1/D2/E/F/G 假设 |
| 6 | 中超赛事 | [feedback_csl_fundamentals.md](feedback_csl_fundamentals.md) | 基本面层：主场反弹潜力；单败非崩溃；极端动力差异覆盖条件（E 节） |
| 7 | 有 H2H 交锋数据，或两队积分差 ≤3 分 | [feedback_h2h_weight.md](feedback_h2h_weight.md) | 基本面层：H2H 事实读法；高平局率只记录为事实，不自动调整首选 |
| 8 | 有近 10 场近况数据 | [feedback_home_away_quality.md](feedback_home_away_quality.md) | 基本面层：近况按对手质量分级；主场输强队≠崩溃 |
| 9 | 基本面与盘口矛盾 / 基本面一边倒（疑似碾压）/ 战意动力差异（保级、争冠、德比、疲态） | [feedback_fundamentals_weight.md](feedback_fundamentals_weight.md) | 基本面层：碾压级五要素（覆盖触发条件）；冠军压力客场等读法 |
| 10 | 强队让 -1.5 及以上深盘 / 热门浅让（-0.5）拒绝加深 / 世界杯及杯赛强弱对话 / 多家客胜凯利偏低 | [feedback_strong_team_deep.md](feedback_strong_team_deep.md) | H-X 系列：深盘降赔式诱上、高平赔冷平、受让方 +1 赢盘、浅让不升盘 |

> 典型加载量：一场常规联赛命中 4~6 个文件；触发条件不满足的文件不读。

## 通用防错清单（所有比赛必查）

- [ ] **热门方按机械定义**：基准庄欧赔去抽水概率初→终 ≥+1.5pp 为热门（≥3pp 大热），并与亚盘水位下降方核对；写明「热门方=X，证据」（→ 主题 1）
- [ ] **Betfair 按交易所逻辑**解读：赔率/凯利下降=散户大热，非庄家看好（→ 主题 1）
- [ ] **Pinnacle 全面偏移**（三项凯利同侧）且间距 < 0.05 → 该公司凯利读数不计入任何假设触发（→ 主题 5）
- [ ] **即时节点凯利异动**区分趋势延续型/突变反转型并如实记录（对应假设 H-K-E，→ 主题 5）
- [ ] **近况按对手质量量化**：输给强队 ≠ 状态/主场崩溃（→ 主题 8）
- [ ] **基本面与盘口矛盾**时两者并列记录，不互相改写；未达覆盖条件不改方向（→ 主题 9）
- [ ] **杯赛独立评估**：杯赛状态与联赛脱节，不可用联赛近况直接定杯赛（→ 主题 9）
- [ ] **H2H 平局率 ≥ 60%** → 记录为基本面事实，不自动调整首选（→ 主题 7）
- [ ] **任一公司平局凯利 < 0.96** → 记录为市场层事实（假设 H-K22 命中），不自动列平局为首选/次选（→ 主题 5/7）
- [ ] **积分差 ≤ 3 分且排名相邻** → 记录为基本面事实（→ 主题 7）
- [ ] **临场④急剧变盘 ≥ 0.25 球** → 数据质量扣 10 分（→ 主题 4）
- [ ] **无倾向检查**：本场没有「已验证」假设、基本面也未触发覆盖时，第 7 节三个判定必须写「无倾向」

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
| 20 | Bournemouth 1-1 Man City | 2026-05-20 | 英超（冠军争夺战） | 平局，上盘 -0.75 全输 | fundamentals D2/D3；kelly G；lure_variants D；h2h E | [20260520_case_02_bournemouth_vs_man_city.md](20260520_case_02_bournemouth_vs_man_city.md) |
| 21 | England 0-0 Ghana | 2026-06-24 | 世界杯小组赛 | 深盘上盘全输，冷平 | strong_team_deep A | [England_vs_Ghana_review.md](../../report/2026/06/2026-06-24/England_vs_Ghana_review.md) |
| 22 | Ecuador 2-1 Germany | 2026-06-26 | 世界杯小组赛 | 受让方 +1 赢盘且直接主胜 | strong_team_deep B | [Ecuador_vs_Germany_review.md](../../report/2026/06/2026-06-26/Ecuador_vs_Germany_review.md) |
| 23 | 河南 0-2 浙江 | 2026-05-30 | 中超 | 下盘客队赢球赢盘 | late_stage B；kelly E；csl C；h2h B | [20260530_case_05_henan_vs_zhejiang.md](20260530_case_05_henan_vs_zhejiang.md) |
| 24 | 重庆铜梁龙 2-3 北京国安 | 2026-05-30 | 中超 | 客队赢球赢盘 | kelly F；late_stage C/D；csl D | [20260531_case_03_chongqing_vs_beijing.md](20260531_case_03_chongqing_vs_beijing.md) |
| 25 | 天津津门虎 1-0 大连英博 | 2026-05-31 | 中超 | 主队赢球赢盘 | csl E（kelly F 的例外） | [20260531_case_04_tianjin_vs_dalian.md](20260531_case_04_tianjin_vs_dalian.md) |
| 26 | Brazil 1-2 Norway | 2026-07-06 | 世界杯 | 客胜，受让方 +0.5 赢盘 | strong_team_deep C | [20260706_case_06_brazil_vs_norway.md](20260706_case_06_brazil_vs_norway.md) |
| 27 | KA阿克雷里 3-2 IA阿克拉内斯 | 2026-07-13 | 冰岛超 | 主队3-2取胜；亚盘方向命中，平局首选错误 | kelly_signals H | [20260713_case_11_KA_Akureyri_vs_IA_Akranes.md](20260713_case_11_KA_Akureyri_vs_IA_Akranes.md) |
| 28 | 法国 0-2 西班牙 | 2026-07-15 | 世界杯 | 西班牙客胜并赢下 +0.25，法国 -0.25 全输，小 2.5 打出。 | sync_pricing D | [20260715_case_12_France_vs_Spain.md](20260715_case_12_France_vs_Spain.md) |
| 29 | Hapoel Beer Sheva 1-0 FK Crvena Zvezda | 2026-08-05 | 欧冠 | 受让+1判断命中，但客胜与大球判断失误，主队1-0直接取胜。 | sync_pricing E | [20260805_case_13_Hapoel_Beer_Sheva_vs_FK_Crvena_Zvezda.md](20260805_case_13_Hapoel_Beer_Sheva_vs_FK_Crvena_Zvezda.md) |
> 表中主题缩写对应文件：sync_pricing=[feedback_sync_pricing.md](feedback_sync_pricing.md)、lure_variants=[feedback_lure_variants.md](feedback_lure_variants.md)、late_stage=[feedback_late_stage_shift.md](feedback_late_stage_shift.md)、kelly=[feedback_kelly_signals.md](feedback_kelly_signals.md)、csl=[feedback_csl_fundamentals.md](feedback_csl_fundamentals.md)、h2h=[feedback_h2h_weight.md](feedback_h2h_weight.md)、home_away=[feedback_home_away_quality.md](feedback_home_away_quality.md)、fundamentals=[feedback_fundamentals_weight.md](feedback_fundamentals_weight.md)、heat_direction=[feedback_heat_direction.md](feedback_heat_direction.md)、strong_team_deep=[feedback_strong_team_deep.md](feedback_strong_team_deep.md)

## 新教训的归档规则

> **机器人自动归档**：完整可执行步骤见 [ARCHIVING_PROTOCOL.md](ARCHIVING_PROTOCOL.md)（前置编号 → 路由 → 三写一改 → 自检 → **自动 commit**）。**机器人到 commit 为止，push 由人工审阅后执行**——「归档成功」不等于「已推送」。下列为要点摘录。

1. 新复盘教训**先找可归入的现有主题文件**，在对应节内写触发条件（可机械判定）与预测，**同时在 [假设登记表](../假设登记表.md) 新增一条状态为「假设」的记录**；不得直接写成定方向的规则，也不得写置信度加减分/封顶
2. 无法归入现有主题时才新建 `feedback_<主题>.md`，并在本文件「主题路由表」加一行触发条件
3. 有原始盘口数据需存档时，新建 `20260xxx_case_xx_<对阵>.md` 数据卡，并在「案例索引」末尾追加一行；**案例号与 case_xx 号是两套独立序列，分别取当前最大值 +1**
4. 全场通用、不依赖场景触发的检查项，才可加入本文件「通用防错清单」
5. 若扩展了某主题的**触发边界**，须同步更新该主题文件顶部「触发条件」与本文件「主题路由表」对应行（两处都改）
