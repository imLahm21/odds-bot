"""历史精算报告回测：第 7 节结论 vs 实际赛果。

回答的问题：报告里哪些特征（置信度、盘口定性、军规、上/下盘、盘口深度、
分层一致性、数据完整度……）真的预测了「方向命中」。

三步，可分开跑：
    python -m scripts.backtest_reports extract            # 报告 → 特征
    python -m scripts.backtest_reports results            # 按日期补比分（调 API-Football）
    python -m scripts.backtest_reports stats              # 方向命中率统计表（Markdown）
    python -m scripts.backtest_reports roi                # 按候选表赔率算 ROI（Markdown）
    python -m scripts.backtest_reports lean               # 影子倾向置信度：定线期分布 / H-CONF01 检验
    python -m scripts.backtest_reports lean --threshold 65 --since 2026-12-01

只读 report/，不改任何报告；中间产物与结果写到 --out 目录（默认 scripts/output/backtest）。
`results` 每个比赛日 1 次请求（/fixtures?date=），响应落盘缓存，重跑不重复消耗额度。
"""
from __future__ import annotations

import argparse
import difflib
import glob
import json
import os
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

TZ = "Asia/Shanghai"
_NUM = r"[+\-−－]?\d+(?:\.\d+)?"


# ─── 通用小工具 ──────────────────────────────────────────────────────────────
def _plain(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    s = re.sub(r"\*\*|__|`|\$", "", s)
    return s.replace("−", "-").replace("－", "-").replace("–", "-")


def _norm_team(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold()
    s = re.sub(r"\b(fc|cf|sc|afc|fk|if|bk|sk|club|de|the)\b", " ", s)
    return re.sub(r"[^a-z0-9㐀-鿿]+", "", s)


def _section(text: str, num: str) -> str:
    m = re.search(rf"(?m)^#{{2,4}}\s*{re.escape(num)}[\.、．]\s*.*$", text)
    if not m:
        return ""
    rest = text[m.end():]
    nxt = re.search(r"(?m)^#{2,3}\s*\d+[a-d]?[\.、．]", rest)
    return rest[: nxt.start()] if nxt else rest


def _field(section: str, label: str) -> str:
    """取「- **label**：……」这一条（含其后缩进续行）。"""
    m = re.search(rf"(?m)^\s*[-*]?\s*{label}\s*[：:](.*)$", section)
    if not m:
        return ""
    out = [m.group(1)]
    for line in section[m.end():].splitlines()[1:]:
        if re.match(r"^\s{2,}\S", line) and not re.match(r"^\s*[-*]\s+\S+判定", line):
            out.append(line)
        else:
            break
    return "\n".join(out).strip()


# ─── 第 7 节各判定解析 ───────────────────────────────────────────────────────
def _team_side(text: str, home: str, away: str) -> list[tuple[int, str]]:
    """文本中出现的主/客队名位置 → [(pos, 'home'|'away')]。"""
    hits = []
    low = text.casefold()
    for side, name in (("home", home), ("away", away)):
        variants = {name, name.replace("_", " ")}
        for tok in re.split(r"[\s_/]+", name):
            if len(tok) >= 4 and tok.casefold() not in ("united", "city", "club", "real", "sporting"):
                variants.add(tok)
        for v in variants:
            v = v.casefold().strip()
            if not v:
                continue
            for m in re.finditer(re.escape(v), low):
                hits.append((m.start(), side))
    for m in re.finditer(r"主队|主让|主受让", text):
        hits.append((m.start(), "home"))
    for m in re.finditer(r"客队|客让|客受让", text):
        hits.append((m.start(), "away"))
    return sorted(set(hits))


def parse_ah(raw: str, home: str, away: str) -> dict:
    """亚盘判定 → {side: home/away, line: 该队让球（负=让出）} 或 {skip: 原因}。"""
    t = _plain(raw).split("\n")[0]
    t = re.split(r"[;；。]|若只能|保守|稳健|次选|或选择|备选", t)[0]
    if not t.strip():
        return {"skip": "empty"}
    if re.search(r"回避|不下注|放弃|观望|pass|不建议下注|无法判定", t, re.I):
        return {"skip": "abstain"}
    team_hits = _team_side(t, home, away)
    # 盘口数字及其附着的球队
    lines = []
    for m in re.finditer(rf"(受让|让)?\s*(?<![\d.])({_NUM})(?![\d.%])", t):
        v = m.group(2)
        try:
            val = float(v)
        except ValueError:
            continue
        if abs(val) > 5 or (abs(val) * 4) % 1:
            continue
        signed = v[0] in "+-"
        if m.group(1) == "受让":
            val, signed = abs(val), True
        elif m.group(1) == "让":
            val, signed = -abs(val), True
        before = [s for p, s in team_hits if p < m.start()]
        lines.append((m.start(), val, signed, before[-1] if before else None))
    if re.search(r"平手", t) and not lines:
        lines.append((t.find("平手"), 0.0, True, None))
    if re.search(r"平半|平/半", t) and not lines:
        lines.append((t.find("平"), -0.25, False, None))
    if re.search(r"一球半", t) and not lines:
        lines.append((t.find("一球半"), -1.5, False, None))

    kw = re.search(r"下注|倾向|小注|轻倾|首选|选择|取", t)
    after = t[kw.end():] if kw else t
    after_off = kw.end() if kw else 0
    pick_side = None
    upper = re.search(r"上盘", after)
    lower = re.search(r"下盘", after)
    teams_after = [(p, s) for p, s in team_hits if p >= after_off]
    if teams_after:
        pick_side = teams_after[0][1]
    # 盘口归属：优先取带符号且附着球队的数字
    fav = None
    owned = [(val, side) for _, val, signed, side in lines if signed and side]
    if owned:
        val, side = owned[0]
        if val < 0:
            fav = side
        elif val > 0:
            fav = "away" if side == "home" else "home"
    absline = abs(lines[0][1]) if lines else None
    if pick_side is None and (upper or lower) and fav:
        is_upper = bool(upper) and not (lower and lower.start() < upper.start())
        pick_side = fav if is_upper else ("away" if fav == "home" else "home")
    if pick_side is None or absline is None:
        return {"skip": "unparsed", "raw": t[:120]}
    if absline == 0:
        line = 0.0
    else:
        if fav is None:
            # 无归属：用上/下盘词推断选中方是否让球方
            if upper and not lower:
                fav = pick_side
            elif lower and not upper:
                fav = "away" if pick_side == "home" else "home"
            else:
                return {"skip": "no_favourite", "raw": t[:120]}
        line = -absline if pick_side == fav else absline
    return {"side": pick_side, "line": line}


def parse_1x2(raw: str, home: str, away: str) -> dict:
    t = _plain(raw).split("\n")[0]

    def one(seg: str) -> str | None:
        seg = seg.strip()
        m = re.search(r"主胜|客胜|平局|平(?![手半])|(\S+?)\s*(?:客)?胜", seg)
        if not m:
            return None
        g = m.group(0)
        if g.startswith("主胜"):
            return "home"
        if g.startswith("客胜"):
            return "away"
        if g.startswith("平"):
            return "draw"
        hits = _team_side(seg[: m.end()], home, away)
        return hits[-1][1] if hits else None

    first = re.search(r"首选(.*?)(?:[,，;；。]|次选|$)", t)
    second = re.search(r"次选(.*?)(?:[,，;；。(（]|$)", t)
    return {"first": one(first.group(1)) if first else None,
            "second": one(second.group(1)) if second else None}


def parse_ou(raw: str) -> dict:
    t = _plain(raw).split("\n")[0]
    t = re.split(r"[;；。]|保守|次选|备选", t)[0]
    if not t.strip():
        return {"skip": "empty"}
    m_line = re.search(r"(\d(?:\.\d+)?)\s*球", t)
    m_side = re.search(r"大球|小球|\bOver\b|\bUnder\b|大\s*\d|小\s*\d", t, re.I)
    if re.search(r"回避|不下注|放弃|观望", t) and not m_side:
        return {"skip": "abstain"}
    if not (m_line and m_side):
        return {"skip": "unparsed", "raw": t[:100]}
    s = m_side.group(0).lower()
    return {"side": "over" if s[0] in "大o" else "under",
            "line": float(m_line.group(1))}


def parse_scores(raw: str) -> list[tuple[int, int]]:
    return [(int(a), int(b)) for a, b in
            re.findall(r"(\d)\s*[:：\-]\s*(\d)", _plain(raw))][:2]


def _layer(text: str, key: str) -> str | None:
    m = re.search(rf"{key}\s*(?:层)?\s*(?:[(（][^)）]*[)）])?\s*[=：:]\s*\[?\s*([^\n｜|]*)", text)
    if not m:
        return None
    seg = m.group(1)[:40]
    for word, tag in (("冲突", "conflict"), ("中性", "neutral"), ("支持", "support"),
                      ("无", "neutral"), ("一致", "support")):
        if word in seg:
            return tag
    return None


# ─── decision_final 注释（影子倾向只从这里读，不解析正文）──────────────────────
# 与 bot/typesafe_decisions.py 的 _DECISION_FINAL_RE 同口径；脚本保持不依赖 bot 包。
_DECISION_FINAL_RE = re.compile(
    r"(?m)^[ \t]*<!--[ \t]*decision_final[ \t]+(\{.*\})[ \t]*-->")
_LEAN_ID_RE = re.compile(r"C\d{2,}")


def decision_final(text: str) -> dict | None:
    """报告里最后一条 decision_final 注释 → dict；没有或不合法返回 None。"""
    matches = list(_DECISION_FINAL_RE.finditer(text or ""))
    if not matches:
        return None
    try:
        data = json.loads(matches[-1].group(1))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) and data.get("schema") == "decision_final/1" else None


def _lean_from_final(final: dict | None) -> dict | None:
    """decision_final.lean → {id, play, odds, conf}；字段缺失或越界返回 None。"""
    lean = (final or {}).get("lean")
    if not isinstance(lean, dict):
        return None
    cid, play, odds, conf = lean.get("id"), lean.get("play"), lean.get("odds"), lean.get("conf")
    if (not isinstance(cid, str) or not _LEAN_ID_RE.fullmatch(cid)
            or not isinstance(play, str) or not play.strip()
            or isinstance(odds, bool) or not isinstance(odds, (int, float)) or not odds > 1.0
            or isinstance(conf, bool) or not isinstance(conf, int) or not 50 <= conf <= 100):
        return None
    return {"id": cid, "play": play.strip(), "odds": float(odds), "conf": conf}


def _lean_line_kind(plain7: str) -> str | None:
    """仅作诊断计数：第 7 节有没有写影子倾向行（None 无 / "none" 写「无」 / "pick" 选了边）。
    样本数据一律取 decision_final，不从这里取。"""
    m = re.search(r"(?m)^\s*[-*]?\s*影子倾向\s*[：:]\s*(.*)$", plain7)
    if not m:
        return None
    return "none" if re.match(r"^[\[【「(（]*无", m.group(1).strip()) else "pick"


def extract_one(path: str, text: str) -> dict | None:
    head = re.search(r"(?m)^##\s*比赛\s*[：:]\s*(.+?)\s+vs\.?\s+(.+?)\s*$", text)
    stem = Path(path).name.replace("_report.md", "")
    if head:
        home, away = head.group(1).strip(" *"), head.group(2).strip(" *")
    elif "_vs_" in stem:
        home, away = (x.replace("_", " ") for x in stem.split("_vs_", 1))
    else:
        return None
    ko = re.search(r"开球时间\s*[：:]\s*\**\s*(\d{4}-\d{2}-\d{2})(?:\s+(\d{1,2}:\d{2}))?", text)
    league = re.search(r"(?m)^##\s*赛事\s*[：:]\s*(.+?)\s*开球时间", text)
    folder = Path(path).parent.name
    s2, s4, s7 = _section(text, "2"), _section(text, "4"), _section(text, "7")
    plain7 = _plain(s7)
    conf = re.search(r"(?m)^\s*[-*]?\s*置信度\s*[：:]\s*(\d{1,3})", plain7)
    p2 = _plain(s2)
    qual = (re.search(r"【[^】]{0,12}?(开大|深开|开小|浅开|合适|实力盘)", p2)
            or re.search(r"判定[^\n]{0,30}?(开大|深开|开小|浅开|合适|实力盘)", p2))
    rule = re.search(r"#\s*(\d{1,2})", _plain(_field(s4, r"\**匹配军规\**") or s4[:300]))
    got = re.search(r"已获取\s*(\d{1,2})\s*/\s*10", _plain(text))
    missed = re.search(r"已错过\s*(\d{1,2})", _plain(text))
    row = {
        "path": path, "folder": folder, "home": home, "away": away,
        "league": league.group(1).strip(" *") if league else "",
        "kickoff_date": ko.group(1) if ko else folder,
        "kickoff_time": ko.group(2) if ko and ko.group(2) else "",
        "confidence": int(conf.group(1)) if conf and int(conf.group(1)) <= 100 else None,
        "qualitative": ({"深开": "开大", "浅开": "开小", "实力盘": "合适"}.get(
            qual.group(1), qual.group(1)) if qual else None),
        "rule": int(rule.group(1)) if rule else None,
        "nodes_got": int(got.group(1)) if got else None,
        "nodes_missed": int(missed.group(1)) if missed else None,
        "ah": parse_ah(_field(plain7, "亚盘判定"), home, away),
        "x12": parse_1x2(_field(plain7, "胜平负判定"), home, away),
        "ou": parse_ou(_field(plain7, "大小球判定")),
        "scores": parse_scores(_field(plain7, "比分预测")),
        "layers": {k: _layer(plain7, k) for k in "BCD"},
        "s8": parse_section8(_section(text, "8"), home, away),
        # 新格式（2026-11 重新分层）：第 7 节信号强度 / 数据质量，第 4 节假设表；旧报告为 None / []
        "signal": _signal(plain7),
        "quality": _quality(plain7),
        "hypotheses": _hypothesis_rows(s4),
        "no_lean": {k: "无倾向" in (_field(plain7, label) or "")
                    for k, label in (("ah", "亚盘判定"), ("x12", "胜平负判定"), ("ou", "大小球判定"))},
    }
    # 影子倾向（docs/置信度影子倾向计划.md）：只读 decision_final
    final = decision_final(text)
    lean = _lean_from_final(final)
    row["has_final"] = final is not None
    row["lean"] = lean
    row["writer_model"] = str((final or {}).get("writer_model") or "")
    row["lean_cls"] = classify_play(lean["play"], home, away) if lean else None
    row["lean_line"] = _lean_line_kind(plain7)
    if row["rule"] is None and row["hypotheses"]:
        m = next((h["code"] for h in row["hypotheses"] if h["code"].startswith("H-M")), None)
        row["rule"] = int(m[3:]) if m and m[3:].isdigit() else None
    return row


def _signal(plain7: str) -> str | None:
    m = re.search(r"(?m)^\s*[-*]?\s*信号强度\s*[：:]\s*\[?\s*(无|弱|中|强)", plain7)
    return {"无": "none", "弱": "weak", "中": "medium", "强": "strong"}[m.group(1)] if m else None


def _quality(plain7: str) -> int | None:
    m = re.search(r"(?m)^\s*[-*]?\s*数据质量\s*[：:]\s*\D{0,4}?(\d{1,3})", plain7)
    return int(m.group(1)) if m and int(m.group(1)) <= 100 else None


def _hypothesis_rows(s4: str) -> list[dict]:
    """第 4 节假设表：| 假设 | 触发依据 | 预测 | 状态 | 登记命中率 |"""
    out = []
    for line in s4.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 4 and re.fullmatch(r"H-[A-Za-z0-9-]+", _plain(cells[0])):
            out.append({"code": _plain(cells[0]), "prediction": _plain(cells[2]),
                        "status": _plain(cells[3])})
    return out


# ─── 第 8 节候选表 ───────────────────────────────────────────────────────────
_CN_LINE = (("平手", 0.0), ("平/半", 0.25), ("平半", 0.25), ("半/一", 0.75),
            ("半一", 0.75), ("一/球半", 1.25), ("球半", 1.5), ("半球", 0.5))


def _first_num(cell: str) -> float | None:
    m = re.search(_NUM, _plain(cell))
    return float(m.group(0)) if m else None


_CODE_AH_RE = re.compile(rf"^让球\s+(.+?)\s+({_NUM})$")


def classify_play(play: str, home: str, away: str) -> dict | None:
    """候选表「玩法」文本 → {market, side, line}；认不出返回 None。"""
    t = _plain(play)
    t = re.sub(r"[(（][^)）]*[)）]", "", t).strip()
    # 代码候选表的让球行「让球 <队名> <线>」（bot/candidates.py）：队名与主/客队精确匹配时直接取，
    # 避免队名里的数字（Schalke 04）或两队共享词（Manchester）干扰下面的通用解析
    m_code = _CODE_AH_RE.match(t)
    if m_code:
        team = _norm_team(m_code.group(1))
        side = ("home" if team and team == _norm_team(home) else
                "away" if team and team == _norm_team(away) else None)
        val = float(m_code.group(2))
        if side and abs(val) <= 5 and not (abs(val) * 4) % 1:
            return {"market": "ah", "side": side, "line": val}
    t = re.sub(r"胜平负|主线|次线|相邻线?|全场|大小球", " ", t)
    if re.search(r"双进|BTTS|两队都进", t, re.I):
        if re.search(r"no(?![a-z])|否", t, re.I):
            return {"market": "btts", "side": "no"}
        if re.search(r"yes|是|都进球?$|双进$", t, re.I):
            return {"market": "btts", "side": "yes"}
        return None
    if re.search(r"不败|双重|串|波胆|比分|半场|角球", t):
        return None
    hits = _team_side(t, home, away)
    m_ou = re.search(rf"(大|小|over|under)\s*球?\s*({_NUM})", t, re.I)
    if m_ou and not re.search(r"让", t):
        side = "over" if m_ou.group(1).lower()[0] in "大o" else "under"
        line = abs(float(m_ou.group(2)))
        if line > 8 or (line * 4) % 1:
            return None
        owner = [s for p, s in hits if p < m_ou.start()]
        if owner:
            return {"market": f"tt_{owner[-1]}", "side": side, "line": line}
        return {"market": "ou", "side": side, "line": line}
    if re.search(r"胜", t) and not re.search(r"让|受让|[+\-]\d", t):
        if "主胜" in t:
            return {"market": "1x2", "side": "home"}
        if "客胜" in t:
            return {"market": "1x2", "side": "away"}
        if hits:
            return {"market": "1x2", "side": hits[-1][1]}
        return None
    if re.search(r"平局|^(?:胜平负)?[\s\-—－:：]*平$", t) and not re.search(r"让|受让|平手|平/半|平半", t):
        return {"market": "1x2", "side": "draw"}
    # 亚盘
    for m in re.finditer(r"(主|客)\s*(?=[+\-]\d|受让|让)", t):
        hits.append((m.start(), "home" if m.group(1) == "主" else "away"))
    hits.sort()
    if not hits:
        return None
    side = hits[-1][1]
    tail = t[hits[-1][0]:]
    line = None
    m = re.search(rf"(受让|让)?\s*(?<![\d.])({_NUM})(?![\d.%])", tail)
    if m:
        v = m.group(2)
        val = float(v)
        if abs(val) <= 5 and not (abs(val) * 4) % 1:
            if m.group(1) == "受让":
                line = abs(val)
            elif m.group(1) == "让":
                line = -abs(val)
            elif v[0] in "+-" or val == 0:
                line = val
    if line is None:
        for word, val in _CN_LINE:
            if word in t:
                if val == 0:
                    line = 0.0
                elif "受让" in tail:
                    line = val
                elif "让" in tail:
                    line = -val
                break
    if line is None:
        return None
    return {"market": "ah", "side": side, "line": line}


def parse_section8(s8: str, home: str, away: str) -> dict | None:
    if not s8.strip():
        return None
    lines = s8.splitlines()
    header, cols = None, {}
    rows = []
    for i, line in enumerate(lines):
        if not line.strip().startswith("|"):
            if header is not None and rows:
                break
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if header is None:
            keys = [re.sub(r"[\s*_`$\\{}]", "", _plain(c)).lower() for c in cells]
            for j, k in enumerate(keys):
                if "玩法" in k or "候选" in k and "id" not in k:
                    cols.setdefault("play", j)
                elif "赔率" in k and "门槛" not in k:
                    cols.setdefault("odds", j)
                elif k.startswith("edge"):
                    cols.setdefault("edge", j)
                elif k.startswith("p") and "市场" in k:
                    cols.setdefault("p_market", j)
                elif k.startswith("p") and "最终" in k:
                    cols.setdefault("p_final", j)
                elif "资格" in k:
                    cols.setdefault("elig", j)
            if "play" in cols and "odds" in cols:
                header = i
            else:
                cols = {}
            continue
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c) or len(cells) <= max(cols.values()):
            continue
        play, odds_cell = cells[cols["play"]], cells[cols["odds"]]
        odds = _first_num(odds_cell)
        edge = None
        if "edge" in cols:
            e = _first_num(cells[cols["edge"]])
            if e is not None and "%" in cells[cols["edge"]]:
                edge = e / 100
        excluded = ("?" in odds_cell or "排除" in line or "excluded" in line.lower()
                    or "附近" in odds_cell)
        if odds is None or odds <= 1.01:
            continue

        def prob(key: str) -> float | None:
            if key not in cols:
                return None
            v = _first_num(cells[cols[key]])
            return v / 100 if v is not None and "%" in cells[cols[key]] and 0 <= v <= 100 else None

        rows.append({"play": _plain(play), "odds": odds, "edge": edge,
                     "p_market": prob("p_market"), "p_final": prob("p_final"),
                     "excluded": excluded, "cls": classify_play(play, home, away)})
    if not rows:
        return None
    plain = _plain(s8)
    sel = re.search(r"(?m)^\s*[-*]?\s*(?:最终)?选中项\s*[：:]\s*(.*)$", plain)
    sel_text = sel.group(1).strip() if sel else ""
    return {"rows": rows, "selected": sel_text}


