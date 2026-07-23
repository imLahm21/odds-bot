"""
精算报告 → 微信公众号草稿。

自包含模块（仿 ghost_publish.py 结构）：access_token 换取与缓存、默认封面上传成
永久素材、报告 md → 合规基本面文章（纯基本面 + 结尾白话猜测，无任何博彩术语）、
关键词黑名单合规扫描、draft/add 存草稿。

设计要点（符合中国大陆法规）：
  - 只存草稿（draft/add），不直接群发/发布，由人工在公众号后台确认后再发；
  - 正文只取报告【免费正文】的基本面段（近况/交锋/赛程），绝不含盘口/结论；
  - 合规双闸：LLM 生成层（analyzer.wx_compliant_article 的 prompt 硬规则）+
    本模块 _compliance_scan 正则黑名单，命中即拦截不存草稿。

配置（.env）：
  WECHAT_APPID          公众号 AppID（设置与开发→基本配置）
  WECHAT_APPSECRET      公众号 AppSecret
  WECHAT_DEFAULT_COVER  默认封面图本地路径（草稿 thumb 必填，用它兜底）

注意：调用方所在服务器公网 IP 必须加入公众号后台「IP 白名单」，否则 40164。
"""

import os
import re
import time
import logging
import threading

import requests
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("odds_bot.wechat_publish")

WECHAT_APPID = os.getenv("WECHAT_APPID", "").strip()
WECHAT_APPSECRET = os.getenv("WECHAT_APPSECRET", "").strip()
WECHAT_DEFAULT_COVER = os.getenv("WECHAT_DEFAULT_COVER", "").strip()

_API = "https://api.weixin.qq.com/cgi-bin"

# ── 报告锚点（与 ghost_publish 一致，报告格式由 analyzer prompt 固定产出）──
_MATCH_RE = re.compile(r"^\#\#\s*比赛[：:]\s*(.+?)\s+vs\s+(.+?)\s*$", re.MULTILINE)
_EVENT_RE = re.compile(r"^\#\#\s*赛事[：:]\s*(.+?)\s*$", re.MULTILINE)
_ARCHIVE_LINE_RE = re.compile(r"(?m)^\s*>?\s*归档路径[：:].*$\n?")

# ── 合规黑名单：任何博彩/操盘术语命中即拦截，不存草稿 ──
# 面向大陆法规——公众号文章只能是纯基本面 + 球迷观点，不得涉赌。
# 用词边界宽松匹配（含中英文庄家名、盘口/水位/凯利/让球/大小球/下注引导等）。
_BANNED_PATTERNS = [
    r"让球", r"受让", r"平手盘", r"半球", r"一球", r"球半", r"两球",
    r"亚盘", r"欧赔", r"欧指", r"盘口", r"水位", r"凯利", r"返还率",
    r"大小球", r"大球", r"小球", r"上盘", r"下盘", r"初盘", r"临场盘",
    r"诱盘", r"诱上", r"诱下", r"阻盘", r"给水", r"降盘", r"升盘",
    r"下注", r"投注", r"注额", r"串关", r"串[0-9]", r"押注", r"稳胆",
    r"庄家", r"操盘", r"资金流向", r"赔率",
    r"[Bb]et365", r"365", r"[Pp]innacle", r"平博", r"威廉", r"[Ww]illiam",
    r"[Ss]bo", r"[Bb]etano", r"[Nn]ordic[Bb]et", r"1x[Bb]et", r"澳门",
]
_BANNED_RE = re.compile("|".join(f"(?:{p})" for p in _BANNED_PATTERNS))


class WechatError(Exception):
    """微信接口业务错误或合规拦截，message 已是可读文案。"""


def available() -> bool:
    """是否已配置微信公众号发布（仿 ghost_publish.available()）。"""
    return bool(WECHAT_APPID and WECHAT_APPSECRET)


# ─── access_token 缓存 ───────────────────────────────────────────────────────
_token_lock = threading.Lock()
_token_cache = {"token": "", "expire_at": 0.0}


