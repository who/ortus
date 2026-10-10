"""Jev records carry what offline labeling needs, and nothing it must not."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from ortus.core import judge_log
from ortus.core.judge import (
    GateAction, GateDecision, GateReason, JudgeAnswers, JudgeConfig, JudgeMode,
    JudgePhase, JudgeRoute,
)
from ortus.core.judge_log import (
    MAX_COMMAND_CHARS, DecisionEvent, JudgeLogError, ModelRouteEvent, OutcomeStatus,
    question_version, write_decision, write_model_route, write_tool_decision,
)
from ortus.core.judge_post import Outcome, OutcomeVerdict, WorkerOutcome, apply_outcome
from ortus.core.judge_progress import (
    PROGRESS_LOG_NAME, ProgressCheck, ProgressMode, ProgressVerdict, WorkerProgress,
    log_progress_check,
)
from ortus.core.judge_replay import ReplayError, read_events, validate_event
from ortus.core.judge_state import TRUNCATION_MARKER
from ortus.core.judge_stuck import StuckAction, decide_stuck_claim, log_stuck_decision
from ortus.core.judge_tools import REDACTED, ToolInput, cap_command, redacted_command
from ortus.core.judge_triage import TriageClass, TriageVector, log_triage, route_triage
from ortus.core.judge_typesafe import JudgeUsage

CONFIG = JudgeConfig(enabled=True)
SECRET = "s3cr3t-value-9f8e7d"
SIDE = {"baseline_action", "jev_action", "question_version", "option_order"}
REQUEST = {"latency_ms", "input_tokens", "output_tokens"}


def bash(command: str) -> ToolInput:
    return ToolInput("Bash", {"command": command})


def redact(tmp_path: Path, tool: ToolInput, config: JudgeConfig = CONFIG,
           secrets: tuple[str, ...] = (SECRET,)) -> str:
    return redacted_command(tool, config, (tmp_path,), secrets)


def lines(repo: Path, name: str = judge_log.LOG_NAME) -> list[dict]:
    return [json.loads(line) for line in (repo / "logs" / name).read_text().splitlines()]


def validated(repo: Path) -> list[dict]:
    return list(read_events(repo / "logs" / judge_log.LOG_NAME))


class Tracker:
    def __init__(self) -> None:
        self.rows = {"demo-1": {"status": "in_progress", "labels": []}}

    def show(self, issue_id):
        return dict(self.rows[issue_id])

    def add_label(self, issue_id, label):
        self.rows[issue_id]["labels"].append(label)

    def add_comment(self, issue_id, body):
        pass


def tool_record(**changes) -> dict:
    record = {
        "reason": "judged", "tool": "Bash", "action": "deny_call",
        "effective_action": "deny_call",
        "vector": {"allow": 0.2, "deny_call": 0.7, "park_bead": 0.1},
        "failure": None, "latency_ms": 41.5, "input_tokens": 120, "output_tokens": 9,
        "command": "rm -rf build", "baseline_action": "allow", "jev_action": "deny_call",
        "question_version": "pre-tool-v1:" + "0" * 16, "option_order": None,
    }
    record.update(changes)
    return record


# --- AC-1: the command ---------------------------------------------------------


def test_command_secret_env_value_is_replaced(tmp_path):
    logged = redact(tmp_path, bash(f"curl -H x:{SECRET} https://example.test"))
    assert SECRET not in logged
    assert REDACTED in logged
    assert logged.startswith("curl ")


def test_command_sensitive_paths_are_replaced(tmp_path):
    config = replace(CONFIG, sensitive_paths=("payroll",))
    logged = redact(tmp_path, bash("cat ~/.ssh/id_rsa data/payroll.csv README.md"), config)
    assert "id_rsa" not in logged and "payroll" not in logged
    assert logged == f"cat {REDACTED} {REDACTED} README.md"


def test_command_credential_assignment_is_replaced(tmp_path):
    logged = redact(tmp_path, bash("API_TOKEN=abc123 make deploy"))
    assert "abc123" not in logged
    assert logged == f"{REDACTED} make deploy"


def test_command_of_only_a_secret_is_the_placeholder(tmp_path):
    assert redact(tmp_path, bash(SECRET)) == REDACTED


def test_command_longer_than_tool_cap_is_truncated_with_a_marker(tmp_path):
    config = replace(CONFIG, tool_cap=40)
    logged = redact(tmp_path, bash("echo " + "é" * 200), config)
    assert len(logged) == 40
    assert logged.endswith(TRUNCATION_MARKER)
    logged.encode("utf-8")  # whole code points only
    assert cap_command("short", 40) == "short"


def test_command_heredoc_body_is_counted_not_copied(tmp_path):
    command = "cat > notes.txt <<'EOF'\nfirst private line\nsecond private line\nEOF\ngit add notes.txt"
    logged = redact(tmp_path, bash(command))
    assert "private" not in logged
    assert logged == "cat > notes.txt <<'EOF'\n<heredoc:2 lines>\nEOF\ngit add notes.txt"


def test_command_repository_and_home_paths_are_relabeled(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path.parent))
    logged = redact(tmp_path, bash(f"ls {tmp_path}/src && cat --file={tmp_path.parent}/x"))
    assert str(tmp_path) not in logged
    assert logged == "ls src && cat --file=~/x"


def test_command_for_a_write_is_its_screened_path_only(tmp_path):
    tool = ToolInput("Write", {"file_path": str(tmp_path / "src" / "a.py"),
                               "content": "PRIVATE BODY"})
    assert redact(tmp_path, tool) == "src/a.py"
    secret = ToolInput("Edit", {"file_path": ".env", "old_string": "x", "new_string": "y"})
    assert redact(tmp_path, secret) == REDACTED


def test_command_reaches_the_tool_decision_record_bounded_and_rescreened(tmp_path, monkeypatch):
    monkeypatch.setenv("SERVICE_TOKEN", SECRET)
    config = replace(CONFIG, tool_cap=4 * MAX_COMMAND_CHARS)
    write_tool_decision(tmp_path, config, uuid4(), "ortus-1",
                        tool_record(command="x" * (2 * MAX_COMMAND_CHARS)))
    write_tool_decision(tmp_path, config, uuid4(), "ortus-1",
                        tool_record(command=f"echo {SECRET}"))
    long, leaked = validated(tmp_path)
    assert len(long["command"]) == MAX_COMMAND_CHARS
    assert long["command"].endswith(TRUNCATION_MARKER)
    assert SECRET not in leaked["command"]


def test_command_too_long_or_not_text_is_rejected_by_validate():
    with pytest.raises(JudgeLogError):
        write_tool_decision(Path("/nonexistent"), CONFIG, uuid4(), "ortus-1",
                            tool_record(command=7))


# --- AC-2: the comparison and request fields -------------------------------------


def test_fields_on_a_gate_decision(tmp_path):
    event = DecisionEvent(
        run_id=uuid4(), seat="product", issue_id="ortus-1", phase=JudgePhase.PRE_TURN,
        answers=JudgeAnswers(JudgeRoute.CODEX, .9, .9, .9, 0, .95),
        decision=GateDecision(GateAction.HUMAN, None, GateReason.NEEDS_HUMAN),
        model="jev-1.13.0", criteria_version="pre-turn-v2", criteria_hash="ab" * 32,
        latency_ms=12.5, usage=JudgeUsage(30, 4),
    )
    write_decision(tmp_path, CONFIG, event)
    failed = replace(event, answers=None, failure=judge_log.JudgeFailure.TIMEOUT,
                     decision=GateDecision(GateAction.PROCEED, JudgeRoute.CODEX,
                                           GateReason.SERVICE_FAILURE),
                     decision_id=uuid4(), usage=None)
    write_decision(tmp_path, CONFIG, failed)
    answered, timed_out = validated(tmp_path)
    assert answered["baseline_action"] == "proceed"
    assert answered["jev_action"] == "human"
    assert answered["question_version"] == "pre-turn-v2:" + "ab" * 8
    assert (answered["latency_ms"], answered["input_tokens"]) == (12.5, 30)
    assert timed_out["jev_action"] is None
    assert timed_out["latency_ms"] == 12.5 and timed_out["input_tokens"] is None


def test_fields_on_post_turn_stuck_and_triage(tmp_path):
    bd = Tracker()
    observation = WorkerOutcome(0, False, OutcomeStatus.IN_PROGRESS, False)
    verdict = OutcomeVerdict(Outcome.PLAN_GAP, .8, latency_ms=250.0,
                             usage=JudgeUsage(400, 12))
    apply_outcome(bd, tmp_path, "demo-1", observation, verdict,
                  replace(CONFIG, mode=JudgeMode.SHADOW), uuid4())
    decision = decide_stuck_claim(verdict, 2, False)
    log_stuck_decision(tmp_path, CONFIG, uuid4(), "demo-1", decision, decision.action)
    vector = TriageVector(0.1, 0.7, 0.2, latency_ms=90.0, usage=JudgeUsage(200, 5))
    log_triage(tmp_path, CONFIG, uuid4(), "demo-1", vector, route_triage(vector),
               TriageClass.PLANNER_FIX)
    post, stuck, triage = validated(tmp_path)
    assert SIDE | REQUEST <= set(post) and SIDE | REQUEST <= set(stuck)
    assert (post["baseline_action"], post["jev_action"]) == ("preserve", "human")
    assert post["question_version"].startswith("post-turn-v1:")
    assert (post["latency_ms"], post["input_tokens"], post["output_tokens"]) == (250.0, 400, 12)
    assert stuck["baseline_action"] == "escalate"
    assert stuck["jev_action"] == decision.action.value
    assert stuck["latency_ms"] is stuck["input_tokens"] is stuck["question_version"] is None
    assert (triage["baseline_action"], triage["jev_action"]) == ("needs_human", "planner_fix")
    assert triage["question_version"].startswith("triage-v1:")
    assert (triage["latency_ms"], triage["input_tokens"]) == (90.0, 200)


def test_fields_for_a_regex_triage_and_a_failed_post_turn_name_no_jev_action(tmp_path):
    regex = TriageVector(1.0, 0.0, 0.0, regex_hit=True)
    log_triage(tmp_path, CONFIG, uuid4(), "demo-1", regex, TriageClass.PIN_SKEW,
               TriageClass.PIN_SKEW)
    apply_outcome(Tracker(), tmp_path, "demo-1",
                  WorkerOutcome(1, False, OutcomeStatus.IN_PROGRESS, False),
                  OutcomeVerdict(failure=judge_log.JudgeFailure.SERVICE_ERROR, latency_ms=5.0),
                  CONFIG, uuid4())
    triage, post = validated(tmp_path)
    assert (triage["baseline_action"], triage["jev_action"]) == ("pin_skew", None)
    assert triage["latency_ms"] is triage["question_version"] is None
    assert post["jev_action"] is None
    assert post["latency_ms"] == 5.0 and post["input_tokens"] is None


def test_fields_for_a_local_policy_tool_denial(tmp_path):
    write_tool_decision(tmp_path, CONFIG, uuid4(), "ortus-1", tool_record(
        reason="force_push", vector=None, latency_ms=0, input_tokens=None,
        output_tokens=None, baseline_action="deny_call", jev_action=None,
        question_version=None, command="git push --force",
    ))
    (row,) = validated(tmp_path)
    assert (row["baseline_action"], row["jev_action"]) == ("deny_call", None)
    assert row["input_tokens"] is None and row["command"] == "git push --force"


def test_fields_on_progress_and_model_route(tmp_path):
    verdict = ProgressVerdict(0.9, 0.1, 0.9, latency_ms=70.0, usage=JudgeUsage(50, 2))
    log_progress_check(tmp_path, CONFIG, ProgressCheck(
        uuid4(), uuid4(), "demo-1", ProgressMode.SHADOW,
        WorkerProgress(30.0, False, OutcomeStatus.IN_PROGRESS), verdict, True, False,
    ))
    (check,) = lines(tmp_path, PROGRESS_LOG_NAME)
    assert (check["baseline_action"], check["jev_action"]) == ("continue", "reap")
    assert check["question_version"].startswith("progress-v1:")
    assert (check["latency_ms"], check["input_tokens"]) == (70.0, 50)
    write_model_route(tmp_path, CONFIG, ModelRouteEvent(
        uuid4(), uuid4(), "product", "demo-1", "claude", "baseline", "unmappable_profile",
        difficulty=0.1, jev_tier="cheap",
    ))
    (route,) = lines(tmp_path, judge_log.ROUTE_LOG_NAME)
    assert (route["baseline_action"], route["jev_action"]) == ("baseline", "cheap")
    assert route["latency_ms"] is route["input_tokens"] is None


def test_fields_reject_an_action_outside_the_site_vocabulary(tmp_path):
    with pytest.raises(JudgeLogError):
        write_tool_decision(tmp_path, CONFIG, uuid4(), "ortus-1",
                            tool_record(jev_action="escalate"))
    with pytest.raises(JudgeLogError):
        write_tool_decision(tmp_path, CONFIG, uuid4(), "ortus-1",
                            tool_record(question_version="free text"))


def test_question_version_names_the_set_and_its_hash():
    first = question_version("triage-v1", {"q": {"type": "choice"}})
    assert first.startswith("triage-v1:") and len(first) == len("triage-v1:") + 16
    assert question_version("triage-v1", {"q": {"type": "noul"}}) != first
    with pytest.raises(JudgeLogError):
        question_version("Not A Name", {})


def _tool_row(tmp_path: Path, **changes) -> dict:
    write_tool_decision(tmp_path, CONFIG, uuid4(), "ortus-1", tool_record())
    (row,) = lines(tmp_path)
    row.update(changes)
    return row


@pytest.mark.parametrize("changes", [
    {"baseline_action": "proceed"},
    {"jev_action": "reap"},
    {"question_version": "pre-tool-v1:short"},
    {"option_order": ["allow", "allow"]},
    {"option_order": ["approve"]},
    {"command": "x" * (MAX_COMMAND_CHARS + 1)},
    {"command": None},
])
def test_validate_rejects_malformed_side_fields(tmp_path, changes):
    with pytest.raises(ReplayError):
        validate_event(_tool_row(tmp_path, **changes))


def test_validate_rejects_side_fields_on_an_outcome_and_half_token_pairs(tmp_path):
    stuck = decide_stuck_claim(None, 2, False)
    log_stuck_decision(tmp_path, CONFIG, uuid4(), "demo-1", stuck, StuckAction.ESCALATE)
    (row,) = lines(tmp_path)
    with pytest.raises(ReplayError):
        validate_event({**row, "input_tokens": 3})
    with pytest.raises(ReplayError):
        validate_event({**row, "latency_ms": -1})
    outcome = {"schema_version": 1, "event": "outcome", "timestamp": row["timestamp"],
               "run_id": row["run_id"], "decision_id": row["decision_id"],
               "observed_status": "closed", "elapsed_worker_ms": 1, "input_tokens": None,
               "output_tokens": None, "measured_cost_usd": None}
    validate_event(dict(outcome))
    with pytest.raises(ReplayError):
        validate_event({**outcome, "baseline_action": "proceed"})


def test_validate_accepts_option_order_from_the_question_choices(tmp_path):
    row = _tool_row(tmp_path, option_order=["park_bead", "allow", "deny_call"])
    assert validate_event(row)["option_order"] == ["park_bead", "allow", "deny_call"]


# --- AC-3: logs written before the fields existed ----------------------------------


def test_legacy_records_without_the_new_fields_still_validate_and_replay(tmp_path):
    bd = Tracker()
    write_tool_decision(tmp_path, CONFIG, uuid4(), "ortus-1", tool_record())
    apply_outcome(bd, tmp_path, "demo-1",
                  WorkerOutcome(0, False, OutcomeStatus.IN_PROGRESS, False),
                  OutcomeVerdict(Outcome.CONTINUE, .5), CONFIG, uuid4())
    log_stuck_decision(tmp_path, CONFIG, uuid4(), "demo-1",
                       decide_stuck_claim(None, 1, False), StuckAction.CONTINUE)
    vector = TriageVector()
    log_triage(tmp_path, CONFIG, uuid4(), "demo-1", vector, route_triage(vector),
               TriageClass.NEEDS_HUMAN)
    event = DecisionEvent(
        run_id=uuid4(), seat="product", issue_id="ortus-1", phase=JudgePhase.PRE_TURN,
        answers=None, failure=judge_log.JudgeFailure.KEY_MISSING,
        decision=GateDecision(GateAction.PROCEED, JudgeRoute.CODEX, GateReason.SERVICE_FAILURE),
        model="jev-1.13.0", criteria_version="v1", criteria_hash="a" * 64, latency_ms=1,
    )
    write_decision(tmp_path, CONFIG, event)
    path = tmp_path / "logs" / judge_log.LOG_NAME
    added = SIDE | {"command"}
    legacy = []
    for row in lines(tmp_path):
        stripped = {k: v for k, v in row.items() if k not in added}
        if row["event"] in ("post_turn", "stuck_decision", "triage"):
            stripped = {k: v for k, v in stripped.items() if k not in REQUEST}
        legacy.append(stripped)
    path.write_text("".join(json.dumps(row) + "\n" for row in legacy))
    replayed = list(read_events(path))
    assert [row["event"] for row in replayed] == [
        "tool_decision", "post_turn", "stuck_decision", "triage", "decision",
    ]
    assert all(not (added & set(row)) for row in replayed)


def test_legacy_hook_record_without_side_fields_is_logged_in_the_old_shape(tmp_path):
    record = {k: v for k, v in tool_record().items() if k not in SIDE | {"command"}}
    write_tool_decision(tmp_path, CONFIG, uuid4(), "ortus-1", record)
    (row,) = validated(tmp_path)
    assert not (SIDE | {"command"}) & set(row)


def test_legacy_repository_logs_still_validate():
    path = Path(__file__).resolve().parents[1] / "logs" / judge_log.LOG_NAME
    if not path.is_file():
        pytest.skip("no local decision log in this checkout")
    for _ in read_events(path):
        pass