def cmd_extract(args) -> None:
    if args.reports_json:
        texts = json.load(open(args.reports_json, encoding="utf-8"))
    else:
        texts = {f.replace("\\", "/"): open(f, encoding="utf-8", errors="replace").read()
                 for f in glob.glob(os.path.join(args.reports, "**", "*.md"), recursive=True)}
    rows = []
    skipped = 0          # 复盘（_review.md）、只盲推（_review_blind.md）等非赛前报告
    for path, text in sorted(texts.items()):
        if not path.endswith("_report.md"):
            skipped += 1
            continue
        row = extract_one(path, text)
        if row:
            rows.append(row)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    json.dump(rows, open(out / "features.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    n = len(rows)
    print(f"报告 {n} 份")
    print("  置信度", sum(r["confidence"] is not None for r in rows))
    print("  亚盘方向", sum("side" in r["ah"] for r in rows),
          "弃权", sum(r["ah"].get("skip") == "abstain" for r in rows),
          "未解析", sum(r["ah"].get("skip") in ("unparsed", "no_favourite", "empty") for r in rows))
    print("  胜平负首选", sum(bool(r["x12"]["first"]) for r in rows))
    print("  大小球", sum("side" in r["ou"] for r in rows))
    print("  定性", sum(bool(r["qualitative"]) for r in rows),
          "军规", sum(r["rule"] is not None for r in rows))
    print(f"影子倾向 {sum(r['lean'] is not None for r in rows)} 份"
          f"（其中玩法可解析 {sum(_lean_ah(r) is not None for r in rows)} 份）")
    print(f"  已按文件名排除非赛前报告（复盘 / 盲推等，非 _report.md）{skipped} 份")


# ─── 赛果 ────────────────────────────────────────────────────────────────────
def cmd_results(args) -> None:
    # 只用标准库（不依赖 requests/dotenv），便于在没装依赖的本机直接跑
    import time
    import urllib.parse
    import urllib.request

    root = Path(__file__).resolve().parent.parent
    key = os.getenv("APIFOOTBALL_KEY", "")
    if not key and (root / ".env").exists():
        for line in (root / ".env").read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("APIFOOTBALL_KEY="):
                key = line.split("=", 1)[1].strip().strip("'\"")
                break
    if not key:
        raise SystemExit("未找到 APIFOOTBALL_KEY")
    state = {"remaining": None}

    def api_get(endpoint: str, params: dict | None = None) -> dict | None:
        url = "https://v3.football.api-sports.io" + endpoint
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"x-apisports-key": key})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                state["remaining"] = resp.headers.get("x-ratelimit-requests-remaining")
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            print("  请求异常", type(exc).__name__, getattr(exc, "code", ""))
            return None
        if data.get("errors"):
            print("  API errors:", data["errors"])
            return None
        return data

    status = api_get("/status")
    if status:
        resp = status.get("response") or {}
        print("套餐", (resp.get("subscription") or {}).get("plan"),
              "今日用量", resp.get("requests"))
    if args.status_only:
        return

    out = Path(args.out)
    rows = json.load(open(out / "features.json", encoding="utf-8"))
    cache = out / "fixtures_by_date"
    cache.mkdir(exist_ok=True)
    dates = sorted({r["kickoff_date"] for r in rows})
    todo = [d for d in dates if not (cache / f"{d}.json").exists()]
    print(f"比赛日 {len(dates)} 个，需请求 {len(todo)} 个")
    if args.limit:
        todo = todo[: args.limit]
    for d in todo:
        data = api_get("/fixtures", {"date": d, "timezone": TZ})
        time.sleep(0.25)
        if not data:
            print("  失败", d)
            continue
        slim = [{"id": x["fixture"]["id"], "date": x["fixture"]["date"],
                 "status": x["fixture"]["status"]["short"],
                 "league": x["league"]["name"], "country": x["league"]["country"],
                 "home": x["teams"]["home"]["name"], "away": x["teams"]["away"]["name"],
                 "gh": x["goals"]["home"], "ga": x["goals"]["away"],
                 "ft": x["score"]["fulltime"]}
                for x in data.get("response", [])]
        json.dump(slim, open(cache / f"{d}.json", "w", encoding="utf-8"),
                  ensure_ascii=False)
        print(f"  {d}: {len(slim)} 场，剩余额度 {state['remaining']}")


