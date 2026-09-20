"""Offline replay contracts, using real logger records rather than invented schemas."""
from dataclasses import replace
import json
import os
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.commands.judge import _write_atomic
from ortus.core.judge import (
    GateAction, GateDecision, GateReason, JudgeAnswers, JudgeConfig, JudgeMode, JudgePhase, JudgeRoute,
)
from ortus.core.judge_log import (
    DecisionEvent, OutcomeEvent, OutcomeStatus, write_decision, write_outcome, write_shadow_outcome,
)
from ortus.core.judge_post import Outcome, OutcomeVerdict, WorkerOutcome, apply_outcome
from ortus.core.judge_replay import (
    MAX_EVENT_BYTES, ReplayError, join_events, read_events, read_labels, summarize,
)
from ortus.core.judge_typesafe import JudgeFailure


@pytest.fixture
def records(tmp_path):
    config = JudgeConfig(enabled=True)
    event = DecisionEvent(
        run_id=uuid4(), seat="default", issue_id="sample-1", phase=JudgePhase.PRE_TURN,
        answers=JudgeAnswers(JudgeRoute.CODEX, .9, .1, .9, 0, .9),
        decision=GateDecision(action=GateAction.PROCEED, reason=GateReason.ROUTED, backend=JudgeRoute.CODEX),
        model=config.model, criteria_version="v1", criteria_hash="a" * 64, latency_ms=10,
    )
    def make(*, shadow=False, claimed="sample-1", outcome=True, failure=False):
        cfg = replace(config, mode=JudgeMode.SHADOW if shadow else JudgeMode.ENFORCE)
        ev = replace(event, decision_id=uuid4(),
                     answers=None if failure else event.answers,
                     failure=JudgeFailure.TIMEOUT if failure else None)
        write_decision(tmp_path, cfg, ev, environ={})
        if outcome:
            out = OutcomeEvent(ev.run_id, ev.decision_id, OutcomeStatus.CLOSED, 100)
            if shadow:
                write_shadow_outcome(tmp_path, cfg, out, observed_issue_id="sample-1", actual_claimed_id=claimed)
            else:
                write_outcome(tmp_path, cfg, out)
        path = tmp_path / "logs" / "jev-decisions.jsonl"
        return list(read_events(path))[-(2 if outcome else 1):]
    return make


def save(tmp_path, values, suffix=b""):
    path = tmp_path / "events.jsonl"
    path.write_bytes(b"".join(json.dumps(v).encode() + b"\n" for v in values) + suffix)
    return path


def label(d, action="proceed", human=False, **extra):
    return {d["decision_id"]: {"expected_action": action, "needs_human": human, **extra}}


def test_real_logger_roundtrip_and_determinism(records):
    events = records()
    first = join_events(events)
    assert first == join_events(events[::-1] + events)
    metrics = summarize(first, label(events[0]))
    assert metrics["decisions"] == metrics["paired_outcomes"] == 1
    assert metrics["accuracy"] == {"count": 1, "denominator": 1, "rate": 1}
    assert metrics["measured_cost_usd"]["judge"] is None


def test_exact_percentiles_denominators_and_unknown_cost(records):
    base = records(outcome=False)[0]
    events = [dict(base, decision_id=str(uuid4()), latency_ms=n) for n in range(1, 21)]
    events[0].update(effective_action="human", backend=None)
    events[1].update(answers=None, failure="timeout", intended_action=None)
    labels = {**label(events[0]), **label(events[1], action="human", human=True, worker_cost_usd=2.5)}
    metrics = summarize(join_events(events), labels)
    assert metrics["latency_ms"] == {"p50": 10, "p95": 19}
    assert metrics["failures"] == metrics["fallbacks"] == metrics["escalations"] == 1
    assert metrics["coverage"] == .1 and metrics["unlabeled"] == 18
    assert metrics["false_escalate"] == metrics["false_proceed"] == {"count": 1, "denominator": 1, "rate": 1}
    assert metrics["measured_cost_usd"]["worker"] == 2.5
    assert metrics["measured_cost_usd"]["judge"] is None


@pytest.mark.parametrize("claimed,eligible", [("sample-1", True), ("sample-2", False), (None, False)])
def test_shadow_attribution(records, claimed, eligible):
    events = records(shadow=True, claimed=claimed)
    metrics = summarize(join_events(events), label(events[0]))
    assert metrics["accuracy"]["denominator"] == int(eligible)
    assert metrics["excluded_shadow"] == int(not eligible)


def test_shadow_without_outcome_cannot_score(records):
    events = records(shadow=True, outcome=False)
    assert summarize(join_events(events), label(events[0]))["accuracy"]["denominator"] == 0


