"""
进球分布模型 —— 从盘口反推比分概率，供 edge 计算用

解决两个问题：
  1. 结算状态分布（审查第 11 项）：整数盘的走盘概率、四分之一盘的半赢/半输概率，
     二元式 `p×O−1` 算不出来，须先有比分分布。
  2. 净胜球分布（审查第 21 项）：-1/-1.5 等深盘要区分「赢1球」与「赢2球以上」，
     胜平负概率合并做不到。

方法：Dixon-Coles 修正的双泊松。
  · 从大小球盘口去抽水反推**全场期望总进球** λ_total
  · 从亚盘让球线把 λ_total 拆成主客两队期望 λ_home / λ_away
  · 假设两队进球各自服从泊松，联合分布 P(i,j) = Pois(i;λ_h) × Pois(j;λ_a) × τ(i,j)
  · τ 为 Dixon-Coles 低分修正：纯泊松低估 0:0/1:0/0:1、高估 1:1

⚠️ 模型局限（用前必读）：
  · 泊松假设「进球独立、强度恒定」，真实足球并非如此（红牌、战术变化、比赛状态）。
  · 结果完全依赖输入的 λ 估计；大小球盘口若本身被操盘扭曲，输出同样偏。
  · rho 取经验值 −0.05（业界常用区间 −0.03 ~ −0.15），未用本人战绩拟合。
  · 故输出属**模型估计**，不是观测频率。与 p_市场 冲突时以 p_市场 为准，
    本模型只用于 p_市场 无法给出的那部分（走盘/半赢概率、净胜球分布）。
"""

import math

# Dixon-Coles 低分修正强度。负值=提升 0:0/1:0/0:1、压低 1:1（纯泊松的已知偏差）。
DC_RHO = -0.05
# 比分枚举上界：单队进球数算到 MAX_GOALS，尾部概率极小可忽略（8 球时 <1e-5）
MAX_GOALS = 8


def _pois(k: int, lam: float) -> float:
    """泊松概率 P(X=k)。lam<=0 时退化为「必然 0 球」。"""
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * lam ** k / math.factorial(k)


def _dc_tau(i: int, j: int, lam_h: float, lam_a: float, rho: float) -> float:
    """Dixon-Coles 修正因子，仅作用于 0-0/0-1/1-0/1-1 四个低分格。"""
    if i == 0 and j == 0:
        return 1.0 - lam_h * lam_a * rho
    if i == 0 and j == 1:
        return 1.0 + lam_h * rho
    if i == 1 and j == 0:
        return 1.0 + lam_a * rho
    if i == 1 and j == 1:
        return 1.0 - rho
    return 1.0


def score_matrix(lam_home: float, lam_away: float,
                 rho: float = DC_RHO) -> dict[tuple[int, int], float]:
    """比分联合概率 {(主进球, 客进球): 概率}，已归一。"""
    m: dict[tuple[int, int], float] = {}
    for i in range(MAX_GOALS + 1):
        for j in range(MAX_GOALS + 1):
            p = _pois(i, lam_home) * _pois(j, lam_away) * _dc_tau(
                i, j, lam_home, lam_away, rho)
            if p > 0:
                m[(i, j)] = p
    tot = sum(m.values())
    if tot > 0:
        m = {k: v / tot for k, v in m.items()}
    return m


