"""Versioned, candidate-bound verifier verdict parsing and reporting."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

VERDICT_SCHEMA = 1
VERDICT_PREFIX = "ORTUS_VERDICT:"
MAX_REPORT_CHARS = 24_000
# Space held back from the report body for the normalized CodeGraph engagement
# block grind appends afterwards, so the two never crowd each other out.
ENGAGEMENT_RESERVE = 4_000
REPORT_BUDGET = MAX_REPORT_CHARS - ENGAGEMENT_RESERVE
_MIN_ENTRY_CHARS = 120
_ENTRY_ELLIPSIS = " …[entry truncated]"
_TRUNCATION_MARKER = (
    "\n[report truncated; complete verdict retained in transaction artifacts]\n"
)
_SECRET = re.compile(
    r"(?i)(api[_-]?key|authorization|token|secret|password)(\s*[:=]\s*)([^\r\n]+)"
)


class VerdictError(ValueError):
    """Verifier output did not contain one valid, current verdict."""


@dataclass(frozen=True)
class Verdict:
    candidate_hash: str
    decision: str
    criteria: tuple[dict[str, str], ...]
    commands: tuple[str, ...]
    reviewed_files: tuple[str, ...]
    reviewed_interfaces: tuple[str, ...]
    risks: tuple[str, ...]
    findings: tuple[str, ...]
    codegraph: tuple[str, ...]
    schema: int = VERDICT_SCHEMA
    # Criterion ids that did not line up with the authoritative packet. Only a
    # fail verdict can carry these — a pass verdict with any of them is fatal —
    # and they are appended last so existing positional construction is safe.
    missing_criteria: tuple[str, ...] = ()
    unexpected_criteria: tuple[str, ...] = ()
    duplicated_criteria: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.decision == "pass"


def _strings(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise VerdictError(f"{field} must be an array of non-empty strings")
    return tuple(item.strip() for item in value)


def _criteria_discrepancy(
    actual_ids: tuple[str, ...], expected_ids: tuple[str, ...]
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Missing, unexpected, and duplicated ids, each in first-seen order."""

    actual = set(actual_ids)
    expected = set(expected_ids)
    missing = tuple(item for item in dict.fromkeys(expected_ids) if item not in actual)
    unexpected = tuple(
        item for item in dict.fromkeys(actual_ids) if item not in expected
    )
    duplicated = tuple(
        item for item in dict.fromkeys(actual_ids) if actual_ids.count(item) > 1
    )
    return missing, unexpected, duplicated


def _named_ids(values: tuple[str, ...]) -> str:
    return ", ".join(values) if values else "none"


def _collapse_duplicates(criteria: list[dict[str, str]]) -> list[dict[str, str]]:
    """Keep one row per criterion id, preferring the failing report.

    A recorded fail verdict renders one matrix row per criterion, so a repeated
    id must not be counted twice; keeping the failing row means the matrix still
    explains the decision it belongs to.
    """

    kept: dict[str, dict[str, str]] = {}
    for item in criteria:
        current = kept.get(item["id"])
        if current is None or (current["status"] == "pass" and item["status"] == "fail"):
            kept[item["id"]] = item
    return list(kept.values())


