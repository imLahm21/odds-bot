"""
实战教训「三写一改」落盘引擎（纯确定性，不碰 LLM）

对齐 rules/实战教训/ARCHIVING_PROTOCOL.md：
- 采番：案例号 #（索引表末行）与数据卡号 case_NN（文件名）是两套独立序列，各自 +1
- 字母节：扫主题文件已有 `## A.`/`## B.` 取下一字母
- 三写一改：① 写数据卡 ② 主题文件加规则节+检查项(+触发/相关) ③④ 改总览索引(+路由表)
- 原子性：先在内存构建所有目标文件新内容，全部成功才逐个 os.replace；任一步抛异常整批不落盘
- 自检：落盘后跑协议第四节清单

analyzer.route_lesson / compose_archive_plan 产文本方案，本模块据此做编辑。
"""

import os
import re
import logging

log = logging.getLogger("odds_bot.lesson_archive")

LESSONS_DIR = os.path.join("rules", "实战教训")
OVERVIEW = "reference_case_lessons.md"           # 相对 LESSONS_DIR

# ─── 采番 ────────────────────────────────────────────────────────────────────


def next_case_no(lessons_dir: str = LESSONS_DIR) -> int:
    """案例号：读总览『案例索引』表末尾各行的 # 列，取最大值 +1。"""
    path = os.path.join(lessons_dir, OVERVIEW)
    mx = 0
    try:
        with open(path, encoding="utf-8") as f:
            txt = f.read()
    except OSError:
        return 1
    # 只在「## 案例索引」段内找表格行，避免误抓路由表的 #
    seg = re.search(r"## 案例索引.*?(?=\n## |\Z)", txt, re.S)
    body = seg.group(0) if seg else txt
    for m in re.finditer(r"^\|\s*(\d+)\s*\|", body, re.M):
        mx = max(mx, int(m.group(1)))
    return mx + 1


def next_data_card_no(lessons_dir: str = LESSONS_DIR) -> int:
    """数据卡号：扫目录 `..._case_NN_...md` 文件名，取最大 NN +1。"""
    mx = 0
    try:
        for name in os.listdir(lessons_dir):
            m = re.search(r"_case_(\d+)_", name)
            if m:
                mx = max(mx, int(m.group(1)))
    except OSError:
        pass
    return mx + 1


def next_section_letter(topic_file_text: str) -> str:
    """主题文件下一个规则节字母：扫 `## A.`/`## B.` 取最大字母的下一个。无则 A。"""
    letters = re.findall(r"^##\s+([A-Z])\.\s", topic_file_text, re.M)
    if not letters:
        return "A"
    nxt = max(ord(c) for c in letters) + 1
    return chr(nxt)


def slug_to_filename(slug: str) -> str:
    return f"feedback_{slug}.md"


def card_filename(date_yyyymmdd: str, case_card_no: int,
                  home: str, away: str) -> str:
    teams = f"{home}_vs_{away}".replace(" ", "_")
    teams = re.sub(r"[\\/:*?\"<>|]", "", teams)
    return f"{date_yyyymmdd}_case_{case_card_no:02d}_{teams}.md"


# ─── 文本编辑原语（返回新文本，不落盘）────────────────────────────────────────


def _insert_rule_section(topic_text: str, rule_section_md: str) -> str:
    """在主题文件『## 本主题检查项』前插入规则节。无该标题则追加到末尾。"""
    marker = "\n## 本主题检查项"
    idx = topic_text.find(marker)
    block = rule_section_md.strip() + "\n\n"
    if idx == -1:
        return topic_text.rstrip() + "\n\n" + block
    return topic_text[:idx].rstrip() + "\n\n" + block + topic_text[idx:].lstrip("\n")


def _append_checklist(topic_text: str, items: list[str]) -> str:
    """把检查项追加到『## 本主题检查项』段最后一个 `- [ ]` 之后。"""
    if not items:
        return topic_text
    m = re.search(r"## 本主题检查项.*?(?=\n## |\Z)", topic_text, re.S)
    if not m:
        return topic_text
    seg = m.group(0)
    add = "\n".join(it if it.startswith("- [") else f"- [ ] {it}"
                    for it in items)
    new_seg = seg.rstrip() + "\n" + add + "\n"
    return topic_text[:m.start()] + new_seg + topic_text[m.end():]