def split_lambdas(total_goals: float, handicap: float) -> tuple[float, float]:
    """把期望总进球按让球线拆成主客两队期望进球（**粗略回退法**）。

    total_goals: 大小球盘口去抽水后的期望总进球（如 2.6）
    handicap:    内部口径让球（负=主队让出，见 parser.extract_asian_handicap）

    假设「让球线 ≈ 期望净胜球」，故 λ_h + λ_a = total、λ_h − λ_a = −handicap。

    ⚠️ 该假设在深盘处偏差很大：盘口深度还受抽水、资金分布、庄家风控影响，
    与期望净胜球并非线性对应。实测一场 λ_total=4.27 / 让 -1.25 的比赛，
    本法算出客胜 19.0%，而市场只给 6.3%（差 18.7 个百分点）。
    **有胜平负赔率时一律改用 `split_lambdas_from_1x2()`**，本函数仅作无欧赔时的回退。
    """
    exp_margin = -handicap                      # 主队期望净胜球
    lam_h = (total_goals + exp_margin) / 2
    lam_a = (total_goals - exp_margin) / 2
    # 期望进球不能为负（极深盘时可能算出负值）
    return max(lam_h, 0.05), max(lam_a, 0.05)


def split_lambdas_from_1x2(total_goals: float, p_home: float, p_draw: float,
                           p_away: float, rho: float = DC_RHO,
                           ) -> tuple[float, float, float]:
    """按**去抽水胜平负**反标定 λ 拆分（首选方法）。

    固定 λ_total 不变（大小球盘口决定进球总量，较可靠），在所有满足
    λ_h + λ_a = total 的拆分中，搜索使模型胜平负最贴近市场的那一个。

    这样模型与市场在胜平负维度天然对齐，只用它补市场给不出的东西
    （走盘/半赢概率、净胜球分布），避免「让球线≈期望净胜」的线性假设带来的偏差。

    返回 (λ_home, λ_away, 残差)；残差 = 三项概率的绝对偏差之和，越小越贴合。
    """
    s = p_home + p_draw + p_away
    if s <= 0:
        return split_lambdas(total_goals, 0.0) + (1.0,)
    ph, pd, pa = p_home / s, p_draw / s, p_away / s   # 归一，防传入未归一的值

    def residual(lam_h: float) -> float:
        lam_a = total_goals - lam_h
        if lam_a <= 0.01:
            return 9.9
        o = outcome_probs(score_matrix(lam_h, lam_a, rho))
        return (abs(o["home"] - ph) + abs(o["draw"] - pd)
                + abs(o["away"] - pa))

    # λ_h 的可行区间：(0, total)。残差关于 λ_h 单峰（主队越强→主胜越高），
    # 用三分搜索找最小残差点，60 轮足以收敛到 1e-6 量级。
    lo, hi = 0.02, max(total_goals - 0.02, 0.03)
    for _ in range(60):
        m1 = lo + (hi - lo) / 3
        m2 = hi - (hi - lo) / 3
        if residual(m1) <= residual(m2):
            hi = m2
        else:
            lo = m1
    lam_h = (lo + hi) / 2
    return lam_h, max(total_goals - lam_h, 0.05), residual(lam_h)


def ah_states(matrix: dict, line: float) -> dict[str, float]:
    """亚盘某条线的结算状态概率（**上盘视角**，line 为内部口径让球）。

    返回 {"win","half_win","push","half_lose","lose"}，和为 1。
    四分之一盘拆成相邻两条线各半本金，两半结果组合成半赢/半输。
    """
    out = {k: 0.0 for k in
           ("win", "half_win", "push", "half_lose", "lose")}
    quarter = abs(line * 4) % 2 == 1             # .25 / .75 结尾
    lo, hi = line - 0.25, line + 0.25
    for (i, j), p in matrix.items():
        if not quarter:
            adj = (i - j) + line                 # line 负=主队让出，故相加
            key = ("win" if adj > 1e-9 else
                   "lose" if adj < -1e-9 else "push")
            out[key] += p
            continue
        # 四分之一盘：本金各半押相邻两线，**同一比分同时判两条线**
        # （两线结果由同一比分决定、完全相关，不可当独立事件做笛卡尔积）
        margin = i - j
        r_lo = margin + lo
        r_hi = margin + hi
        w = sum(1 for r in (r_lo, r_hi) if r > 1e-9)
        l = sum(1 for r in (r_lo, r_hi) if r < -1e-9)
        if w == 2:
            out["win"] += p
        elif l == 2:
            out["lose"] += p
        elif w == 1 and l == 0:
            out["half_win"] += p                 # 一半赢、一半走
        elif l == 1 and w == 0:
            out["half_lose"] += p                # 一半输、一半走
        else:
            out["push"] += p                     # 两线皆走（相邻线不会一赢一输）
    return out


