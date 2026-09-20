"""Gate logs preserve typed evidence without preserving sensitive inputs."""

from __future__ import annotations

import fcntl
import json
import multiprocessing
import os
import stat
from dataclasses import replace
from uuid import uuid4

import pytest

from ortus.core import judge_log
from ortus.core.judge import (
    GateAction, GateDecision, GateReason, JudgeAnswers, JudgeConfig, JudgePhase,
    JudgeRoute,
)
from ortus.core.judge_log import (
    DecisionEvent, JudgeLogError, LogFailure, OutcomeEvent, OutcomeStatus,
    elapsed_ms, write_decision, write_outcome,
)
from ortus.core.judge_typesafe import JudgeFailure, JudgeUsage

CONFIG = JudgeConfig(enabled=True)


def decision(**changes):
    event = DecisionEvent(
        run_id=uuid4(), seat="product", issue_id="ortus-example",
        phase=JudgePhase.PRE_TURN,
        answers=JudgeAnswers(JudgeRoute.CODEX, .9, .1, .9, 0, .95),
        decision=GateDecision(GateAction.PROCEED, JudgeRoute.CODEX, GateReason.ROUTED),
        model="jev-1.13.0", criteria_version="v1", criteria_hash="a" * 64,
        latency_ms=12.5,
    )
    return replace(event, **changes)


def records(repo):
    return [json.loads(line) for line in (repo / "logs" / judge_log.LOG_NAME).read_text().splitlines()]


def test_complete_decision_and_correlated_outcome(tmp_path, capsys):
    event = decision(usage=JudgeUsage(23, 11))
    assert write_decision(tmp_path, CONFIG, event) == event.decision_id
    outcome = OutcomeEvent(event.run_id, event.decision_id, OutcomeStatus.CLOSED, 1250)
    assert write_outcome(tmp_path, CONFIG, outcome) == event.decision_id
    logged, finished = records(tmp_path)
    assert set(logged) == {
        "schema_version", "event", "timestamp", "run_id", "decision_id", "seat",
        "issue_id", "phase", "answers", "intended_action", "effective_action",
        "backend", "reason", "model", "criteria_version", "criteria_hash",
        "latency_ms", "failure", "input_tokens", "output_tokens", "measured_cost_usd",
    }
    assert logged["schema_version"] == 1
    assert logged["timestamp"].endswith("+00:00")
    assert logged["seat"] == "product"
    assert logged["issue_id"] == "ortus-example"
    assert logged["phase"] == "pre_turn"
    assert logged["model"] == "jev-1.13.0"
    assert logged["criteria_version"] == "v1"
    assert logged["criteria_hash"] == "a" * 64
    assert logged["answers"] == {
        "route": "codex", "route_confidence": .9, "needs_human": .1,
        "noul_confidence": .9, "action_risk": 0, "risk_confidence": .95,
    }
    assert logged["intended_action"] == logged["backend"] == "codex"
    assert logged["effective_action"] == "proceed"
    assert logged["input_tokens"] == 23
    assert logged["output_tokens"] == 11
    assert logged["measured_cost_usd"] is None
    assert logged["latency_ms"] == 12.5
    assert finished["event"] == "outcome"
    assert finished["run_id"] == logged["run_id"] == str(event.run_id)
    assert finished["decision_id"] == logged["decision_id"] == str(event.decision_id)
    assert finished["observed_status"] == "closed"
    assert finished["elapsed_worker_ms"] == 1250
    assert finished["input_tokens"] is finished["measured_cost_usd"] is None
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "judge proceed reason=routed failure=none latency_ms=12" in captured.err
    assert "judge outcome closed elapsed_worker_ms=1250" in captured.err
    assert "product" not in captured.err


def test_intent_is_distinct_from_effective_policy(tmp_path):
    event = decision(decision=GateDecision(GateAction.HUMAN, None, GateReason.HIGH_RISK))
    write_decision(tmp_path, CONFIG, event)
    row = records(tmp_path)[0]
    assert row["intended_action"] == "codex"
    assert row["effective_action"] == "human"
    assert row["backend"] is None


