"""Offline outcome policy, transport, and grind lifecycle checks."""
import asyncio
import json
import subprocess
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from ortus.commands import grind as grind_mod
from ortus.core.config import Config
from ortus.core.judge import FailureMode, JudgeConfig, JudgeMode, parse_judge_config
from ortus.core.judge_log import JudgeLogError, LogFailure, OutcomeStatus
from ortus.core.judge_post import (
    Outcome, OutcomeVerdict, WorkerOutcome, apply_outcome,
    evaluate_outcome,
)
from ortus.core.judge_state import StateError
from ortus.core.judge_typesafe import JudgeFailure
from ortus.core.profiles import ProfileError
from tests.test_grind_judge import Tracker, answer, gate  # noqa: F401 - shared CLI fixture


ENV = {'TYPESAFE_API_KEY': 'secret-test-credential'}


def observation(status='in_progress', watchdog=False):
    return WorkerOutcome(143 if watchdog else 0, watchdog, OutcomeStatus(status), False)


def response(choice='continue', confidence=.9):
    return {'model': 'jev-1.13.0', 'answers': {
        'outcome': {'type': 'choice', 'choice': choice, 'confidence': confidence}}}


def client(body, calls):
    async def system_one(*args, **kwargs):
        calls.append((args, kwargs))
        return body
    return SimpleNamespace(system_one=system_one)


def test_config_is_independent_and_strict():
    assert JudgeConfig().post_turn is False
    config = parse_judge_config(Config({'judge': {'post_turn': True}}), environ={})
    assert config.post_turn and not config.enabled and not config.pre_tool
    with pytest.raises(ProfileError):
        parse_judge_config(Config({'judge': {'post_turn': 'yes'}}), environ={})


@pytest.mark.parametrize('choice', list(Outcome))
def test_single_choice_and_sanitized_facts(choice):
    calls = []
    config = JudgeConfig(post_turn=True, include_issue_text=True, include_log_tail=True)
    issue = {'id': 'demo-1', 'status': 'in_progress', 'labels': ['judge-private'],
             'title': 'secret-test-credential', 'description': 'private details',
             'log_tail': 'raw transcript', 'notes': 'secret-test-credential'}
    verdict = evaluate_outcome(issue, observation(), config, environ=ENV,
                               client_factory=lambda _: client(response(choice), calls))
    assert verdict == OutcomeVerdict(choice, .9)
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert set(args[1]) == {'outcome'}
    assert set(args[1]['outcome']['criteria']) == {x.value for x in Outcome}
    assert args[0]['phase'] == 'post_turn'
    assert args[0]['worker'] == observation().payload()
    wire = json.dumps(args[0])
    assert all(text not in wire for text in ('private details', 'raw transcript', ENV['TYPESAFE_API_KEY']))
    assert kwargs['timeout'] == config.timeout_seconds


@pytest.mark.parametrize('bad', [
    {}, {'model': 'wrong', 'answers': response()['answers']},
    {'model': 'jev-1.13.0', 'answers': {}},
    {'model': 'jev-1.13.0', 'answers': dict(response()['answers'], extra={})},
    response('invented'), response(confidence=True), response(confidence=float('nan')),
    response(confidence=1.1), response(confidence=-.1), response(confidence=None),
    {'model': 'jev-1.13.0', 'answers': {'outcome': {'type': 'score', 'score': 1}}},
])
def test_invalid_answers_are_typed_failures(bad):
    result = evaluate_outcome({'id': 'demo-1'}, observation(), JudgeConfig(), environ=ENV,
                              client_factory=lambda _: client(bad, []))
    assert result.failure == JudgeFailure.INVALID_ANSWER


@pytest.mark.parametrize('error,expected', [
    (ImportError(), JudgeFailure.SDK_MISSING),
    (RuntimeError('sensitive provider message'), JudgeFailure.SERVICE_ERROR),
])
def test_service_errors_never_escape(error, expected):
    def factory(_):
        raise error
    verdict = evaluate_outcome({'id': 'demo-1'}, observation(), JudgeConfig(),
                               environ=ENV, client_factory=factory)
    assert verdict == OutcomeVerdict(failure=expected)


