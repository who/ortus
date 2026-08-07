from __future__ import annotations

import json
from pathlib import Path

import pytest

from ortus.core.verdict import (
    MAX_REPORT_CHARS,
    VERDICT_PREFIX,
    VerdictError,
    bound_report,
    parse_verdict,
    render_rejection_report,
    render_report,
    validate_verdict,
)


HASH = "a" * 64


def valid_payload(**updates: object) -> dict:
    payload = {
        "schema": 1,
        "candidate_hash": HASH,
        "decision": "pass",
        "criteria": [{"id": "AC-1", "status": "pass", "evidence": "covered"}],
        "commands": ["uv run pytest tests/test_verdict.py -q"],
        "reviewed_files": ["src/ortus/core/verdict.py"],
        "reviewed_interfaces": ["parse_verdict"],
        "risks": ["transcript duplication"],
        "findings": ["none"],
        "codegraph": ["impact query reviewed callers"],
    }
    payload.update(updates)
    return payload


def _event(payload: object) -> str:
    text = VERDICT_PREFIX + " " + json.dumps(payload)
    return json.dumps(
        {"type": "item.completed", "item": {"type": "agent_message", "text": text}}
    )


def test_valid_verdict_is_bound_to_candidate_hash_and_renders_report(
    tmp_path: Path,
) -> None:
    log = tmp_path / "log.jsonl"
    log.write_text(_event(valid_payload()) + "\n")

    verdict = parse_verdict(log, start_offset=0, expected_hash=HASH)
    report = render_report(
        verdict,
        issue_id="repo-1",
        base_head="abc123",
        issue_packet_hash="b" * 64,
        attempt=2,
        profiles={"implementation": "fast", "verification": "slow"},
    )

    assert verdict.passed
    assert HASH in report
    assert "AC-1: pass" in report
    assert "CodeGraph evidence" in report
    assert "Base commit: `abc123`" in report
    assert "Verifier attempt: 2" in report
    assert "Verification profile: slow" in report


@pytest.mark.parametrize(
    "payload,match",
    [
        (valid_payload(schema=2), "unsupported"),
        (valid_payload(candidate_hash="b" * 64), "stale"),
        (
            valid_payload(
                decision="pass",
                criteria=[{"id": "AC-1", "status": "fail", "evidence": "bad"}],
            ),
            "contradicts",
        ),
        (valid_payload(decision="fail"), "contradicts"),
        (valid_payload(commands=[]), "commands"),
        ({"schema": 1}, "fields"),
    ],
)
def test_invalid_verdicts_are_rejected(payload: dict, match: str) -> None:
    with pytest.raises(VerdictError, match=match):
        validate_verdict(payload, HASH)


@pytest.mark.parametrize(
    "criteria",
    [
        [{"id": "AC-1", "status": "pass", "evidence": "covered"}],
        [
            {"id": "AC-1", "status": "pass", "evidence": "covered"},
            {"id": "AC-1", "status": "pass", "evidence": "duplicate"},
        ],
    ],
)
def test_verdict_must_cover_each_authoritative_criterion_once(
    criteria: list[dict[str, str]],
) -> None:
    with pytest.raises(VerdictError, match="authoritative issue packet"):
        validate_verdict(
            valid_payload(criteria=criteria),
            HASH,
            expected_criteria=("AC-1", "AC-2"),
        )


def test_invalid_missing_multiple_and_malformed_envelopes(tmp_path: Path) -> None:
    log = tmp_path / "log.jsonl"
    log.write_text(json.dumps({"type": "assistant", "message": {"content": []}}) + "\n")
    with pytest.raises(VerdictError, match="found 0"):
        parse_verdict(log, start_offset=0, expected_hash=HASH)

    log.write_text(_event(valid_payload()) + "\n" + _event(valid_payload()) + "\n")
    with pytest.raises(VerdictError, match="found 2"):
        parse_verdict(log, start_offset=0, expected_hash=HASH)

    malformed = json.dumps(
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": VERDICT_PREFIX + " {"}]},
        }
    )
    log.write_text(malformed + "\n")
    with pytest.raises(VerdictError, match="malformed"):
        parse_verdict(log, start_offset=0, expected_hash=HASH)


def test_report_redacts_sensitive_values() -> None:
    verdict = validate_verdict(
        valid_payload(
            findings=["token=do-not-leak"],
            risks=["authorization: Bearer do-not-leak-either"],
        ),
        HASH,
    )
    report = render_report(verdict, issue_id="repo-1")
    assert "do-not-leak" not in report
    assert "bearer-value" not in report
    assert "do-not-leak-either" not in report
    assert "[REDACTED]" in report


def test_oversized_valid_verdict_keeps_every_mandatory_section() -> None:
    """AC-6: one huge field must not push later headings out of the report."""
    verdict = validate_verdict(
        valid_payload(
            commands=["pytest " + "x" * (MAX_REPORT_CHARS * 2)],
            reviewed_files=[f"src/file{i}.py" for i in range(400)],
            findings=["y" * (MAX_REPORT_CHARS * 2)],
        ),
        HASH,
    )
    report = render_report(verdict, issue_id="repo-1", attempt=3)
    report = bound_report(report + "\n" + "codegraph engagement " * MAX_REPORT_CHARS)

    for heading in (
        "### Acceptance criteria",
        "### Commands",
        "### Files reviewed",
        "### Interfaces reviewed",
        "### Risks",
        "### Findings",
        "### CodeGraph evidence",
    ):
        assert heading in report
    assert "- AC-1: pass — covered" in report
    assert "impact query reviewed callers" in report
    assert len(report) <= MAX_REPORT_CHARS


def test_rejection_report_has_complete_sections_and_is_bounded() -> None:
    report = render_rejection_report(
        issue_id="repo-1",
        candidate_hash=HASH,
        failure="token=do-not-leak " + "x" * (MAX_REPORT_CHARS * 2),
        expected_criteria=("AC-1", "AC-2"),
        attempt=2,
    )
    report = bound_report(report + "\n" + "codegraph " * MAX_REPORT_CHARS)

    assert "do-not-leak" not in report
    assert "AC-1: not assessed" in report
    assert "### Commands" in report
    assert "### Files reviewed" in report
    assert "### Interfaces reviewed" in report
    assert "### Risks" in report
    assert "### Findings" in report
    assert "### CodeGraph evidence" in report
    assert "[report truncated" in report
    assert len(report) < MAX_REPORT_CHARS + 100