def ou_states(matrix: dict, line: float) -> dict[str, float]:
    """大小球某条线的状态概率（**大球视角**）。整数线（2.0/3.0）有走盘。"""
    out = {k: 0.0 for k in
           ("win", "half_win", "push", "half_lose", "lose")}
    quarter = abs(line * 4) % 2 == 1             # 2.25 / 2.75 等
    lo, hi = line - 0.25, line + 0.25
    for (i, j), p in matrix.items():
        tot = i + j
        if not quarter:
            key = ("win" if tot > line + 1e-9 else
                   "lose" if tot < line - 1e-9 else "push")
            out[key] += p
            continue
        # 同一比分同时判相邻两线（与亚盘同理，两线完全相关）
        w = sum(1 for ln in (lo, hi) if tot > ln + 1e-9)
        l = sum(1 for ln in (lo, hi) if tot < ln - 1e-9)
        if w == 2:
            out["win"] += p
        elif l == 2:
            out["lose"] += p
        elif w == 1 and l == 0:
            out["half_win"] += p
        elif l == 1 and w == 0:
            out["half_lose"] += p
        else:
            out["push"] += p
    return out


def margin_dist(matrix: dict) -> dict[int, float]:
    """净胜球分布 {净胜球: 概率}（主队视角，负=主队净负）。第 21 项要的就是这个。"""
    out: dict[int, float] = {}
    for (i, j), p in matrix.items():
        out[i - j] = out.get(i - j, 0.0) + p
    return out


def outcome_probs(matrix: dict) -> dict[str, float]:
    """胜平负 + 双进概率，供与 p_市场 交叉校验（模型是否离市场太远）。"""
    home = draw = away = btts = 0.0
    for (i, j), p in matrix.items():
        if i > j:
            home += p
        elif i == j:
            draw += p
        else:
            away += p
        if i > 0 and j > 0:
            btts += p
    return {"home": home, "draw": draw, "away": away,
            "btts_yes": btts, "btts_no": 1.0 - btts}


def edge_from_states(states: dict[str, float], odds: float) -> float:
    """按五态算 edge（审查第 11 项的状态式）。

    单位净收益：全赢 +(O−1)、半赢 +0.5(O−1)、走盘 0、半输 −0.5、全输 −1。
    """
    return (states.get("win", 0.0) * (odds - 1)
            + states.get("half_win", 0.0) * 0.5 * (odds - 1)
            + states.get("push", 0.0) * 0.0
            - states.get("half_lose", 0.0) * 0.5
            - states.get("lose", 0.0) * 1.0)


# ─── CSV → 状态分布：供 /analyze 注入 prompt ─────────────────────────────────
def _devig_two(a: float, b: float) -> tuple[float, float]:
    """两出口按隐含概率比例去抽水。"""
    if not a or not b or a <= 1 or b <= 1:
        return 0.0, 0.0
    ia, ib = 1 / a, 1 / b
    s = ia + ib
    return ia / s, ib / s


