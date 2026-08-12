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
    with pytest.raises(VerdictError, match="authoritative work spec"):
        validate_verdict(
            valid_payload(criteria=criteria),
            HASH,
            expected_criteria=("AC-1", "AC-2"),
        )


@pytest.mark.parametrize(
    "criteria",
    [
        # missing AC-2
        [{"id": "AC-1", "status": "pass", "evidence": "covered"}],
        # invented id alongside the real ones
        [
            {"id": "AC-1", "status": "pass", "evidence": "covered"},
            {"id": "AC-2", "status": "pass", "evidence": "covered"},
            {"id": "AC-TESTS", "status": "pass", "evidence": "invented"},
        ],
        # duplicate
        [
            {"id": "AC-1", "status": "pass", "evidence": "covered"},
            {"id": "AC-1", "status": "pass", "evidence": "again"},
            {"id": "AC-2", "status": "pass", "evidence": "covered"},
        ],
    ],
)
def test_pass_verdict_mismatch_is_fatal(criteria: list[dict[str, str]]) -> None:
    """AC-4: accepting a pass would close an issue against unmapped criteria."""
    with pytest.raises(VerdictError, match="authoritative work spec"):
        validate_verdict(
            valid_payload(criteria=criteria),
            HASH,
            expected_criteria=("AC-1", "AC-2"),
        )


def test_criterion_extra_key_is_named_and_no_key_is_claimed_missing() -> None:
    """AC-1: the observed failure — all three keys present, one key too many.

    The old wording said the criterion needed the fields it already carried, so
    a correction attempt re-emitted the identical shape three times over. The
    message is asserted verbatim-ish because an unactionable diagnosis is
    exactly the defect, and only reading the text can pin it.
    """

    with pytest.raises(VerdictError) as excinfo:
        validate_verdict(
            valid_payload(
                criteria=[
                    {
                        "id": "AC-1",
                        "status": "pass",
                        "evidence": "covered",
                        "verdict": "pass",
                    }
                ]
            ),
            HASH,
        )
    message = str(excinfo.value)
    assert 'unexpected keys: "verdict"' in message
    assert "missing keys: none" in message
    assert "\n" not in message


def test_criterion_missing_key_is_named() -> None:
    """AC-2: a genuinely absent key is still rejected, and named."""

    with pytest.raises(VerdictError) as excinfo:
        validate_verdict(
            valid_payload(criteria=[{"id": "AC-1", "status": "pass"}]), HASH
        )
    message = str(excinfo.value)
    assert 'missing keys: "evidence"' in message
    assert "unexpected keys: none" in message


def test_criterion_missing_and_extra_keys_are_reported_separately() -> None:
    """Opposite corrections, so conflating them is what made the message useless."""

    with pytest.raises(VerdictError) as excinfo:
        validate_verdict(
            valid_payload(
                criteria=[{"id": "AC-1", "status": "pass", "verdict": "pass"}]
            ),
            HASH,
        )
    message = str(excinfo.value)
    assert 'missing keys: "evidence"' in message
    assert 'unexpected keys: "verdict"' in message


def test_criterion_fault_is_located_by_position_and_id() -> None:
    """AC-3: with eight criteria the author has to know which one to fix."""

    criteria = [
        {"id": f"AC-{index}", "status": "pass", "evidence": "covered"}
        for index in range(1, 9)
    ]
    criteria[5]["verdict"] = "pass"
    with pytest.raises(VerdictError) as excinfo:
        validate_verdict(valid_payload(criteria=criteria), HASH)

    assert "criterion 6 (id AC-6)" in str(excinfo.value)


def test_criterion_fault_is_located_by_position_without_an_id() -> None:
    """An unidentifiable criterion is still findable by where it sits."""

    with pytest.raises(VerdictError) as excinfo:
        validate_verdict(
            valid_payload(
                criteria=[
                    {"id": "AC-1", "status": "pass", "evidence": "covered"},
                    {"status": "pass", "evidence": "covered"},
                ]
            ),
            HASH,
        )
    message = str(excinfo.value)
    assert message.startswith("criterion 2:")
    assert 'missing keys: "id"' in message