def test_key_budget_and_deadline():
    factory = Mock()
    assert evaluate_outcome({'id': 'demo-1'}, observation(), JudgeConfig(),
                            environ={}, client_factory=factory).failure == JudgeFailure.KEY_MISSING
    factory.assert_not_called()
    with pytest.raises(StateError):
        evaluate_outcome({'id': 'demo-1'}, observation(), JudgeConfig(total_bytes_cap=1),
                         environ=ENV, client_factory=factory)
    factory.assert_not_called()
    closed = []

    async def slow(*args, **kwargs):
        await asyncio.sleep(1)

    async def close():
        closed.append(True)

    verdict = evaluate_outcome({'id': 'demo-1'}, observation(),
                               JudgeConfig(timeout_seconds=.01), environ=ENV,
                               client_factory=lambda _: SimpleNamespace(system_one=slow, aclose=close))
    assert verdict.failure == JudgeFailure.TIMEOUT
    assert closed == [True]


@pytest.mark.parametrize('confidence', [True, None, float('inf'), -1, 2])
def test_verdict_contract_rejects_invalid_confidence(confidence):
    with pytest.raises(ValueError):
        OutcomeVerdict(Outcome.CONTINUE, confidence)


@pytest.mark.parametrize('field,value', [('exit_status', True), ('watchdog', 1),
                                       ('observed_status', 'open'), ('branch_advanced', 1)])
def test_observation_requires_typed_facts(field, value):
    with pytest.raises(ValueError):
        replace(observation(), **{field: value})


@pytest.mark.parametrize('status', ['open', 'in_progress', 'closed'])
@pytest.mark.parametrize('choice', list(Outcome))
@pytest.mark.parametrize('shadow', [False, True])
def test_policy_never_changes_status_or_candidate(tmp_path, status, choice, shadow):
    bd = Tracker()
    bd.rows['demo-1']['status'] = status
    candidate = tmp_path / 'candidate.py'
    candidate.write_text('pending work')
    config = JudgeConfig(post_turn=True, mode=JudgeMode.SHADOW if shadow else JudgeMode.ENFORCE)
    apply_outcome(bd, tmp_path, 'demo-1', observation(status), OutcomeVerdict(choice, .9),
                  config, uuid4())
    assert bd.rows['demo-1']['status'] == status
    assert candidate.read_text() == 'pending work'
    human = not shadow and status != 'closed' and choice in {
        Outcome.PLAN_GAP, Outcome.AUTH, Outcome.NEEDS_HUMAN}
    assert ('human' in bd.rows['demo-1']['labels']) == human
    assert 'release' not in bd.calls and 'claim' not in bd.calls
    comments = [c[1] for c in bd.calls if isinstance(c, tuple)]
    assert len(comments) == int(human)
    if human and choice == Outcome.PLAN_GAP:
        assert comments[0].startswith('PLAN-GAP:')
    event = json.loads((tmp_path / 'logs/jev-decisions.jsonl').read_text())
    assert event['intended_outcome'] == ('done' if status == 'closed' else choice.value)
    assert event['disagreement'] == (choice == Outcome.DONE and status != 'closed')
    if shadow:
        assert event['effective_action'] == 'baseline'


@pytest.mark.parametrize('failure', list(JudgeFailure))
@pytest.mark.parametrize('mode', list(FailureMode))
def test_failure_policy(tmp_path, failure, mode):
    bd = Tracker()
    apply_outcome(bd, tmp_path, 'demo-1', observation('open'), OutcomeVerdict(failure=failure),
                  JudgeConfig(failure_mode=mode), uuid4())
    assert ('human' in bd.rows['demo-1']['labels']) == (mode == FailureMode.CLOSED)
    assert bd.rows['demo-1']['status'] == 'open'


@pytest.mark.parametrize('confidence,human', [(.799, True), (.8, False)])
def test_fixed_confidence_boundary(tmp_path, confidence, human):
    bd = Tracker()
    apply_outcome(bd, tmp_path, 'demo-1', observation('open'),
                  OutcomeVerdict(Outcome.CONTINUE, confidence), JudgeConfig(), uuid4())
    assert ('human' in bd.rows['demo-1']['labels']) == human