def _match_result(row: dict, cache: Path) -> dict | None:
    f = cache / f"{row['kickoff_date']}.json"
    if not f.exists():
        return None
    fixtures = json.loads(f.read_text(encoding="utf-8"))
    h, a = _norm_team(row["home"]), _norm_team(row["away"])
    best, best_score = None, 0.0
    for x in fixtures:
        sh = difflib.SequenceMatcher(None, h, _norm_team(x["home"])).ratio()
        sa = difflib.SequenceMatcher(None, a, _norm_team(x["away"])).ratio()
        score = min(sh, sa)
        if score > best_score:
            best, best_score = x, score
    if best is None or best_score < 0.82:
        return None
    ft = best.get("ft") or {}
    gh, ga = ft.get("home"), ft.get("away")
    if gh is None or ga is None or best["status"] not in ("FT", "AET", "PEN"):
        return None
    return {"gh": gh, "ga": ga, "fixture_id": best["id"], "status": best["status"]}


# ─── 结算 ────────────────────────────────────────────────────────────────────
def settle_ah(margin: int, line: float) -> float:
    """选中方净胜球 margin、让球 line（负=让出）→ 单位结果 +1/+0.5/0/−0.5/−1。"""
    def half(l: float) -> float:
        v = margin + l
        return 1.0 if v > 1e-9 else (-1.0 if v < -1e-9 else 0.0)
    if abs(line * 4) % 2 == 1:
        return (half(line - 0.25) + half(line + 0.25)) / 2
    return half(line)


