#!/usr/bin/env python3
"""Offline TypeSafe evaluation helpers; network requests require an explicit live flag."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot import config
from bot import typesafe_decisions as decisions


def _print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))


def _report_paths(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(
        path for path in root.rglob("*_report*.md")
        if path.is_file()
        and not path.name.endswith(("_review.md", "_review_consult.md"))
    )


def evaluate_reports(root: Path) -> dict[str, Any]:
    reports = _report_paths(root)
    stats: dict[str, Any] = {
        "reports_scanned": len(reports),
        "section8_found": 0,
        "candidate_table_parsed": 0,
        "pass_identified": 0,
        "non_pass_identified": 0,
        "unique_name_matches": 0,
        "explicit_id_matches": 0,
        "needs_jev": 0,
        "legacy_llm_fallback": 0,
        "selected_row_numeric_copies": 0,
        "numeric_copy_mismatches": 0,
        "unreadable_reports": 0,
        "legacy_llm_differences": None,
        "legacy_llm_differences_note": "offline scan does not call the legacy LLM extractor",
        "unparsed_examples": [],
    }
    for path in reports:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            stats["unreadable_reports"] += 1
            continue
        section = decisions.extract_decision_section(text)
        if section is None:
            continue
        stats["section8_found"] += 1
        parsed = decisions.parse_decision_section(text)
        if parsed is not None and parsed.candidates:
            stats["candidate_table_parsed"] += 1
        if parsed is None or parsed.source == "invalid":
            stats["legacy_llm_fallback"] += 1
            if len(stats["unparsed_examples"]) < 20:
                stats["unparsed_examples"].append({
                    "path": path.relative_to(root).as_posix(),
                    "reason": (parsed.warnings[0] if parsed and parsed.warnings
                               else "parse_failed"),
                })
            continue
        if parsed.source == "ambiguous":
            stats["needs_jev"] += 1
            continue
        if parsed.source == "exact_name":
            stats["unique_name_matches"] += 1
        elif parsed.source == "explicit_id":
            stats["explicit_id_matches"] += 1

        if parsed.selected_id == "PASS" or parsed.is_pass:
            stats["pass_identified"] += 1
        elif parsed.selected_id:
            stats["non_pass_identified"] += 1
            output = decisions.decision_to_extract_dict(parsed)
            candidate = next(
                (item for item in parsed.candidates
                 if item.candidate_id == parsed.selected_id),
                None,
            )
            if output and candidate:
                copied = (
                    math.isclose(output["odds"], candidate.odds, abs_tol=1e-12)
                    and math.isclose(output["edge"], candidate.edge, abs_tol=1e-12)
                    and math.isclose(output["p_final"], candidate.p_final, abs_tol=1e-12)
                )
                if copied:
                    stats["selected_row_numeric_copies"] += 1
                else:
                    stats["numeric_copy_mismatches"] += 1
            else:
                stats["legacy_llm_fallback"] += 1
        else:
            stats["legacy_llm_fallback"] += 1
    stats["reports_without_section8"] = (
        stats["reports_scanned"] - stats["unreadable_reports"]
        - stats["section8_found"]
    )
    return stats


def inspect_lesson_cases(path: Path) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    malformed = 0
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if not isinstance(row, dict) or not row.get("case_id"):
                malformed += 1
                continue
            records.append(row)

    confirmed = [
        row for row in records
        if row.get("label_status") == "confirmed"
        and isinstance(row.get("expected_new_topic"), bool)
        and (row.get("expected_new_topic") or row.get("expected_recommended"))
    ]
    return {
        "cases": len(records),
        "confirmed_labels": len(confirmed),
        "pending_labels": len(records) - len(confirmed),
        "malformed_rows": malformed,
        "ready_for_live_metrics": bool(confirmed),
    }


def prepare_lesson_cases(report_root: Path, output_path: Path) -> int:
    """Export sections 4/5 for annotation; every label starts explicitly pending."""
    records = []
    for path in sorted(report_root.rglob("*_review.md")):
        try:
            report = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        excerpt = decisions._lesson_excerpt(report)
        if not excerpt:
            continue
        stem = path.stem.removesuffix("_review")
        home, separator, away = stem.partition("_vs_")
        league_match = re.search(
            r"(?m)^##\s*赛事[：:]\s*(.+?)(?:\s{2,}开球|\r?$)",
            report,
        )
        records.append({
            "case_id": path.relative_to(report_root).as_posix(),
            "home": home.replace("_", " ") if separator else "",
            "away": away.replace("_", " ") if separator else "",
            "league": league_match.group(1).strip() if league_match else "",
            "review_sections": excerpt,
            "expected_recommended": None,
            "accepted_slugs": [],
            "expected_new_topic": None,
            "label_status": "pending",
        })
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records),
        encoding="utf-8",
    )
    return len(records)


def inspect_decision_cases(path: Path) -> dict[str, Any]:
    checked = 0
    correct = 0
    malformed = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            result = decisions.parse_decision_section(str(row["report"]))
        except (json.JSONDecodeError, KeyError, TypeError):
            malformed += 1
            continue
        if result is None:
            malformed += 1
            continue
        checked += 1
        if (
            result.source == row.get("expected_source")
            and result.selected_id == row.get("expected_selected_id")
            and result.is_pass == row.get("expected_pass")
        ):
            correct += 1
    return {
        "cases": checked,
        "correct": correct,
        "mismatches": checked - correct,
        "malformed_rows": malformed,
    }


def _retry_policy(sdk: Any) -> Any:
    return sdk.RetryPolicy(
        max_retries=config.TYPESAFE_MAX_RETRIES,
        timeout=config.TYPESAFE_RETRY_BUDGET_SECONDS,
        http_statuses={408, 429, *range(500, 600)},
        api_connection_error=True,
        api_timeout_error=True,
    )


def live_probe() -> dict[str, Any]:
    if not config.TYPESAFE_API_KEY:
        raise RuntimeError("TYPESAFE_API_KEY is not configured")
    import logging
    logging.getLogger("typesafe_sdk").disabled = True
    import typesafe_sdk as sdk

    args: dict[str, Any] = {
        "api_key": config.TYPESAFE_API_KEY,
        "model": config.TYPESAFE_MODEL,
        "timeout": config.TYPESAFE_HTTP_TIMEOUT_SECONDS,
        "retry": _retry_policy(sdk),
    }
    if config.TYPESAFE_BASE_URL:
        args["base_url"] = config.TYPESAFE_BASE_URL
    with sdk.TypeSafeClient(**args) as client:
        listed = client.models.list()
        names = [
            str(getattr(model, "name", ""))
            for model in getattr(listed, "models", [])
        ]
        if config.TYPESAFE_MODEL not in names:
            raise RuntimeError("configured model is not listed for this account")

    judgment, reason = decisions._request_choice_result(
        {"probe": "Return the only valid option."},
        {"ok": "The probe request is functioning.", "none": "No valid option."},
        "Choose the functioning probe option.",
        question_id="probe",
    )
    if judgment is None:
        raise RuntimeError(f"Choice probe failed: {reason or 'unknown failure'}")
    return {
        "models_listed": True,
        "configured_model_available": True,
        "choice": judgment.choice,
        "confidence": judgment.confidence,
        "request_id_present": bool(judgment.request_id),
        "actual_model": judgment.actual_model,
        "latency_ms": judgment.latency_ms,
        "token_usage": dict(judgment.token_usage),
    }


def evaluate_lesson_live(path: Path) -> dict[str, Any]:
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            if row.get("label_status") == "confirmed":
                records.append(row)
    if not records:
        return {"cases": 0, "reason": "no confirmed lesson labels"}

    top1 = 0
    top3 = 0
    new_correct = 0
    usable = 0
    failures = 0
    for row in records:
        judgment = decisions.route_lesson_topic(
            str(row.get("review_sections") or ""),
            row.get("topic_catalog"),
            str(row.get("home") or ""),
            str(row.get("away") or ""),
            str(row.get("league") or ""),
        )
        if judgment is None:
            failures += 1
            continue
        usable += 1
        expected_new = bool(row["expected_new_topic"])
        if expected_new:
            if judgment.need_new_topic:
                new_correct += 1
                top1 += 1
                top3 += 1
            continue
        expected = str(row.get("expected_recommended") or "")
        acceptable = set(row.get("accepted_slugs") or [expected])
        if judgment.recommended == expected:
            top1 += 1
        if expected in judgment.candidates or acceptable.intersection(judgment.candidates):
            top3 += 1
    return {
        "cases": len(records),
        "usable": usable,
        "failures": failures,
        "top1_accuracy": top1 / len(records),
        "top3_recall": top3 / len(records),
        "new_topic_correct": new_correct,
    }


def evaluate_decision_live(root: Path) -> dict[str, Any]:
    reports = _report_paths(root)
    stats = {"reports_scanned": len(reports), "ambiguous_cases": 0,
             "jev_resolved": 0, "fallbacks": 0, "high_confidence": 0}
    threshold = config.TYPESAFE_DECISION_MIN_CONFIDENCE
    for path in reports:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        parsed = decisions.parse_decision_section(text)
        if parsed is None or parsed.source != "ambiguous":
            continue
        stats["ambiguous_cases"] += 1
        result = decisions.resolve_decision(text)
        if result is None or result.source in {"invalid", "jev_none"}:
            stats["fallbacks"] += 1
            continue
        if decisions.decision_to_extract_dict(result) is None:
            stats["fallbacks"] += 1
            continue
        stats["jev_resolved"] += 1
        if threshold is not None and result.confidence is not None and result.confidence >= threshold:
            stats["high_confidence"] += 1
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lesson-cases", type=Path)
    parser.add_argument("--decision-cases", type=Path)
    parser.add_argument("--prepare-lesson-cases", type=Path)
    parser.add_argument(
        "--output", type=Path,
        default=ROOT / "tests" / "fixtures" / "typesafe" / "lesson_routes.jsonl",
    )
    parser.add_argument("--reports", type=Path)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--lesson-live", action="store_true")
    parser.add_argument("--decision-live", action="store_true")
    args = parser.parse_args(argv)

    live_flags = [args.probe, args.lesson_live, args.decision_live]
    if args.offline and any(live_flags):
        parser.error("--offline cannot be combined with a live request")
    if sum(bool(flag) for flag in live_flags) > 1:
        parser.error("choose only one live operation per invocation")

    if args.prepare_lesson_cases:
        count = prepare_lesson_cases(args.prepare_lesson_cases, args.output)
        _print({"lesson_cases_prepared": count, "output": str(args.output)})
    if args.decision_cases:
        _print({"decision_case_inventory": inspect_decision_cases(args.decision_cases)})
    if args.lesson_cases:
        _print({"lesson_case_inventory": inspect_lesson_cases(args.lesson_cases)})
    if args.decision_live:
        report_root = args.reports or (ROOT / "report")
        try:
            _print({"decision_live": evaluate_decision_live(report_root)})
        except Exception as exc:
            print(f"decision live evaluation failed: {type(exc).__name__}", file=sys.stderr)
            return 2
    elif args.reports:
        _print({"decision_offline": evaluate_reports(args.reports)})
    if args.probe:
        try:
            _print({"probe": live_probe()})
        except Exception as exc:
            print(f"TypeSafe probe failed: {type(exc).__name__}", file=sys.stderr)
            return 2
    if args.lesson_live:
        if not args.lesson_cases:
            parser.error("--lesson-live requires --lesson-cases")
        try:
            _print({"lesson_live": evaluate_lesson_live(args.lesson_cases)})
        except Exception as exc:
            print(f"lesson live evaluation failed: {type(exc).__name__}", file=sys.stderr)
            return 2
    if not args.lesson_cases and not args.decision_cases and not args.prepare_lesson_cases and not args.reports and not any(live_flags):
        parser.print_help()
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