def test_disabled_writes_do_not_validate_or_touch_disk(tmp_path, capsys):
    assert write_decision(tmp_path, JudgeConfig(), None) is None
    assert write_outcome(tmp_path, JudgeConfig(), None) is None
    assert list(tmp_path.iterdir()) == []
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("failure", list(JudgeFailure))
def test_failure_events_have_no_raw_error_or_invented_usage(tmp_path, failure):
    event = decision(answers=None, failure=failure,
                     decision=GateDecision(GateAction.PROCEED, JudgeRoute.CLAUDE,
                                           GateReason.SERVICE_FAILURE))
    write_decision(tmp_path, CONFIG, event)
    row = records(tmp_path)[0]
    assert row["answers"] is row["intended_action"] is row["input_tokens"] is None
    assert row["failure"] == failure.value


@pytest.mark.parametrize("field", ["seat", "issue_id", "criteria_version"])
@pytest.mark.parametrize("value", ["private@example.test", "bearer sensitive-text",
                                    "vault-value", "/private/cases/17", "x" * 161])
def test_sensitive_and_oversize_metadata_are_omitted(tmp_path, capsys, field, value):
    event = decision(**{field: value})
    # Extra attributes are never serialized, even on dataclass subclasses or
    # objects populated by a caller that carries unrelated packet data.
    object.__setattr__(event, "title", "private title")
    object.__setattr__(event, "raw_error", "provider exception with key")
    write_decision(tmp_path, replace(CONFIG, sensitive_paths=("/private/cases",)),
                   event, environ={"TYPESAFE_API_KEY": "vault-value"})
    assert records(tmp_path)[0][field] is None
    text = (tmp_path / "logs" / judge_log.LOG_NAME).read_text()
    assert value not in text
    assert "private title" not in text
    assert "provider exception" not in text
    assert value not in capsys.readouterr().err


@pytest.mark.parametrize("changes", [
    {"latency_ms": float("nan")}, {"latency_ms": float("inf")},
    {"latency_ms": -1}, {"latency_ms": True}, {"latency_ms": 10**400},
    {"phase": "pre_turn"}, {"model": "jev-latest"},
    {"criteria_hash": "not-a-hash"}, {"criteria_version": "\ud800"},
    {"run_id": "secret id"}, {"decision_id": "secret id"},
    {"answers": None}, {"failure": JudgeFailure.TIMEOUT},
    {"answers": JudgeAnswers(JudgeRoute.CODEX, float("nan"), .1, .9, 0, .9)},
    {"answers": JudgeAnswers("secret route", .9, .1, .9, 0, .9)},
    {"usage": JudgeUsage(-1, 1)}, {"usage": JudgeUsage(True, 1)},
])
def test_invalid_events_fail_before_creating_a_log(tmp_path, changes):
    with pytest.raises(JudgeLogError) as exc:
        write_decision(tmp_path, CONFIG, decision(**changes))
    assert exc.value.failure == LogFailure.INVALID_EVENT
    assert str(exc.value) == "judge log failed: invalid_event"
    assert not (tmp_path / "logs").exists()


def test_monotonic_elapsed_measurement(monkeypatch):
    monkeypatch.setattr(judge_log.time, "monotonic", lambda: 101.25)
    assert elapsed_ms(100) == 1250
    with pytest.raises(JudgeLogError):
        elapsed_ms(102)


@pytest.mark.parametrize("mode,expected", [(None, 0o600), (0o666, 0o600), (0o200, 0o200)])
def test_private_permissions_preserve_stricter_existing_modes(tmp_path, mode, expected):
    path = tmp_path / "logs" / judge_log.LOG_NAME
    if mode is not None:
        path.parent.mkdir()
        path.touch(mode=mode)
        path.chmod(mode)
    # A write-only file cannot be opened RDWR by an ordinary user. Refusal must
    # preserve permissions too; the logger may not broaden them to get access.
    try:
        write_decision(tmp_path, CONFIG, decision())
    except JudgeLogError as exc:
        assert mode == 0o200 and exc.failure == LogFailure.IO_ERROR
    assert stat.S_IMODE(path.stat().st_mode) == expected


