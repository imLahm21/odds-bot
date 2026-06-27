"""
精算报告 → Ghost 博客发布。

自包含模块（不依赖 Ghost/ 项目目录）：把 Ghost Admin API 的 JWT 鉴权、
发文逻辑搬过来，并针对本项目的精算报告做定制转换（提标题、第 7 节前插付费墙）。

配置（.env）：
  GHOST_ADMIN_API_KEY   形如 id:secret（secret 为 hex）
  GHOST_ADMIN_API_URL   形如 https://blog.lahmxavi.top
  GHOST_DEFAULT_VISIBILITY  public/members/paid（默认 paid）

付费墙策略：第 1-6 节（数据/分析过程）免费，第 7 节「最终精算结论」付费解锁。
"""

import os
import re
import time
import logging

import jwt
import markdown
import requests
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("odds_bot.ghost_publish")

GHOST_ADMIN_API_KEY = os.getenv("GHOST_ADMIN_API_KEY", "").strip()
GHOST_ADMIN_API_URL = os.getenv("GHOST_ADMIN_API_URL", "").strip().rstrip("/")
GHOST_DEFAULT_VISIBILITY = os.getenv("GHOST_DEFAULT_VISIBILITY", "paid").strip().lower()
GHOST_API_VERSION = "v5.0"

_MD_EXTENSIONS = ["extra", "fenced_code", "tables", "sane_lists"]

# 报告锚点（由 analyzer 的 prompt 固定产出，稳定）
# 首行： ## 比赛：墨西哥（…） vs 南非（…）
_MATCH_RE = re.compile(r"^\#\#\s*比赛[：:]\s*(.+?)\s+vs\s+(.+?)\s*$", re.MULTILINE)
# 第二行：## 赛事：… 开球时间：…
_EVENT_RE = re.compile(r"^\#\#\s*赛事[：:]\s*(.+?)\s*$", re.MULTILINE)
# 付费墙锚点：### 7. 最终精算结论（允许 7 后面是 . 、 中文顿号或空格）
_PAYWALL_RE = re.compile(r"^\#{3}\s*7\s*[\.、]?\s*最终精算结论", re.MULTILINE)


class GhostError(Exception):
    """Ghost 返回的业务错误，message 已是可读文案。"""


def available() -> bool:
    """是否已配置 Ghost 发布（仿 analyzer.available()）。"""
    return bool(GHOST_ADMIN_API_KEY and ":" in GHOST_ADMIN_API_KEY
                and GHOST_ADMIN_API_URL)


# ─── JWT 鉴权（照搬 Ghost/bot/ghost_auth.py）─────────────────────────────────
def _make_token() -> str:
    key_id, secret_hex = GHOST_ADMIN_API_KEY.split(":", 1)
    secret_bytes = bytes.fromhex(secret_hex)
    iat = int(time.time())
    payload = {"iat": iat, "exp": iat + 300, "aud": "/admin/"}
    headers = {"kid": key_id, "alg": "HS256", "typ": "JWT"}
    return jwt.encode(payload, secret_bytes, algorithm="HS256", headers=headers)


# ─── 报告 → 文章 ─────────────────────────────────────────────────────────────
def _clean_team(name: str) -> str:
    """去掉队名里的括号注释，如 '墨西哥（Mexico）' → '墨西哥'，
    'South Africa（南非）' → 'South Africa'。中英文括号都处理。"""
    return re.sub(r"[（(].*?[）)]", "", name).strip()


def _slugify(text: str) -> str:
    """生成 URL slug：仅保留 ASCII 字母数字，其余转连字符。
    非 ASCII（中文）会被丢弃 → 若结果为空则返回 ''（调用方据此回退）。"""
    s = text.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)   # 非字母数字 → 连字符
    return s.strip("-")