def test_empty_input():
    metrics = summarize(join_events([]), {})
    assert metrics["decisions"] == metrics["unlabeled"] == 0
    assert metrics["latency_ms"] == {"p50": None, "p95": None}
    assert metrics["accuracy"]["rate"] is None


def test_partial_last_line_warns_but_complete_unterminated_line_is_valid(tmp_path, records):
    events = records()
    warnings = []
    path = save(tmp_path, events, b'{"secret":"private')
    assert list(read_events(path, warn=warnings.append)) == events
    assert warnings == ["event line 3: ignored incomplete final record"]
    path.write_text(json.dumps(events[0]))
    assert list(read_events(path)) == events[:1]


@pytest.mark.parametrize("change", [
    {"schema_version": 2}, {"schema_version": True}, {"raw_state": "secret"},
    {"latency_ms": -1}, {"latency_ms": True}, {"phase": "secret"},
    {"answers": {"raw": "secret"}}, {"run_id": "secret"}, {"timestamp": "secret"},
    {"failure": "secret"}, {"model": "secret"}, {"input_tokens": -1},
    {"issue_id": "sensitive-value"}, {"reason": "secret"},
])
def test_strict_allowlist_and_safe_errors(tmp_path, records, change, monkeypatch):
    monkeypatch.setenv("REPLAY_TEST_SECRET", "sensitive-value")
    event = {**records(outcome=False)[0], **change}
    with pytest.raises(ReplayError, match="event line 1: invalid version or event shape") as error:
        list(read_events(save(tmp_path, [event])))
    assert "secret" not in str(error.value)


def test_unknown_version_even_on_last_fragment_is_fatal(tmp_path, records):
    event = dict(records(outcome=False)[0], schema_version=2)
    path = save(tmp_path, [])
    path.write_text(json.dumps(event))
    with pytest.raises(ReplayError, match="line 1"):
        list(read_events(path))


@pytest.mark.parametrize("raw", [b"invalid\n", b'{"a":1,"a":2}\n', b'{"latency_ms":NaN}\n', b'\xff\n'])
def test_malformed_nonfinal_records_fail_with_line(tmp_path, raw):
    path = save(tmp_path, [], raw)
    with pytest.raises(ReplayError, match="line 1"):
        list(read_events(path))


def test_line_bound(tmp_path):
    with pytest.raises(ReplayError, match="too large"):
        list(read_events(save(tmp_path, [], b"x" * (MAX_EVENT_BYTES + 1))))


def test_duplicate_conflict_and_run_or_issue_mismatch(records):
    d, o = records(shadow=True)
    for events in ([d, dict(d, latency_ms=11)], [d, dict(o, run_id=str(uuid4()))],
                   [d, dict(o, observed_issue_id="sample-2")]):
        with pytest.raises(ReplayError):
            join_events(events)
    assert join_events([o]).orphan_outcomes == 1


@pytest.mark.parametrize("body", [
    '[]', '{"bad":{}}', '{"a":1,"a":2}',
    lambda key: json.dumps({key: {"expected_action": "secret", "needs_human": False}}),
    lambda key: json.dumps({key: {"expected_action": "human", "needs_human": 1}}),
    lambda key: json.dumps({key: {"expected_action": "human", "needs_human": True, "worker_cost_usd": -1}}),
])
def test_invalid_labels_are_private(tmp_path, body):
    path = tmp_path / "labels.json"
    path.write_text(body(str(uuid4())) if callable(body) else body)
    with pytest.raises(ReplayError, match="invalid labels document"):
        read_labels(path)


def test_explicit_cost_and_usage_never_inferred(records):
    d, o = records()
    d.update(input_tokens=100, output_tokens=20)
    metrics = summarize(join_events([d, o]), {})
    assert metrics["measured_cost_usd"]["judge"] is None
    d["measured_cost_usd"] = .2
    o["measured_cost_usd"] = 3
    metrics = summarize(join_events([d, o]), label(d, worker_cost_usd=4))
    assert metrics["measured_cost_usd"]["judge"] == .2
    assert metrics["measured_cost_usd"]["worker"] == 4


def test_post_turn_from_real_logger(tmp_path):
    class Tracker:
        def show(self, _):
            return {"status": "closed"}
    apply_outcome(Tracker(), tmp_path, "sample-1",
                  WorkerOutcome(0, False, OutcomeStatus.CLOSED, True),
                  OutcomeVerdict(Outcome.DONE, .9), JudgeConfig(enabled=True), uuid4())
    events = list(read_events(tmp_path / "logs" / "jev-decisions.jsonl"))
    assert summarize(join_events(events), {})["post_turn_events"] == 1