def implied_total_goals(ou_rows: list[tuple[float, float, float]]) -> float | None:
    """从大小球各线反推期望总进球 λ_total。

    ou_rows: [(盘口线, 大球赔率, 小球赔率), ...]（同一节点、多庄可先取均值）
    做法：对每条线求去抽水后的 P(大球)，用泊松总进球分布拟合出最匹配的 λ。
    取多条线的拟合结果中位数，抗单线噪声。
    """
    cands = []
    for line, over, under in ou_rows:
        p_over, _ = _devig_two(over, under)
        if p_over <= 0.02 or p_over >= 0.98:
            continue                             # 极端值不可靠，跳过
        # 在 [0.3, 6.0] 上二分找使 P(总进球 > line) = p_over 的 λ
        lo, hi = 0.3, 6.0
        for _ in range(60):
            mid = (lo + hi) / 2
            # P(总进球 > line)：总进球服从 Pois(λ)（两独立泊松之和仍是泊松）
            p = 1.0 - sum(_pois(k, mid) for k in range(int(line) + 1))
            if p < p_over:
                lo = mid
            else:
                hi = mid
        cands.append((lo + hi) / 2)
    if not cands:
        return None
    cands.sort()
    return cands[len(cands) // 2]


def states_for_csv(ah_lines: list[float], ou_rows: list[tuple[float, float, float]],
                   main_handicap: float | None,
                   h2h: tuple[float, float, float] | None = None) -> dict | None:
    """给一场比赛算出各盘口的状态分布，供 prompt 注入。

    ah_lines:       要评估的让球线列表（内部口径，负=主队让出）
    ou_rows:        [(总进球线, 大球赔率, 小球赔率), ...]
    main_handicap:  主盘口让球线（仅在无 h2h 时用于粗略拆分 λ）
    h2h:            (主胜赔率, 平局赔率, 客胜赔率)——有则按胜平负反标定 λ 拆分（首选）
    返回 None 表示数据不足（无大小球盘口 → 估不出 λ）。
    """
    lam_total = implied_total_goals(ou_rows)
    if lam_total is None:
        return None

    fit_note, residual = "", None
    if h2h and all(x and x > 1 for x in h2h):
        ih = [1 / x for x in h2h]
        s = sum(ih)
        ph, pd, pa = (v / s for v in ih)         # 去抽水胜平负
        lam_h, lam_a, residual = split_lambdas_from_1x2(
            lam_total, ph, pd, pa)
        fit_note = "按去抽水胜平负反标定"
    elif main_handicap is not None:
        lam_h, lam_a = split_lambdas(lam_total, main_handicap)
        fit_note = "按让球线粗略拆分（无欧赔，偏差可能较大）"
    else:
        return None

    m = score_matrix(lam_h, lam_a)
    out = {
        "lambda_total": round(lam_total, 3),
        "lambda_home": round(lam_h, 3),
        "lambda_away": round(lam_a, 3),
        "fit": fit_note,
        "residual": None if residual is None else round(residual, 4),
        "market_1x2": None,
        "outcome": {k: round(v, 4) for k, v in outcome_probs(m).items()},
        "margin": {k: round(v, 4) for k, v in sorted(margin_dist(m).items())
                   if v >= 0.001},
        "ah": {}, "ou": {},
    }
    if h2h and all(x and x > 1 for x in h2h):
        out["market_1x2"] = {"home": round(ph, 4), "draw": round(pd, 4),
                             "away": round(pa, 4)}
    for ln in ah_lines:
        out["ah"][ln] = {k: round(v, 4)
                         for k, v in ah_states(m, ln).items() if v > 0}
    for line, _o, _u in ou_rows:
        out["ou"][line] = {k: round(v, 4)
                           for k, v in ou_states(m, line).items() if v > 0}
    # 大小球一致性：λ_total 由大小球反推、λ 拆分由胜平负反标定，
    # 两个约束不必然兼容（市场可同时认为「主队大胜」且「总进球不多」）。
    # 记下模型与市场 P(大球) 的最大偏差，供 prompt 显式提示。
    gaps = []
    for line, over, under in ou_rows:
        p_mkt, _ = _devig_two(over, under)
        if p_mkt <= 0:
            continue
        s_mod = out["ou"].get(line, {})
        p_mod = s_mod.get("win", 0.0) + 0.5 * s_mod.get("half_win", 0.0)
        denom = 1.0 - s_mod.get("push", 0.0)
        if denom > 0.01:
            p_mod = p_mod / denom          # 排除走盘后的条件概率，与市场同口径
        gaps.append(abs(p_mod - p_mkt))
    out["ou_gap"] = round(max(gaps), 4) if gaps else None
    return out


def format_states_block(st: dict | None) -> str:
    """把 states_for_csv 的结果渲染成 prompt 里的一段文本。"""
    if not st:
        return ("### 进球分布模型\n"
                "（大小球盘口缺失或不可用，无法估 λ；|让球| ≥0.75 的深盘"
                "本场标注「无法估 edge」，只用 p_市场 参考、不下注）\n")
    lines = [
        "### 进球分布模型（Dixon-Coles 修正双泊松，由盘口反推）",
        "",
        f"- 期望总进球 λ = {st['lambda_total']}"
        f"（主 {st['lambda_home']} / 客 {st['lambda_away']}）",
        f"- λ 拆分方式：{st.get('fit') or '未知'}",
    ]
    mk = st.get("market_1x2")
    if mk:
        o = st["outcome"]
        gap = max(abs(o["home"] - mk["home"]), abs(o["draw"] - mk["draw"]),
                  abs(o["away"] - mk["away"])) * 100
        lines += [
            f"- 市场去抽水胜平负：主 {mk['home']:.1%} / 平 {mk['draw']:.1%}"
            f" / 客 {mk['away']:.1%}",
            f"- 模型胜平负：主 {o['home']:.1%} / 平 {o['draw']:.1%}"
            f" / 客 {o['away']:.1%}"
            f"　（最大偏差 {gap:.1f} 个百分点"
            + ("，**已对齐**）" if gap <= 3 else
               ("，尚可）" if gap <= 10 else
                "，⚠️ **偏差过大，λ 估计不可靠，请以 p_市场 为准**）")),
        ]
    else:
        o = st["outcome"]
        lines.append(
            "- 模型胜平负：" + "、".join(
                f"{k} {v:.1%}" for k, v in
                (("主胜", o['home']), ("平", o['draw']), ("客胜", o['away'])))
            + "　（**无欧赔可对齐，仅供参考**）")
    lines += [
        f"- 模型双进 Yes {st['outcome']['btts_yes']:.1%}",
    ]
    ou_gap = st.get("ou_gap")
    if ou_gap is not None:
        lines.append(
            f"- 大小球一致性：模型 vs 市场最大偏差 {ou_gap * 100:.1f} 个百分点"
            + ("（已对齐）" if ou_gap <= 0.03 else
               ("（尚可）" if ou_gap <= 0.10 else
                "，⚠️ **大小球方向请以 p_市场 为准**"
                "（λ_total 由大小球反推、λ 拆分由胜平负反标定，"
                "两约束在本场不完全兼容）")))
    lines += [
        "",
        "**净胜球分布**（主队视角）：",
        "  " + "、".join(f"{k:+d}球 {v:.1%}" for k, v in st['margin'].items()),
        "",
        "**各让球线结算状态**（上盘视角）：",
        "",
        "| 让球线 | 全赢 | 半赢 | 走盘 | 半输 | 全输 |",
        "|--------|------|------|------|------|------|",
    ]
    for ln, s in sorted(st["ah"].items()):
        lines.append(
            f"| {ln:+g} | {s.get('win',0):.1%} | {s.get('half_win',0):.1%} "
            f"| {s.get('push',0):.1%} | {s.get('half_lose',0):.1%} "
            f"| {s.get('lose',0):.1%} |")
    lines += ["", "**各大小球线结算状态**（大球视角）：", "",
              "| 总进球线 | 全赢 | 半赢 | 走盘 | 半输 | 全输 |",
              "|----------|------|------|------|------|------|"]
    for ln, s in sorted(st["ou"].items()):
        lines.append(
            f"| {ln:g} | {s.get('win',0):.1%} | {s.get('half_win',0):.1%} "
            f"| {s.get('push',0):.1%} | {s.get('half_lose',0):.1%} "
            f"| {s.get('lose',0):.1%} |")
    lines += [
        "",
        "> ⚠️ 这是**模型估计**，非观测频率：泊松假设进球独立且强度恒定，",
        "> 且结果完全依赖上面 λ 的估计（由大小球盘口去抽水反推）。",
        "> **算 edge 时用本表的状态概率代入五态式**（reference_staking_kelly 1.2b），",
        "> 但 **p 仍以 p_市场 为主锚**——若模型胜平负与 p_市场 差 >10 个百分点，",
        "> 说明 λ 估偏，应以 p_市场 为准并在报告中说明。",
        "",
    ]
    return "\n".join(lines)


def parse_csv_lines(csv_text: str) -> tuple[list[float], list[tuple],
                                            float | None,
                                            tuple[float, float, float] | None]:
    """从 /export 的 CSV 文本里取出评估所需的盘口线。

    返回 (让球线列表, [(总进球线, 大球赔率, 小球赔率)...], 主盘口让球线,
          (主胜, 平局, 客胜) 欧赔中位数或 None)。
    取**最新节点**的数据；同一线多庄时取赔率中位数（抗单庄挂错）。
    主盘口 = 挂出庄家数最多的那条让球线（并列时取绝对值最小的）。
    """
    import csv as _csv
    import io as _io
    from statistics import median

    try:
        rows = list(_csv.DictReader(_io.StringIO(csv_text)))
    except Exception:
        return [], [], None
    if not rows:
        return [], [], None

    def _f(v):
        try:
            return float(str(v).strip())
        except (TypeError, ValueError):
            return None

    # 按「快照时间」取最新一批（该列形如 "2026-09-05 12:00（临场②）"）
    times = sorted({r.get("快照时间(CST)", "") for r in rows
                    if r.get("快照时间(CST)")})
    latest = times[-1] if times else None
    cur = [r for r in rows if r.get("快照时间(CST)") == latest] or rows

    ah: dict[float, list[tuple[float, float]]] = {}
    ou: dict[float, list[tuple[float, float]]] = {}
    h2h_h: list[float] = []
    h2h_d: list[float] = []
    h2h_a: list[float] = []
    for r in cur:
        kind = (r.get("盘口类型") or "").strip()
        if kind == "欧指":
            ho, do, ao = (_f(r.get("主胜赔率")), _f(r.get("平局赔率")),
                          _f(r.get("客胜赔率")))
            if ho and do and ao:
                h2h_h.append(ho)
                h2h_d.append(do)
                h2h_a.append(ao)
            continue
        line = _f(r.get("让球"))
        hw, aw = _f(r.get("主队水位")), _f(r.get("客队水位"))
        if line is None or hw is None or aw is None:
            continue
        if kind == "亚盘":
            ah.setdefault(line, []).append((hw, aw))
        elif kind == "大小球":
            ou.setdefault(line, []).append((hw, aw))

    ah_lines = sorted(ah)
    ou_rows = [(ln, median([o for o, _ in v]), median([u for _, u in v]))
               for ln, v in sorted(ou.items())]
    main_hc = None
    if ah:
        top = max(len(v) for v in ah.values())
        main_hc = min((ln for ln, v in ah.items() if len(v) == top), key=abs)
    h2h = ((median(h2h_h), median(h2h_d), median(h2h_a))
           if h2h_h and h2h_d and h2h_a else None)
    return ah_lines, ou_rows, main_hc, h2h


def states_from_csv(csv_text: str) -> dict | None:
    """一步到位：CSV 文本 → 状态分布（供 /analyze 注入）。数据不足返回 None。"""
    ah_lines, ou_rows, main_hc, h2h = parse_csv_lines(csv_text)
    if not ou_rows:
        return None
    return states_for_csv(ah_lines, ou_rows, main_hc, h2h)