def _extend_trigger(topic_text: str, extension: str) -> str:
    """把触发扩展追加到主题文件顶部『> **触发条件**：…』行末（句号前补）。"""
    if not extension:
        return topic_text
    def repl(mo):
        line = mo.group(0).rstrip()
        return line + f"；{extension.strip()}"
    return re.sub(r"> \*\*触发条件\*\*：[^\n]*", repl, topic_text, count=1)


def _append_related_case(topic_text: str, related_append: str) -> str:
    """把案例项追加到主题文件『## 相关』段末尾（若段内已有『案例：』行则续写）。"""
    if not related_append:
        return topic_text
    m = re.search(r"## 相关.*?(?=\n## |\Z)", topic_text, re.S)
    if not m:
        return topic_text.rstrip() + "\n\n" + related_append.strip() + "\n"
    seg = m.group(0).rstrip()
    add = related_append.strip()
    if not add.startswith("-"):
        add = "- " + add
    new_seg = seg + "\n" + add + "\n"
    return topic_text[:m.start()] + new_seg + topic_text[m.end():]


def _append_index_row(overview_text: str, case_no: int, row: dict,
                      card_link: str) -> str:
    """在『## 案例索引』表末尾追加一行。"""
    line = (f"| {case_no} | {row.get('teams','')} | {row.get('date','')} | "
            f"{row.get('league','')} | {row.get('result','')} | "
            f"{row.get('topic_ref','')} | [{card_link}]({card_link}) |")
    seg = re.search(r"## 案例索引.*?(?=\n## |\n> 表中主题缩写|\Z)",
                    overview_text, re.S)
    if not seg:
        return overview_text
    block = seg.group(0)
    # 找到段内最后一个表格行，在其后插入
    rows = list(re.finditer(r"^\|.*\|\s*$", block, re.M))
    if not rows:
        return overview_text
    last = rows[-1]
    abs_end = seg.start() + last.end()
    return overview_text[:abs_end] + "\n" + line + overview_text[abs_end:]


def _extend_route_row(overview_text: str, slug: str, extension: str) -> str:
    """（触发扩展时）在路由表命中 slug 的行『核心要点』列尾追加一句。"""
    if not extension:
        return overview_text
    fname = slug_to_filename(slug)
    lines = overview_text.split("\n")
    for i, ln in enumerate(lines):
        if fname in ln and ln.strip().startswith("|"):
            cells = ln.rstrip().rstrip("|").split("|")
            if cells:
                cells[-1] = cells[-1].rstrip() + f"；{extension.strip()} "
                lines[i] = "|".join(cells) + "|"
            break
    return "\n".join(lines)


# ─── 新建主题分支 ────────────────────────────────────────────────────────────

_NEW_TOPIC_TEMPLATE = """---
name: {slug_kebab}
description: {desc}
metadata:
  type: feedback
---

# {title}

> **触发条件**：{trigger}

{first_section}

## 本主题检查项

{checklist}

## 相关

- 案例：{related}
"""


def build_new_topic_file(slug: str, plan: dict, trigger: str,
                         title: str = "", desc: str = "") -> str:
    checklist = "\n".join(
        it if it.startswith("- [") else f"- [ ] {it}"
        for it in plan.get("checklist_items", [])) or "- [ ] （待补充）"
    return _NEW_TOPIC_TEMPLATE.format(
        slug_kebab=slug.replace("_", "-"),
        desc=desc or plan.get("index_row", {}).get("result", slug),
        title=title or slug.replace("_", " "),
        trigger=trigger or plan.get("index_row", {}).get("result", ""),
        first_section=plan["rule_section_md"].strip(),
        checklist=checklist,
        related=plan.get("related_case_append", "") or "（本文件首案）",
    )