def test_cli_export_replay_and_overwrite(tmp_path, records, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("offline command attempted provider call")
    monkeypatch.setattr("ortus.core.judge_typesafe.TypeSafeJudge.evaluate", forbidden)
    events = records()
    source = save(tmp_path, events, b"{")
    target = tmp_path / "export.jsonl"
    runner = CliRunner()
    args = ["judge", "export", str(source), "--output", str(target)]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert result.stdout == ""
    assert "done (2 events exported)" in result.stderr
    assert "ignored incomplete" in result.stderr
    assert list(read_events(target)) == list(join_events(events).events)
    assert os.stat(target).st_mode & 0o777 == 0o600
    before = target.read_bytes()
    assert runner.invoke(app, args).exit_code == 1
    assert target.read_bytes() == before
    assert runner.invoke(app, args + ["--force"]).exit_code == 0
    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps(label(events[0])))
    metrics = tmp_path / "metrics.json"
    result = runner.invoke(app, ["judge", "replay", str(target), "--labels", str(labels), "--output", str(metrics)])
    assert result.exit_code == 0, result.output
    assert result.stdout == "" and "done (1 decisions, 1 labeled)" in result.stderr
    assert json.loads(metrics.read_text())["accuracy"]["rate"] == 1


def test_invalid_event_preserves_output_and_hides_path(tmp_path, records):
    source = save(tmp_path, [dict(records(outcome=False)[0], secret="private")])
    target = tmp_path / "output.jsonl"
    target.write_text("original")
    result = CliRunner().invoke(app, ["judge", "export", str(source), "--output", str(target), "--force"])
    assert result.exit_code == 1
    assert target.read_text() == "original"
    assert str(source) not in result.output and "private" not in result.output


def test_atomic_write_failure_leaves_original_and_no_temp(tmp_path, monkeypatch):
    target = tmp_path / "out"
    target.write_text("original")
    def broken(*args):
        raise OSError("private")
    monkeypatch.setattr(os, "replace", broken)
    with pytest.raises(ReplayError, match="cannot write output"):
        _write_atomic(target, ["new"], True)
    assert target.read_text() == "original"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out"]


def test_no_clobber_race_and_symlink(tmp_path, monkeypatch):
    target = tmp_path / "out"
    real_link = os.link
    def race(src, dst):
        dst.write_text("other writer")
        real_link(src, dst)
    monkeypatch.setattr(os, "link", race)
    with pytest.raises(ReplayError, match="output exists"):
        _write_atomic(target, ["new"], False)
    assert target.read_text() == "other writer"
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(ReplayError):
        _write_atomic(link, ["new"], True)
    assert target.read_text() == "other writer"


@pytest.mark.parametrize("shadow", [False, True])
def test_logger_failure_event_is_supported(records, shadow):
    events = records(shadow=shadow, failure=True)
    metrics = summarize(join_events(events), {})
    assert metrics["failures"] == metrics["fallbacks"] == 1


@pytest.mark.parametrize("shadow", [False, True])
@pytest.mark.parametrize("failure", [False, True])
def test_post_turn_failure_and_shadow_records(tmp_path, shadow, failure):
    class Tracker:
        def show(self, _):
            return {"status": "in_progress"}
        def add_label(self, *args):
            pass
        def add_comment(self, *args):
            pass
    config = JudgeConfig(enabled=True, mode=JudgeMode.SHADOW if shadow else JudgeMode.ENFORCE)
    verdict = OutcomeVerdict(failure=JudgeFailure.TIMEOUT) if failure else OutcomeVerdict(Outcome.PLAN_GAP, .9)
    apply_outcome(Tracker(), tmp_path, "sample-1",
                  WorkerOutcome(1, False, OutcomeStatus.IN_PROGRESS, False), verdict, config, uuid4())
    events = list(read_events(tmp_path / "logs" / "jev-decisions.jsonl"))
    assert summarize(join_events(events), {})["failures"] == int(failure)


def test_labels_bound_and_duplicate_key(tmp_path, monkeypatch):
    path = tmp_path / "labels.json"
    monkeypatch.setattr("ortus.core.judge_replay.MAX_LABEL_BYTES", 5)
    path.write_bytes(b" " * 6)
    with pytest.raises(ReplayError):
        read_labels(path)
    monkeypatch.setattr("ortus.core.judge_replay.MAX_LABEL_BYTES", 1000)
    key = str(uuid4())
    path.write_text('{"' + key + '":{},"' + key + '":{}}')
    with pytest.raises(ReplayError):
        read_labels(path)


def test_cli_refuses_input_replacement_and_missing_input(tmp_path, records):
    path = save(tmp_path, records())
    before = path.read_bytes()
    runner = CliRunner()
    result = runner.invoke(app, ["judge", "export", str(path), "--output", str(path), "--force"])
    assert result.exit_code == 1 and path.read_bytes() == before
    result = runner.invoke(app, ["judge", "export", str(tmp_path / "missing"), "--output", str(path), "--force"])
    assert result.exit_code == 1 and path.read_bytes() == before
    assert str(tmp_path) not in result.output