def test_criterion_bad_status_is_named_with_its_value() -> None:
    """AC-4: `passed` for `pass` must say so rather than assert malformedness."""

    with pytest.raises(VerdictError) as excinfo:
        validate_verdict(
            valid_payload(
                criteria=[{"id": "AC-1", "status": "passed", "evidence": "covered"}]
            ),
            HASH,
        )
    message = str(excinfo.value)
    assert "criterion 1 (id AC-1)" in message
    assert "status must be pass or fail" in message
    assert '"passed"' in message


def test_criterion_bad_status_is_named_when_it_is_not_even_a_string() -> None:
    """An unhashable status must reject the verdict, not crash the run."""

    with pytest.raises(VerdictError) as excinfo:
        validate_verdict(
            valid_payload(
                criteria=[{"id": "AC-1", "status": ["pass"], "evidence": "covered"}]
            ),
            HASH,
        )
    message = str(excinfo.value)
    assert "status must be pass or fail" in message
    assert '["pass"]' in message


@pytest.mark.parametrize("field", ["id", "evidence"])
def test_criterion_bad_value_names_the_field_and_value(field: str) -> None:
    criterion = {"id": "AC-1", "status": "pass", "evidence": "covered"}
    criterion[field] = "   "
    with pytest.raises(VerdictError) as excinfo:
        validate_verdict(valid_payload(criteria=[criterion]), HASH)
    message = str(excinfo.value)
    assert f"{field} must be a non-empty string" in message
    assert '"   "' in message


def test_non_object_criterion_is_reported_as_such() -> None:
    """Not a key problem — say what it actually is."""

    with pytest.raises(VerdictError) as excinfo:
        validate_verdict(valid_payload(criteria=["AC-1 passed"]), HASH)

    assert "criterion 1 must be a JSON object, got str" in str(excinfo.value)


def test_key_differing_by_case_or_whitespace_is_reported_as_unexpected() -> None:
    """Quoting is what makes ` id` legible as different from `id`."""

    with pytest.raises(VerdictError) as excinfo:
        validate_verdict(
            valid_payload(
                criteria=[{" id": "AC-1", "Status": "pass", "evidence": "covered"}]
            ),
            HASH,
        )
    message = str(excinfo.value)
    assert '" id"' in message
    assert '"Status"' in message
    assert '"id", "status"' in message


def test_criterion_diagnostic_stays_bounded_and_single_line() -> None:
    """A pathological payload must not emit a message nothing can carry."""

    criterion = {"id": "AC-1", "status": "pass", "evidence": "covered"}
    criterion.update({f"stray-{index}": "x" for index in range(200)})
    criterion["huge\nkey"] = "x"
    with pytest.raises(VerdictError) as excinfo:
        validate_verdict(valid_payload(criteria=[criterion]), HASH)
    message = str(excinfo.value)
    assert "\n" not in message
    assert len(message) < 400
    assert "and 195 more" in message


def test_rejection_report_carries_the_criterion_diagnostic_verbatim() -> None:
    """Step 5: a precise message that never reaches the author fixes nothing."""

    with pytest.raises(VerdictError) as excinfo:
        validate_verdict(
            valid_payload(
                criteria=[
                    {
                        "id": "AC-1",
                        "status": "pass",
                        "evidence": "covered",
                        "verdict": "pass",
                    }
                ]
            ),
            HASH,
        )
    failure = str(excinfo.value)
    report = render_rejection_report(
        issue_id="repo-1",
        candidate_hash=HASH,
        failure=failure,
        expected_criteria=("AC-1",),
    )

    assert failure in report
    assert len(report) <= MAX_REPORT_CHARS