def _new_route_table_row(slug: str, trigger: str, key_point: str) -> str:
    """新建主题时给路由表加的整行（# 号由调用处按现有最大 +1 定）。"""
    return f"| {{ROUTE_NO}} | {trigger} | [{slug_to_filename(slug)}]({slug_to_filename(slug)}) | {key_point} |"


# ─── 组装 + 原子落盘 ──────────────────────────────────────────────────────────


def build_changeset(plan: dict, meta: dict, topic_slug: str, *,
                    is_new_topic: bool = False,
                    lessons_dir: str = LESSONS_DIR) -> dict:
    """把 LLM 方案 + 采番组装成『目标文件 → 新内容』的 changeset（不落盘）。

    返回 {
      "case_no": int, "card_no": int, "section_letter": str,
      "files": {abs_path: new_full_text, ...},   # 待原子写入
      "card_path": abs_path,                      # 数据卡（新文件）
      "summary": [人类可读的改动点...],
    }
    占位符 {CASE_NO}/{CARD_LINK} 在此回填。
    """
    date_raw = (meta.get("kick_cst") or plan.get("index_row", {}).get("date", ""))
    date_yyyymmdd = date_raw[:10].replace("-", "") or "00000000"
    home = meta.get("home", "主队")
    away = meta.get("away", "客队")

    case_no = next_case_no(lessons_dir)
    card_no = next_data_card_no(lessons_dir)
    card_fn = card_filename(date_yyyymmdd, card_no, home, away)
    card_path = os.path.join(lessons_dir, card_fn)

    overview_path = os.path.join(lessons_dir, OVERVIEW)
    topic_fn = slug_to_filename(topic_slug)
    topic_path = os.path.join(lessons_dir, topic_fn)

    # 现有主题文件内容（新建主题时为空）
    topic_text = ""
    if not is_new_topic:
        with open(topic_path, encoding="utf-8") as f:
            topic_text = f.read()
    section_letter = plan.get("_section_letter") or next_section_letter(topic_text)

    # 回填占位符
    def fill(s: str) -> str:
        return (s or "").replace("{CASE_NO}", str(case_no)) \
                        .replace("{CARD_LINK}", card_fn)

    card_md = fill(plan["card_md"])
    rule_section = fill(plan["rule_section_md"])
    plan = {**plan, "rule_section_md": rule_section}

    files: dict[str, str] = {card_path: card_md.rstrip() + "\n"}
    summary = [f"① 新数据卡：{card_fn}（案例 #{case_no}，数据卡号 {card_no:02d}）"]

    # ② 主题文件
    if is_new_topic:
        trig = plan.get("trigger_extension") or ""
        new_topic = build_new_topic_file(topic_slug, plan, trig)
        files[topic_path] = new_topic
        summary.append(f"② 新建主题文件：{topic_fn}（{section_letter} 节起）")
    else:
        t = _insert_rule_section(topic_text, rule_section)
        t = _append_checklist(t, plan.get("checklist_items", []))
        if plan.get("trigger_extension"):
            t = _extend_trigger(t, plan["trigger_extension"])
        if plan.get("related_case_append"):
            t = _append_related_case(t, plan["related_case_append"])
        files[topic_path] = t
        summary.append(f"② {topic_fn}：新增 {section_letter} 节 + "
                       f"{len(plan.get('checklist_items', []))} 条检查项"
                       + ("（并扩展触发条件）" if plan.get("trigger_extension")
                          else ""))

    # ③④ 总览索引
    with open(overview_path, encoding="utf-8") as f:
        ov = f.read()
    ov2 = _append_index_row(ov, case_no, plan.get("index_row", {}), card_fn)
    if is_new_topic:
        route_no = _next_route_no(ov2)
        row = _new_route_table_row(
            topic_slug, plan.get("trigger_extension", "") or "（待补充触发条件）",
            plan.get("index_row", {}).get("result", "")).replace(
            "{ROUTE_NO}", str(route_no))
        ov2 = _insert_route_row(ov2, row)
        ov2 = _append_slug_note(ov2, topic_slug)
        summary.append(f"③④ 总览：案例索引 +1 行、路由表 +1 行（新主题 #{route_no}）")
    else:
        if plan.get("trigger_extension"):
            ov2 = _extend_route_row(ov2, topic_slug, plan["trigger_extension"])
            summary.append("③④ 总览：案例索引 +1 行、路由表触发条件同步扩展")
        else:
            summary.append("③④ 总览：案例索引 +1 行")
    files[overview_path] = ov2

    return {
        "case_no": case_no, "card_no": card_no,
        "section_letter": section_letter,
        "files": files, "card_path": card_path, "summary": summary,
    }


