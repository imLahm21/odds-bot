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
    """把期望总进球按让球线拆成主客两队期望进球。

    total_goals: 大小球盘口去抽水后的期望总进球（如 2.6）
    handicap:    内部口径让球（负=主队让出，见 parser.extract_asian_handicap）

    让球线近似等于期望净胜球：主队让 0.75 球 ≈ 期望净胜 0.75。
    故 λ_h + λ_a = total，λ_h − λ_a = −handicap（handicap 负=主队强）。
    """
    exp_margin = -handicap                      # 主队期望净胜球
    lam_h = (total_goals + exp_margin) / 2
    lam_a = (total_goals - exp_margin) / 2
    # 期望进球不能为负（极深盘时可能算出负值）
    return max(lam_h, 0.05), max(lam_a, 0.05)


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
