from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import time
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

from . import config

log = logging.getLogger("odds_bot.typesafe_decisions")
_LESSONS_DIR = Path(__file__).resolve().parents[1] / "rules" / "实战教训"
_NUMBER_RE = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
_CANDIDATE_ID_RE = re.compile(r"C\d{2,}", re.I)
_LEGACY_UNAVAILABLE_MARKERS = (
    "无报价", "未报价", "无直接报价", "数据异常", "盘口字段异常",
    "无法计算", "无法可靠计算", "不计算", "不纳入", "无数据", "跳过",
    "n/a",
)


def _frozen_map(value: Mapping | None = None) -> Mapping:
    return MappingProxyType(dict(value or {}))


@dataclass(frozen=True, slots=True)
class ChoiceJudgment:
    choice: str
    confidence: float
    probabilities: Mapping[str, float]
    requested_model: str
    actual_model: str
    request_id: str
    latency_ms: int
    token_usage: Mapping[str, int] = field(default_factory=_frozen_map)

    def __post_init__(self) -> None:
        object.__setattr__(self, "probabilities", _frozen_map(self.probabilities))
        object.__setattr__(self, "token_usage", _frozen_map(self.token_usage))


@dataclass(frozen=True, slots=True)
class LessonRouteJudgment:
    recommended: str
    candidates: tuple[str, ...]
    need_new_topic: bool
    confidence: float
    probabilities: Mapping[str, float]
    choice_judgment: ChoiceJudgment | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidates", tuple(self.candidates))
        object.__setattr__(self, "probabilities", _frozen_map(self.probabilities))


@dataclass(frozen=True, slots=True)
class DecisionCandidate:
    candidate_id: str
    play: str
    odds: float | None
    p_market: float | None
    p_final: float | None
    edge: float | None
    eligibility: str
    raw_row: str


@dataclass(frozen=True, slots=True)
class DecisionParseResult:
    selected_id: str | None
    is_pass: bool
    evidence: str
    stake: float | None
    candidates: tuple[DecisionCandidate, ...]
    source: str
    warnings: tuple[str, ...] = ()
    confidence: float | None = None
    probabilities: Mapping[str, float] = field(default_factory=_frozen_map)
    choice_judgment: ChoiceJudgment | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidates", tuple(self.candidates))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        object.__setattr__(self, "probabilities", _frozen_map(self.probabilities))