@pytest.mark.parametrize("destination", ["directory", "file", "hardlink", "fifo"])
def test_unsafe_destinations_are_refused(tmp_path, destination):
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "keep"
    target.write_text("unchanged\n")
    logs = tmp_path / "logs"
    if destination == "directory":
        logs.symlink_to(outside, target_is_directory=True)
    else:
        logs.mkdir()
        path = logs / judge_log.LOG_NAME
        if destination == "file":
            path.symlink_to(target)
        elif destination == "hardlink":
            os.link(target, path)
        else:
            os.mkfifo(path)
    with pytest.raises(JudgeLogError) as exc:
        write_decision(tmp_path, CONFIG, decision())
    assert exc.value.failure == LogFailure.UNSAFE_PATH
    assert target.read_text() == "unchanged\n"


def test_partial_tail_is_never_joined_to_a_new_event(tmp_path):
    path = tmp_path / "logs" / judge_log.LOG_NAME
    path.parent.mkdir()
    path.write_bytes(b'{"event":')
    with pytest.raises(JudgeLogError) as exc:
        write_decision(tmp_path, CONFIG, decision())
    assert exc.value.failure == LogFailure.INCOMPLETE_LOG
    assert path.read_bytes() == b'{"event":'


def test_short_append_rolls_back_and_halts_before_worker(tmp_path, monkeypatch, capsys):
    write_decision(tmp_path, CONFIG, decision())
    capsys.readouterr()
    path = tmp_path / "logs" / judge_log.LOG_NAME
    original = path.read_bytes()
    real_write = os.write
    monkeypatch.setattr(judge_log.os, "write", lambda fd, data: real_write(fd, data[:12]))
    launched = []
    with pytest.raises(JudgeLogError) as exc:
        write_decision(tmp_path, CONFIG, decision())
        launched.append(True)
    assert exc.value.failure == LogFailure.IO_ERROR
    assert not launched
    assert path.read_bytes() == original
    assert capsys.readouterr().err == ""


def test_io_exception_text_is_suppressed(tmp_path, monkeypatch):
    def failed(*args, **kwargs):
        raise OSError("TYPESAFE_API_KEY=do-not-print")

    monkeypatch.setattr(judge_log.os, "open", failed)
    with pytest.raises(JudgeLogError) as exc:
        write_decision(tmp_path, CONFIG, decision())
    assert str(exc.value) == "judge log failed: io_error"
    assert exc.value.__suppress_context__


def _append_in_process(repo, event, started, finished):
    started.set()
    for _ in range(8):
        current = replace(event, decision_id=uuid4())
        write_decision(repo, CONFIG, current)
        write_outcome(repo, CONFIG, OutcomeEvent(
            current.run_id, current.decision_id, OutcomeStatus.IN_PROGRESS, 25,
        ))
    finished.set()


@pytest.mark.integration
def test_process_appends_take_the_lock_and_remain_complete(tmp_path):
    event = decision()
    write_decision(tmp_path, CONFIG, event)
    ctx = multiprocessing.get_context("spawn")
    processes = []
    with (tmp_path / "logs" / judge_log.LOG_NAME).open("rb") as locked:
        fcntl.flock(locked, fcntl.LOCK_EX)
        try:
            for _ in range(3):
                started, finished = ctx.Event(), ctx.Event()
                process = ctx.Process(target=_append_in_process,
                                      args=(tmp_path, event, started, finished))
                process.start()
                processes.append(process)
                assert started.wait(5)
                assert not finished.wait(.05)
            assert len(records(tmp_path)) == 1
        finally:
            fcntl.flock(locked, fcntl.LOCK_UN)
            for process in processes:
                process.join(10)
                if process.is_alive():
                    process.terminate()
                    process.join()
        assert all(process.exitcode == 0 for process in processes)
    rows = records(tmp_path)
    assert len(rows) == 49
    decisions = {row["decision_id"] for row in rows if row["event"] == "decision"}
    outcomes = {row["decision_id"] for row in rows if row["event"] == "outcome"}
    assert len(decisions) == 25
    assert outcomes == decisions - {str(event.decision_id)}
    assert all(len(json.dumps(row).encode()) <= judge_log.MAX_EVENT_BYTES for row in rows)