def _next_route_no(overview_text: str) -> int:
    seg = re.search(r"## 主题路由表.*?(?=\n## |\Z)", overview_text, re.S)
    body = seg.group(0) if seg else overview_text
    mx = 0
    for m in re.finditer(r"^\|\s*(\d+)\s*\|", body, re.M):
        mx = max(mx, int(m.group(1)))
    return mx + 1


def _insert_route_row(overview_text: str, row: str) -> str:
    seg = re.search(r"## 主题路由表.*?(?=\n> 典型加载量|\n## |\Z)",
                    overview_text, re.S)
    if not seg:
        return overview_text
    block = seg.group(0)
    rows = list(re.finditer(r"^\|.*\|\s*$", block, re.M))
    if not rows:
        return overview_text
    last = rows[-1]
    abs_end = seg.start() + last.end()
    return overview_text[:abs_end] + "\n" + row + overview_text[abs_end:]


def _append_slug_note(overview_text: str, slug: str) -> str:
    """新建主题时，在『表中主题缩写对应文件』注释行末补一项。"""
    fname = slug_to_filename(slug)
    add = f"、{slug}=[{fname}]({fname})"
    return re.sub(r"(> 表中主题缩写对应文件：[^\n]*)",
                  lambda m: m.group(1).rstrip("。") + add,
                  overview_text, count=1)


def apply_changeset(changeset: dict) -> None:
    """原子落盘：先把每个目标写到同目录 .tmp，全部写成功后逐个 os.replace。
    任一 tmp 写失败则清理已写 tmp 并抛异常（原文件不动）。"""
    tmps: dict[str, str] = {}
    try:
        for path, content in changeset["files"].items():
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(content)
            tmps[path] = tmp
    except OSError:
        for tmp in tmps.values():
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise
    # 全部 tmp 就绪 → 原子替换
    for path, tmp in tmps.items():
        os.replace(tmp, path)


# ─── 落盘后自检（协议第四节）──────────────────────────────────────────────────