def validate_verdict(
    payload: Any,
    expected_hash: str,
    *,
    expected_criteria: Iterable[str] = (),
) -> Verdict:
    if not isinstance(payload, dict):
        raise VerdictError("verdict must be a JSON object")
    required = {
        "schema",
        "candidate_hash",
        "decision",
        "criteria",
        "commands",
        "reviewed_files",
        "reviewed_interfaces",
        "risks",
        "findings",
        "codegraph",
    }
    if set(payload) != required:
        raise VerdictError("verdict fields do not match schema v1")
    if payload["schema"] != VERDICT_SCHEMA:
        raise VerdictError("unsupported verdict schema")
    if payload["candidate_hash"] != expected_hash:
        raise VerdictError("verdict candidate hash is stale")
    decision = payload["decision"]
    if decision not in {"pass", "fail"}:
        raise VerdictError("decision must be pass or fail")
    raw_criteria = payload["criteria"]
    if not isinstance(raw_criteria, list) or not raw_criteria:
        raise VerdictError("criteria must be a non-empty array")
    criteria: list[dict[str, str]] = []
    for item in raw_criteria:
        if not isinstance(item, dict) or set(item) != {"id", "status", "evidence"}:
            raise VerdictError("each criterion needs id, status, and evidence")
        if item["status"] not in {"pass", "fail"} or not all(
            isinstance(item[key], str) and item[key].strip()
            for key in ("id", "evidence")
        ):
            raise VerdictError("criterion values are malformed")
        criteria.append({key: item[key].strip() for key in item})
    expected_ids = tuple(expected_criteria)
    actual_ids = tuple(item["id"] for item in criteria)
    missing: tuple[str, ...] = ()
    unexpected: tuple[str, ...] = ()
    duplicated: tuple[str, ...] = ()
    if expected_ids:
        missing, unexpected, duplicated = _criteria_discrepancy(
            actual_ids, expected_ids
        )
        # Asymmetric on purpose. Accepting a pass whose ids were never mapped to
        # the packet would close an issue and commit code against criteria
        # nobody authorized, so that stays fatal. A fail commits nothing and
        # leaves the issue open either way, so it is recorded with the
        # discrepancy attached instead of aborting the run over a schema detail
        # the correction loop could otherwise act on.
        if (missing or unexpected or duplicated) and decision == "pass":
            raise VerdictError(
                "verdict criteria do not match the authoritative issue packet; "
                f"missing: {_named_ids(missing)}; "
                f"unexpected: {_named_ids(unexpected)}; "
                f"duplicated: {_named_ids(duplicated)}"
            )
        if duplicated:
            criteria = _collapse_duplicates(criteria)
    failed = any(item["status"] == "fail" for item in criteria)
    if (decision == "pass" and failed) or (decision == "fail" and not failed):
        raise VerdictError("verdict decision contradicts criterion statuses")
    return Verdict(
        candidate_hash=expected_hash,
        decision=decision,
        criteria=tuple(criteria),
        commands=_strings(payload["commands"], "commands"),
        reviewed_files=_strings(payload["reviewed_files"], "reviewed_files"),
        reviewed_interfaces=_strings(
            payload["reviewed_interfaces"], "reviewed_interfaces"
        ),
        risks=_strings(payload["risks"], "risks"),
        findings=_strings(payload["findings"], "findings"),
        codegraph=_strings(payload["codegraph"], "codegraph"),
        missing_criteria=missing,
        unexpected_criteria=unexpected,
        duplicated_criteria=duplicated,
    )


def _assistant_text(event: dict[str, Any]) -> Iterable[str]:
    if event.get("type") == "assistant":
        content = event.get("message", {}).get("content", [])
        for part in content if isinstance(content, list) else [content]:
            if isinstance(part, dict) and part.get("type") == "text":
                yield str(part.get("text", ""))
            elif isinstance(part, str):
                yield part
    if event.get("type") == "item.completed":
        item = event.get("item", {})
        if isinstance(item, dict) and item.get("type") == "agent_message":
            yield str(item.get("text", ""))


def parse_verdict(
    log_path: Path,
    *,
    start_offset: int,
    expected_hash: str,
    expected_criteria: Iterable[str] = (),
) -> Verdict:
    envelopes: list[Any] = []
    with log_path.open("rb") as fh:
        fh.seek(start_offset)
        for raw in fh:
            try:
                event = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(event, dict):
                continue
            for text in _assistant_text(event):
                for line in text.splitlines():
                    if line.strip().startswith(VERDICT_PREFIX):
                        encoded = line.strip()[len(VERDICT_PREFIX) :].strip()
                        try:
                            envelopes.append(json.loads(encoded))
                        except json.JSONDecodeError as exc:
                            raise VerdictError("malformed verdict JSON") from exc
    if len(envelopes) != 1:
        raise VerdictError(
            f"expected exactly one verdict envelope; found {len(envelopes)}"
        )
    return validate_verdict(
        envelopes[0], expected_hash, expected_criteria=expected_criteria
    )


def _clean(value: str) -> str:
    return _SECRET.sub(r"\1\2[REDACTED]", value)


def _entry(value: str, budget: int) -> str:
    """Render one bulleted entry, clipped to `budget` characters of content."""

    if len(value) <= budget:
        return f"- {value}"
    keep = max(0, budget - len(_ENTRY_ELLIPSIS))
    return "- " + value[:keep] + _ENTRY_ELLIPSIS


