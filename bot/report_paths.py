"""报告目录结构工具。

标准布局：
    report/<年>/<月>/<YYYY-MM-DD>/<报告文件>

读取时兼容历史布局 report/<YYYY-MM-DD>/，便于代码与数据分步部署；所有新写入
一律使用标准布局。
"""

import os
from datetime import date as _date


def is_date_name(value: str) -> bool:
    """是否为严格的 YYYY-MM-DD 日期目录名。"""
    if not isinstance(value, str) or len(value) != 10:
        return False
    try:
        return _date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def canonical_date_dir(base: str, value: str) -> str:
    """返回标准日期目录；未知日期等非日期值仍直接放在 base 下。"""
    if not is_date_name(value):
        return os.path.join(base, value)
    return os.path.join(base, value[:4], value[5:7], value)


def date_dir_candidates(base: str, value: str) -> list[str]:
    """按新结构优先返回读取候选目录，并附带旧扁平结构兼容路径。"""
    canonical = canonical_date_dir(base, value)
    legacy = os.path.join(base, value)
    if os.path.normpath(canonical) == os.path.normpath(legacy):
        return [canonical]
    return [canonical, legacy]


def resolve_date_dir(base: str, value: str) -> str:
    """解析现有日期目录：优先新结构，无现存目录时返回标准新路径。"""
    candidates = date_dir_candidates(base, value)
    for path in candidates:
        if os.path.isdir(path):
            return path
    return candidates[0]


def iter_date_dirs(base: str) -> list[tuple[str, str]]:
    """列出管理员报告日期目录，返回按日期倒序的 (日期, 路径)。

    同一日期若新旧结构同时存在，优先返回新结构。非日期目录（如 visitors）忽略。
    """
    if not os.path.isdir(base):
        return []

    found: dict[str, str] = {}
    try:
        top_entries = list(os.scandir(base))
    except OSError:
        return []

    # 先登记旧扁平目录；下方发现标准目录时覆盖同日期值。
    for entry in top_entries:
        if entry.is_dir() and is_date_name(entry.name):
            found[entry.name] = entry.path

    for year_entry in top_entries:
        year = year_entry.name
        if not (year_entry.is_dir() and len(year) == 4 and year.isdigit()):
            continue
        try:
            month_entries = list(os.scandir(year_entry.path))
        except OSError:
            continue
        for month_entry in month_entries:
            month = month_entry.name
            if not (month_entry.is_dir() and len(month) == 2
                    and month.isdigit() and 1 <= int(month) <= 12):
                continue
            try:
                date_entries = os.scandir(month_entry.path)
            except OSError:
                continue
            with date_entries:
                for date_entry in date_entries:
                    value = date_entry.name
                    if (date_entry.is_dir() and is_date_name(value)
                            and value[:4] == year and value[5:7] == month):
                        found[value] = date_entry.path

    return sorted(found.items(), reverse=True)
