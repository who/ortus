"""Semantic advice cannot weaken schema validation or become a claim gate."""

import json

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.commands import ingest as ingest_mod, validate as validate_mod
from ortus.commands.grind import _compose_work_prompt
from ortus.commands.plan import _plan_prompt
from ortus.core.config import Config
from ortus.core.judge import (
    FailureMode, JudgeAnswers, JudgeConfig, JudgePhase, JudgeRoute, parse_judge_config,
)
from ortus.core import judge_readiness as semantic
from ortus.core.judge_replay import read_events
from ortus.core.judge_state import pack_state
from ortus.core.judge_typesafe import JudgeFailure, JudgeVerdict, build_questions
from tests.test_readiness import ready_issue


@pytest.fixture
def packet():
    return {**ready_issue(), "title": "Add a preview flag"}


@pytest.fixture
def judge(monkeypatch):
    calls = []

    def evaluate(self, state):
        calls.append(state)
        return JudgeVerdict(answers=JudgeAnswers(JudgeRoute.HUMAN, .1, .9, .1, 2, .1))

    monkeypatch.setattr(semantic.TypeSafeJudge, "evaluate", evaluate)
    return calls


def test_semantic_readiness_logs_low_confidence_without_escalation(tmp_path, packet, judge):
    config = JudgeConfig(enabled=True, failure_mode=FailureMode.CLOSED)
    advice = semantic.evaluate_readiness(tmp_path, packet, config)
    assert advice["status"] == "advisory"
    assert advice["answers"]["route"] == "human"
    assert len(judge) == 1
    assert judge[0].design == packet["design"]
    assert judge[0].acceptance == packet["acceptance_criteria"]
    event, = read_events(tmp_path / "logs/jev-decisions.jsonl")
    assert event["phase"] == "semantic_readiness"
    assert event["effective_action"] == "baseline"
    assert event["answers"]["route_confidence"] == .1
    assert event["criteria_hash"] == judge[0].criteria_hash
    assert packet["title"] not in json.dumps(event)


@pytest.mark.parametrize("change", [{"description": "missing"}, {"issue_type": "epic"}])
def test_semantic_readiness_schema_failure_and_exemption_never_call(tmp_path, packet, judge, change):
    assert semantic.evaluate_readiness(tmp_path, {**packet, **change}, JudgeConfig(enabled=True)) is None
    assert not judge
    assert not (tmp_path / "logs").exists()


@pytest.mark.parametrize("change,settings", [
    ({}, {"include_issue_text": False}),
    ({"labels": ["judge-private"]}, {}),
    ({"title": ""}, {}),
    ({"design": ready_issue()["design"] + "\napi_key=private-value"}, {}),
    ({}, {"objective_cap": 10}),
    ({}, {"total_bytes_cap": 600}),
])
def test_semantic_readiness_requires_all_screened_text(tmp_path, packet, judge, change, settings):
    advice = semantic.evaluate_readiness(tmp_path, {**packet, **change}, JudgeConfig(enabled=True, **settings))
    assert advice["status"] == "text_unavailable"
    assert not judge
    assert not (tmp_path / "logs").exists()


def test_semantic_readiness_disabled_has_no_observation(tmp_path, packet, judge):
    assert semantic.evaluate_readiness(tmp_path, packet, JudgeConfig()) is None
    assert not judge
    assert not (tmp_path / "logs").exists()


@pytest.mark.parametrize("failure", list(JudgeFailure))
def test_semantic_readiness_service_failure_preserves_schema(tmp_path, packet, monkeypatch, failure):
    monkeypatch.setattr(semantic.TypeSafeJudge, "evaluate", lambda *args: JudgeVerdict(failure=failure))
    advice = semantic.evaluate_readiness(tmp_path, packet, JudgeConfig(enabled=True, failure_mode=FailureMode.CLOSED))
    assert advice["status"] == failure.value
    event, = read_events(tmp_path / "logs/jev-decisions.jsonl")
    assert event["effective_action"] == "baseline"
    assert event["failure"] == failure.value


def test_semantic_readiness_log_failure_preserves_ready(tmp_path, packet, judge, monkeypatch):
    monkeypatch.setattr(semantic, "parse_judge_config", lambda *args: JudgeConfig(enabled=True))
    monkeypatch.setattr(semantic, "write_decision", lambda *args: (_ for _ in ()).throw(OSError("private")))
    verdict = validate_mod._verdict_for(packet, tmp_path)
    assert verdict.ok
    assert verdict.semantic_readiness["status"] == "unavailable"


def test_semantic_readiness_criteria_rewrite_changes_hash_only_for_semantic(packet):
    base = JudgeConfig(enabled=True)
    edited = parse_judge_config(Config(values={"judge": {
        "enabled": True,
        "semantic_readiness_criteria": {"needs_human": {
            "true": ["Acceptance has no observable outcome."],
            "false": ["Acceptance describes an observable outcome."],
        }},
    }}), environ={})
    states = [pack_state(packet, cfg, phase=JudgePhase.SEMANTIC_READINESS).state for cfg in (base, edited)]
    assert states[0].criteria_hash != states[1].criteria_hash
    assert build_questions(edited, states[1])["needs_human"]["criteria"]["true"] == ["Acceptance has no observable outcome."]
    assert pack_state(packet, base).state.criteria_hash == pack_state(packet, edited).state.criteria_hash


def test_semantic_readiness_validate_json_and_ingest_stdout(tmp_path, packet, judge, monkeypatch):
    (tmp_path / ".beads").mkdir()
    class Tracker:
        repo = tmp_path

        def show(self, issue_id):
            return {**packet, "id": issue_id}

        def create(self, **fields):
            return "demo-2"

    monkeypatch.setattr(validate_mod, "_make_bd", lambda _: Tracker())
    monkeypatch.setattr(ingest_mod, "_make_bd", lambda _: Tracker())
    monkeypatch.setattr(semantic, "parse_judge_config", lambda *args: JudgeConfig(enabled=True))
    cli = CliRunner()
    result = cli.invoke(app, ["validate", str(tmp_path), "demo-1", "--json"])
    assert result.exit_code == 0, result.output
    row, = json.loads(result.stdout)["issues"]
    assert row["ok"] and row["semantic_readiness"]["answers"]["route"] == "human"
    result = cli.invoke(app, ["ingest", str(tmp_path), "--stdin"], input=json.dumps(packet))
    assert result.exit_code == 0, result.output
    assert result.stdout == "demo-2\n"
    assert "semantic readiness advice" in result.stderr
    assert len(judge) == 2


def test_semantic_readiness_enters_worker_and_planner_context(tmp_path, packet, judge):
    advice = semantic.readiness_context(semantic.evaluate_readiness(tmp_path, packet, JudgeConfig(enabled=True)))
    for backend in ("codex", "claude"):
        prompt = _compose_work_prompt("", packet, backend, semantic_advice=advice)
        assert advice in prompt
        assert '"route":"human"' in prompt
        assert "not a stop or claim instruction" in prompt
    assert "semantic_readiness" in _plan_prompt(tmp_path)
    assert "ortus validate --json" in _plan_prompt(tmp_path)