def test_concurrent_close_overrides_human_and_timeout_overrides_model(tmp_path):
    bd = Tracker()
    bd.rows['demo-1']['status'] = 'closed'
    apply_outcome(bd, tmp_path, 'demo-1', observation(),
                  OutcomeVerdict(Outcome.AUTH, .99), JudgeConfig(), uuid4())
    assert bd.calls == []
    bd.rows['demo-1']['status'] = 'in_progress'
    apply_outcome(bd, tmp_path, 'demo-1', observation(watchdog=True),
                  OutcomeVerdict(Outcome.AUTH, .99), JudgeConfig(), uuid4())
    assert bd.calls == []
    events = [json.loads(s) for s in (tmp_path / 'logs/jev-decisions.jsonl').read_text().splitlines()]
    assert [e['reason'] for e in events] == ['tracker_closed', 'worker_timeout']


def test_log_failure_prevents_annotations(tmp_path, monkeypatch):
    from ortus.core import judge_post
    bd = Tracker()
    monkeypatch.setattr(judge_post, '_append', Mock(side_effect=JudgeLogError(LogFailure.IO_ERROR)))
    with pytest.raises(JudgeLogError):
        apply_outcome(bd, tmp_path, 'demo-1', observation('open'),
                      OutcomeVerdict(Outcome.AUTH, .99), JudgeConfig(), uuid4())
    assert bd.calls == []


@pytest.mark.parametrize('enabled', [False, True])
def test_grind_independent_post_switch(gate, monkeypatch, enabled):
    gate.config.values['judge'].update(enabled=False, post_turn=enabled)
    evaluate = Mock(return_value=OutcomeVerdict(Outcome.AUTH, .99))
    monkeypatch.setattr(grind_mod, 'evaluate_outcome', evaluate)
    result = gate.invoke('--tasks', '1')
    assert result.exit_code == 0, result.output + str(result.exception)
    assert evaluate.call_count == int(enabled)
    assert gate.bd.rows['demo-1']['status'] == 'closed'
    assert 'human' not in gate.bd.rows['demo-1']['labels']
    if enabled:
        assert gate.events()[-1]['reason'] == 'tracker_closed'
    gate.judge.evaluate.assert_not_called()


@pytest.mark.parametrize('choice', [Outcome.FLAKE, Outcome.CONTINUE, Outcome.PLAN_GAP, Outcome.DONE])
def test_grind_leaves_claim_and_counter_for_next_window(gate, monkeypatch, choice):
    gate.config.values['judge']['post_turn'] = True
    gate.bd.rows['demo-1'].update(status='in_progress', assignee='prior')
    gate.worker.run.side_effect = lambda *a, **kw: 0
    evaluate = Mock(return_value=OutcomeVerdict(choice, .99))
    monkeypatch.setattr(grind_mod, 'evaluate_outcome', evaluate)
    counter = Mock()
    monkeypatch.setattr(grind_mod, '_record_no_close_window', counter)
    result = gate.invoke('--iterations', '4')
    assert result.exit_code == 0, result.output + str(result.exception)
    gate.worker.run.assert_called_once()
    evaluate.assert_called_once()
    assert counter.call_args.args[2] == 1
    assert gate.bd.rows['demo-1']['status'] == 'in_progress'
    assert 'release' not in gate.bd.calls


def test_no_worker_no_post_request(gate, monkeypatch):
    gate.config.values['judge']['post_turn'] = True
    gate.judge.evaluate.return_value = answer('skip')
    evaluate = Mock()
    monkeypatch.setattr(grind_mod, 'evaluate_outcome', evaluate)
    result = gate.invoke()
    assert result.exit_code == 0, result.output
    gate.worker.run.assert_not_called()
    evaluate.assert_not_called()