def settle_ou(total: int, line: float, side: str) -> float:
    def half(l: float) -> float:
        v = total - l
        r = 1.0 if v > 1e-9 else (-1.0 if v < -1e-9 else 0.0)
        return r if side == "over" else -r
    if abs(line * 4) % 2 == 1:
        return (half(line - 0.25) + half(line + 0.25)) / 2
    return half(line)


class Tally:
    __slots__ = ("n", "win", "lose", "push", "units")

    def __init__(self) -> None:
        self.n = self.win = self.lose = self.push = 0
        self.units = 0.0

    def add(self, r: float) -> None:
        self.n += 1
        self.units += r
        if r > 0:
            self.win += 1
        elif r < 0:
            self.lose += 1
        else:
            self.push += 1

    def hit(self) -> float | None:
        d = self.win + self.lose
        return self.win / d if d else None


def _ci(p: float, n: int) -> float:
    return 1.96 * (p * (1 - p) / n) ** 0.5 if n else 0.0


def _table(title: str, groups: dict[str, Tally], order=None, min_n: int = 1) -> list[str]:
    out = [f"### {title}", "",
           "| 分组 | 场次 | 命中 | 未中 | 走盘 | 方向命中率 | ±95% |",
           "|------|------|------|------|------|-----------|------|"]
    keys = order or sorted(groups, key=lambda k: -groups[k].n)
    for k in keys:
        t = groups.get(k)
        if not t or t.n < min_n:
            continue
        h = t.hit()
        d = t.win + t.lose
        out.append(f"| {k} | {t.n} | {t.win} | {t.lose} | {t.push} | "
                   + (f"{h:.1%} | ±{_ci(h, d):.1%} |" if h is not None else "— | — |"))
    out.append("")
    return out


def conf_bucket(c) -> str:
    if c is None:
        return "无"
    for lo, hi, name in ((0, 60, "<60"), (60, 64, "60–64"), (64, 66, "64–66"),
                         (66, 70, "66–70"), (70, 74, "70–74"), (74, 101, "74+")):
        if lo <= c < hi:
            return name
    return "无"


CONF_ORDER = ["<60", "60–64", "64–66", "66–70", "70–74", "74+", "无"]