def report_to_post(report_md: str, *, title: str | None = None,
                   is_review: bool = False) -> tuple[str, str, str, str | None]:
    """精算报告 markdown → (title, html, excerpt, slug)。

    title 传入则用之（管理员自定义）；否则从首行 '## 比赛：X vs Y' 生成。
    slug 始终从报告里的英文队名生成（如 derry-city-vs-drogheda-united-prediction），
    与标题语言无关，保证 URL 是干净英文；无法生成时返回 None（让 Ghost 自动生成）。
    付费墙：第 7 节「最终精算结论」之前免费，之后付费。
    """
    text = report_md.replace("\r\n", "\n").replace("\r", "\n")

    # 队名匹配（标题与 slug 共用）
    m = _MATCH_RE.search(text)
    home = _clean_team(m.group(1)) if m else ""
    away = _clean_team(m.group(2)) if m else ""

    # 标题
    if not title:
        if m:
            suffix = "复盘" if is_review else "精算预测"
            title = f"{home} vs {away} — {suffix}"
        else:
            title = "精算复盘" if is_review else "精算预测"

    # slug：英文队名 + prediction/review 后缀；队名无 ASCII（纯中文）时退回 None
    slug = None
    if m:
        home_slug = _slugify(home)
        away_slug = _slugify(away)
        if home_slug and away_slug:   # 两队都有英文才生成，避免 'vs-prediction' 这种残缺
            suffix_en = "review" if is_review else "prediction"
            slug = f"{home_slug}-vs-{away_slug}-{suffix_en}"

    # 摘要：取「## 赛事：…」一行
    em = _EVENT_RE.search(text)
    excerpt = em.group(1).strip() if em else ""

    # 付费墙切分
    pm = _PAYWALL_RE.search(text)
    if pm:
        free_md = text[:pm.start()].rstrip()
        paid_md = text[pm.start():].strip()
    else:
        # 找不到第 7 节锚点 → 整篇付费（安全兜底）
        free_md, paid_md = "", text.strip()

    free_html = _render(free_md)
    paid_html = _render(paid_md)
    if free_html:
        html = f"{free_html}\n<!--members-only-->\n{paid_html}"
    else:
        html = f"<!--members-only-->\n{paid_html}"

    return title, html, excerpt, slug


def _render(md_text: str) -> str:
    if not md_text.strip():
        return ""
    return markdown.markdown(md_text, extensions=_MD_EXTENSIONS)


# ─── 发文（照搬 Ghost/bot/ghost_client.py）───────────────────────────────────
def _admin_url(path: str) -> str:
    # Ghost 5.x：版本号不在 URL 路径里，通过 Accept-Version 请求头传
    return f"{GHOST_ADMIN_API_URL}/ghost/api/admin/{path}"


def create_post(title: str, html: str, *, status: str = "published",
                visibility: str = "paid",
                custom_excerpt: str | None = None,
                slug: str | None = None) -> dict:
    """创建文章，返回 Ghost 的 post 对象（含前台 url / id）。失败抛 GhostError。"""
    post: dict = {
        "title": title,
        "html": html,
        "status": status,
        "visibility": visibility,
    }
    if custom_excerpt:
        post["custom_excerpt"] = custom_excerpt[:300]
    if slug:
        post["slug"] = slug

    body = {"posts": [post]}
    headers = {
        "Authorization": f"Ghost {_make_token()}",
        "Content-Type": "application/json",
        "Accept-Version": GHOST_API_VERSION,
    }
    try:
        r = requests.post(_admin_url("posts/"), params={"source": "html"},
                          json=body, headers=headers, timeout=60)
    except requests.exceptions.RequestException as e:
        log.warning("Ghost 请求异常: %s", e)
        raise GhostError(f"网络错误：{e}") from e

    data = {}
    try:
        data = r.json()
    except ValueError:
        pass

    if r.status_code >= 400 or "errors" in data:
        msg = _extract_error(data) or f"HTTP {r.status_code}"
        log.warning("Ghost 发文失败: %s", msg)
        raise GhostError(msg)

    try:
        return data["posts"][0]
    except (KeyError, IndexError) as e:
        raise GhostError(f"响应格式异常：{data}") from e


def _extract_error(data: dict) -> str:
    try:
        errs = data.get("errors") or []
        if errs:
            e = errs[0]
            parts = [e.get("message", "")]
            if e.get("context"):
                parts.append(str(e["context"]))
            return " — ".join(p for p in parts if p)
    except Exception:
        pass
    return ""