def test_mismatch_names_ids() -> None:
    """AC-2: the operator must not have to diff the sets by hand."""
    with pytest.raises(VerdictError) as excinfo:
        validate_verdict(
            valid_payload(
                criteria=[
                    {"id": "AC-1", "status": "pass", "evidence": "covered"},
                    {"id": "AC-1", "status": "pass", "evidence": "again"},
                    {"id": "AC-TESTS", "status": "pass", "evidence": "invented"},
                ]
            ),
            HASH,
            expected_criteria=("AC-1", "AC-2"),
        )
    message = str(excinfo.value)
    assert "missing: AC-2" in message
    assert "unexpected: AC-TESTS" in message
    assert "duplicated: AC-1" in message


def test_fail_verdict_unknown_id_degrades() -> None:
    """AC-3: a fail commits nothing, so record it instead of aborting the run."""
    verdict = validate_verdict(
        valid_payload(
            decision="fail",
            criteria=[
                {"id": "AC-1", "status": "pass", "evidence": "covered"},
                {
                    "id": "AC-TESTS",
                    "status": "fail",
                    "evidence": "targeted tests never ran",
                },
            ],
        ),
        HASH,
        expected_criteria=("AC-1", "AC-2"),
    )

    assert not verdict.passed
    assert verdict.missing_criteria == ("AC-2",)
    assert verdict.unexpected_criteria == ("AC-TESTS",)
    assert verdict.duplicated_criteria == ()

    report = render_report(verdict, issue_id="repo-1")
    assert "Decision: **FAIL**" in report
    assert "### Criterion id mismatch" in report
    assert "missing from the verdict: AC-2" in report
    assert "not in the work spec: AC-TESTS" in report
    assert len(report) <= MAX_REPORT_CHARS


def test_fail_verdict_duplicate_id_is_not_double_counted() -> None:
    verdict = validate_verdict(
        valid_payload(
            decision="fail",
            criteria=[
                {"id": "AC-1", "status": "pass", "evidence": "first"},
                {"id": "AC-1", "status": "fail", "evidence": "second"},
                {"id": "AC-2", "status": "pass", "evidence": "covered"},
            ],
        ),
        HASH,
        expected_criteria=("AC-1", "AC-2"),
    )

    assert verdict.duplicated_criteria == ("AC-1",)
    assert tuple(item["id"] for item in verdict.criteria) == ("AC-1", "AC-2")

    report = render_report(verdict, issue_id="repo-1")
    assert report.count("- AC-1:") == 1
    assert "- AC-1: fail — second" in report
    assert "reported more than once: AC-1" in report


def test_regression_extra_id_on_fail(tmp_path: Path) -> None:
    """Observed 2026-08-07: an EROFS sandbox forced an extra AC-TESTS row.

    AC-5: that fail verdict must reach the correction loop as a recorded
    failure rather than killing the run with a schema error.
    """
    packet_ids = tuple(f"AC-{n}" for n in range(1, 7))
    criteria = [
        {
            "id": "AC-TESTS",
            "status": "fail",
            "evidence": "every Bash call failed with EROFS; targeted tests never ran",
        }
    ] + [
        {"id": cid, "status": "pass", "evidence": "read-only review only"}
        for cid in packet_ids
    ]
    log = tmp_path / "log.jsonl"
    log.write_text(_event(valid_payload(decision="fail", criteria=criteria)) + "\n")

    verdict = parse_verdict(
        log, start_offset=0, expected_hash=HASH, expected_criteria=packet_ids
    )

    assert verdict.decision == "fail"
    assert not verdict.passed
    assert verdict.unexpected_criteria == ("AC-TESTS",)
    assert verdict.missing_criteria == ()
    assert "AC-TESTS" in render_report(verdict, issue_id="demo-1a2b")


def test_empty_expected_criteria_still_bypasses_the_id_check() -> None:
    """A packet with no AC-N ids leaves nothing to compare against."""
    verdict = validate_verdict(
        valid_payload(
            criteria=[
                {"id": "one", "status": "pass", "evidence": "covered"},
                {"id": "one", "status": "pass", "evidence": "again"},
            ]
        ),
        HASH,
        expected_criteria=(),
    )
    assert verdict.passed
    assert len(verdict.criteria) == 2
    assert "### Criterion id mismatch" not in render_report(verdict, issue_id="repo-1")


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