def _section(title: str, entries: Iterable[str], budget: int) -> list[str]:
    """Header plus as many bounded entries as `budget` allows.

    Truncation is per section rather than over the assembled string: a single
    oversized command must never be able to push the Findings or CodeGraph
    headings off the end of a report that AC-6 requires to be complete.
    """

    values = tuple(entries)
    lines = ["", f"### {title}"]
    if not values:
        return lines
    per_entry = max(_MIN_ENTRY_CHARS, budget // len(values))
    used = 0
    shown = 0
    for value in values:
        rendered = _entry(value, per_entry)
        if shown and used + len(rendered) > budget:
            break
        lines.append(rendered)
        used += len(rendered) + 1
        shown += 1
    dropped = len(values) - shown
    if dropped:
        lines.append(f"- [{dropped} more entries truncated; see transaction artifacts]")
    return lines


def _mismatch_entries(verdict: Verdict) -> tuple[str, ...]:
    """Keep an accepted id discrepancy visible to the operator and correction pass."""

    labelled = (
        ("missing from the verdict", verdict.missing_criteria),
        ("not in the issue packet", verdict.unexpected_criteria),
        ("reported more than once", verdict.duplicated_criteria),
    )
    return tuple(
        f"{label}: {', '.join(values)}" for label, values in labelled if values
    )


def render_report(
    verdict: Verdict,
    *,
    issue_id: str,
    base_head: str = "",
    issue_packet_hash: str = "",
    attempt: int | None = None,
    profiles: dict[str, str] | None = None,
) -> str:
    clean = _clean

    lines = [
        f"## Ortus verifier report (schema v{verdict.schema})",
        "",
        f"Issue: {issue_id}",
        f"Candidate: `{verdict.candidate_hash}`",
        f"Decision: **{verdict.decision.upper()}**",
    ]
    if base_head:
        lines.append(f"Base commit: `{base_head}`")
    if issue_packet_hash:
        lines.append(f"Issue packet: `{issue_packet_hash}`")
    if attempt is not None:
        lines.append(f"Verifier attempt: {attempt}")
    for phase, profile in sorted((profiles or {}).items()):
        lines.append(f"{phase.title()} profile: {clean(profile)}")
    sections = (
        ("Commands", verdict.commands),
        ("Files reviewed", verdict.reviewed_files),
        ("Interfaces reviewed", verdict.reviewed_interfaces),
        ("Risks", verdict.risks),
        ("Findings", verdict.findings),
        ("CodeGraph evidence", verdict.codegraph),
    )
    mismatch = _mismatch_entries(verdict)
    if mismatch:
        sections = (("Criterion id mismatch", mismatch),) + sections
    # The criterion matrix is the audit spine, so it gets the larger share and
    # the remaining evidence sections split what is left evenly.
    body = max(0, REPORT_BUDGET - len("\n".join(lines)))
    lines.extend(
        _section(
            "Acceptance criteria",
            (
                f"{clean(item['id'])}: {item['status']} — {clean(item['evidence'])}"
                for item in verdict.criteria
            ),
            body * 2 // 5,
        )
    )
    per_section = (body - body * 2 // 5) // len(sections)
    for title, values in sections:
        lines.extend(_section(title, (clean(value) for value in values), per_section))
    return bound_report("\n".join(lines) + "\n")


def render_rejection_report(
    *,
    issue_id: str,
    candidate_hash: str,
    failure: str,
    expected_criteria: Iterable[str] = (),
    base_head: str = "",
    issue_packet_hash: str = "",
    attempt: int | None = None,
    profiles: dict[str, str] | None = None,
) -> str:
    """Render a complete report even when no trustworthy envelope exists."""

    clean = _clean
    reason = clean(failure)
    lines = [
        f"## Ortus verifier report (schema v{VERDICT_SCHEMA})",
        "",
        f"Issue: {issue_id}",
        f"Candidate: `{candidate_hash}`",
        "Decision: **REJECTED**",
    ]
    if base_head:
        lines.append(f"Base commit: `{base_head}`")
    if issue_packet_hash:
        lines.append(f"Issue packet: `{issue_packet_hash}`")
    if attempt is not None:
        lines.append(f"Verifier attempt: {attempt}")
    for phase, profile in sorted((profiles or {}).items()):
        lines.append(f"{phase.title()} profile: {clean(profile)}")
    criteria = tuple(
        f"{clean(criterion)}: not assessed — verdict rejected"
        for criterion in expected_criteria
    ) or ("unavailable — authoritative criteria were not extractable",)
    unavailable = "unavailable — verifier did not produce a valid envelope"
    body = max(0, REPORT_BUDGET - len("\n".join(lines)))
    lines.extend(_section("Acceptance criteria", criteria, body * 2 // 5))
    remaining = body - body * 2 // 5
    for title in ("Commands", "Files reviewed", "Interfaces reviewed", "Risks"):
        lines.extend(_section(title, (unavailable,), remaining // 6))
    lines.extend(_section("Findings", (reason,), remaining // 6))
    lines.extend(
        _section(
            "CodeGraph evidence",
            ("see the normalized CodeGraph engagement block below",),
            remaining // 6,
        )
    )
    return bound_report("\n".join(lines) + "\n")


def bound_report(report: str) -> str:
    """Keep persisted comments bounded after all report blocks are composed.

    Both ends are preserved: the verdict body leads and the CodeGraph
    engagement block trails, so a front-only clip would silently drop the
    engagement evidence AC-6 requires.
    """

    if len(report) <= MAX_REPORT_CHARS:
        return report
    head = MAX_REPORT_CHARS - ENGAGEMENT_RESERVE
    tail = ENGAGEMENT_RESERVE - len(_TRUNCATION_MARKER)
    return report[:head] + _TRUNCATION_MARKER + report[-tail:]
