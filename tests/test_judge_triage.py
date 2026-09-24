"""Triage of a parked bead into a route (ortus-n9lk).

Three unlike things reach the human queue by one door: a version the
installed toolchain cannot honour, a work spec nobody wrote properly, and a
decision only an operator can make. The routing here is System Two over a
probability vector, so the cases below drive the pure argmax on its own,
then the two automatic routes against a tracker double, then the paths that
must leave today's park exactly as it was.

Nothing here reaches a provider. The judge is injected, the planner turn is a
callable that records its id, and the readiness validator — the only thing
allowed to put a bead back in the queue — runs for real against the packet
the fake turn leaves behind.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from ortus.commands.grind import _flag_unready_for_human
from ortus.core.judge import JudgeConfig, JudgeMode
from ortus.core.judge_replay import validate_event
from ortus.core.judge_triage import (
    HUMAN_LABEL,
    RESPEC_MARKER,
    TriageClass,
    TriageVector,
    route_triage,
    triage_parked,
    triage_parked_bead,
)
from ortus.core.pin_skew import PIN_SKEW_LABEL
from ortus.core.readiness import validate_issue
from tests.test_readiness import ready_issue

# The transcript shape the conservative regex pass recognises: a declared
# compatibility date above the runtime the installed test runner bundles.
_SKEW = (
    "PLAN-GAP: `wrangler.toml` declares compatibility_date 2026-09-01, but the "
    "installed @cloudflare/vitest-pool-workers 0.22.0 bundles a runtime whose "
    "maximum compatibility date is 2026-08-22, so the suite refuses to start."
)

# A park no amount of re-specification answers.
_TRUE_GAP = (
    "PLAN-GAP: the deploy needs a production API token nobody has supplied, "
    "and whether this ships behind a flag is a decision nobody has made."
)


class _FakeBd:
    """Tracker double holding labels, comments and the packet per issue."""

    def __init__(self, issues: dict[str, dict[str, Any]]) -> None:
        self.issues = issues
        self.comments_by_id: dict[str, list[str]] = {
            issue_id: [] for issue_id in issues
        }

    def show(self, issue_id: str) -> dict[str, Any]:
        return {**self.issues[issue_id], "labels": list(self.issues[issue_id]["labels"])}

    def comments(self, issue_id: str) -> list[dict[str, Any]]:
        return [{"text": text} for text in self.comments_by_id[issue_id]]

    def add_label(self, issue_id: str, label: str) -> None:
        labels = self.issues[issue_id]["labels"]
        if label not in labels:
            labels.append(label)

    def remove_label(self, issue_id: str, label: str) -> None:
        labels = self.issues[issue_id]["labels"]
        if label in labels:
            labels.remove(label)

    def add_comment(self, issue_id: str, body: str) -> None:
        self.comments_by_id[issue_id].append(body)

    def labels(self, issue_id: str) -> list[str]:
        return list(self.issues[issue_id]["labels"])


def _parked(issue_id: str = "demo-1", **fields: Any) -> dict[str, Any]:
    packet = {
        "id": issue_id,
        "issue_type": "task",
        "status": "open",
        "labels": [HUMAN_LABEL],
    }
    packet.update(fields)
    return packet


def _vector(**weights: float) -> TriageVector:
    return TriageVector(
        p_pin_skew=weights.get("pin_skew", 0.0),
        p_planner_fix=weights.get("planner_fix", 0.0),
        p_needs_human=weights.get("needs_human", 0.0),
    )


def _triage(
    bd: _FakeBd,
    issue_id: str,
    evidence: str,
    repo: Path,
    *,
    vector: TriageVector | None = None,
    evaluate: Any = None,
    respec: Any = None,
    config: JudgeConfig | None = None,
) -> tuple[TriageClass | None, list[str]]:
    logged: list[str] = []
    if evaluate is None:
        def evaluate(issue: Any, text: str, cfg: JudgeConfig) -> TriageVector:
            assert vector is not None, "the judge was called without a vector"
            return vector
    applied = triage_parked_bead(
        bd,
        issue_id,
        evidence=evidence,
        repo=repo,
        config=config if config is not None else JudgeConfig(enabled=True),
        run_id=uuid4(),
        write_log=logged.append,
        respec=respec,
        evaluate=evaluate,
    )
    return applied, logged


def _records(repo: Path) -> list[dict]:
    path = repo / "logs" / "jev-decisions.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line]


# --- AC-1: the routing rule itself -----------------------------------------


@pytest.mark.parametrize(
    "weights,expected",
    [
        ({"pin_skew": 0.6, "planner_fix": 0.3, "needs_human": 0.1}, TriageClass.PIN_SKEW),
        ({"pin_skew": 0.2, "planner_fix": 0.5, "needs_human": 0.3}, TriageClass.PLANNER_FIX),
        ({"pin_skew": 0.1, "planner_fix": 0.2, "needs_human": 0.7}, TriageClass.NEEDS_HUMAN),
    ],
)
def test_route_triage_takes_the_argmax(weights: dict, expected: TriageClass) -> None:
    assert route_triage(_vector(**weights)) is expected


def test_route_triage_has_no_confidence_floor() -> None:
    """A barely-leading class still wins: hedging is not parking."""
    vector = _vector(pin_skew=0.34, planner_fix=0.33, needs_human=0.33)
    assert route_triage(vector) is TriageClass.PIN_SKEW


def test_route_triage_resolves_a_flat_vector_to_the_park() -> None:
    uniform = _vector(pin_skew=1 / 3, planner_fix=1 / 3, needs_human=1 / 3)
    assert route_triage(uniform) is TriageClass.NEEDS_HUMAN


def test_route_record_is_replayable(tmp_path: Path) -> None:
    bd = _FakeBd({"demo-1": _parked()})
    applied, _ = _triage(
        bd, "demo-1", _TRUE_GAP, tmp_path, vector=_vector(needs_human=1.0)
    )
    assert applied is TriageClass.NEEDS_HUMAN
    (record,) = _records(tmp_path)
    assert validate_event(record)["triage_class"] == "needs_human"
    assert record["effective_action"] == "needs_human"


def test_route_shadow_mode_records_the_class_and_parks_the_bead(tmp_path: Path) -> None:
    bd = _FakeBd({"demo-1": _parked()})
    applied, _ = _triage(
        bd,
        "demo-1",
        _TRUE_GAP,
        tmp_path,
        vector=_vector(planner_fix=1.0),
        config=JudgeConfig(enabled=True, mode=JudgeMode.SHADOW),
        respec=lambda issue_id: pytest.fail("shadow mode ran a re-spec turn"),
    )
    assert applied is TriageClass.NEEDS_HUMAN
    assert bd.labels("demo-1") == [HUMAN_LABEL]
    (record,) = _records(tmp_path)
    assert record["triage_class"] == "planner_fix"
    assert record["effective_action"] == "needs_human"
    assert validate_event(record)


# --- AC-2: the regex first pass --------------------------------------------


def test_regex_first_pass_routes_pin_skew_without_calling_jev(tmp_path: Path) -> None:
    bd = _FakeBd({"demo-1": _parked()})

    def never(issue: Any, text: str, config: JudgeConfig) -> TriageVector:
        pytest.fail("the judge was asked about a park the regex already classified")

    applied, _ = _triage(bd, "demo-1", _SKEW, tmp_path, evaluate=never)

    assert applied is TriageClass.PIN_SKEW
    assert HUMAN_LABEL not in bd.labels("demo-1")
    assert PIN_SKEW_LABEL in bd.labels("demo-1")
    (record,) = _records(tmp_path)
    assert record["regex_hit"] is True
    assert record["fail_open"] is False
    assert validate_event(record)


def test_regex_first_pass_leaves_a_true_gap_to_the_judge(tmp_path: Path) -> None:
    bd = _FakeBd({"demo-1": _parked()})
    applied, _ = _triage(
        bd, "demo-1", _TRUE_GAP, tmp_path, vector=_vector(needs_human=0.9)
    )

    assert applied is TriageClass.NEEDS_HUMAN
    assert bd.labels("demo-1") == [HUMAN_LABEL]
    assert PIN_SKEW_LABEL not in bd.labels("demo-1")
    assert _records(tmp_path)[0]["regex_hit"] is False


# --- AC-3: the planner-fix route -------------------------------------------


def _respec_to(bd: _FakeBd, packet: dict[str, Any], attempts: list[str]) -> Any:
    def run(issue_id: str) -> bool:
        attempts.append(issue_id)
        bd.issues[issue_id].update(
            {key: value for key, value in packet.items() if key != "labels"}
        )
        return True

    return run


def test_planner_fix_unparks_a_packet_the_schema_accepts(tmp_path: Path) -> None:
    bd = _FakeBd({"demo-1": _parked()})
    attempts: list[str] = []
    applied, _ = _triage(
        bd,
        "demo-1",
        "readiness schema v1 failed; every section is missing",
        tmp_path,
        vector=_vector(planner_fix=0.8),
        respec=_respec_to(bd, ready_issue("demo-1"), attempts),
    )

    assert attempts == ["demo-1"]
    assert applied is TriageClass.PLANNER_FIX
    assert HUMAN_LABEL not in bd.labels("demo-1")
    assert validate_issue(bd.show("demo-1")).ready
    assert validate_event(_records(tmp_path)[0])


def test_planner_fix_keeps_a_packet_the_schema_still_rejects_parked(
    tmp_path: Path,
) -> None:
    bd = _FakeBd({"demo-1": _parked()})
    attempts: list[str] = []
    applied, _ = _triage(
        bd,
        "demo-1",
        "readiness schema v1 failed; every section is missing",
        tmp_path,
        vector=_vector(planner_fix=0.8),
        respec=_respec_to(bd, {"description": "## Objective\nStill vague."}, attempts),
    )

    assert attempts == ["demo-1"]
    assert applied is TriageClass.NEEDS_HUMAN
    assert bd.labels("demo-1") == [HUMAN_LABEL]
    record = _records(tmp_path)[0]
    assert record["triage_class"] == "planner_fix"
    assert record["effective_action"] == "needs_human"
    assert validate_event(record)


def test_planner_fix_is_spent_once_per_bead(tmp_path: Path) -> None:
    bd = _FakeBd({"demo-1": _parked()})
    attempts: list[str] = []
    respec = _respec_to(bd, {"description": "## Objective\nStill vague."}, attempts)
    for _ in range(2):
        _triage(
            bd,
            "demo-1",
            "readiness schema v1 failed; every section is missing",
            tmp_path,
            vector=_vector(planner_fix=0.8),
            respec=respec,
        )

    assert attempts == ["demo-1"]
    assert sum(RESPEC_MARKER in text for text in bd.comments_by_id["demo-1"]) == 1


def test_planner_fix_without_a_respec_runner_stays_parked(tmp_path: Path) -> None:
    bd = _FakeBd({"demo-1": _parked()})
    applied, _ = _triage(
        bd,
        "demo-1",
        "readiness schema v1 failed; every section is missing",
        tmp_path,
        vector=_vector(planner_fix=0.9),
        respec=None,
    )

    assert applied is TriageClass.NEEDS_HUMAN
    assert bd.labels("demo-1") == [HUMAN_LABEL]


def test_planner_fix_never_bypasses_the_schema_for_an_epic(tmp_path: Path) -> None:
    bd = _FakeBd({"demo-1": _parked(issue_type="epic")})
    applied, _ = _triage(
        bd,
        "demo-1",
        "readiness schema v1 failed; every section is missing",
        tmp_path,
        vector=_vector(planner_fix=1.0),
        respec=lambda issue_id: pytest.fail("an epic was re-specified"),
    )

    assert applied is None
    assert bd.labels("demo-1") == [HUMAN_LABEL]
    assert not (tmp_path / "logs").exists()


# --- AC-4: the fail-open park ----------------------------------------------


def test_fail_open_keeps_todays_label_and_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A judge that cannot answer leaves the readiness park byte-identical."""

    bd = _FakeBd({"demo-1": {"id": "demo-1", "issue_type": "task", "status": "open", "labels": []}})
    report = validate_issue({"id": "demo-1", "issue_type": "task"})
    assert not report.ready
    logged: list[str] = []

    def triage(issue_id: str, evidence: str) -> None:
        triage_parked_bead(
            bd,
            issue_id,
            evidence=evidence,
            repo=tmp_path,
            config=JudgeConfig(enabled=True),
            run_id=uuid4(),
            write_log=logged.append,
            # The real request path, with an environment holding no key: the
            # typed failure is produced rather than simulated.
            evaluate=lambda issue, text, config: triage_parked(
                issue, text, config, environ={}
            ),
        )

    _flag_unready_for_human(bd, [report], logged.append, triage=triage)

    assert bd.labels("demo-1") == [HUMAN_LABEL]
    diagnostic = bd.comments_by_id["demo-1"][0]
    assert diagnostic.startswith("readiness schema v1 failed; grind will not repair")
    assert report.diagnostic() in diagnostic
    record = _records(tmp_path)[0]
    assert record["fail_open"] is True
    assert record["triage_failure"] == "key_missing"
    assert record["effective_action"] == "needs_human"
    assert validate_event(record)


def test_fail_open_when_the_seat_is_disabled_never_asks(tmp_path: Path) -> None:
    bd = _FakeBd({"demo-1": _parked()})
    applied, _ = _triage(
        bd,
        "demo-1",
        _TRUE_GAP,
        tmp_path,
        config=JudgeConfig(),
        evaluate=lambda issue, text, config: pytest.fail("a disabled seat asked"),
    )

    assert applied is TriageClass.NEEDS_HUMAN
    assert bd.labels("demo-1") == [HUMAN_LABEL]
    assert _records(tmp_path)[0]["fail_open"] is True


def test_fail_open_on_a_tracker_that_cannot_be_read(tmp_path: Path) -> None:
    class _Down(_FakeBd):
        def show(self, issue_id: str) -> dict[str, Any]:
            raise RuntimeError("tracker down")

    bd = _Down({"demo-1": _parked()})
    applied, logged = _triage(
        bd, "demo-1", _SKEW, tmp_path, vector=_vector(pin_skew=1.0)
    )

    assert applied is None
    assert bd.labels("demo-1") == [HUMAN_LABEL]
    assert any("could not read demo-1" in line for line in logged)
