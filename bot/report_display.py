"""报告展示层：隐藏机器字段，原始报告仍由决策和归档流程保留。"""

from __future__ import annotations

import re
import unicodedata


_SEPARATOR = re.compile(r":?-{3,}:?")


def _plain(value: str) -> str:
    return re.sub(r"\*\*|__|~~|`", "", unicodedata.normalize("NFKC", value)).strip()


def _cells(line: str) -> list[str] | None:
    value = line.strip()
    if not value.startswith("|"):
        return None
    value = value[1:]
    if value.endswith("|") and not value.endswith(r"\|"):
        value = value[:-1]
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", value)]


def _display_columns(headers: list[str]) -> tuple[list[int], int] | None:
    keys = [re.sub(r"[\s_]+", "", _plain(cell)).casefold() for cell in headers]
    status = [index for index, key in enumerate(keys)
              if "资格" in key or key in {"eligibility", "eligible", "candidateeligibility"}]
    play = next((index for index, key in enumerate(keys)
                 if "玩法" in key or key in {"play", "market", "selection"}), None)
    has_odds = any("赔率" in key or key in {"odds", "decimalodds"} for key in keys)
    if not status or play is None or not has_odds or not any(key.startswith("edge") for key in keys):
        return None
    return status, play


def _render_row(cells: list[str], original: str) -> str:
    content = original.rstrip("\r\n")
    ending = original[len(content):]
    indent = content[:len(content) - len(content.lstrip())]
    return indent + "| " + " | ".join(cells) + " |" + ending


def hide_eligibility_column(text: str) -> str:
    """隐藏候选表资格列；排除项保留中文提示，不改候选数字或其他表格。

    只处理有完整表头、分隔行且列数一致的候选表。无法可靠拆分的表格保留原样，
    避免为隐藏字段而误删赔率、概率或 edge。重复调用不会继续改写已处理的表格。
    """
    lines = text.splitlines(keepends=True)
    output: list[str] = []
    index = 0
    while index < len(lines):
        headers = _cells(lines[index])
        columns = _display_columns(headers) if headers else None
        separator = _cells(lines[index + 1]) if index + 1 < len(lines) else None
        if (columns is None or separator is None or len(separator) != len(headers)
                or not all(_SEPARATOR.fullmatch(cell) for cell in separator)):
            output.append(lines[index])
            index += 1
            continue

        rows = [headers, separator]
        end = index + 2
        while end < len(lines):
            cells = _cells(lines[end])
            if cells is None:
                break
            rows.append(cells)
            end += 1
        if any(len(row) != len(headers) for row in rows):
            output.extend(lines[index:end])
            index = end
            continue

        status_columns, play_column = columns
        for offset, cells in enumerate(rows):
            cells = list(cells)
            if offset >= 2 and any(
                _plain(cells[column]).casefold() not in {"", "eligible", "合格", "可选"}
                for column in status_columns
            ):
                cells[play_column] += "（已排除）"
            visible = [cell for column, cell in enumerate(cells) if column not in status_columns]
            output.append(_render_row(visible, lines[index + offset]))
        index = end
    return "".join(output)