def cmd_stats(args) -> None:
    out = Path(args.out)
    rows = json.load(open(out / "features.json", encoding="utf-8"))
    cache = out / "fixtures_by_date"
    matched = []
    for r in rows:
        res = _match_result(r, cache)
        if res:
            matched.append((r, res))
    md = ["# 历史报告回测", "",
          f"- 报告 {len(rows)} 份；匹配到完场比分 {len(matched)} 份",
          "- 方向命中率 =（全赢+半赢）/（总数−走盘）；±95% 为正态近似区间",
          "- 亚盘 ≈1.90 赔率下的保本命中率约 52.6%", ""]

    G: dict[str, dict[str, Tally]] = defaultdict(lambda: defaultdict(Tally))
    for r, res in matched:
        gh, ga = res["gh"], res["ga"]
        cb = conf_bucket(r["confidence"])
        ah, ou, x12 = r["ah"], r["ou"], r["x12"]
        actual = "home" if gh > ga else ("away" if ga > gh else "draw")
        month = r["kickoff_date"][:7]
        if "side" in ah:
            margin = (gh - ga) if ah["side"] == "home" else (ga - gh)
            res_ah = settle_ah(margin, ah["line"])
            role = "让球方(上盘)" if ah["line"] < 0 else ("受让方(下盘)" if ah["line"] > 0 else "平手")
            depth = abs(ah["line"])
            dname = ("0~0.25" if depth <= 0.25 else "0.5~0.75" if depth <= 0.75
                     else "1~1.25" if depth <= 1.25 else "≥1.5")
            G["亚盘｜全部"]["全部"].add(res_ah)
            G["亚盘｜按置信度"][cb].add(res_ah)
            G["亚盘｜≥70 vs <70"]["≥70" if (r["confidence"] or 0) >= 70 else "<70"].add(res_ah)
            G["亚盘｜选上盘还是下盘"][role].add(res_ah)
            G["亚盘｜盘口深度"][dname].add(res_ah)
            G["亚盘｜选主队还是客队"]["主队" if ah["side"] == "home" else "客队"].add(res_ah)
            G["亚盘｜初盘定性"][r["qualitative"] or "未识别"].add(res_ah)
            G["亚盘｜定性×上下盘"][f"{r['qualitative'] or '未识别'}·{role}"].add(res_ah)
            G["亚盘｜军规编号"][f"#{r['rule']}" if r["rule"] else "无"].add(res_ah)
            G["亚盘｜月份"][month].add(res_ah)
            G["亚盘｜联赛"][r["league"][:18] or "未知"].add(res_ah)
            for k, name in (("B", "基本面"), ("C", "风控")):
                if r["layers"].get(k):
                    G[f"亚盘｜{k} 层({name})与 A 层关系"][r["layers"][k]].add(res_ah)
            if r["nodes_got"] is not None:
                g = r["nodes_got"]
                G["亚盘｜已获取节点数"]["10" if g == 10 else "7~9" if g >= 7 else "4~6" if g >= 4 else "≤3"].add(res_ah)
            if x12["first"]:
                agree = x12["first"] == ah["side"]
                G["亚盘｜与胜平负首选是否同队"]["同队" if agree else
                                         ("首选平局" if x12["first"] == "draw" else "相反")].add(res_ah)
            # 对照基线：同一批比赛里，无脑买上盘 / 下盘
            if ah["line"] != 0:
                fav_res = res_ah if ah["line"] < 0 else -res_ah
                G["亚盘｜基线（同批比赛同一条线）"]["无脑上盘"].add(fav_res)
                G["亚盘｜基线（同批比赛同一条线）"]["无脑下盘"].add(-fav_res)
        if x12["first"]:
            hit = 1.0 if x12["first"] == actual else -1.0
            G["胜平负首选｜全部"]["全部"].add(hit)
            G["胜平负首选｜按置信度"][cb].add(hit)
            G["胜平负首选｜选了什么"][{"home": "主胜", "draw": "平局", "away": "客胜"}[x12["first"]]].add(hit)
            if x12["second"]:
                both = 1.0 if actual in (x12["first"], x12["second"]) else -1.0
                G["胜平负首选+次选｜按置信度"][cb].add(both)
        if "side" in ou:
            res_ou = settle_ou(gh + ga, ou["line"], ou["side"])
            G["大小球｜全部"]["全部"].add(res_ou)
            G["大小球｜按置信度"][cb].add(res_ou)
            G["大小球｜选大还是小"]["大球" if ou["side"] == "over" else "小球"].add(res_ou)
            ln = ou["line"]
            G["大小球｜盘口线"]["≤2.25" if ln <= 2.25 else "2.5~2.75" if ln <= 2.75
                              else "3~3.25" if ln <= 3.25 else "≥3.5"].add(res_ou)
            G["大小球｜月份"][month].add(res_ou)
        if r["scores"]:
            G["比分预测｜任一命中"]["全部"].add(1.0 if (gh, ga) in [tuple(s) for s in r["scores"]] else -1.0)

    for title in G:
        order = CONF_ORDER if "置信度" in title else None
        min_n = 8 if any(k in title for k in ("联赛", "军规", "×")) else 1
        md += _table(title, G[title], order, min_n)

    # 实际结果分布（供参照）
    dist = Tally()
    actual_cnt = defaultdict(int)
    for r, res in matched:
        actual_cnt["主胜" if res["gh"] > res["ga"] else "客胜" if res["ga"] > res["gh"] else "平局"] += 1
    md += ["### 实际赛果分布（参照）", ""] + [
        f"- {k}：{v}（{v / max(len(matched), 1):.1%}）" for k, v in actual_cnt.items()] + [""]
    (out / "backtest.md").write_text("\n".join(md), encoding="utf-8")
    print("\n".join(md))


# ─── ROI：用第 8 节候选表里的赔率结算 ────────────────────────────────────────
def settle_cls(cls: dict, gh: int, ga: int) -> float | None:
    """候选 → 单位结果 +1/+0.5/0/−0.5/−1。"""
    m = cls["market"]
    if m == "ah":
        margin = (gh - ga) if cls["side"] == "home" else (ga - gh)
        return settle_ah(margin, cls["line"])
    if m == "ou":
        return settle_ou(gh + ga, cls["line"], cls["side"])
    if m in ("tt_home", "tt_away"):
        return settle_ou(gh if m == "tt_home" else ga, cls["line"], cls["side"])
    if m == "btts":
        yes = gh > 0 and ga > 0
        return 1.0 if yes == (cls["side"] == "yes") else -1.0
    if m == "1x2":
        actual = "home" if gh > ga else ("away" if ga > gh else "draw")
        return 1.0 if actual == cls["side"] else -1.0
    return None


def pnl(result: float, odds: float) -> float:
    """单位注额净收益：赢按 (赔率−1) 计，输按 1 计，半赢半输减半。"""
    return result * (odds - 1.0) if result > 0 else result


class Roi:
    __slots__ = ("n", "win", "lose", "push", "profit", "odds_sum", "sq")

    def __init__(self) -> None:
        self.n = self.win = self.lose = self.push = 0
        self.profit = self.odds_sum = self.sq = 0.0

    def add(self, result: float, odds: float) -> None:
        self.n += 1
        self.odds_sum += odds
        gain = pnl(result, odds)
        self.profit += gain
        self.sq += gain * gain
        if result > 0:
            self.win += 1
        elif result < 0:
            self.lose += 1
        else:
            self.push += 1


def _roi_table(title: str, groups: dict[str, Roi], order=None, min_n: int = 1) -> list[str]:
    out = [f"### {title}", "",
           "| 分组 | 注数 | 方向命中率 | 平均赔率 | 净盈亏(单位) | ROI | ROI ±95% |",
           "|------|------|-----------|---------|-------------|-----|----------|"]
    for k in (order or sorted(groups, key=lambda k: -groups[k].n)):
        g = groups.get(k)
        if not g or g.n < min_n:
            continue
        d = g.win + g.lose
        hit = f"{g.win / d:.1%}" if d else "—"
        roi = g.profit / g.n
        var = max(g.sq / g.n - roi * roi, 0.0)          # 单注收益的样本方差
        ci = 1.96 * (var / g.n) ** 0.5
        out.append(f"| {k} | {g.n} | {hit} | {g.odds_sum / g.n:.2f} | "
                   f"{g.profit:+.1f} | {roi:+.1%} | ±{ci:.1%} |")
    out.append("")
    return out


def _edge_bucket(e: float | None) -> str:
    if e is None:
        return "无 edge"
    for hi, name in ((-0.10, "< −10%"), (-0.05, "−10~−5%"), (0.0, "−5~0%"),
                     (0.05, "0~+5%"), (0.10, "+5~+10%")):
        if e <= hi:
            return name
    return "> +10%"


EDGE_ORDER = ["< −10%", "−10~−5%", "−5~0%", "0~+5%", "+5~+10%", "> +10%", "无 edge"]
_MARKET_NAME = {"ah": "让球", "ou": "全场大小球", "1x2": "胜平负", "btts": "双进",
                "tt_home": "单队大小球", "tt_away": "单队大小球"}


def _norm_play(s: str) -> str:
    return re.sub(r"[^0-9a-z㐀-鿿+\-.]", "", _plain(s).casefold())


