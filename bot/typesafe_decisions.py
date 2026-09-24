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
    return _extract_heading_section(
        report or "",
        r"^###\s*8(?:[.．、]|\s)\s*(?:投注决策|decision)[^\r\n]*",
    )


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


def _table_rows(section: str) -> tuple[dict[str, int] | None,
                                       list[tuple[list[str], str]],
                                       str | None]:
    lines = section.splitlines()
    for index, line in enumerate(lines):
        headers = _split_pipe_row(line)
        if not headers:
            continue
        columns = _column_indexes(headers)
        if columns is None:
            continue
        rows: list[tuple[list[str], str]] = []
        for row_line in lines[index + 1:]:
            cells = _split_pipe_row(row_line)
            if cells is None:
                if rows:
                    break
                continue
            if all(re.fullmatch(r":?-{3,}:?", _plain(cell)) for cell in cells):
                continue
            if len(cells) != len(headers):
                return columns, rows, "table_column_count_mismatch"
            if not any(cell.strip() for cell in cells):
                continue
            rows.append((cells, row_line.strip()))
        return columns, rows, None
    return None, [], "candidate_table_not_found"


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