def _value(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def typesafe_unavailable_reason() -> str | None:
    """Return a safe availability code without importing or constructing the SDK."""
    if not config.TYPESAFE_API_KEY:
        return "missing_api_key"
    try:
        from importlib.util import find_spec
        return None if find_spec("typesafe_sdk") is not None else "sdk_missing"
    except (ImportError, ValueError):
        return "sdk_missing"


def typesafe_available() -> bool:
    return typesafe_unavailable_reason() is None


def _token_usage(raw: Any) -> Mapping[str, int]:
    values: dict[str, int] = {}
    for name in ("input_tokens", "output_tokens"):
        value = _value(raw, name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            values[name] = value
    return _frozen_map(values)


def _request_choice_result(
    state: Mapping[str, Any],
    criteria: Mapping[str, Any],
    instructions: str,
    *,
    question_id: str,
) -> tuple[ChoiceJudgment | None, str | None]:
    """Make one bounded Choice request. Returns only typed fields, never raw bodies."""
    if not config.TYPESAFE_API_KEY:
        return None, "missing_api_key"
    if not criteria or len(criteria) > 255:
        return None, "invalid_choice_criteria"

    started = time.perf_counter()
    try:
        from typesafe_sdk import Choice, RetryPolicy, TypeSafeClient
    except Exception:
        return None, "sdk_missing"

    # SDK debug output can include request and response bodies. Keep its logger silent.
    logging.getLogger("typesafe_sdk").disabled = True
    try:
        retry = RetryPolicy(
            max_retries=config.TYPESAFE_MAX_RETRIES,
            timeout=config.TYPESAFE_RETRY_BUDGET_SECONDS,
            http_statuses={408, 429, *range(500, 600)},
            api_connection_error=True,
            api_timeout_error=True,
        )
        client_args: dict[str, Any] = {
            "api_key": config.TYPESAFE_API_KEY,
            "model": config.TYPESAFE_MODEL,
            "timeout": config.TYPESAFE_HTTP_TIMEOUT_SECONDS,
            "retry": retry,
        }
        if config.TYPESAFE_BASE_URL:
            client_args["base_url"] = config.TYPESAFE_BASE_URL
        with TypeSafeClient(**client_args) as client:
            response = client.system_one(
                state=dict(state),
                questions={
                    question_id: Choice(
                        instructions=instructions,
                        criteria=dict(criteria),
                    )
                },
                model=config.TYPESAFE_MODEL,
            )

        answer_map = _value(response, "answers")
        answer = answer_map.get(question_id) if isinstance(answer_map, Mapping) else None
        if answer is None:
            answer_map = _value(response, "choices")
            answer = answer_map.get(question_id) if isinstance(answer_map, Mapping) else None
        if answer is None:
            return None, "missing_choice_answer"

        choice = _value(answer, "choice")
        confidence_raw = _value(answer, "confidence")
        probability_raw = _value(answer, "probabilities")
        if not isinstance(choice, str) or choice not in criteria:
            return None, "unknown_choice"
        try:
            confidence = float(confidence_raw)
        except (TypeError, ValueError):
            return None, "invalid_confidence"
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            return None, "invalid_confidence"
        if not isinstance(probability_raw, Mapping):
            return None, "invalid_probabilities"

        probabilities: dict[str, float] = {}
        for option in criteria:
            if option not in probability_raw:
                return None, "incomplete_probabilities"
            try:
                probability = float(probability_raw[option])
            except (TypeError, ValueError):
                return None, "invalid_probabilities"
            if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
                return None, "invalid_probabilities"
            probabilities[option] = probability
        total = sum(probabilities.values())
        if abs(total - 1.0) > 0.03:
            return None, "invalid_probability_total"
        if probabilities[choice] + 1e-9 < max(probabilities.values()):
            return None, "choice_probability_mismatch"

        elapsed_ms = max(0, round((time.perf_counter() - started) * 1000))
        usage = _token_usage(_value(response, "usage"))
        return ChoiceJudgment(
            choice=choice,
            confidence=confidence,
            probabilities=_frozen_map(probabilities),
            requested_model=config.TYPESAFE_MODEL,
            actual_model=str(_value(response, "model") or config.TYPESAFE_MODEL),
            request_id=str(_value(response, "request_id") or ""),
            latency_ms=elapsed_ms,
            token_usage=usage,
        ), None
    except Exception as exc:
        # Exception text may contain service details or request data; log only its type.
        log.warning("TypeSafe 请求失败（%s）", type(exc).__name__)
        return None, f"request_error:{type(exc).__name__}"


def _extract_heading_section(text: str, heading_pattern: str) -> str | None:
    match = re.search(heading_pattern, text or "", re.I | re.M)
    if not match:
        return None
    tail = (text or "")[match.start():]
    next_heading = re.search(r"(?m)^###\s+", tail[match.end() - match.start():])
    if next_heading:
        end = match.end() + next_heading.start()
        return tail[:end - match.start()].strip()
    return tail.strip()


def _lesson_excerpt(review_report: str) -> str:
    patterns = (
        r"^###\s*4\s*[.．、]?\s*信号有效性复盘[^\r\n]*",
        r"^###\s*5\s*[.．、]?\s*经验教训[^\r\n]*",
    )
    parts = []
    for pattern in patterns:
        section = _extract_heading_section(review_report, pattern)
        if section:
            parts.append(section)
    return "\n\n".join(parts)


def _topic_dir(path: str | Path | None = None) -> Path:
    return Path(path) if path is not None else _LESSONS_DIR


def load_lesson_topics(
    topic_dir: str | Path | None = None,
) -> dict[str, dict[str, str]]:
    """Read only each feedback file's trigger condition and explicit guardrails."""
    topics: dict[str, dict[str, str]] = {}
    directory = _topic_dir(topic_dir)
    try:
        files = sorted(directory.glob("feedback_*.md"))
    except OSError:
        return topics
    for path in files:
        slug = path.stem.removeprefix("feedback_")
        try:
            body = path.read_text(encoding="utf-8")
        except OSError:
            continue
        trigger_match = re.search(
            r"(?ms)^>\s*\*\*触发条件\*\*：?(.*?)(?=\n\s*\n|\n## |\Z)", body)
        trigger = " ".join(
            line.strip().removeprefix("> ").strip()
            for line in trigger_match.group(1).splitlines()
        ) if trigger_match else ""
        guardrails = []
        for match in re.finditer(
            r"(?ms)^>\s*\*\*(?:前置过滤|排除边界|适用边界|不适用)\*\*：?(.*?)(?=\n\s*\n|\n## |\Z)",
            body,
        ):
            guardrails.append(" ".join(
                line.strip().removeprefix("> ").strip()
                for line in match.group(1).splitlines()
            ))
        topics[slug] = {
            "trigger": trigger,
            "exclusions": " ".join(item for item in guardrails if item),
        }
    return topics


def _coerce_topic_catalog(
    topic_catalog: Mapping[str, Any] | str | Path | None,
) -> dict[str, dict[str, str]]:
    if topic_catalog is None:
        return load_lesson_topics()
    if isinstance(topic_catalog, (str, Path)):
        return load_lesson_topics(topic_catalog)
    result: dict[str, dict[str, str]] = {}
    if not isinstance(topic_catalog, Mapping):
        return result
    for raw_slug, raw_value in topic_catalog.items():
        slug = str(raw_slug).strip()
        if not slug or slug == "__new_topic__":
            continue
        if isinstance(raw_value, Mapping):
            trigger = str(raw_value.get("trigger", "")).strip()
            exclusions = str(raw_value.get("exclusions", "")).strip()
        else:
            trigger = str(raw_value or "").strip()
            exclusions = ""
        result[slug] = {"trigger": trigger, "exclusions": exclusions}
    return result


def route_lesson_topic(
    review_report: str,
    topic_catalog: Mapping[str, Any] | str | Path | None,
    home: str,
    away: str,
    league: str,
) -> LessonRouteJudgment | None:
    """Choose among known lesson slugs using only review sections 4 and 5."""
    excerpt = _lesson_excerpt(review_report or "")
    topics = _coerce_topic_catalog(topic_catalog)
    if not excerpt or not topics or len(topics) >= 255:
        return None

    criteria: dict[str, Any] = {}
    for slug, detail in topics.items():
        criteria[slug] = {
            "trigger": detail["trigger"] or "No trigger condition recorded.",
            "exclusions": detail["exclusions"] or "No explicit exclusion recorded.",
        }
    criteria["__new_topic__"] = {
        "when": "The core lesson does not fit any listed existing topic.",
        "boundary": "Use only as a no-match choice. Do not invent a slug or a reason.",
    }
    state = {
        "match": {"home": home, "away": away, "league": league},
        "review_sections": excerpt,
        "topic_catalog": criteria,
    }
    judgment, _reason = _request_choice_result(
        state,
        criteria,
        "Which existing lesson topic best fits the core lesson in review sections "
        "4 and 5? Choose __new_topic__ only when none of the existing topics fits.",
        question_id="lesson_topic",
    )
    if judgment is None:
        return None

    ordered = sorted(
        ((slug, judgment.probabilities[slug]) for slug in topics),
        key=lambda item: item[1],
        reverse=True,
    )
    candidates = tuple(slug for slug, _ in ordered[:3])
    need_new = judgment.choice == "__new_topic__"
    recommended = "" if need_new else judgment.choice
    if not need_new and recommended not in topics:
        return None
    return LessonRouteJudgment(
        recommended=recommended,
        candidates=candidates,
        need_new_topic=need_new,
        confidence=judgment.confidence,
        probabilities=judgment.probabilities,
        choice_judgment=judgment,
    )
def extract_decision_section(report: str) -> str | None:
    """Return only section 8; never pass the surrounding match report to Jev."""
    return _extract_heading_section(report or "", _SECTION_8_RE)


def _plain(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    return re.sub(r"\*\*|__|~~|\x60", "", value).strip()


def _header_key(value: str) -> str:
    return re.sub(r"[\s_\x60*]+", "", _plain(value)).lower()


def _split_pipe_row(line: str) -> list[str] | None:
    line = line.strip()
    if not line.startswith("|"):
        return None
    cells = line.strip("|").split("|")
    return [cell.strip() for cell in cells]


def _column_indexes(headers: list[str]) -> dict[str, int] | None:
    indexes: dict[str, int] = {}
    for index, raw in enumerate(headers):
        key = _header_key(raw)
        if key in {"id", "candidateid", "候选id", "候选编号"}:
            indexes.setdefault("id", index)
        elif "玩法" in key:
            indexes.setdefault("play", index)
        elif "赔率" in key:
            indexes.setdefault("odds", index)
        elif "p市场" in key or "pmarket" in key or "市场概率" in key:
            indexes.setdefault("p_market", index)
        elif "p最终" in key or "pfinal" in key or "最终概率" in key:
            indexes.setdefault("p_final", index)
        elif key == "edge" or key.startswith("edge"):
            indexes.setdefault("edge", index)
        elif "资格" in key or "eligibility" in key or "eligible" in key:
            indexes.setdefault("eligibility", index)
    required = {"play", "odds", "p_market", "p_final", "edge"}
    return indexes if required.issubset(indexes) else None


def _numeric_value(raw: str, *, percent: bool = False) -> float | None:
    text = unicodedata.normalize("NFKC", raw or "")
    has_percent = "%" in text or "％" in text
    text = text.replace("−", "-").replace("﹣", "-").replace("－", "-")
    text = text.replace("＋", "+").replace(",", "")
    match = _NUMBER_RE.search(text)
    if not match:
        return None
    try:
        value = float(match.group(0))
    except ValueError:
        return None
    if has_percent:
        value /= 100.0
    return value if math.isfinite(value) else None


def _eligibility(raw: str | None, odds_cell: str, row: str) -> str:
    status = _header_key(raw or "")
    row_key = unicodedata.normalize("NFKC", row).casefold()
    unavailable = any(
        marker.casefold() in row_key for marker in _LEGACY_UNAVAILABLE_MARKERS
    )
    suspicious = (
        "?" in odds_cell
        or "排除" in row
        or "疑似串市场" in row
        or "不可执行" in row
        or unavailable
    )
    if suspicious or "excluded" in row_key or any(
        token in status for token in ("excluded", "排除", "不合格")
    ):
        return "excluded"
    if raw is None or not status:
        return "eligible"
    if status in {"eligible", "合格", "可选"}:
        return "eligible"
    return "invalid"


@dataclass(frozen=True, slots=True)
class _TableLayout:
    header_index: int
    headers: list[str]
    columns: dict[str, int]
    rows: list[tuple[int, list[str]]]      # (line index, cells) of candidate rows
    table_lines: tuple[int, ...]           # every line index that belongs to the table
    error: str | None


def _table_layout(lines: list[str]) -> _TableLayout | None:
    """Locate the candidate table. Parser and rewriter share this, so row N here
    is always candidate C{N:02d} in both places."""
    for index, line in enumerate(lines):
        headers = _split_pipe_row(line)
        if not headers:
            continue
        columns = _column_indexes(headers)
        if columns is None:
            continue
        rows: list[tuple[int, list[str]]] = []
        table_lines = [index]
        error = None
        for offset in range(index + 1, len(lines)):
            cells = _split_pipe_row(lines[offset])
            if cells is None:
                if rows:
                    break
                continue
            table_lines.append(offset)
            if all(re.fullmatch(r":?-{3,}:?", _plain(cell)) for cell in cells):
                continue
            if len(cells) != len(headers):
                error = "table_column_count_mismatch"
                break
            if not any(cell.strip() for cell in cells):
                continue
            rows.append((offset, cells))
        return _TableLayout(index, headers, columns, rows,
                            tuple(table_lines), error)
    return None


def _table_rows(section: str) -> tuple[dict[str, int] | None,
                                       list[tuple[list[str], str]],
                                       str | None]:
    lines = section.splitlines()
    layout = _table_layout(lines)
    if layout is None:
        return None, [], "candidate_table_not_found"
    rows = [(cells, lines[offset].strip()) for offset, cells in layout.rows]
    return layout.columns, rows, layout.error


def _selected_fields(section: str) -> tuple[str | None, str]:
    id_match = re.search(
        r"(?im)^\s*[-*]\s*\*\*选中候选\s*ID\*\*\s*[：:]\s*([A-Za-z0-9_-]+)",
        section,
    )
    text_match = re.search(
        r"(?im)^\s*[-*]\s*\*\*选中项\*\*\s*[：:]\s*(.*)$",
        section,
    )
    return (
        id_match.group(1).strip().upper() if id_match else None,
        text_match.group(1).strip() if text_match else "",
    )


def _looks_like_pass(text: str) -> bool:
    normalized = unicodedata.normalize("NFKC", text or "")
    return bool(re.search(r"(?i)\bpass\b|空仓|不下注|无下注", normalized))


def _normalized_play(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").casefold()
    text = re.sub(r"\*\*|__|~~|\x60", "", text)
    return "".join(char for char in text if char.isalnum() or "\u3400" <= char <= "\u9fff")


def _extract_evidence(section: str) -> str:
    normalized = _plain(section)
    match = re.search(
        r"(?im)证据强度(?:与凯利分数)?\s*[：:]\s*([^\r\n]+)", normalized)
    value = (match.group(1) if match else "").casefold()
    if re.search(r"\bstrong\b", value) or re.match(r"[\s*]*强(?:\s|[,，/]|$)", value):
        return "strong"
    if re.search(r"\bmedium\b", value) or re.match(r"[\s*]*中(?:\s|等|[,，/]|$)", value):
        return "medium"
    if re.search(r"\bweak\b", value) or re.match(r"[\s*]*弱(?:\s|[,，/]|$)", value):
        return "weak"
    if re.search(r"\bnone\b", value) or re.match(r"[\s*]*无(?:\s|[,，/]|$)", value):
        return "none"
    return "unknown"

def _extract_stake(section: str) -> tuple[float | None, bool]:
    lines = section.splitlines()
    for index, line in enumerate(lines):
        if not re.search(r"(?i)\*\*注额\*\*\s*[：:]", line):
            continue
        block = [line]
        for following in lines[index + 1:]:
            if re.match(r"^\s*[-*]\s+\*\*[^*]+\*\*\s*[：:]", following):
                break
            block.append(following)
        text = "\n".join(block)
        if re.search(r"(?i)(?<![A-Za-z])(?:nan|inf(?:inity)?)(?![A-Za-z])", text):
            return None, True
        if _looks_like_pass(text):
            return 0.0, False
        money = re.findall(
            r"(?:[$￥¥]\s*)([+-]?(?:\d[\d,]*(?:\.\d*)?|\.\d+))", text)
        if money:
            value = _numeric_value(money[-1])
            return value, value is None or value < 0
        value = _numeric_value(line.split("：", 1)[-1].split(":", 1)[-1])
        return value, value is not None and value < 0
    return None, False


def _invalid_parse(
    candidates: tuple[DecisionCandidate, ...],
    warnings: list[str],
    evidence: str,
    stake: float | None,
) -> DecisionParseResult:
    return DecisionParseResult(
        selected_id=None,
        is_pass=False,
        evidence=evidence,
        stake=stake,
        candidates=candidates,
        source="invalid",
        warnings=tuple(warnings),
    )


def parse_decision_section(report: str) -> DecisionParseResult | None:
    """Pure parser for section 8. Values are code-parsed and range-checked."""
    section = extract_decision_section(report)
    if not section:
        return None
    columns, rows, table_error = _table_rows(section)
    evidence = _extract_evidence(section)
    stake, bad_stake = _extract_stake(section)
    warnings: list[str] = []
    if bad_stake:
        warnings.append("invalid_stake")
    if columns is None or table_error:
        warnings.append(table_error or "candidate_table_not_found")
        return _invalid_parse((), warnings, evidence, stake)

    candidates: list[DecisionCandidate] = []
    ids_valid = True
    seen_ids: set[str] = set()
    for row_number, (cells, raw_row) in enumerate(rows, start=1):
        play = _plain(cells[columns["play"]])
        odds_cell = cells[columns["odds"]]
        explicit_id = (
            _plain(cells[columns["id"]]).upper()
            if "id" in columns else f"C{row_number:02d}"
        )
        expected_id = f"C{row_number:02d}"
        if (not _CANDIDATE_ID_RE.fullmatch(explicit_id)
                or explicit_id != expected_id
                or explicit_id in seen_ids):
            ids_valid = False
            warnings.append("invalid_or_nonsequential_candidate_id")
        seen_ids.add(explicit_id)

        odds = _numeric_value(odds_cell)
        p_market = _numeric_value(cells[columns["p_market"]], percent=True)
        p_final = _numeric_value(cells[columns["p_final"]], percent=True)
        edge = _numeric_value(cells[columns["edge"]], percent=True)
        eligibility_cell = (
            cells[columns["eligibility"]]
            if "eligibility" in columns else None
        )
        eligibility = _eligibility(eligibility_cell, odds_cell, raw_row)

        numeric_ok = (
            odds is not None and odds > 1.0
            and p_market is not None and 0.0 <= p_market <= 1.0
            and p_final is not None and 0.0 <= p_final <= 1.0
            and edge is not None and -1.0 <= edge <= odds - 1.0 + 0.05
        )
        if not numeric_ok:
            # Legacy reports sometimes list a market explicitly marked no-quote or
            # data-anomaly; it is not an executable candidate and may lack numbers.
            row_key = unicodedata.normalize("NFKC", raw_row).casefold()
            legacy_unavailable_row = (
                "eligibility" not in columns
                and any(marker.casefold() in row_key
                        for marker in _LEGACY_UNAVAILABLE_MARKERS)
            )
            if not legacy_unavailable_row:
                warnings.append(f"invalid_candidate_numbers:{explicit_id}")
                if eligibility == "eligible":
                    eligibility = "invalid"
        candidates.append(DecisionCandidate(
            candidate_id=explicit_id,
            play=play,
            odds=odds,
            p_market=p_market,
            p_final=p_final,
            edge=edge,
            eligibility=eligibility,
            raw_row=raw_row,
        ))

    candidate_tuple = tuple(candidates)
    if not candidates:
        return _invalid_parse(
            candidate_tuple, warnings + ["candidate_table_empty"], evidence, stake)
    if not ids_valid:
        return _invalid_parse(candidate_tuple, warnings, evidence, stake)
    if any(warning.startswith("invalid_candidate_numbers:")
           for warning in warnings):
        return _invalid_parse(candidate_tuple, warnings, evidence, stake)

    selected_id, selected_text = _selected_fields(section)
    selected: DecisionCandidate | None = None
    if selected_id:
        if selected_id == "NONE":
            return _invalid_parse(
                candidate_tuple, warnings + ["NONE_not_allowed_in_report"],
                evidence, stake)
        if selected_id == "PASS":
            return DecisionParseResult(
                selected_id="PASS", is_pass=True, evidence=evidence, stake=0.0,
                candidates=candidate_tuple, source="explicit_pass",
                warnings=tuple(warnings),
            )
        if "id" not in columns:
            return _invalid_parse(
                candidate_tuple, warnings + ["selected_id_without_id_column"],
                evidence, stake)
        selected = next(
            (candidate for candidate in candidates
             if candidate.candidate_id == selected_id),
            None,
        )
        if selected is None:
            return _invalid_parse(
                candidate_tuple, warnings + ["unknown_selected_id"],
                evidence, stake)
        if selected.eligibility != "eligible":
            return _invalid_parse(
                candidate_tuple, warnings + ["selected_candidate_not_eligible"],
                evidence, stake)

    eligible = [
        candidate for candidate in candidates
        if candidate.eligibility == "eligible"
    ]
    if not any(candidate.edge is not None and candidate.edge > 0
               for candidate in eligible):
        return DecisionParseResult(
            selected_id="PASS", is_pass=True, evidence=evidence, stake=0.0,
            candidates=candidate_tuple, source="deterministic_pass",
            warnings=tuple(warnings + ["no_positive_eligible_candidates"]),
        )
    if selected is not None:
        return DecisionParseResult(
            selected_id=selected.candidate_id, is_pass=False,
            evidence=evidence, stake=stake, candidates=candidate_tuple,
            source="explicit_id", warnings=tuple(warnings),
        )
    if _looks_like_pass(selected_text):
        return DecisionParseResult(
            selected_id="PASS", is_pass=True, evidence=evidence, stake=0.0,
            candidates=candidate_tuple, source="explicit_pass",
            warnings=tuple(warnings),
        )

    if selected_text:
        normalized = _normalized_play(selected_text)
        matches = [
            candidate for candidate in candidates
            if _normalized_play(candidate.play) == normalized
        ]
        if len(matches) == 1:
            selected = matches[0]
            if selected.eligibility == "eligible":
                return DecisionParseResult(
                    selected_id=selected.candidate_id, is_pass=False,
                    evidence=evidence, stake=stake, candidates=candidate_tuple,
                    source="exact_name", warnings=tuple(warnings),
                )
            return _invalid_parse(
                candidate_tuple, warnings + ["selected_candidate_not_eligible"],
                evidence, stake)
    return DecisionParseResult(
        selected_id=None,
        is_pass=False,
        evidence=evidence,
        stake=stake,
        candidates=candidate_tuple,
        source="ambiguous",
        warnings=tuple(warnings),
    )


def resolve_decision(report: str) -> DecisionParseResult | None:
    """Resolve an explicit or uniquely named choice, then use Jev only for ambiguity."""
    parsed = parse_decision_section(report)
    if parsed is None or parsed.source == "invalid":
        return None
    if parsed.selected_id is not None:
        return parsed
    if not parsed.candidates or any(
        candidate.eligibility == "invalid" for candidate in parsed.candidates
    ):
        return None

    criteria: dict[str, Any] = {}
    for candidate in parsed.candidates:
        criteria[candidate.candidate_id] = {
            "play": candidate.play,
            "eligibility": candidate.eligibility,
            "row": candidate.raw_row,
        }
    criteria["PASS"] = "The final section 8 decision explicitly says pass or empty stake."
    criteria["NONE"] = "The section does not identify one candidate or an explicit pass."
    section = extract_decision_section(report) or ""
    judgment, _reason = _request_choice_result(
        {"decision_section": section, "candidates": criteria},
        criteria,
        "Which candidate ID is the final selected wager in section 8? Choose PASS "
        "only when the final decision explicitly says pass or empty stake. Choose "
        "NONE when the section does not support one unambiguous answer.",
        question_id="decision_candidate",
    )
    if judgment is None:
        return None
    if judgment.choice == "NONE":
        return replace(
            parsed, selected_id=None, source="jev_none",
            confidence=judgment.confidence,
            probabilities=judgment.probabilities,
            choice_judgment=judgment,
            warnings=parsed.warnings + ("jev_selected_none",),
        )
    if judgment.choice == "PASS":
        return replace(
            parsed, selected_id="PASS", is_pass=True, stake=0.0,
            source="jev", confidence=judgment.confidence,
            probabilities=judgment.probabilities,
            choice_judgment=judgment,
        )
    selected = next(
        (candidate for candidate in parsed.candidates
         if candidate.candidate_id == judgment.choice),
        None,
    )
    if selected is None or selected.eligibility != "eligible":
        return replace(
            parsed, selected_id=None, source="invalid",
            confidence=judgment.confidence,
            probabilities=judgment.probabilities,
            choice_judgment=judgment,
            warnings=parsed.warnings + ("jev_selected_invalid_candidate",),
        )
    return replace(
        parsed, selected_id=selected.candidate_id, is_pass=False,
        source="jev", confidence=judgment.confidence,
        probabilities=judgment.probabilities,
        choice_judgment=judgment,
    )


def decision_to_extract_dict(result: DecisionParseResult) -> dict[str, Any] | None:
    """Copy the chosen row's values into analyzer.extract_decision's stable contract."""
    if result.source == "invalid" or "invalid_stake" in result.warnings:
        return None
    if result.is_pass or result.selected_id == "PASS":
        return {
            "pass": True, "play": "", "odds": 0.0, "edge": 0.0,
            "p_final": 0.0, "evidence": result.evidence,
            "stake": 0.0, "warnings": list(result.warnings),
        }
    selected = next(
        (candidate for candidate in result.candidates
         if candidate.candidate_id == result.selected_id),
        None,
    )
    if (selected is None or selected.eligibility != "eligible"
            or selected.odds is None or selected.odds <= 1.0
            or selected.edge is None or not math.isfinite(selected.edge)
            or selected.p_final is None
            or not 0.0 <= selected.p_final <= 1.0
            or result.stake is not None
            and (not math.isfinite(result.stake) or result.stake < 0)):
        return None
    return {
        "pass": False,
        "play": selected.play,
        "odds": selected.odds,
        "edge": selected.edge,
        "p_final": selected.p_final,
        "evidence": result.evidence,
        "stake": result.stake,
        "warnings": list(result.warnings),
    }


# ─── 生成时选中项：代码筛正 edge → Jev 在正 edge 候选 + PASS 中选 → 失败退回 edge 最高 ──
# Jev 只返回候选 ID；赔率/edge/p_final 一律从该行复制，k 与注额由代码按 SOP 7.5 算。

_SECTION_8_RE = r"^###\s*8(?:[.．、]|\s)\s*(?:投注决策|decision)[^\r\n]*"
_SECTION_7_RE = r"^###\s*7(?:[.．、]|\s)\s*最终精算结论[^\r\n]*"
_SECTION_7_MAX_CHARS = 6000
_DECISION_FINAL_RE = re.compile(
    r"(?m)^[ \t]*<!--[ \t]*decision_final[ \t]+(\{.*\})[ \t]*-->[ \t]*(?:\r?\n)?")
_EVIDENCE_LABELS = {
    "strong": "强", "medium": "中", "weak": "弱", "none": "无", "unknown": "未写明",
}
_FALLBACK_LABELS = {
    "jev_disabled": "未启用",
    "missing_api_key": "未配置 TYPESAFE_API_KEY",
    "sdk_missing": "未安装 TypeSafe SDK",
    "section_7_missing": "报告缺第 7 节结论",
    "no_confidence_threshold": "未配置 TYPESAFE_DECISION_MIN_CONFIDENCE",
    "low_confidence": "Jev 置信度低于阈值",
}


@dataclass(frozen=True, slots=True)
class DecisionSelection:
    selected_id: str                        # C01.. 或 PASS
    candidate: DecisionCandidate | None
    source: str                             # jev / jev_pass / edge_max / no_positive_edge / zero_kelly / zero_stake
    fallback_reason: str                    # Jev 未参与或未被采纳的原因码；采纳时为空
    evidence: str
    confidence_score: int | None            # 第 7 节置信度 0~100
    k: float
    stake: float
    candidates: tuple[DecisionCandidate, ...]
    positive_ids: tuple[str, ...]
    judgment: ChoiceJudgment | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidates", tuple(self.candidates))
        object.__setattr__(self, "positive_ids", tuple(self.positive_ids))


def extract_conclusion_section(report: str) -> str | None:
    return _extract_heading_section(report or "", _SECTION_7_RE)


def parse_report_confidence(report: str) -> int | None:
    """第 7 节「置信度」数值；兼容 `**72 / 100**`、`**74** / 100` 等写法。"""
    section = extract_conclusion_section(report)
    if not section:
        return None
    match = re.search(r"(?m)^\s*[-*]?\s*置信度\s*[：:]\s*(\d{1,3})", _plain(section))
    if not match:
        return None
    value = int(match.group(1))
    return value if 0 <= value <= 100 else None


def kelly_fraction(evidence: str, confidence_score: int | None) -> float:
    """SOP 7.5：证据强度定 k（未写明按默认中档 1/4）；置信度 <60 压至不高于 1/8。"""
    k_table = config.PARLAY_EVIDENCE_K
    k = k_table.get(evidence, k_table["medium"])
    if confidence_score is not None and confidence_score < 60:
        k = min(k, k_table["weak"])
    return k


def stake_for(candidate: DecisionCandidate, k: float) -> float:
    """注额 = 本金 × k × edge/(赔率−1)，截到单注上限，保留 1 位小数。"""
    if candidate.odds is None or candidate.odds <= 1.0 or candidate.edge is None:
        return 0.0
    raw = config.PARLAY_STAKE_BANKROLL * k * candidate.edge / (candidate.odds - 1.0)
    return round(max(0.0, min(raw, config.PARLAY_STAKE_CAP)), 1)


def _selection_pool(report: str) -> tuple[tuple[DecisionCandidate, ...], str] | None:
    """候选表 → (按行序重编号的候选, 证据强度)。模型写的「选中候选 ID」不参与此处判断。"""
    parsed = parse_decision_section(report)
    if parsed is None or not parsed.candidates:
        return None
    if any(warning in {"table_column_count_mismatch", "candidate_table_not_found"}
           for warning in parsed.warnings):
        return None
    candidates = tuple(
        replace(candidate, candidate_id=f"C{index:02d}")
        for index, candidate in enumerate(parsed.candidates, start=1)
    )
    return candidates, parsed.evidence


def _positive_candidates(
    candidates: tuple[DecisionCandidate, ...],
) -> list[DecisionCandidate]:
    # 解析时数字不合法的 eligible 行已被降为 invalid，这里只剩数字已校验的行
    return [
        candidate for candidate in candidates
        if candidate.eligibility == "eligible"
        and candidate.odds is not None and candidate.odds > 1.0
        and candidate.p_final is not None
        and candidate.edge is not None and candidate.edge > 0
    ]


def _request_selection(
    report: str,
    positives: list[DecisionCandidate],
    home: str,
    away: str,
    league: str,
    confidence_score: int | None,
) -> tuple[ChoiceJudgment | None, str | None]:
    conclusion = extract_conclusion_section(report)
    if not conclusion:
        return None, "section_7_missing"
    criteria: dict[str, Any] = {
        candidate.candidate_id: {
            "play": candidate.play,
            "odds": round(candidate.odds, 3),
            "edge": f"{candidate.edge:+.1%}",
            "p_final": f"{candidate.p_final:.1%}",
        }
        for candidate in positives
    }
    criteria["PASS"] = {
        "when": "Every candidate contradicts the section 7 direction, or the "
                "section 7 risk notes explicitly argue against each of them.",
        "boundary": "A thin but positive edge alone is not a reason to pass.",
    }
    state = {
        "match": {"home": home, "away": away, "league": league},
        "section_7_conclusion": conclusion[:_SECTION_7_MAX_CHARS],
        "report_confidence": confidence_score,
        "candidates": criteria,
    }
    return _request_choice_result(
        state,
        criteria,
        "Every listed candidate is a section 8 wager whose positive edge was "
        "computed and checked by code. Using the section 7 final conclusion "
        "(direction per market, confidence, key evidence, risks), which single "
        "candidate should be the wager? Prefer the candidate whose side and market "
        "agree with the section 7 judgments and key evidence; among equally "
        "consistent candidates prefer the higher edge. Choose PASS only when no "
        "candidate is consistent with section 7.",
        question_id="decision_select",
    )


def select_decision(
    report: str,
    home: str = "",
    away: str = "",
    league: str = "",
    *,
    min_confidence: float | None,
    use_jev: bool = True,
) -> DecisionSelection | None:
    """代码筛出 eligible 且 edge>0 的候选；无则直接 PASS（不调 Jev）。
    否则 Jev 在正 edge 候选 + PASS 中选；Jev 失败/低置信度 → edge 最高的正值项。
    候选表无法解析时返回 None（报告保持原样，串关抽取走既有 fallback）。"""
    pool = _selection_pool(report)
    if pool is None:
        return None
    candidates, evidence = pool
    confidence_score = parse_report_confidence(report)
    positives = _positive_candidates(candidates)
    common = {
        "evidence": evidence,
        "confidence_score": confidence_score,
        "candidates": candidates,
        "positive_ids": tuple(c.candidate_id for c in positives),
    }
    if not positives:
        return DecisionSelection(
            selected_id="PASS", candidate=None, source="no_positive_edge",
            fallback_reason="", k=0.0, stake=0.0, **common)

    judgment: ChoiceJudgment | None = None
    reason: str | None = "jev_disabled"
    if use_jev:
        judgment, reason = _request_selection(
            report, positives, home, away, league, confidence_score)

    chosen: DecisionCandidate | None = None
    source = "edge_max"
    fallback = reason or ""
    if judgment is not None:
        if min_confidence is None:
            fallback = "no_confidence_threshold"
        elif judgment.confidence < min_confidence:
            fallback = "low_confidence"
        elif judgment.choice == "PASS":
            return DecisionSelection(
                selected_id="PASS", candidate=None, source="jev_pass",
                fallback_reason="", k=0.0, stake=0.0, judgment=judgment,
                **common)
        else:
            chosen = next(
                (c for c in positives if c.candidate_id == judgment.choice), None)
            if chosen is None:
                fallback = "unknown_choice"
            else:
                source, fallback = "jev", ""
    if chosen is None:
        chosen = max(positives, key=lambda c: c.edge)   # 并列取表中靠前者

    k = kelly_fraction(evidence, confidence_score)
    if k <= 0:
        return DecisionSelection(
            selected_id="PASS", candidate=None, source="zero_kelly",
            fallback_reason=fallback, k=0.0, stake=0.0, judgment=judgment,
            **common)
    stake = stake_for(chosen, k)
    if stake <= 0:                    # 薄 edge × 高赔率：注额舍入到 $0.0，等同空仓
        return DecisionSelection(
            selected_id="PASS", candidate=None, source="zero_stake",
            fallback_reason=fallback, k=k, stake=0.0, judgment=judgment,
            **common)
    return DecisionSelection(
        selected_id=chosen.candidate_id, candidate=chosen, source=source,
        fallback_reason=fallback, k=k, stake=stake,
        judgment=judgment, **common)


def _k_label(k: float) -> str:
    for label, value in (("1/2", 0.5), ("1/4", 0.25), ("1/8", 0.125), ("0", 0.0)):
        if abs(k - value) < 1e-9:
            return label
    return f"{k:g}"


def _fallback_label(reason: str) -> str:
    if reason.startswith("request_error:"):
        return f"调用失败（{reason.split(':', 1)[1]}）"
    return _FALLBACK_LABELS.get(reason, reason or "未知原因")


def _selection_block(selection: DecisionSelection) -> list[str]:
    """第 8 节最终选定段（代码写入，替代模型初选）。"""
    judgment = selection.judgment
    jev_note = (
        f"{judgment.actual_model}，置信度 {judgment.confidence:.2f}"
        if judgment else ""
    )
    n_pos = len(selection.positive_ids)
    if selection.source == "jev":
        source_line = f"Jev（{jev_note}）在 {n_pos} 个正 edge 候选 + PASS 中选定"
    elif selection.source == "jev_pass":
        source_line = f"Jev（{jev_note}）判定 PASS：正 edge 候选均与第 7 节结论不一致"
    elif selection.source == "no_positive_edge":
        source_line = "代码判定：无 eligible 且 edge>0 的候选，直接 pass（未调用 Jev）"
    elif selection.source == "zero_stake":
        source_line = (f"选定项 edge 过薄，按 k={_k_label(selection.k)} 算出的注额舍入为 0，"
                       f"不下注")
    else:
        fallback = _fallback_label(selection.fallback_reason)
        if judgment and selection.fallback_reason in {"low_confidence",
                                                      "no_confidence_threshold"}:
            fallback += f"；Jev 选 {judgment.choice}，{jev_note}"
        if selection.source == "zero_kelly":
            source_line = (f"证据强度=无（k=0），不下注（edge 最高规则；"
                           f"Jev 未参与：{fallback}）")
        else:
            source_line = f"代码 edge 最高规则（Jev 未参与：{fallback}）"

    lines = [f"- **选中候选 ID**：{selection.selected_id}"]
    candidate = selection.candidate
    if candidate is None:
        lines.append("- **选中项**：pass，空仓")
    else:
        lines.append(
            f"- **选中项**：{candidate.play} @ {candidate.odds:.2f}"
            f"（edge {candidate.edge:+.1%}，p_最终 {candidate.p_final:.1%}）")
    lines.append(f"- **决策来源**：{source_line}")
    if candidate is None:
        lines.append("- **注额**：pass，空仓")
    else:
        low = (selection.confidence_score is not None
               and selection.confidence_score < 60)
        # 括注里不再出现 $ 金额：parse_decision_section 取该行最后一个 $ 数作注额
        lines.append(
            f"- **注额**：${selection.stake:.1f}"
            f"（k={_k_label(selection.k)}，证据强度"
            f"{_EVIDENCE_LABELS.get(selection.evidence, selection.evidence)}"
            + (f"，置信度 {selection.confidence_score}<60 压至 ≤1/8" if low else "")
            + f"；本金 {config.PARLAY_STAKE_BANKROLL:.0f}×k×edge/(赔率−1)，"
              f"已截单注上限）")
    payload = {
        "schema": "decision_final/1",
        "selected_id": selection.selected_id,
        "pass": candidate is None,
        "play": candidate.play if candidate else "",
        "odds": round(candidate.odds, 6) if candidate else 0.0,
        "edge": round(candidate.edge, 6) if candidate else 0.0,
        "p_final": round(candidate.p_final, 6) if candidate else 0.0,
        "evidence": selection.evidence,
        "k": selection.k,
        "stake": selection.stake,
        "source": selection.source,
        "fallback_reason": selection.fallback_reason,
        "jev_confidence": judgment.confidence if judgment else None,
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).replace("-->", "--\\u003e")
    # 注释放在段首：放在注额行之后会被注额解析当作续行（含 "pass" 字样）
    return [f"<!-- decision_final {body} -->"] + lines


_MODEL_PICK_LABELS = (
    (re.compile(r"^(\s*[-*]\s*)\*\*选中项\*\*"), r"\1**模型初选**"),
    (re.compile(r"^(\s*[-*]\s*)\*\*注额\*\*"), r"\1**模型初选注额**"),
)
_MODEL_SELECTED_ID_RE = re.compile(r"^\s*[-*]\s*\*\*选中候选\s*ID\*\*")
# 代码写入的最终选定段（紧跟 decision_final 注释的四行）
_PRIOR_BLOCK_LINE_RE = re.compile(
    r"^- \*\*(?:选中候选 ID|选中项|决策来源|注额)\*\*：")


def _section_span(report: str) -> tuple[int, int] | None:
    match = re.search(_SECTION_8_RE, report, re.I | re.M)
    if not match:
        return None
    rest = report[match.end():]
    following = re.search(r"(?m)^###\s+", rest)
    end = match.end() + (following.start() if following else len(rest))
    return match.start(), end


def apply_selection(report: str, selection: DecisionSelection) -> str:
    """把代码/Jev 的最终选定写回第 8 节：
    · 候选表 ID 列归一为 C01..Cn（缺列则补），与 selected_id 对应；
    · 模型原「选中项/注额」改名为「模型初选/模型初选注额」，删去其「选中候选 ID」；
    · 表后插入最终选定段 + 机器可读的 decision_final 注释；
    · 已有代码写入的最终选定段先整段移除，重复调用结果不叠加。"""
    report = report or ""
    span = _section_span(report)
    if span is None:
        return report
    start, end = span
    lines = report[start:end].split("\n")
    layout = _table_layout(lines)
    if layout is None or layout.error:
        return report
    prior_block: set[int] = set()
    for index, line in enumerate(lines):
        if not _DECISION_FINAL_RE.match(line + "\n"):
            continue
        prior_block.add(index)
        follow = index + 1
        while follow < len(lines) and _PRIOR_BLOCK_LINE_RE.match(lines[follow]):
            prior_block.add(follow)
            follow += 1
        if follow < len(lines) and not lines[follow].strip():
            prior_block.add(follow)

    has_id = "id" in layout.columns
    row_numbers = {offset: n for n, (offset, _) in enumerate(layout.rows, start=1)}
    table_set = set(layout.table_lines)
    table_end = layout.table_lines[-1]
    output: list[str] = []
    for index, line in enumerate(lines):
        if index in prior_block:
            continue
        if index in table_set:
            cells = _split_pipe_row(line) or []
            if index == layout.header_index:
                if not has_id:
                    cells.insert(0, "ID")
            elif index in row_numbers:
                row_id = f"C{row_numbers[index]:02d}"
                if has_id:
                    cells[layout.columns["id"]] = row_id
                else:
                    cells.insert(0, row_id)
            elif all(re.fullmatch(r":?-{3,}:?", _plain(c)) for c in cells):
                if not has_id:
                    cells.insert(0, "----")
            else:
                continue                      # 全空行不计编号，直接丢弃
            output.append("| " + " | ".join(cells) + " |")
            if index == table_end:
                output.append("")
                output.extend(_selection_block(selection))
                if index + 1 < len(lines) and lines[index + 1].strip():
                    output.append("")
            continue
        if _MODEL_SELECTED_ID_RE.match(line):
            continue
        for pattern, replacement in _MODEL_PICK_LABELS:
            line = pattern.sub(replacement, line)
        output.append(line)
    return report[:start] + "\n".join(output) + report[end:]


def read_final_decision(report: str) -> dict[str, Any] | None:
    """读取 apply_selection 写入的 decision_final，转成 extract_decision 契约。
    字段缺失或越界一律返回 None，由调用方走既有解析/LLM 路径。"""
    matches = list(_DECISION_FINAL_RE.finditer(report or ""))
    if not matches:
        return None
    try:
        data = json.loads(matches[-1].group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("schema") != "decision_final/1":
        return None
    evidence = data.get("evidence")
    if evidence not in _EVIDENCE_LABELS:
        return None

    def number(key: str) -> float | None:
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        value = float(value)
        return value if math.isfinite(value) else None

    stake = number("stake")
    if data.get("pass") is True:
        return {
            "pass": True, "play": "", "odds": 0.0, "edge": 0.0,
            "p_final": 0.0, "evidence": evidence, "stake": 0.0, "warnings": [],
        }
    odds, edge, p_final = number("odds"), number("edge"), number("p_final")
    play = data.get("play")
    if (data.get("pass") is not False or not isinstance(play, str) or not play
            or odds is None or odds <= 1.0
            or edge is None or edge <= 0
            or p_final is None or not 0.0 <= p_final <= 1.0
            or stake is None or stake < 0):
        return None
    return {
        "pass": False, "play": play, "odds": odds, "edge": edge,
        "p_final": p_final, "evidence": evidence, "stake": stake,
        "warnings": [],
    }


def _case_hash(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text or "").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]


def _log_shadow(record: dict[str, Any]) -> None:
    log.info(
        "TYPESAFE_SHADOW %s",
        json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
    )


def log_lesson_shadow(
    review_report: str,
    baseline: Mapping[str, Any] | None,
) -> None:
    """Run after the baseline route and log only a hash and typed comparison fields."""
    excerpt = _lesson_excerpt(review_report or "")
    judgment = route_lesson_topic(review_report, None, "", "", "")
    baseline_choice = ""
    baseline_new = False
    if isinstance(baseline, Mapping):
        baseline_new = bool(baseline.get("need_new_topic"))
        baseline_choice = (
            "__new_topic__" if baseline_new
            else str(baseline.get("recommended") or "")
        )

    typesafe_choice = ""
    top3: list[str] = []
    confidence: float | None = None
    probabilities: Mapping[str, float] = {}
    metadata: ChoiceJudgment | None = None
    fallback_reason = ""
    if judgment is None:
        fallback_reason = "no_valid_typesafe_judgment"
    else:
        typesafe_choice = (
            "__new_topic__" if judgment.need_new_topic else judgment.recommended
        )
        top3 = list(judgment.candidates)
        confidence = judgment.confidence
        probabilities = judgment.probabilities
        metadata = judgment.choice_judgment

    agreement = None
    if isinstance(baseline, Mapping) and judgment is not None:
        agreement = (
            baseline_new == judgment.need_new_topic
            and (baseline_new or baseline_choice == judgment.recommended)
        )
    _log_shadow({
        "feature": "lesson",
        "case_hash": _case_hash(excerpt),
        "baseline_choice": baseline_choice,
        "typesafe_choice": typesafe_choice,
        "top3": top3,
        "confidence": confidence,
        "probabilities": dict(probabilities),
        "agreement": agreement,
        "requested_model": metadata.requested_model if metadata else config.TYPESAFE_MODEL,
        "actual_model": metadata.actual_model if metadata else "",
        "request_id": metadata.request_id if metadata else "",
        "latency_ms": metadata.latency_ms if metadata else 0,
        "token_usage": dict(metadata.token_usage) if metadata else {},
        "fallback_reason": fallback_reason,
    })


def log_decision_shadow(
    report: str,
    baseline: Mapping[str, Any] | None,
) -> None:
    """Compare the LLM baseline with a section-8-only TypeSafe result."""
    section = extract_decision_section(report or "") or ""
    result = resolve_decision(report or "")
    resolved = decision_to_extract_dict(result) if result else None
    metadata = result.choice_judgment if result else None
    baseline_choice = ""
    if isinstance(baseline, Mapping):
        baseline_choice = (
            "PASS" if baseline.get("pass")
            else str(baseline.get("play") or "")
        )

    typesafe_choice = result.selected_id if result and result.selected_id else "NONE"
    probabilities = result.probabilities if result else {}
    top3 = [
        key for key, _ in sorted(
            probabilities.items(), key=lambda item: item[1], reverse=True
        )[:3]
    ]
    if result is None:
        fallback_reason = "no_valid_typesafe_decision"
    elif result.source == "jev_none":
        fallback_reason = "jev_selected_none"
    elif resolved is None:
        fallback_reason = "decision_validation_failed"
    else:
        fallback_reason = ""

    agreement: bool | None = None
    if isinstance(baseline, Mapping) and resolved is not None:
        if baseline.get("pass"):
            agreement = bool(resolved.get("pass"))
        elif resolved.get("pass"):
            agreement = False
        elif result and result.selected_id:
            selected = next(
                (candidate for candidate in result.candidates
                 if candidate.candidate_id == result.selected_id),
                None,
            )
            agreement = bool(
                selected
                and _normalized_play(selected.play)
                == _normalized_play(str(baseline.get("play") or ""))
            )

    _log_shadow({
        "feature": "decision",
        "case_hash": _case_hash(section),
        "baseline_choice": baseline_choice,
        "typesafe_choice": typesafe_choice,
        "top3": top3,
        "confidence": result.confidence if result else None,
        "probabilities": dict(probabilities),
        "agreement": agreement,
        "requested_model": metadata.requested_model if metadata else config.TYPESAFE_MODEL,
        "actual_model": metadata.actual_model if metadata else "",
        "request_id": metadata.request_id if metadata else "",
        "latency_ms": metadata.latency_ms if metadata else 0,
        "token_usage": dict(metadata.token_usage) if metadata else {},
        "fallback_reason": fallback_reason,
    })


def _model_pick_id(report: str) -> str:
    """报告模型自己写的选中候选（影子对比基线）；无法确定时为 NONE。"""
    parsed = parse_decision_section(report)
    if parsed is None or parsed.source in {"invalid", "ambiguous"}:
        return "NONE"
    return parsed.selected_id or "NONE"


def selection_record(
    report: str,
    selection: DecisionSelection | None,
    *,
    feature: str,
) -> dict[str, Any]:
    """选中项决策的结构化日志：只含哈希、候选 ID 和数值，不含报告正文。"""
    section = extract_decision_section(report or "") or ""
    judgment = selection.judgment if selection else None
    baseline = _model_pick_id(report)
    final_choice = selection.selected_id if selection else ""
    probabilities = dict(judgment.probabilities) if judgment else {}
    return {
        "feature": feature,
        "case_hash": _case_hash(section),
        "baseline_choice": baseline,
        "typesafe_choice": judgment.choice if judgment else "",
        "final_choice": final_choice,
        "source": selection.source if selection else "",
        "positive_ids": list(selection.positive_ids) if selection else [],
        "top3": [key for key, _ in sorted(
            probabilities.items(), key=lambda item: item[1], reverse=True)[:3]],
        "confidence": judgment.confidence if judgment else None,
        "probabilities": probabilities,
        "agreement": (baseline == final_choice) if selection else None,
        "requested_model": judgment.requested_model if judgment else config.TYPESAFE_MODEL,
        "actual_model": judgment.actual_model if judgment else "",
        "request_id": judgment.request_id if judgment else "",
        "latency_ms": judgment.latency_ms if judgment else 0,
        "token_usage": dict(judgment.token_usage) if judgment else {},
        "fallback_reason": (
            selection.fallback_reason if selection
            else "candidate_table_unavailable"),
    }


def log_selection_shadow(report: str, home: str, away: str, league: str) -> None:
    """影子模式：后台跑一遍生成时选定，只记日志，不改报告。"""
    selection = select_decision(
        report, home, away, league,
        min_confidence=config.TYPESAFE_DECISION_MIN_CONFIDENCE)
    _log_shadow(selection_record(report, selection, feature="decision_select"))


def log_selection(report: str, selection: DecisionSelection | None) -> None:
    """active 模式的审计行（同 TYPESAFE_SHADOW 字段口径）。"""
    log.info(
        "TYPESAFE_DECISION %s",
        json.dumps(selection_record(report, selection, feature="decision_select"),
                   ensure_ascii=False, separators=(",", ":"), allow_nan=False),
    )