def _same_cls(a: dict | None, b: dict | None) -> bool:
    if not a or not b or a["market"] != b["market"] or a.get("side") != b.get("side"):
        return False
    return abs(a.get("line", 0.0) - b.get("line", 0.0)) < 1e-9


def _sane(row: dict) -> bool:
    cls = row["cls"]
    if not cls or row["excluded"]:
        return False
    if cls["market"] in ("ah", "ou", "tt_home", "tt_away") and not 1.30 <= row["odds"] <= 3.50:
        return False                      # 两出口盘的极端价多为不可执行副线
    return 1.05 <= row["odds"] <= 30


def cmd_roi(args) -> None:
    out = Path(args.out)
    rows = json.load(open(out / "features.json", encoding="utf-8"))
    cache = out / "fixtures_by_date"
    G: dict[str, dict[str, Roi]] = defaultdict(lambda: defaultdict(Roi))
    n_tab = n_match = 0
    cover: dict[str, int] = defaultdict(int)
    for r in rows:
        if not r.get("s8"):
            continue
        n_tab += 1
        res = _match_result(r, cache)
        if not res:
            continue
        n_match += 1
        gh, ga = res["gh"], res["ga"]
        cb = conf_bucket(r["confidence"])
        ge70 = "≥70" if (r["confidence"] or 0) >= 70 else "<70"
        table = [x for x in r["s8"]["rows"] if _sane(x)]

        def find(cls: dict | None, table=table) -> dict | None:
            return next((x for x in table if _same_cls(x["cls"], cls)), None)

        # A. 第 7 节判定 → 候选表里对应行的赔率
        ah, ou, x12 = r["ah"], r["ou"], r["x12"]
        if "side" in ah:
            cover["亚盘判定"] += 1
            row = find({"market": "ah", "side": ah["side"], "line": ah["line"]})
            if row:
                cover["亚盘判定·配到赔率"] += 1
                rs = settle_cls(row["cls"], gh, ga)
                role = ("让球方(上盘)" if ah["line"] < 0 else
                        "受让方(下盘)" if ah["line"] > 0 else "平手")
                G["A1 第7节亚盘判定｜全部"]["全部"].add(rs, row["odds"])
                G["A1 第7节亚盘判定｜按置信度"][cb].add(rs, row["odds"])
                G["A1 第7节亚盘判定｜≥70 vs <70"][ge70].add(rs, row["odds"])
                G["A1 第7节亚盘判定｜上盘还是下盘"][role].add(rs, row["odds"])
                G["A1 第7节亚盘判定｜初盘定性"][r["qualitative"] or "未识别"].add(rs, row["odds"])
                G["A1 第7节亚盘判定｜报告自算 edge"][_edge_bucket(row["edge"])].add(rs, row["odds"])
                if x12["first"]:
                    G["A1 第7节亚盘判定｜与胜平负首选关系"][
                        "同队" if x12["first"] == ah["side"] else
                        "首选平局" if x12["first"] == "draw" else "相反"].add(rs, row["odds"])
        if "side" in ou:
            cover["大小球判定"] += 1
            row = find({"market": "ou", "side": ou["side"], "line": ou["line"]})
            if row:
                cover["大小球判定·配到赔率"] += 1
                rs = settle_cls(row["cls"], gh, ga)
                G["A2 第7节大小球判定｜全部"]["全部"].add(rs, row["odds"])
                G["A2 第7节大小球判定｜按置信度"][cb].add(rs, row["odds"])
                G["A2 第7节大小球判定｜大还是小"][
                    "大球" if ou["side"] == "over" else "小球"].add(rs, row["odds"])
        if x12["first"]:
            cover["胜平负首选"] += 1
            row = find({"market": "1x2", "side": x12["first"]})
            if row:
                cover["胜平负首选·配到赔率"] += 1
                rs = settle_cls(row["cls"], gh, ga)
                G["A3 第7节胜平负首选｜全部"]["全部"].add(rs, row["odds"])
                G["A3 第7节胜平负首选｜按置信度"][cb].add(rs, row["odds"])
                G["A3 第7节胜平负首选｜选了什么"][
                    {"home": "主胜", "draw": "平局", "away": "客胜"}[x12["first"]]].add(rs, row["odds"])

        # B. 候选表全部可结算行：报告自算的 edge 有没有预测力
        for x in table:
            rs = settle_cls(x["cls"], gh, ga)
            name = _MARKET_NAME[x["cls"]["market"]]
            G["B1 候选表全部行｜按玩法"][name].add(rs, x["odds"])
            G["B2 候选表全部行｜按报告自算 edge"][_edge_bucket(x["edge"])].add(rs, x["odds"])
            if x["edge"] is not None and x["edge"] > 0:
                G["B3 正 edge 行｜按玩法"][name].add(rs, x["odds"])
                G["B4 正 edge 行｜按置信度"][cb].add(rs, x["odds"])

        # C. 每份报告 edge 最高的正值行（代码兜底选法）
        pos = [x for x in table if x["edge"] is not None and x["edge"] > 0]
        if pos:
            top = max(pos, key=lambda x: x["edge"])
            rs = settle_cls(top["cls"], gh, ga)
            name = _MARKET_NAME[top["cls"]["market"]]
            G["C1 每场 edge 最高正值项｜全部"]["全部"].add(rs, top["odds"])
            G["C1 每场 edge 最高正值项｜按置信度"][cb].add(rs, top["odds"])
            G["C1 每场 edge 最高正值项｜≥70 vs <70"][ge70].add(rs, top["odds"])
            G["C1 每场 edge 最高正值项｜按玩法"][name].add(rs, top["odds"])
        else:
            cover["无正 edge 行"] += 1

        # E. 正 edge 的来源拆解（WP-0.7）：校准加成 / 负向平移副作用 / 纯价格偏离
        for x in table:
            if x["edge"] is None or x["edge"] <= 0:
                continue
            pm, pf = x.get("p_market"), x.get("p_final")
            if pm is None or pf is None:
                src = "p 列缺失"
            elif pf - pm > 0.0005:
                src = "p_最终 > p_市场（校准加成）"
            elif pm - pf > 0.0005:
                src = "p_最终 < p_市场（负向平移的对立面）"
            else:
                src = "p_最终 = p_市场（纯价格偏离）"
            G["E1 正 edge 行｜按来源"][src].add(settle_cls(x["cls"], gh, ga), x["odds"])

        # F. 阶段 0 之后的入池预估：p_最终 = p_市场，按 p_市场×赔率−1 重算（二元近似）
        repriced = []
        for x in table:
            pm = x.get("p_market")
            if pm is not None and not x.get("excluded"):
                repriced.append((pm * x["odds"] - 1, x))
        if repriced:
            best_edge, best = max(repriced, key=lambda t: t[0])
            rs = settle_cls(best["cls"], gh, ga)
            if best_edge > 0:
                tier = "价值注（市场价 edge>0）"
            elif best_edge >= -0.03:
                tier = "探索注（−3% ≤ edge ≤ 0）"
            else:
                tier = "空仓（edge < −3%）"
            G["F1 过渡期入池预估｜每场最高 edge 项（二元近似）"][tier].add(rs, best["odds"])
            cover["F 有 p_市场 列"] += 1

        # D. 报告自己写的「选中项」
        sel = r["s8"]["selected"]
        if sel and not re.search(r"(?i)pass|空仓|不下注|全负", sel[:40]):
            nsel = _norm_play(sel)
            cands = [x for x in table if len(_norm_play(x["play"])) >= 3
                     and _norm_play(x["play"]) in nsel]
            row = max(cands, key=lambda x: len(_norm_play(x["play"]))) if cands else None
            if row is None:
                row = find(classify_play(
                    re.split(r"[,，;；。@]|赔率|edge", sel)[0], r["home"], r["away"]))
            cover["报告写了选中项"] += 1
            if row:
                cover["选中项·配到行"] += 1
                rs = settle_cls(row["cls"], gh, ga)
                name = _MARKET_NAME[row["cls"]["market"]]
                G["D1 报告选中项｜全部"]["全部"].add(rs, row["odds"])
                G["D1 报告选中项｜按置信度"][cb].add(rs, row["odds"])
                G["D1 报告选中项｜≥70 vs <70"][ge70].add(rs, row["odds"])
                G["D1 报告选中项｜按玩法"][name].add(rs, row["odds"])
        elif sel:
            cover["报告判 pass"] += 1

    md = ["# 历史报告 ROI 回测", "",
          f"- 有第 8 节候选表的报告 {n_tab} 份；配到完场比分 {n_match} 份",
          "- 赔率取自报告候选表（大庄参考价，非 BCG 可执行价）；每注 1 单位平注",
          "- ROI = 净盈亏 / 注数；±95% 按单注收益样本方差估，仅示意量级",
          "- 覆盖：" + "；".join(f"{k} {v}" for k, v in cover.items()),
          "- E/F 两节需要候选表的 p_市场 / p_最终 列：旧 features.json 没有这两列时先重跑 `extract`；"
          "F 节按 p_市场×赔率−1 重算 edge，对含走盘/半赢的盘口是近似值", ""]
    for title in sorted(G):
        order = (CONF_ORDER if "置信度" in title else
                 EDGE_ORDER if "自算 edge" in title else None)
        md += _roi_table(title, G[title], order)
    (out / "roi.md").write_text("\n".join(md), encoding="utf-8")
    print("\n".join(md))