def _get_token(force: bool = False) -> str:
    """取 access_token，内存缓存。微信给的 expires_in 通常 7200s，
    这里提前 200s 过期重取，避免边界失效。多线程共享用锁保护。"""
    with _token_lock:
        now = time.time()
        if not force and _token_cache["token"] and now < _token_cache["expire_at"]:
            return _token_cache["token"]
        try:
            r = requests.get(f"{_API}/token", params={
                "grant_type": "client_credential",
                "appid": WECHAT_APPID, "secret": WECHAT_APPSECRET,
            }, timeout=15)
            data = r.json()
        except Exception as e:
            raise WechatError(f"获取 access_token 网络异常：{e}")
        token = data.get("access_token")
        if not token:
            code = data.get("errcode")
            msg = data.get("errmsg", "")
            if code == 40164:
                raise WechatError(
                    f"服务器 IP 不在公众号白名单：{msg}。请到「设置与开发→基本配置→"
                    "IP 白名单」添加本机公网 IP。")
            raise WechatError(f"获取 access_token 失败：errcode={code} {msg}")
        _token_cache["token"] = token
        _token_cache["expire_at"] = now + int(data.get("expires_in", 7200)) - 200
        return token


# ─── 默认封面（永久素材）缓存 ────────────────────────────────────────────────
_thumb_lock = threading.Lock()
_thumb_cache = {"media_id": "", "src": ""}


def _upload_default_thumb() -> str:
    """把默认封面图上传成永久图片素材，返回 thumb_media_id，缓存复用。
    草稿 draft/add 的 thumb_media_id 必填，用这张兜底（用户暂不管封面）。
    源文件路径变化（改了 .env）时重传。"""
    if not WECHAT_DEFAULT_COVER:
        raise WechatError("未配置 WECHAT_DEFAULT_COVER（默认封面图路径）——"
                          "微信草稿封面必填，请在 .env 指定一张本地图片。")
    if not os.path.isfile(WECHAT_DEFAULT_COVER):
        raise WechatError(f"默认封面图不存在：{WECHAT_DEFAULT_COVER}")
    with _thumb_lock:
        if _thumb_cache["media_id"] and _thumb_cache["src"] == WECHAT_DEFAULT_COVER:
            return _thumb_cache["media_id"]
        token = _get_token()
        try:
            with open(WECHAT_DEFAULT_COVER, "rb") as f:
                r = requests.post(
                    f"{_API}/material/add_material",
                    params={"access_token": token, "type": "image"},
                    files={"media": (os.path.basename(WECHAT_DEFAULT_COVER), f)},
                    timeout=30)
            data = r.json()
        except Exception as e:
            raise WechatError(f"上传封面素材网络异常：{e}")
        media_id = data.get("media_id")
        if not media_id:
            raise WechatError(
                f"上传封面素材失败：errcode={data.get('errcode')} "
                f"{data.get('errmsg', '')}")
        _thumb_cache["media_id"] = media_id
        _thumb_cache["src"] = WECHAT_DEFAULT_COVER
        return media_id


# ─── 报告 → 合规文章 ─────────────────────────────────────────────────────────
def _clean_team(name: str) -> str:
    """去掉队名里的括号注释，如 '墨西哥（Mexico）' → '墨西哥'。"""
    return re.sub(r"[（(].*?[）)]", "", name).strip()


def _compliance_scan(*texts: str) -> None:
    """合规扫描：任一文本命中博彩术语黑名单则抛 WechatError，拦截存草稿。
    这是发到公众号前的最后一道闸，防 LLM 偶尔漏词把涉赌内容发出去。"""
    hits: list[str] = []
    for t in texts:
        for m in _BANNED_RE.finditer(t or ""):
            hits.append(m.group(0))
    if hits:
        uniq = sorted(set(hits))
        raise WechatError(
            "合规扫描拦截：正文/标题命中疑似博彩术语 "
            f"{', '.join(uniq[:12])}"
            f"{' 等' if len(uniq) > 12 else ''}。已阻止存草稿，请检查报告或重试。")


def _md_to_wx_html(md_text: str) -> str:
    """把合规正文（LLM 产出，纯段落、可能带简单换行）转成微信内联样式 HTML。
    微信图文正文只认内联 style，不吃 <style>/class；这里做最小排版：
    段落 <p> + 行距/字号，段间留白。不引入图片/外链。"""
    paras = [p.strip() for p in re.split(r"\n{2,}", md_text.strip()) if p.strip()]
    out = []
    for p in paras:
        # 段内单换行转 <br/>，其余按普通段落
        inner = p.replace("\n", "<br/>")
        out.append(
            '<p style="margin:0 0 18px;font-size:16px;line-height:1.75;'
            f'color:#2c3e50">{inner}</p>')
    return "\n".join(out)