def test_grind_watchdog_is_a_fact_and_handshake_failure_wins(gate, monkeypatch):
    gate.config.values['judge']['post_turn'] = True
    gate.worker.run.side_effect = subprocess.TimeoutExpired('worker', 1)
    evaluate = Mock(return_value=OutcomeVerdict(Outcome.DONE, .99))
    monkeypatch.setattr(grind_mod, 'evaluate_outcome', evaluate)
    result = gate.invoke('--worker-timeout', '1')
    assert result.exit_code == 0, result.output + str(result.exception)
    facts = evaluate.call_args.args[1]
    assert facts.watchdog and facts.exit_status == 143
    assert gate.bd.rows['demo-1']['status'] == 'in_progress'
    assert gate.events()[-1]['reason'] == 'worker_timeout'

    from ortus.core.codegraph import CodeGraphUnavailable
    evaluate.reset_mock()
    monkeypatch.setattr(grind_mod, 'require_handshake', Mock(side_effect=CodeGraphUnavailable('missing')))
    result = gate.invoke('--worker-timeout', '1')
    assert result.exit_code != 0
    evaluate.assert_not_called()


def test_grind_shadow_preserves_baseline(gate, monkeypatch):
    gate.config.values['judge'].update(enabled=False, post_turn=True, mode='shadow')
    gate.worker.run.side_effect = lambda *a, **kw: gate.bd.rows['demo-1'].update(status='in_progress') or 0
    monkeypatch.setattr(grind_mod, 'evaluate_outcome',
                        Mock(return_value=OutcomeVerdict(Outcome.PLAN_GAP, .99)))
    result = gate.invoke()
    assert result.exit_code == 0, result.output + str(result.exception)
    assert gate.bd.rows['demo-1']['labels'] == []
    assert gate.events()[-1]['effective_action'] == 'baseline'


def test_grind_log_error_reports_without_mutation(gate, monkeypatch):
    gate.config.values['judge'].update(enabled=False, post_turn=True)
    gate.worker.run.side_effect = lambda *a, **kw: 0
    monkeypatch.setattr(grind_mod, 'evaluate_outcome',
                        Mock(return_value=OutcomeVerdict(Outcome.AUTH, .99)))
    monkeypatch.setattr(grind_mod, 'apply_outcome',
                        Mock(side_effect=JudgeLogError(LogFailure.IO_ERROR)))
    result = gate.invoke('--iterations', '4', '--idle-sleep', '1')
    assert result.exit_code == 0, result.output + str(result.exception)
    gate.worker.run.assert_called_once()
    assert gate.bd.rows['demo-1']['status'] == 'open'
    assert gate.bd.rows['demo-1']['labels'] == []
    assert 'preserving worker result' in result.output


@pytest.mark.parametrize('choice', [Outcome.FLAKE, Outcome.CONTINUE])
def test_open_outcome_does_not_retry_same_window(gate, monkeypatch, choice):
    gate.config.values['judge'].update(enabled=False, post_turn=True)
    gate.worker.run.side_effect = lambda *a, **kw: 0
    monkeypatch.setattr(grind_mod, 'evaluate_outcome',
                        Mock(return_value=OutcomeVerdict(choice, .99)))
    result = gate.invoke('--iterations', '4', '--idle-sleep', '1')
    assert result.exit_code == 0, result.output + str(result.exception)
    gate.worker.run.assert_called_once()
    assert gate.bd.rows['demo-1']['status'] == 'open'


@pytest.mark.parametrize('mode', ['open', 'closed'])
def test_grind_post_service_failure_keeps_claim(gate, monkeypatch, mode):
    gate.config.values['judge'].update(post_turn=True, failure_mode=mode)
    gate.worker.run.side_effect = lambda *a, **kw: 0
    monkeypatch.setattr(grind_mod, 'evaluate_outcome',
                        Mock(return_value=OutcomeVerdict(failure=JudgeFailure.SERVICE_ERROR)))
    result = gate.invoke()
    assert result.exit_code == 0, result.output + str(result.exception)
    assert gate.bd.rows['demo-1']['status'] == 'in_progress'
    assert ('human' in gate.bd.rows['demo-1']['labels']) == (mode == 'closed')
    assert 'release' not in gate.bd.calls


def test_dry_run_has_no_post_request(gate, monkeypatch):
    gate.config.values['judge'].update(enabled=False, post_turn=True)
    evaluate = Mock()
    monkeypatch.setattr(grind_mod, 'evaluate_outcome', evaluate)
    result = gate.invoke('--dry-run')
    assert result.exit_code == 0, result.output + str(result.exception)
    assert 'judge post_turn: enabled' in result.output
    evaluate.assert_not_called()
    gate.worker.run.assert_not_called()
    assert gate.events() == []