# ─── 影子倾向：定线期分布 / H-CONF01 预登记检验（docs/置信度影子倾向计划.md 2.4）──
# 检验口径预先写死，不看结果调参：定线期只看置信度分布给建议线；检验期只检验 H-CONF01。
LEAN_CALIBRATION_N = 60          # 定线期：前 60 场已结算样本
LEAN_TARGET_SHARE = 0.30         # 建议线 T：使约 30% 样本落在 T 及以上
LEAN_T_FLOOR = 60                # T 取 5 的倍数，下限 60
LEAN_PASS_N = 100                # 达标：≥T 组至少 100 场
LEAN_REJECT_N = 60               # 否决：≥T 组至少 60 场
LEAN_BUCKETS = ((50, 55, "50–54"), (55, 60, "55–59"), (60, 65, "60–64"),
                (65, 70, "65–69"), (70, 75, "70–74"), (75, 101, "75+"))


def _lean_ah(row: dict) -> dict | None:
    """有影子倾向且玩法解析为亚盘的 {market, side, line}；否则 None。"""
    cls = row.get("lean_cls")
    return cls if row.get("lean") and cls and cls.get("market") == "ah" else None


def lean_bucket(conf: int) -> str:
    for lo, hi, name in LEAN_BUCKETS:
        if lo <= conf < hi:
            return name
    return "—"


def lean_samples(rows: list[dict], cache: Path) -> tuple[list[dict], dict]:
    """赛前报告 → 已结算的影子倾向样本（按开球日期排序）。
    同一场同一模型出现多份（管理员 + 访客）只算一份，优先管理员报告。"""
    picked: dict[tuple, dict] = {}
    dup = 0
    for r in rows:
        if _lean_ah(r) is None:
            continue
        key = (r["kickoff_date"], _norm_team(r["home"]), _norm_team(r["away"]),
               r.get("writer_model") or "")
        prev = picked.get(key)
        if prev is None:
            picked[key] = r
            continue
        dup += 1
        if "/visitors/" in prev["path"] and "/visitors/" not in r["path"]:
            picked[key] = r
    samples, unmatched = [], 0
    for r in picked.values():
        res = _match_result(r, cache)
        if not res:
            unmatched += 1
            continue
        samples.append({
            "date": r["kickoff_date"], "path": r["path"],
            "model": r.get("writer_model") or "（未记录）",
            "conf": r["lean"]["conf"], "odds": r["lean"]["odds"],
            "result": settle_cls(_lean_ah(r), res["gh"], res["ga"]),
        })
    samples.sort(key=lambda s: (s["date"], s["path"]))
    return samples, {"dup": dup, "unmatched": unmatched}


def lean_summary(samples: list[dict]) -> dict:
    """场数、命中/未中/走盘、方向命中率 ±95%、ROI。
    盈亏按倾向一侧的赔率：全赢 +(O−1)、半赢 +0.5(O−1)、走盘 0、半输 −0.5、全输 −1（Roi/pnl）。"""
    acc = Roi()
    for s in samples:
        acc.add(s["result"], s["odds"])
    d = acc.win + acc.lose
    hit = acc.win / d if d else None
    return {"n": acc.n, "win": acc.win, "lose": acc.lose, "push": acc.push,
            "hit": hit, "ci": _ci(hit, d) if hit is not None else None,
            "profit": acc.profit, "roi": acc.profit / acc.n if acc.n else None}


def suggest_threshold(confs: list[int]) -> int:
    """建议检验线：T ∈ {60, 65, …, 100} 中「≥T 占比」最接近 30% 的那个（并列取较小）。"""
    best: tuple[tuple[float, int], int] | None = None
    for t in range(LEAN_T_FLOOR, 101, 5):
        share = sum(c >= t for c in confs) / len(confs) if confs else 0.0
        key = (abs(share - LEAN_TARGET_SHARE), t)
        if best is None or key < best[0]:
            best = (key, t)
    return best[1]


