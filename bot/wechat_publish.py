"""
精算报告 → 微信公众号草稿。

自包含模块（仿 ghost_publish.py 结构）：access_token 换取与缓存、默认封面上传成
永久素材、报告 md → 合规基本面文章（纯基本面 + 结尾白话猜测，无任何博彩术语）、
关键词合规提示、draft/add 存草稿。

设计要点（符合中国大陆法规）：
  - 只存草稿（draft/add），不直接群发/发布，由人工在公众号后台确认后再发；
  - 正文只取报告【免费正文】的基本面段（近况/交锋/赛程），绝不含盘口/结论；
  - LLM 生成层仍要求输出纯基本面文章；本模块 _compliance_scan 只返回疑似术语
    的具体位置供人工复核，不再阻止内容存入草稿箱。

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
from html import escape as _html_escape

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

# ── 合规提示词：命中后提示人工复核，但不阻止存草稿 ──
# 面向大陆法规——公众号文章只能是纯基本面 + 球迷观点，不得涉赌。
# 高置信术语可直接匹配；「一球/两球/大球/上盘」等同时可能出现在正常足球叙述
# 中的词，必须连同盘口语境匹配，避免误杀「一球小胜」「加拿大球员」「场上盘带」。
_BANNED_PATTERNS = [
    r"让球", r"受让", r"平手盘",
    r"亚盘", r"欧赔", r"欧指", r"盘口", r"水位", r"凯利", r"返还率",
    r"大小球", r"初盘", r"临场盘",
    r"诱盘", r"诱上", r"诱下", r"阻盘", r"给水", r"降盘", r"升盘",
    r"下注", r"投注", r"注额", r"串关", r"串[0-9]", r"押注", r"稳胆",
    r"庄家", r"操盘", r"资金流向", r"赔率",
    r"[Bb]et365", r"365", r"[Pp]innacle", r"平博", r"威廉", r"[Ww]illiam",
    r"[Ss]bo", r"[Bb]etano", r"[Nn]ordic[Bb]et", r"1x[Bb]et", r"澳门",
]
_BANNED_RE = re.compile("|".join(f"(?:{p})" for p in _BANNED_PATTERNS))

# 盘口档位和方向词有正常语义，不能再做无条件子串扫描：
#   - 正常：一球小胜、净胜两球、足球半决赛、加拿大球员、小球员、场上盘带
#   - 涉盘：主让一球、一球盘、一球/球半、看好大球、上盘方向
_HANDICAP_TERM = (
    r"(?:平手|平\s*[/／]\s*半|平半|半球|半\s*[/／]\s*一|半一|"
    r"一球(?:\s*[/／]\s*球半)?|球半|两球(?:\s*[/／]\s*两球半)?|两球半)"
)
_MARKET_SIDE_TERM = r"(?:大球|小球|上盘|下盘)"
_CONTEXTUAL_BANNED_PATTERNS = [
    # 斜杠复合档位本身就是盘口写法。
    r"一球\s*[/／]\s*球半",
    r"平\s*[/／]\s*半",
    r"半\s*[/／]\s*一",
    # 无斜杠档位必须伴随明确的让步/盘口上下文。
    rf"(?:主|客)?(?:让|受让)\s*{_HANDICAP_TERM}",
    rf"{_HANDICAP_TERM}\s*(?:盘|盘口|高水|中水|低水|满水|让步|档位)",
    # 大小/上下盘方向必须伴随明确市场动作，避免跨词误命中。
    rf"{_MARKET_SIDE_TERM}\s*(?:方向|玩法|盘口|水位|赔率|选择|推荐|打出|赢盘|输盘)",
    rf"(?:看好|倾向|选择|推荐|追|买)\s*{_MARKET_SIDE_TERM}",
]
_CONTEXTUAL_BANNED_RE = re.compile(
    "|".join(f"(?:{p})" for p in _CONTEXTUAL_BANNED_PATTERNS))


class WechatError(Exception):
    """微信接口业务错误，message 已是可读文案。"""


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


def _compliance_scan(
        *texts: str | tuple[str, str]) -> list[dict[str, str | int]]:
    """返回疑似术语及其位置，仅供人工复核，不抛异常、不阻止存草稿。

    每项可传纯文本，或 ``(字段名, 文本)``。返回项包含字段名、命中词、原文本
    的 1-based 行列位置和短上下文，供 Telegram 回执直接说明具体位置。
    """
    findings: list[dict[str, str | int]] = []
    seen: set[tuple[str, int, int, str]] = set()
    for index, item in enumerate(texts, start=1):
        if isinstance(item, tuple):
            location, text = item
        else:
            location, text = f"文本{index}", item
        text = text or ""
        for pattern in (_BANNED_RE, _CONTEXTUAL_BANNED_RE):
            for match in pattern.finditer(text):
                key = (location, match.start(), match.end(), match.group(0))
                if key in seen:
                    continue
                seen.add(key)
                line_start = text.rfind("\n", 0, match.start()) + 1
                line = text.count("\n", 0, match.start()) + 1
                column = match.start() - line_start + 1
                context_start = max(line_start, match.start() - 12)
                next_newline = text.find("\n", match.end())
                line_end = len(text) if next_newline < 0 else next_newline
                context_end = min(line_end, match.end() + 12)
                context = re.sub(
                    r"\s+", " ", text[context_start:context_end]).strip()
                findings.append({
                    "location": location,
                    "term": match.group(0),
                    "line": line,
                    "column": column,
                    "end_column": column + len(match.group(0)) - 1,
                    "context": context,
                })
    return findings


def compliance_warning_text(
        findings: list[dict[str, str | int]], limit: int = 12, *,
        draft_saved: bool = True) -> str:
    """把扫描结果整理为适合 Telegram 回执的简短纯文本。"""
    if not findings:
        return ""
    status = ("不拦截，草稿已保存，请人工复核" if draft_saved else
              "本地扫描未拦截；以下位置请人工复核")
    lines = [f"⚠️ 合规扫描提示（{status}）："]
    for finding in findings[:limit]:
        line = int(finding["line"])
        column = int(finding["column"])
        end_column = int(finding["end_column"])
        position = (f"第{line}行第{column}-{end_column}字"
                    if line > 1 else f"第{column}-{end_column}字")
        lines.append(
            f"• {finding['location']} {position}：{finding['term']}"
            f"（上下文：{finding['context']}）")
    if len(findings) > limit:
        lines.append(f"• 另有 {len(findings) - limit} 处命中，请在草稿中继续复核。")
    return "\n".join(lines)


_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


def _bold_to_red(s: str) -> str:
    """把 **加粗** 转成红色加粗内联 span（杂志感重点标注）。"""
    return _BOLD_RE.sub(
        r'<strong style="color:#d0342c">\1</strong>', s)


def _md_to_wx_html(md_text: str) -> str:
    """把合规正文（LLM 产出，段落 + **加粗**）转成微信内联样式 HTML。
    微信图文正文只认内联 style，不吃 <style>/class；段落 <p> + 行距/字号，
    **加粗** 渲染成红字重点。不引入图片/外链。"""
    paras = [p.strip() for p in re.split(r"\n{2,}", md_text.strip()) if p.strip()]
    out = []
    for p in paras:
        inner = _bold_to_red(p.replace("\n", "<br/>"))
        out.append(
            '<p style="margin:0 0 20px;font-size:16px;line-height:1.85;'
            f'color:#3a3a3a;letter-spacing:0.3px">{inner}</p>')
    return "\n".join(out)


def editable_source_document(title: str, content_html: str) -> str:
    """把微信正文片段包装成可直接下载、浏览和编辑的 UTF-8 HTML 源稿。"""
    safe_title = _html_escape(title or "微信公众号草稿")
    return (
        "<!doctype html>\n"
        '<html lang="zh-CN">\n<head>\n'
        '  <meta charset="utf-8"/>\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1"/>\n'
        f"  <title>{safe_title}</title>\n"
        "</head>\n"
        '<body style="max-width:720px;margin:24px auto;padding:0 16px;">\n'
        "<!-- 微信公众号可编辑正文开始 -->\n"
        f"{content_html}\n"
        "<!-- 微信公众号可编辑正文结束 -->\n"
        "</body>\n</html>\n"
    )


def save_editable_source(report_path: str, title: str,
                         content_html: str) -> tuple[str, bytes]:
    """在原报告旁的 wechat_drafts/ 保存唯一版本，并返回绝对路径与文件字节。"""
    report_abs = os.path.abspath(report_path)
    out_dir = os.path.join(os.path.dirname(report_abs), "wechat_drafts")
    os.makedirs(out_dir, exist_ok=True)

    stem = os.path.splitext(os.path.basename(report_abs))[0]
    for suffix in ("_report", "_review"):
        if stem.endswith(suffix):
            stem = stem[:-len(suffix)]
            break
    stamp = time.strftime("%Y%m%d_%H%M%S")
    nonce = f"{time.time_ns() % 1_000_000_000:09d}"
    filename = f"{stem}_wechat_{stamp}_{nonce}.html"
    out_path = os.path.join(out_dir, filename)
    payload = editable_source_document(title, content_html).encode("utf-8")
    # 唯一文件名 + xb：重试只新增版本，绝不覆盖之前可编辑的源稿。
    with open(out_path, "xb") as f:
        f.write(payload)
    return out_path, payload


def report_to_wx_article(report_md: str, home: str, away: str,
                         league: str, *,
                         compliance_findings: list[dict[str, str | int]] | None = None
                         ) -> tuple[str, str]:
    """精算报告 markdown → (title, wx_html)。

    只取报告【免费正文】的基本面段，经 analyzer.wx_compliant_article 改写成
    纯基本面文章 + 结尾白话猜测（无盘口/结论/术语），再转微信内联 HTML。
    生成后过 _compliance_scan；命中术语只写入 compliance_findings 供人工复核。
    LLM 不可用/失败则抛 WechatError（合规内容无法保证，不降级发原文）。
    """
    from . import analyzer

    text = report_md.replace("\r\n", "\n").replace("\r", "\n")
    text = _ARCHIVE_LINE_RE.sub("", text)
    text = analyzer.strip_rules_manifest(text)   # 规则指纹不进公众号正文

    # 只喂免费正文（第 7 节「最终精算结论」之前）给 LLM——天然不含下注结论。
    pm = re.search(r"(?m)^\#{3}\s*7\s*[\.、]?\s*最终精算结论", text)
    free_md = text[:pm.start()] if pm else text

    # 开球时间（供场次条显示）：从「## 赛事：… 开球时间：2026-07-15 03:00」提取
    kick = ""
    km = re.search(r"开球时间[：:]\s*([0-9]{4}-[0-9]{2}-[0-9]{2}\s*[0-9]{1,2}:[0-9]{2})",
                   report_md)
    if km:
        kick = km.group(1).strip()

    generation_errors: list[str] = []
    result = analyzer.wx_compliant_article(
        free_md, home or "主队", away or "客队", league or "足球",
        error_out=generation_errors)
    if not result:
        detail = (generation_errors[-1] if generation_errors else
                  "LLM 未返回可用文章")
        raise WechatError(f"合规文章生成失败：{detail.rstrip('。')}。未存草稿。")
    title = result.get("title", "").strip()
    sections = result.get("sections") or []
    if not title or not sections:
        raise WechatError("合规文章生成结果缺标题或正文，未存草稿。")

    subtitle = result.get("subtitle", "").strip()
    lead = result.get("lead", "").strip()
    compare = result.get("compare") or []
    highlights = result.get("highlights") or []
    prediction = result.get("prediction") or {}

    # 先渲染无副作用的 HTML，排版与原发布流程保持一致。
    wx_html = _build_article_html(
        title, subtitle, lead, sections, compare, highlights, prediction,
        home or "主队", away or "客队", league or "足球", kick)

    # 扫描所有 LLM 产出文本，只回传具体命中位置，不拦截后续 draft/add。
    scan_texts: list[tuple[str, str]] = [
        ("标题", title),
        ("副标题", subtitle),
        ("导语", lead),
        ("个人预判·比分", prediction.get("score", "")),
        ("个人预判·说明", prediction.get("note", "")),
    ]
    for index, sec in enumerate(sections, start=1):
        scan_texts += [
            (f"正文第{index}节标题", sec.get("heading", "")),
            (f"正文第{index}节内容", sec.get("text", "")),
        ]
    for index, row in enumerate(compare, start=1):
        scan_texts += [
            (f"对比表第{index}行项目", row.get("item", "")),
            (f"对比表第{index}行主队", row.get("home", "")),
            (f"对比表第{index}行客队", row.get("away", "")),
        ]
    for index, highlight in enumerate(highlights, start=1):
        scan_texts += [
            (f"看点第{index}项标签", highlight.get("label", "")),
            (f"看点第{index}项内容", highlight.get("value", "")),
        ]
    findings = _compliance_scan(*scan_texts)
    if compliance_findings is not None:
        compliance_findings.extend(findings)
    if findings:
        log.warning("微信公众号草稿内容命中 %d 处疑似术语，继续存草稿并提示人工复核",
                    len(findings))
    return title[:64], wx_html


# ─── 花式排版组装（杂志感，全内联样式，微信兼容）─────────────────────────────
_BRAND = "Lahm的精选日记"   # 品牌栏文字（公众号名），可按需改


def _build_article_html(title: str, subtitle: str, lead: str, sections: list,
                        compare: list, highlights: list, prediction: dict,
                        home: str, away: str, league: str, kick: str) -> str:
    """把结构化内容拼成杂志感图文 HTML（红顶线/品牌栏/大标题/场次条/两队数据对比表/
    带小标题的分节正文/红字加粗/定性看点色块/比分预测框/免责声明）。
    全内联 style，不用 <style>/class。"""
    parts = []
    # 顶部红线 + 品牌栏
    parts.append('<section style="border-top:3px solid #d0342c;padding-top:14px">')
    parts.append(
        f'<p style="margin:0 0 4px;font-size:13px;color:#d0342c;'
        f'letter-spacing:1px">{_BRAND} · 赛前推演</p>')
    # 大标题
    parts.append(
        f'<h1 style="margin:8px 0 6px;font-size:26px;line-height:1.35;'
        f'font-weight:700;color:#1a1a1a">{_bold_to_red(title)}</h1>')
    if subtitle:
        parts.append(
            f'<p style="margin:0 0 10px;font-size:15px;color:#888;'
            f'line-height:1.6">{subtitle}</p>')
    # meta 线（联赛 + 开球时间）
    meta = league + (f"　开球 {kick}" if kick else "")
    parts.append(
        f'<p style="margin:0 0 6px;font-size:12px;color:#bbb">{meta}</p>')
    parts.append('<hr style="border:none;border-top:1px solid #eee;margin:14px 0"/>')

    # 场次条（左 队 vs 队 加粗，右 开球时间 灰底条）
    right = kick if kick else league
    parts.append(
        '<section style="display:flex;justify-content:space-between;'
        'align-items:center;background:#f6f7f9;border-left:4px solid #d0342c;'
        'padding:10px 14px;margin:0 0 20px">'
        f'<span style="font-size:17px;font-weight:700;color:#1a1a1a">'
        f'{home} vs {away}</span>'
        f'<span style="font-size:13px;color:#999">{right}</span>'
        '</section>')

    # 导语
    if lead:
        parts.append(
            '<p style="margin:0 0 22px;font-size:17px;line-height:1.9;'
            f'color:#1a1a1a;font-weight:500">{_bold_to_red(lead)}</p>')

    # 两队数据对比表（放在导语后、正文前，先给读者一个数据全景，打破文字墙）
    if compare:
        rows = [
            '<tr style="background:#d0342c;color:#1a1a1a;text-align:center">'
            '<th style="padding:9px 8px;font-size:13px;text-align:left;'
            'font-weight:600">对比项</th>'
            f'<th style="padding:9px 8px;font-size:13px;font-weight:600">{home}</th>'
            f'<th style="padding:9px 8px;font-size:13px;font-weight:600">{away}</th></tr>']
        for i, row in enumerate(compare):
            bg = "#ffffff" if i % 2 == 0 else "#faf7f7"
            rows.append(
                f'<tr style="background:{bg}">'
                f'<td style="padding:9px 8px;font-size:14px;color:#666;'
                f'border-bottom:1px solid #f0f0f0">{row.get("item","")}</td>'
                f'<td style="padding:9px 8px;font-size:14px;color:#1a1a1a;'
                f'text-align:center;font-weight:600;border-bottom:1px solid #f0f0f0">'
                f'{_bold_to_red(row.get("home",""))}</td>'
                f'<td style="padding:9px 8px;font-size:14px;color:#1a1a1a;'
                f'text-align:center;font-weight:600;border-bottom:1px solid #f0f0f0">'
                f'{_bold_to_red(row.get("away",""))}</td></tr>')
        parts.append(
            '<table style="width:100%;border-collapse:collapse;margin:0 0 24px;'
            'border:1px solid #f0f0f0">' + "".join(rows) + '</table>')

    # 分节正文（每节：红色小标题 + 正文，制造视觉断点）
    for sec in sections:
        head = sec.get("heading", "").strip()
        txt = sec.get("text", "").strip()
        if head:
            parts.append(
                '<h2 style="margin:26px 0 12px;font-size:19px;font-weight:700;'
                'color:#1a1a1a;padding-left:11px;border-left:4px solid #d0342c;'
                f'line-height:1.4">{_bold_to_red(head)}</h2>')
        if txt:
            parts.append(_md_to_wx_html(txt))

    # 定性看点色块（横排三块）
    if highlights:
        cells = []
        for h in highlights[:3]:
            cells.append(
                '<td style="width:33%;text-align:center;padding:14px 8px;'
                'background:#fafafa;border:1px solid #f0f0f0">'
                f'<div style="font-size:12px;color:#999;margin-bottom:6px">'
                f'{h.get("label","")}</div>'
                f'<div style="font-size:15px;color:#d0342c;font-weight:700;'
                f'line-height:1.4">{h.get("value","")}</div></td>')
        parts.append(
            '<table style="width:100%;border-collapse:collapse;margin:8px 0 24px">'
            f'<tr>{"".join(cells)}</tr></table>')

    # 比分预测框（浅红底）
    if prediction.get("score"):
        note = prediction.get("note", "")
        parts.append(
            '<section style="background:#fdf3f2;border:1px solid #f5d9d6;'
            'border-radius:8px;padding:16px 18px;margin:0 0 24px">'
            '<div style="font-size:13px;color:#d0342c;font-weight:700;'
            'margin-bottom:6px">📝 个人预判</div>'
            f'<div style="font-size:20px;font-weight:700;color:#1a1a1a;'
            f'margin-bottom:6px">{home} {prediction["score"]} {away}</div>'
            + (f'<div style="font-size:14px;color:#666;line-height:1.6">'
               f'{_bold_to_red(note)}</div>' if note else "")
            + '</section>')

    # 免责声明页脚
    parts.append('<hr style="border:none;border-top:1px solid #eee;margin:20px 0 12px"/>')
    parts.append(
        '<p style="font-size:12px;color:#bbb;line-height:1.7;text-align:center">'
        f'{_BRAND} · 赛前推演<br/>'
        '本文仅为球迷视角的赛前基本面导读与个人观点，不构成任何投注建议。<br/>'
        '足球魅力在于不确定性，理性观赛，切勿沉迷。</p>')
    parts.append('</section>')
    return "\n".join(parts)


# ─── 存草稿 ──────────────────────────────────────────────────────────────────
def add_draft(title: str, content_html: str, *,
              thumb_media_id: str | None = None,
              author: str = "Lahm", digest: str = "") -> str:
    """调 draft/add 存草稿，返回草稿 media_id。
    thumb_media_id 不传则用默认封面（永久素材）兜底。digest 摘要≤120字。
    正文 UTF-8 编码见下方 ensure_ascii=False 处理。"""
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