def report_to_wx_article(report_md: str, home: str, away: str,
                         league: str) -> tuple[str, str]:
    """精算报告 markdown → (title, wx_html)。

    只取报告【免费正文】的基本面段，经 analyzer.wx_compliant_article 改写成
    纯基本面文章 + 结尾白话猜测（无盘口/结论/术语），再转微信内联 HTML。
    生成后过 _compliance_scan；命中术语则抛 WechatError。
    LLM 不可用/失败则抛 WechatError（合规内容无法保证，不降级发原文）。
    """
    from . import analyzer

    text = report_md.replace("\r\n", "\n").replace("\r", "\n")
    text = _ARCHIVE_LINE_RE.sub("", text)

    # 只喂免费正文（第 7 节「最终精算结论」之前）给 LLM——天然不含下注结论。
    pm = re.search(r"(?m)^\#{3}\s*7\s*[\.、]?\s*最终精算结论", text)
    free_md = text[:pm.start()] if pm else text

    result = analyzer.wx_compliant_article(free_md, home or "主队",
                                           away or "客队", league or "足球")
    if not result:
        raise WechatError("合规文章生成失败（LLM 未配置或返回空），未存草稿。")
    title = result.get("title", "").strip()
    body = result.get("body", "").strip()
    if not title or not body:
        raise WechatError("合规文章生成结果缺标题或正文，未存草稿。")

    # 合规双闸：正则黑名单扫描标题+正文
    _compliance_scan(title, body)

    wx_html = _md_to_wx_html(body)
    # 微信标题上限 64 字，超了截断
    return title[:64], wx_html


# ─── 存草稿 ──────────────────────────────────────────────────────────────────
def add_draft(title: str, content_html: str, *,
              thumb_media_id: str | None = None,
              author: str = "", digest: str = "") -> str:
    """调 draft/add 存草稿，返回草稿 media_id。
    thumb_media_id 不传则用默认封面（永久素材）兜底。digest 摘要≤120字。
    微信要求 UTF-8；requests 直接 json= 会转义中文为 \\uXXXX，微信可接受。"""
    if not thumb_media_id:
        thumb_media_id = _upload_default_thumb()
    token = _get_token()
    article = {
        "title": title[:64],
        "author": author[:8] if author else "",
        "digest": digest[:120],
        "content": content_html,
        "thumb_media_id": thumb_media_id,
        "need_open_comment": 0,
        "only_fans_can_comment": 0,
    }
    try:
        # 必须 ensure_ascii=False + UTF-8 原始字节：否则中文被转成 \uXXXX 转义，
        # 微信当纯文本存、后台显示成一堆 利勒（实测踩坑）。
        import json
        payload = json.dumps({"articles": [article]}, ensure_ascii=False)
        r = requests.post(f"{_API}/draft/add",
                          params={"access_token": token},
                          data=payload.encode("utf-8"), timeout=30)
        r.encoding = "utf-8"
        data = r.json()
    except Exception as e:
        raise WechatError(f"存草稿网络异常：{e}")
    media_id = data.get("media_id")
    if not media_id:
        code = data.get("errcode")
        msg = data.get("errmsg", "")
        if code == 40164:
            raise WechatError(f"服务器 IP 不在白名单：{msg}")
        if code == 48001:
            raise WechatError("账号无草稿箱接口权限（需微信认证）。")
        raise WechatError(f"存草稿失败：errcode={code} {msg}")
    return media_id


def parse_meta(report_md: str) -> tuple[str, str, str]:
    """从报告提取 (home, away, league_cn)，供调用方传给 report_to_wx_article。"""
    m = _MATCH_RE.search(report_md)
    home = _clean_team(m.group(1)) if m else ""
    away = _clean_team(m.group(2)) if m else ""
    em = _EVENT_RE.search(report_md)
    event = em.group(1).strip() if em else ""
    lm = re.search(r"[一-鿿·]+", event.split("开球时间")[0]) if event else None
    league = lm.group(0) if lm else ""
    return home, away, league


# ─── 服务器自测入口 ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) < 2:
        print("用法：python -m bot.wechat_publish <报告md路径>")
        sys.exit(1)
    with open(sys.argv[1], encoding="utf-8") as f:
        md = f.read()
    print("available:", available())
    home, away, league = parse_meta(md)
    print(f"meta: {home} vs {away} @ {league}")
    title, html = report_to_wx_article(md, home, away, league)
    print("title:", title)
    print("html preview:", html[:300])
    mid = add_draft(title, html, digest=f"{home} vs {away} 赛前基本面")
    print("draft media_id:", mid)
    print("✅ 已存入公众号草稿箱，去后台确认后发布。")