def hconf01_verdict(ge: list[dict]) -> tuple[str, list[str], dict]:
    """按第 2.4 节的表判 ≥T 组：达标 / 否决 / 继续观察，并给出未满足的条件。ge 须按开球日期排序。"""
    s = lean_summary(ge)
    n = s["n"]
    first, second = ge[: n // 2], ge[n // 2:]
    roi1, roi2 = lean_summary(first)["roi"], lean_summary(second)["roi"]
    roi_drop = None
    if n >= 2:
        top = max(range(n), key=lambda i: pnl(ge[i]["result"], ge[i]["odds"]))
        roi_drop = lean_summary(ge[:top] + ge[top + 1:])["roi"]
    upper = s["hit"] + s["ci"] if s["hit"] is not None else None
    pass_checks = [
        (f"≥T 组 ≥{LEAN_PASS_N} 场", n >= LEAN_PASS_N),
        ("ROI > 0", s["roi"] is not None and s["roi"] > 0),
        ("前后两半 ROI 同号", roi1 is not None and roi2 is not None and roi1 * roi2 > 0),
        ("去掉盈利最大一笔后 ROI > 0", roi_drop is not None and roi_drop > 0),
    ]
    reject_checks = [
        (f"≥T 组 ≥{LEAN_REJECT_N} 场", n >= LEAN_REJECT_N),
        ("方向命中率 95% 区间上限 < 50%", upper is not None and upper < 0.5),
    ]
    passed = all(ok for _, ok in pass_checks)
    rejected = all(ok for _, ok in reject_checks)
    detail = {"summary": s, "roi_first": roi1, "roi_second": roi2,
              "roi_drop_best": roi_drop, "hit_upper": upper}
    if passed and not rejected:
        return "达标", [], detail
    if rejected and not passed:
        return "否决", [], detail
    unmet = ([f"达标·{name}" for name, ok in pass_checks if not ok]
             + [f"否决·{name}" for name, ok in reject_checks if not ok])
    if passed and rejected:
        unmet = ["达标与否决条件同时满足，需人工判断"]
    return "继续观察", unmet, detail


def _pct(x: float | None, signed: bool = False) -> str:
    if x is None:
        return "—"
    return f"{x:+.1%}" if signed else f"{x:.1%}"


def _lean_group_table(groups: list[tuple[str, list[dict]]], head: str) -> list[str]:
    out = [f"| {head} | 场数 | 方向命中率 | ±95% | ROI |",
           "|------|------|-----------|------|-----|"]
    for name, items in groups:
        s = lean_summary(items)
        out.append(f"| {name} | {s['n']} | {_pct(s['hit'])} | "
                   f"{'±' + _pct(s['ci']) if s['ci'] is not None else '—'} | "
                   f"{_pct(s['roi'], signed=True)} |")
    return out + [""]


def build_lean_md(rows: list[dict], cache: Path, threshold: int = 0,
                  since: str = "") -> list[str]:
    """lean.md 正文。threshold=0（定线期）时只看置信度分布，全文不出现命中率与收益数字。"""
    from statistics import mean, median
    leans = [r for r in rows if r.get("lean")]
    samples, info = lean_samples(rows, cache)
    md = ["# 影子倾向置信度（H-CONF01 预登记检验）", "",
          "- 口径见 docs/置信度影子倾向计划.md 第 2.4 节与 reference_staking_kelly.md 7.4；"
          "数据只取报告第 8 节 decision_final 的 lean 键",
          f"- 模式：{'检验期（T = %d%s）' % (threshold, '，起算 ' + since if since else '') if threshold > 0 else '定线期（--threshold 0）'}",
          "", "## 1. 样本概况", "",
          f"- 赛前报告 {len(rows)} 份；含 decision_final {sum(bool(r.get('has_final')) for r in rows)} 份",
          f"- 有影子倾向 {len(leans)} 份（玩法可解析 {sum(_lean_ah(r) is not None for r in rows)} 份）；"
          f"配到完场比分 {len(samples)} 场；未配到比分 {info['unmatched']} 场；"
          f"同场同模型重复报告去重 {info['dup']} 份",
          f"- 写了影子倾向行但未入库：写「无」{sum(r.get('lean_line') == 'none' for r in rows)} 份；"
          f"选了边却无 lean（不合法 / 非 active 定稿）"
          f"{sum(r.get('lean_line') == 'pick' and not r.get('lean') for r in rows)} 份",
          f"- 新格式报告（decision_final 含 writer_model）缺影子倾向行 "
          f"{sum(bool(r.get('writer_model')) and r.get('lean_line') is None for r in rows)} 份", ""]
    if not leans:
        md += ["暂无样本：现有报告的 decision_final 里都没有 lean 键"
               "（影子倾向上线前的旧报告，或 TYPESAFE_DECISION_MODE 不是 active）。", ""]
        return md

    by_model: dict[str, list[int]] = defaultdict(list)
    for r in leans:
        by_model[r.get("writer_model") or "（未记录）"].append(r["lean"]["conf"])
    md += ["## 2. 按模型", "", "| 模型 | 份数 | 置信度均值 | 中位数 |",
           "|------|------|-----------|--------|"]
    for model, confs in sorted(by_model.items(), key=lambda kv: -len(kv[1])):
        md.append(f"| {model} | {len(confs)} | {mean(confs):.1f} | {median(confs):g} |")
    md += ["", "## 3. 置信度分布（全部有影子倾向的报告）", "", "| 置信度 | 份数 | 占比 |",
           "|--------|------|------|"]
    for _lo, _hi, name in LEAN_BUCKETS:
        k = sum(lean_bucket(r["lean"]["conf"]) == name for r in leans)
        md.append(f"| {name} | {k} | {k / len(leans):.1%} |")
    md.append("")

    if threshold <= 0:
        md += ["## 4. 建议检验线（定线期）", "",
               f"- 定线期样本 = 按开球日期排序的前 {LEAN_CALIBRATION_N} 场已结算样本；只看置信度分布"]
        groups = [("全部模型", samples)]
        if len(by_model) > 1:
            groups += [(m, [s for s in samples if s["model"] == m]) for m in sorted(by_model)]
            md.append("- ⚠️ 模型不止一个：不同模型的打分尺度不同，需分别定线、分别检验")
        for name, items in groups:
            if len(items) < LEAN_CALIBRATION_N:
                md.append(f"- {name}：定线期：还差 {LEAN_CALIBRATION_N - len(items)} 场"
                          f"（已结算 {len(items)} / {LEAN_CALIBRATION_N}）")
                continue
            head = items[:LEAN_CALIBRATION_N]
            t = suggest_threshold([s["conf"] for s in head])
            share = sum(s["conf"] >= t for s in head) / len(head)
            md.append(f"- {name}：建议检验线 T = {t}（前 {LEAN_CALIBRATION_N} 场中 ≥T 占 {share:.0%}）；"
                      f"定线期最后一场开球日期 {head[-1]['date']}，检验期起算日期建议取其后一天")
        md += ["- 需人工确认 T 与起算日期后写入 reference_staking_kelly.md 7.4，之后不改", ""]
        return md

    tested = [s for s in samples if not since or s["date"] >= since]
    ge = [s for s in tested if s["conf"] >= threshold]
    lt = [s for s in tested if s["conf"] < threshold]
    md += ["## 5. 描述性分桶表（仅作描述，不据此调整检验线）", "",
           f"- 样本：{'起算日期 ' + since + ' 之后的' if since else '全部'}已结算样本 {len(tested)} 场", ""]
    md += _lean_group_table(
        [(name, [s for s in tested if lean_bucket(s["conf"]) == name])
         for _lo, _hi, name in LEAN_BUCKETS], "置信度")
    verdict, unmet, detail = hconf01_verdict(ge)
    md += [f"## 6. H-CONF01 检验：置信度 ≥ {threshold} 时按首要分析庄赔率下注亚盘主线该侧，ROI > 0", ""]
    md += _lean_group_table([(f"≥{threshold}", ge), (f"<{threshold}", lt)], "分组")
    md += [f"- ≥T 组前半 ROI {_pct(detail['roi_first'], True)}、后半 ROI {_pct(detail['roi_second'], True)}"
           "（按开球日期排序对半分）",
           f"- 去掉盈利最大一笔后 ROI {_pct(detail['roi_drop_best'], True)}",
           f"- 方向命中率 95% 区间上限 {_pct(detail['hit_upper'])}"]
    models = defaultdict(int)
    for s in ge:
        models[s["model"]] += 1
    md.append("- 各模型 ≥T 场数：" + ("；".join(f"{m} {k}" for m, k in sorted(models.items()))
                                     if models else "无"))
    if len({s["model"] for s in tested}) > 1:
        md.append("- ⚠️ 不同模型需分别检验：上表混合了多个模型的样本，结论只作参考")
    md += ["", f"**结论：{verdict}**" + (f"（未满足：{'；'.join(unmet)}）" if unmet else ""), ""]
    return md


def cmd_lean(args) -> None:
    out = Path(args.out)
    since = args.since.strip()
    if since and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", since):
        raise SystemExit("--since 须为 YYYY-MM-DD")
    f = out / "features.json"
    rows = json.load(open(f, encoding="utf-8")) if f.exists() else []
    md = build_lean_md(rows, out / "fixtures_by_date", args.threshold, since)
    out.mkdir(parents=True, exist_ok=True)
    (out / "lean.md").write_text("\n".join(md), encoding="utf-8")
    print("\n".join(md))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=["extract", "results", "stats", "roi", "lean"])
    ap.add_argument("--reports", default="report")
    ap.add_argument("--reports-json", default="", help="可选：{路径: 正文} 的 JSON 缓存，省去逐个读盘")
    ap.add_argument("--out", default="scripts/output/backtest")
    ap.add_argument("--limit", type=int, default=0, help="results：本次最多请求几个比赛日")
    ap.add_argument("--status-only", action="store_true", help="results：只看套餐与额度，不拉比分")
    ap.add_argument("--threshold", type=int, default=0,
                    help="lean：检验线 T（0 = 还在定线期，只看置信度分布）")
    ap.add_argument("--since", default="", help="lean：检验期起算日期 YYYY-MM-DD（按开球日期过滤）")
    args = ap.parse_args()
    {"extract": cmd_extract, "results": cmd_results, "stats": cmd_stats,
     "roi": cmd_roi, "lean": cmd_lean}[args.cmd](args)


if __name__ == "__main__":
    main()