def run_self_check(changeset: dict, topic_slug: str,
                   lessons_dir: str = LESSONS_DIR) -> list[str]:
    """返回问题列表（空 = 全过）。

    对齐 ARCHIVING_PROTOCOL 第四节的八项承诺。分两类：
      · **本次变更检查**：只看这次归档写入的内容（案例号、字母节、检查项、链接）。
      · **全库检查**：扫整个 lessons_dir（裸章节号残留）。
    历史遗留问题不应阻塞本次归档，故全库类问题标注「[全库]」前缀以区分。
    """
    problems = []
    overview_path = os.path.join(lessons_dir, OVERVIEW)
    topic_path = os.path.join(lessons_dir, slug_to_filename(topic_slug))
    card_name = os.path.basename(changeset["card_path"])

    # ① 数据卡存在
    if not os.path.exists(changeset["card_path"]):
        problems.append("数据卡未写入")

    ov = ""
    try:
        with open(overview_path, encoding="utf-8") as f:
            ov = f.read()
    except OSError:
        problems.append("总览读取失败")

    if ov:
        case_no = changeset["case_no"]
        # ② 索引表含新案例号
        row = re.search(rf"^\|\s*{case_no}\s*\|.*$", ov, re.M)
        if not row:
            problems.append(f"索引表缺案例 #{case_no} 行")
        else:
            # ③ 索引行须链接到本次数据卡（三方一致的一环）
            if card_name not in row.group(0):
                problems.append(f"索引案例 #{case_no} 行未链接数据卡 {card_name}")
            # ④ 索引行的主题缩写须与本次主题对得上
            short = topic_slug.split("_")[0]
            if short and short not in row.group(0):
                problems.append(
                    f"索引案例 #{case_no} 行的主题缩写与本次主题 {topic_slug} 不符")
        # ⑤ 案例号不得重复
        nums = re.findall(r"^\|\s*(\d+)\s*\|", ov, re.M)
        dup = {n for n in nums if nums.count(n) > 1}
        if dup:
            problems.append(f"索引存在重复案例号：{sorted(dup)}")
        # ⑥ 索引表不得被空行截断（空行会让后续行脱离表格渲染）
        if "## 案例索引" in ov:
            sec = ov[ov.index("## 案例索引"):]
            if re.search(r"^\|.*\|\s*\n\s*\n\|", sec, re.M):
                problems.append("索引表中间有空行，会截断 Markdown 表格")

    try:
        with open(topic_path, encoding="utf-8") as f:
            tt = f.read()
    except OSError:
        problems.append("主题文件读取失败")
        tt = ""

    if tt:
        letters = re.findall(r"^##\s+([A-Z])\.\s", tt, re.M)
        # ⑦ 字母节不重复 **且连续**（原实现只查重复，跳号查不出来）
        if len(letters) != len(set(letters)):
            problems.append("主题文件字母节有重复")
        elif letters:
            seq = sorted(letters)
            expect = [chr(ord("A") + i) for i in range(len(seq))]
            if seq != expect:
                missing = [c for c in expect if c not in seq]
                problems.append(
                    f"主题文件字母节不连续，缺 {missing}（现有 {''.join(seq)}）")
        # ⑧ 新增节须在「本主题检查项」补勾选项
        letter = changeset.get("_section_letter") or changeset.get("section_letter")
        if letter:
            if f"## {letter}." not in tt:
                problems.append(f"主题文件缺新增节 {letter}.")
            m = re.search(r"##\s*本主题检查项(.*?)(?=\n##\s|\Z)", tt, re.S)
            if not m:
                problems.append("主题文件缺「本主题检查项」段")
            elif not re.search(r"^- \[ \]", m.group(1), re.M):
                problems.append("「本主题检查项」未补新勾选项")
        # ⑨ 主题文件内部链接指向的文件须存在
        for target in set(re.findall(r"\]\((feedback_[\w-]+\.md)\)", tt)):
            if not os.path.exists(os.path.join(lessons_dir, target)):
                problems.append(f"主题文件链接指向不存在的文件：{target}")

    # ⑩ [全库] 无残留裸章节号
    try:
        for name in os.listdir(lessons_dir):
            if re.match(r"2026\d+_case_.*\.md", name):
                with open(os.path.join(lessons_dir, name), encoding="utf-8") as f:
                    if re.search(r"第[一二三四五六七八九十]+[、~-]?章", f.read()):
                        problems.append(f"[全库] {name} 含裸章节号引用")
    except OSError:
        pass
    return problems


# ─── 归档后自动 commit（不 push）───────────────────────────────────────────────


