"""
API-Football 请求层

移植自旧 main.py 的 key 轮换 + 429/401 自动重试逻辑（main.py:37-515），
改造点：
  - base URL 改为 API-Football，鉴权用请求头 x-apisports-key（非 query 参数）
  - API-Football 即使 HTTP 200 也可能在 body.errors 报错，需额外判定
  - 支持多 key（主 APIFOOTBALL_KEY + 可选 APIFOOTBALL_KEY_BACKUP）
  - 静默运行：用 logging 而非 print（守护进程无人值守）
"""

import os
import time
import logging

import requests
from dotenv import load_dotenv

from . import config

load_dotenv()
log = logging.getLogger("odds_bot.api")

# ─── 多 key 管理（移植 main.py:21-56）────────────────────────────────────────
_API_KEYS: list[dict] = []
for env_name, label in (("APIFOOTBALL_KEY", "主Key"),
                        ("APIFOOTBALL_KEY_BACKUP", "备用Key")):
    val = os.getenv(env_name, "").strip()
    if val:
        _API_KEYS.append({"key": val, "label": label, "exhausted": False})

if not _API_KEYS:
    raise SystemExit("未找到 APIFOOTBALL_KEY，请在 .env 中配置")

_current = 0


def _cur_key() -> str:
    return _API_KEYS[_current]["key"]


def _switch_key() -> bool:
    """切到下一个未耗尽的 key。"""
    global _current
    for i in range(len(_API_KEYS)):
        cand = (i + _current + 1) % len(_API_KEYS)
        if not _API_KEYS[cand]["exhausted"]:
            _current = cand
            log.warning("已切换至 %s", _API_KEYS[cand]["label"])
            return True
    return False


# ─── 通用 GET ────────────────────────────────────────────────────────────────
def api_get(endpoint: str, params: dict | None = None,
            max_retries: int = 3) -> dict | None:
    """
    GET 请求，返回完整 JSON（含 response 列表）。失败返回 None。
    - 429/401：标记当前 key 耗尽并切换重试
    - 网络错误：指数退避重试
    - body.errors 非空：记录并返回 None（参数错或无数据）
    """
    url = f"{config.BASE_URL}{endpoint}"
    attempt = 0
    while True:
        headers = {config.AUTH_HEADER: _cur_key()}
        try:
            resp = requests.get(url, headers=headers, params=params or {},
                                timeout=20)
            # 额度日志（debug 级，不刷屏）
            remain = resp.headers.get("x-ratelimit-requests-remaining", "?")
            log.debug("%s 今日剩余额度 %s", endpoint, remain)

            if resp.status_code in (429, 401):
                reason = "额度耗尽" if resp.status_code == 429 else "Key无效"
                log.warning("%s（HTTP %s），尝试切换 key", reason, resp.status_code)
                _API_KEYS[_current]["exhausted"] = True
                if _switch_key():
                    continue
                log.error("所有 API key 均不可用")
                return None

            resp.raise_for_status()
            data = resp.json()

            # API-Football 特有：200 但 errors 非空
            errors = data.get("errors")
            if errors:
                # errors 可能是 dict 或 list；空 list/dict 视为无错
                if (isinstance(errors, dict) and errors) or \
                   (isinstance(errors, list) and errors):
                    log.warning("%s 返回 errors: %s", endpoint, errors)
                    return None
            return data

        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            attempt += 1
            if attempt > max_retries:
                log.error("%s 网络失败超过 %d 次：%s", endpoint, max_retries, e)
                return None
            backoff = 2 ** attempt
            log.warning("%s 网络异常，%ds 后重试（%d/%d）",
                        endpoint, backoff, attempt, max_retries)
            time.sleep(backoff)
        except requests.exceptions.HTTPError as e:
            log.error("%s HTTP 错误：%s", endpoint, e)
            return None


# ─── 业务端点封装 ────────────────────────────────────────────────────────────
def fetch_fixtures(league_id: int, season: int,
                   date_from: str, date_to: str) -> list:
    """拉某联赛某赛季在日期区间内的赛程。"""
    data = api_get("/fixtures", {
        "league": league_id, "season": season,
        "from": date_from, "to": date_to,
    })
    return (data or {}).get("response", []) if data else []


def fetch_odds(fixture_id: int) -> list:
    """拉单场比赛的当前盘口。返回 response 列表（通常 1 个元素）。"""
    data = api_get("/odds", {"fixture": fixture_id})
    return (data or {}).get("response", []) if data else []
