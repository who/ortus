"""The window-phase Jev questions: progress, post_turn, stuck and triage.

Each site asks narrow questions about one item, pairs every pick with a basis
question, carries code-computed facts in a trusted block apart from untrusted
text, and falls to its code baseline on `none` or a basis below 0.5. The loop
detector's facts are computed here from a real git repository and a synthetic
worker log, so a healthy worker's new files and passing checks outvote any
looping probability.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ortus.core.judge import JudgeConfig, JudgeMode
from ortus.core.judge_log import OutcomeStatus
from ortus.core.judge_post import (
    Outcome, OutcomeVerdict, WorkerOutcome, apply_outcome, build_outcome_questions,
    derive_outcome, evaluate_outcome,
)
from ortus.core.judge_progress import (
    PROGRESS_LOG_NAME, ProgressMode, ProgressSample, ProgressWatch,
    build_progress_questions, last_check_result, progress_facts, should_reap,
)
from ortus.core.judge_replay import validate_event
from ortus.core.judge_stuck import StuckAction, decide_stuck_claim
from ortus.core.judge_triage import (
    TriageClass, build_triage_questions, route_triage, triage_parked,
    triage_parked_bead,
)

ENV = {"TYPESAFE_API_KEY": "secret-test-credential"}
MODEL = "jev-1.13.0"
CONFIG = JudgeConfig(enabled=True, include_log_tail=True)


def nouls(**values: float) -> dict:
    return {name: {"type": "noul", "noul": value} for name, value in values.items()}


def client(bodies: list, calls: list) -> SimpleNamespace:
    async def system_one(*args, **kwargs):
        calls.append((args, kwargs))
        return bodies.pop(0)

    return SimpleNamespace(system_one=system_one)


# --- fixtures: a git repository and a worker log ---------------------------


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         *args], check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "README.md").write_text("start\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "start")
    return root


def bash(ident: str, command: str) -> str:
    return json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": ident, "name": "Bash", "input": {"command": command}},
    ]}})


def result(ident: str, ok: bool) -> str:
    return json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": ident, "is_error": not ok, "content": "out"},
    ]}})


def worker_log(path: Path, runs: list[tuple[str, bool]]) -> Path:
    lines = []
    for index, (command, ok) in enumerate(runs):
        lines += [bash(f"t{index}", command), result(f"t{index}", ok)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def touch_later(path: Path, text: str) -> None:
    """Write a file whose mtime is safely inside a window that started now."""
    path.write_text(text, encoding="utf-8")
    later = time.time() + 5
    os.utime(path, (later, later))


LOOPING = [("uv run pytest tests/test_a.py -q", False)] * 3
PASSING = [("uv run pytest tests/test_a.py -q", False), ("ls src", True),
           ("uv run pytest tests/test_a.py -q", True)]


# --- progress: code-computed facts -----------------------------------------


def test_progress_facts_for_a_looping_worker(repo: Path, tmp_path: Path) -> None:
    log = worker_log(tmp_path / "worker.log", LOOPING)
    facts = progress_facts(repo, git(repo, "rev-parse", "HEAD"), time.time(), log)
    assert facts["log_readable"] is True
    assert facts["new_or_modified_files"] == 0 and facts["file_paths"] == []
    assert facts["commits_since_start"] == 0
    assert facts["last_check"] == "fail"
    assert facts["check_trend"] == ["fail", "fail", "fail"]
    assert facts["check_improving"] is False
    assert (facts["shell_commands"], facts["distinct_shell_commands"]) == (3, 1)
    assert facts["longest_identical_run"] == 3
    assert facts["same_failing_command_repeated"] is True


def test_progress_facts_for_a_progressing_worker(repo: Path, tmp_path: Path) -> None:
    start = git(repo, "rev-parse", "HEAD")
    began = time.time()
    touch_later(repo / "feature.py", "x = 1\n")
    git(repo, "add", "feature.py")
    git(repo, "commit", "-q", "-m", "feature")
    touch_later(repo / "notes.txt", "draft\n")
    log = worker_log(tmp_path / "worker.log", [("ls", True), ("cat feature.py", True)])
    facts = progress_facts(repo, start, began, log)
    assert facts["new_or_modified_files"] == 2
    assert facts["file_paths"] == ["feature.py", "notes.txt"]
    assert facts["commits_since_start"] == 1
    assert facts["last_check"] == "none-run" and facts["check_trend"] == []
    assert facts["same_failing_command_repeated"] is False


def test_progress_facts_see_a_failing_then_passing_check(repo: Path, tmp_path: Path) -> None:
    log = worker_log(tmp_path / "worker.log", PASSING)
    facts = progress_facts(repo, git(repo, "rev-parse", "HEAD"), time.time(), log)
    assert facts["last_check"] == "pass"
    assert facts["check_trend"] == ["fail", "pass"]
    assert facts["check_improving"] is True
    assert facts["same_failing_command_repeated"] is False
    assert last_check_result(log) == "pass"


def test_progress_facts_ignore_edits_from_before_the_window(repo: Path, tmp_path: Path) -> None:
    (repo / "README.md").write_text("left behind\n", encoding="utf-8")
    old = time.time() - 600
    os.utime(repo / "README.md", (old, old))
    log = worker_log(tmp_path / "worker.log", [])
    facts = progress_facts(repo, git(repo, "rev-parse", "HEAD"), time.time(), log)
    assert facts["new_or_modified_files"] == 0


def test_progress_facts_report_a_malformed_log(repo: Path, tmp_path: Path) -> None:
    log = tmp_path / "worker.log"
    log.write_text("not json\n{broken\n", encoding="utf-8")
    facts = progress_facts(repo, git(repo, "rev-parse", "HEAD"), time.time(), log)
    assert facts["log_readable"] is False
    assert facts["shell_commands"] == 0 and facts["last_check"] == "none-run"
    assert progress_facts(repo, "", time.time(), tmp_path / "absent.log")["log_readable"] is False


def test_progress_facts_null_commits_without_a_start_or_on_a_detached_head(
    repo: Path, tmp_path: Path,
) -> None:
    log = worker_log(tmp_path / "worker.log", [])
    missing = progress_facts(repo, "", time.time(), log)
    assert missing["commits_since_start"] is None
    assert missing["new_or_modified_files"] is None
    start = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "--detach")
    detached = progress_facts(repo, start, time.time(), log)
    assert detached["commits_since_start"] is None
    assert detached["detached_head"] is True


def test_progress_reap_rule_reads_the_fact_conditions() -> None:
    looping = [ProgressSample(0.95, "h"), ProgressSample(0.95, "h")]
    assert should_reap(looping) is True
    assert should_reap([*looping[:1], ProgressSample(0.95, "h", fact_progress=True)]) is False
    assert should_reap([*looping[:1], ProgressSample(0.95, "h", start_known=False)]) is False


def progress_watch(repo: Path, log: Path, bodies: list, calls: list, clock: list) -> ProgressWatch:
    head = git(repo, "rev-parse", "HEAD")
    return ProgressWatch(
        repo=repo, config=CONFIG, mode=ProgressMode.ENFORCE, run_id=uuid4(),
        issue_id="ortus-loop", log_path=log, head_oid=lambda: head,
        bead_status=lambda: OutcomeStatus.IN_PROGRESS, write_log=lambda _: None,
        interval=300.0, clock=lambda: clock[0],
        client_factory=lambda _: client(bodies, calls), environ=ENV,
    )


def looping_answers() -> dict:
    return {"model": MODEL, "answers": nouls(
        same_failure_repeating=0.99, no_new_artifacts_by_facts=0.99, has_basis=0.99,
    )}


def test_progress_watch_reaps_a_looping_fixture(repo: Path, tmp_path: Path) -> None:
    log = worker_log(tmp_path / "worker.log", LOOPING)
    calls: list = []
    clock = [0.0]
    watch = progress_watch(repo, log, [looping_answers(), looping_answers()], calls, clock)
    clock[0] += 300
    assert watch.reason() is None
    clock[0] += 300
    assert watch.reason() is not None and watch.reaped is True


def test_progress_watch_spares_a_progressing_fixture_however_high_looping_reads(
    repo: Path, tmp_path: Path,
) -> None:
    log = worker_log(tmp_path / "worker.log", LOOPING)
    calls: list = []
    clock = [0.0]
    watch = progress_watch(repo, log, [looping_answers(), looping_answers()], calls, clock)
    touch_later(repo / "new_module.py", "y = 2\n")
    clock[0] += 300
    assert watch.reason() is None
    clock[0] += 300
    assert watch.reason() is None
    assert watch.reaped is False and watch.would_reap == 0
    payload = calls[0][0][0]
    assert set(payload) == {"facts", "untrusted"}
    assert payload["facts"]["new_or_modified_files"] == 1
    assert payload["facts"]["file_paths"] == ["new_module.py"]
    assert payload["facts"]["same_failing_command_repeated"] is True
    assert set(payload["untrusted"]) == {"log_tail"}
    record = json.loads((repo / "logs" / PROGRESS_LOG_NAME).read_text().splitlines()[-1])
    assert record["question_version"].startswith("progress-v2:")
    assert record["window_facts"]["new_or_modified_files"] == 1
    assert "file_paths" not in record["window_facts"]
    assert record["narrow_answers"]["has_basis"] == 0.99


def test_progress_low_basis_reading_argues_for_nothing(repo: Path, tmp_path: Path) -> None:
    log = worker_log(tmp_path / "worker.log", LOOPING)
    unsure = {"model": MODEL, "answers": nouls(
        same_failure_repeating=0.99, no_new_artifacts_by_facts=0.99, has_basis=0.2,
    )}
    clock = [0.0]
    watch = progress_watch(repo, log, [unsure, dict(unsure)], [], clock)
    for _ in range(2):
        clock[0] += 300
        assert watch.reason() is None
    assert watch.samples[-1].shrunk_looping == 0.5


def test_progress_questions_are_narrow_with_a_basis() -> None:
    questions = build_progress_questions()
    assert set(questions) == {"same_failure_repeating", "no_new_artifacts_by_facts", "has_basis"}
    assert all(question["type"] == "noul" for question in questions.values())


# --- post_turn ---------------------------------------------------------------


POST_FALSE = dict(work_complete=0.1, transient_failure=0.1,
                  needs_operator_credential_or_decision=0.1, spec_gap=0.1)


@pytest.mark.parametrize("yes,outcome", [
    ((), Outcome.CONTINUE),
    (("work_complete",), Outcome.DONE),
    (("transient_failure", "work_complete"), Outcome.FLAKE),
    (("spec_gap", "transient_failure"), Outcome.PLAN_GAP),
    (("needs_operator_credential_or_decision", "spec_gap"), Outcome.NEEDS_HUMAN),
])
def test_post_derives_the_legacy_outcome_by_precedence(yes, outcome) -> None:
    narrow = {**POST_FALSE, **dict.fromkeys(yes, 0.9), "has_basis": 0.9}
    assert derive_outcome(narrow) is outcome


def test_post_questions_are_narrow_with_a_basis() -> None:
    questions = build_outcome_questions()
    assert set(questions) == {*POST_FALSE, "has_basis"}
    assert all(question["type"] == "noul" for question in questions.values())


def test_post_request_splits_trusted_facts_from_issue_text() -> None:
    calls: list = []
    observation = WorkerOutcome(1, False, OutcomeStatus.IN_PROGRESS, True, last_check="fail")
    body = {"model": MODEL, "answers": nouls(**{**POST_FALSE, "spec_gap": 0.8}, has_basis=0.75)}
    config = JudgeConfig(post_turn=True, include_issue_text=True)
    verdict = evaluate_outcome(
        {"id": "demo-1", "status": "in_progress", "title": "Rewrite the parser"},
        observation, config, environ=ENV, client_factory=lambda _: client([body], calls),
    )
    assert verdict == OutcomeVerdict(Outcome.PLAN_GAP, 0.75)
    assert verdict.narrow["spec_gap"] == 0.8
    payload = calls[0][0][0]
    assert payload["facts"]["worker"] == {
        "exit_status": 1, "watchdog": False, "observed_status": "in_progress",
        "branch_advanced": True, "worker_failure": None, "last_check": "fail",
    }
    assert payload["untrusted"]["title"] == "Rewrite the parser"
    assert "title" not in payload["facts"]


class Tracker:
    def __init__(self) -> None:
        self.labels: list[str] = []
        self.comments: list[str] = []

    def show(self, issue_id: str) -> dict:
        return {"id": issue_id, "status": "in_progress", "labels": list(self.labels)}

    def add_label(self, issue_id: str, label: str) -> None:
        self.labels.append(label)

    def add_comment(self, issue_id: str, body: str) -> None:
        self.comments.append(body)


def post_record(repo: Path) -> dict:
    rows = [json.loads(line) for line in (repo / "logs" / "jev-decisions.jsonl").read_text().splitlines()]
    return [row for row in rows if row["event"] == "post_turn"][-1]


def test_post_low_basis_falls_to_the_baseline(tmp_path: Path) -> None:
    narrow = {**POST_FALSE, "needs_operator_credential_or_decision": 0.95, "has_basis": 0.3}
    verdict = OutcomeVerdict(derive_outcome(narrow), 0.3, narrow=narrow)
    bd = Tracker()
    config = JudgeConfig(post_turn=True, mode=JudgeMode.ENFORCE)
    observation = WorkerOutcome(0, False, OutcomeStatus.IN_PROGRESS, False)
    stop = apply_outcome(bd, tmp_path, "demo-1", observation, verdict, config, uuid4())
    assert stop is False and bd.labels == [] and bd.comments == []
    record = post_record(tmp_path)
    assert record["reason"] == "no_basis"
    assert record["jev_action"] == "preserve"
    assert record["narrow_answers"]["has_basis"] == 0.3
    assert record["question_version"].startswith("post-v2:")
    assert validate_event(record)


def test_post_with_a_basis_still_parks_an_operator_need(tmp_path: Path) -> None:
    narrow = {**POST_FALSE, "needs_operator_credential_or_decision": 0.95, "has_basis": 0.9}
    verdict = OutcomeVerdict(derive_outcome(narrow), 0.9, narrow=narrow)
    bd = Tracker()
    config = JudgeConfig(post_turn=True, mode=JudgeMode.ENFORCE)
    observation = WorkerOutcome(0, False, OutcomeStatus.IN_PROGRESS, False)
    assert apply_outcome(bd, tmp_path, "demo-1", observation, verdict, config, uuid4())
    assert bd.labels == ["human"]
    assert validate_event(post_record(tmp_path))


# --- stuck -------------------------------------------------------------------


def test_stuck_reads_the_post_v2_answers_through_the_vector() -> None:
    narrow = {**POST_FALSE, "spec_gap": 0.9, "has_basis": 0.95}
    decision = decide_stuck_claim(OutcomeVerdict(derive_outcome(narrow), 0.95, narrow=narrow), 1, False)
    assert decision.action is StuckAction.REPLAN and not decision.fail_open


def test_stuck_low_basis_shrinks_toward_uniform() -> None:
    narrow = {**POST_FALSE, "needs_operator_credential_or_decision": 0.95, "has_basis": 0.0}
    for windows in (1, 2):
        decision = decide_stuck_claim(
            OutcomeVerdict(derive_outcome(narrow), 0.0, narrow=narrow), windows, False,
        )
        # Zero basis is the uniform vector before the window prior moves mass,
        # whichever outcome the nouls argued for.
        assert decision.vector() == decide_stuck_claim(
            OutcomeVerdict(Outcome.CONTINUE, 0.0), windows, False,
        ).vector()
    assert decide_stuck_claim(
        OutcomeVerdict(derive_outcome(narrow), 0.0, narrow=narrow), 0, False,
    ).action is StuckAction.CONTINUE


# --- triage ------------------------------------------------------------------


def test_triage_questions_shuffle_with_none_deterministically() -> None:
    first = build_triage_questions("ortus-a")
    assert set(first) == {"triage", "has_basis"}
    options = list(first["triage"]["criteria"])
    assert set(options) == {"pin_skew", "planner_fix", "needs_human", "none"}
    assert list(build_triage_questions("ortus-a")["triage"]["criteria"]) == options
    orders = {tuple(build_triage_questions(f"ortus-{n}")["triage"]["criteria"]) for n in range(20)}
    assert len(orders) > 1


def triage_body(choice: str, basis: float) -> dict:
    return {"model": MODEL, "answers": {
        "triage": {"type": "choice", "choice": choice, "confidence": 0.9},
        **nouls(has_basis=basis),
    }}


def test_triage_request_splits_regex_facts_from_park_text() -> None:
    calls: list = []
    vector = triage_parked(
        {"id": "demo-1", "title": "Add an exporter"}, "PLAN-GAP: the spec has no criteria",
        JudgeConfig(enabled=True, include_issue_text=True), environ=ENV,
        client_factory=lambda _: client([triage_body("planner_fix", 0.9)], calls),
    )
    assert route_triage(vector) is TriageClass.PLANNER_FIX
    assert vector.option_order == tuple(build_triage_questions("demo-1")["triage"]["criteria"])
    payload = calls[0][0][0]
    assert payload["facts"]["pin_skew_regex"] == {"pin_able": False, "evidence": []}
    assert "no criteria" in payload["untrusted"]["park_evidence"]
    assert payload["untrusted"]["title"] == "Add an exporter"


@pytest.mark.parametrize("choice,basis", [("none", 0.9), ("planner_fix", 0.3)])
def test_triage_none_or_low_basis_is_the_baseline_park(choice: str, basis: float) -> None:
    vector = triage_parked(
        {"id": "demo-1"}, "PLAN-GAP: unclear", JudgeConfig(enabled=True), environ=ENV,
        client_factory=lambda _: client([triage_body(choice, basis)], []),
    )
    assert route_triage(vector) is TriageClass.NEEDS_HUMAN
    assert vector.fail_open is False and vector.p_needs_human == 1.0
    assert vector.narrow["triage"] == choice


class ParkTracker:
    def __init__(self) -> None:
        self.issue = {"id": "demo-1", "status": "open", "issue_type": "task", "labels": ["human"]}

    def show(self, issue_id: str) -> dict:
        return dict(self.issue)

    def comments(self, issue_id: str) -> list:
        return []

    def add_label(self, issue_id: str, label: str) -> None:
        self.issue["labels"].append(label)

    def remove_label(self, issue_id: str, label: str) -> None:
        self.issue["labels"].remove(label)

    def add_comment(self, issue_id: str, body: str) -> None:
        pass


def test_triage_record_logs_the_option_order_and_answers(tmp_path: Path) -> None:
    config = JudgeConfig(enabled=True)
    applied = triage_parked_bead(
        ParkTracker(), "demo-1", evidence="PLAN-GAP: unclear", repo=tmp_path,
        config=config, run_id=uuid4(), write_log=lambda _: None,
        evaluate=lambda issue, text, cfg: triage_parked(
            issue, text, cfg, environ=ENV,
            client_factory=lambda _: client([triage_body("none", 0.9)], []),
        ),
    )
    assert applied is TriageClass.NEEDS_HUMAN
    record = json.loads((tmp_path / "logs" / "jev-decisions.jsonl").read_text().splitlines()[-1])
    assert record["question_version"].startswith("triage-v2:")
    assert set(record["option_order"]) == {"pin_skew", "planner_fix", "needs_human", "none"}
    assert record["narrow_answers"]["triage"] == "none"
    assert record["jev_action"] == "needs_human"
    assert validate_event(record)