def commit_changeset(changeset: dict, topic_slug: str = "") -> tuple[bool, str]:
    """把本次归档的文件 `git add` + `git commit`，**不 push**。

    职责边界（ARCHIVING_PROTOCOL 第五节）：
      机器人负责「写文件 + 提交」；**推送由人工审阅后执行**。
      故本函数只到 commit，绝不调用 push——远端状态由人决定。

    只暂存本次 changeset 里的文件（不用 `git add -A`），避免把
    data/、report/、.claude/ 或其他人的改动一起卷进来。

    返回 (成功, 说明)。任何异常都不抛出——归档已落盘成功，
    commit 失败不应回滚文件，只需如实告知由人工接手。
    """
    import subprocess

    paths = list(changeset.get("files", {}).keys())
    if not paths:
        return False, "changeset 无文件，跳过 commit"

    def _git(*args: str) -> tuple[int, str]:
        try:
            p = subprocess.run(("git",) + args, capture_output=True,
                               text=True, encoding="utf-8", errors="replace",
                               timeout=30)
            return p.returncode, (p.stdout or "") + (p.stderr or "")
        except Exception as e:            # noqa: BLE001
            return 1, str(e)

    rc, out = _git("rev-parse", "--is-inside-work-tree")
    if rc != 0:
        return False, "非 git 仓库，跳过 commit"

    rc, out = _git("add", "--", *paths)
    if rc != 0:
        return False, f"git add 失败：{out.strip()[:200]}"

    # 无实际差异时不产生空提交（重复归档同内容的情况）
    rc, _ = _git("diff", "--cached", "--quiet")
    if rc == 0:
        return False, "文件内容无变化，未产生提交"

    case_no = changeset.get("case_no", "?")
    title = changeset.get("commit_title") or ""
    if not title:
        title = f"新增案例{case_no}"
        if topic_slug:
            title += f"（{topic_slug}）"
    rc, out = _git("commit", "-m", title)
    if rc != 0:
        return False, f"git commit 失败：{out.strip()[:200]}"

    rc, sha = _git("rev-parse", "--short", "HEAD")
    return True, (sha.strip() if rc == 0 else "已提交")


# ─── dry-run CLI 入口 ─────────────────────────────────────────────────────────


def _dry_run(review_path: str) -> int:
    """读一份 _review.md → 路由 → 产方案 → 打印将写入的 changeset（不落盘）。"""
    import sys
    from . import analyzer
    # Windows 控制台默认 GBK，中文/特殊符号会 UnicodeEncodeError；强制 UTF-8 输出。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:            # noqa: BLE001 老 Python / 非标准流忽略
        pass
    try:
        with open(review_path, encoding="utf-8") as f:
            report = f.read()
    except OSError as e:
        print(f"读取失败：{e}")
        return 2
    fname = os.path.basename(review_path)
    stem = fname[:-len("_review.md")] if fname.endswith("_review.md") else fname
    home, _, away = stem.partition("_vs_")
    date_dir = os.path.basename(os.path.dirname(review_path))
    meta = {"home": home.replace("_", " ") or "主队",
            "away": away.replace("_", " ") or "客队",
            "league": "", "kick_cst": date_dir}

    print("== 路由判断（gpt-5.5）==")
    route, err = analyzer.route_lesson(report, meta["home"], meta["away"],
                                       meta["league"])
    if not route:
        print(f"路由失败：{err}")
        return 1
    print(route)
    slug = route["recommended"] or (route["new_topic_slug"] or "")
    is_new = route["need_new_topic"]
    topic_text = ""
    if not is_new:
        try:
            with open(os.path.join(LESSONS_DIR, slug_to_filename(slug)),
                      encoding="utf-8") as f:
                topic_text = f.read()
        except OSError:
            print(f"主题文件不存在：{slug}")
            return 1
    letter = next_section_letter(topic_text) if not is_new else "A"

    print(f"\n== 产方案（{slug}，新增 {letter} 节）==")
    plan, err = analyzer.compose_archive_plan(
        report, meta, slug, topic_text,
        section_letter=letter, is_new_topic=is_new)
    if not plan:
        print(f"方案失败：{err}")
        return 1
    plan["_section_letter"] = letter
    cs = build_changeset(plan, meta, slug, is_new_topic=is_new)
    print("\n== changeset 摘要 ==")
    for s in cs["summary"]:
        print("  " + s)
    print("\n== 将写入的文件（dry-run，未落盘）==")
    for path, content in cs["files"].items():
        print(f"\n----- {path} （{len(content)} 字符）-----")
        print(content[:1500])
    return 0


if __name__ == "__main__":
    import sys
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    if not args:
        print("用法：python -m bot.lesson_archive --dry-run "
              "<report/年/月/日期/xxx_review.md>")
        raise SystemExit(2)
    raise SystemExit(_dry_run(args[0]))
