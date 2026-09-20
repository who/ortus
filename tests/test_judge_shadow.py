"""Shadow observations must leave the ordinary worker contract intact."""

from copy import deepcopy
from dataclasses import replace
from unittest.mock import Mock

import pytest

from ortus.commands import grind as grind_mod
from ortus.core.config import Config, load_config
from ortus.core.judge import JudgeMode, parse_judge_config
from ortus.core.judge_log import JudgeLogError, LogFailure
from ortus.core.judge_typesafe import JudgeFailure, JudgeVerdict
from ortus.core.profiles import ProfileError
from tests.test_grind_judge import answer, gate as gate
from tests.test_readiness import ready_issue


VERDICTS = [answer(route) for route in ('claude', 'codex', 'human', 'skip')]
VERDICTS += [JudgeVerdict(failure=failure) for failure in JudgeFailure]
VERDICTS += [JudgeVerdict(answers=replace(answer('codex').answers, route_confidence=.1))]
VERDICTS += [JudgeVerdict(answers=replace(answer('codex').answers, action_risk=2))]


def test_mode_parsing_and_kill_switch(tmp_path):
    (tmp_path / '.ortusrc').write_text('[judge]\nenabled=true\nmode="shadow"\n')
    cfg = load_config(repo=tmp_path, home=tmp_path / 'home')
    assert parse_judge_config(cfg, environ={}).mode is JudgeMode.SHADOW
    assert not parse_judge_config(cfg, judge=False, environ={'ORTUS_JUDGE_ENABLED': 'true'}).enabled
    assert parse_judge_config(Config(), environ={}).mode is JudgeMode.ENFORCE
    with pytest.raises(ProfileError, match='judge.mode'):
        parse_judge_config(Config({'judge': {'mode': 'observe'}}), environ={})


@pytest.mark.parametrize('verdict', VERDICTS)
@pytest.mark.parametrize('backend', ['claude', 'codex'])
def test_shadow_matches_baseline_for_all_decisions(gate, monkeypatch, verdict, backend):
    monkeypatch.setattr(grind_mod, 'resolve_backend', lambda *a, **kw: backend)
    initial = deepcopy(gate.bd.rows)
    # Readiness filtering must keep its baseline mutations, too.
    gate.bd.rows = {'unready': dict(id='unready', status='open', labels=[], issue_type='task'),
                    **initial}
    initial = deepcopy(gate.bd.rows)
    gate.config.values['judge'].update(mode='shadow', failure_mode='closed')

    def observe(*flags):
        result = gate.invoke('--tasks', '1', *flags)
        assert result.exit_code == 0, result.output + str(result.exception)
        gate.worker.run.assert_called_once()
        call = gate.worker.run.call_args
        return (call.args[0], call.kwargs['profile'], call.kwargs['timeout'],
                deepcopy(gate.worker.extra_env), deepcopy(gate.bd.calls), deepcopy(gate.bd.rows))

    baseline = observe('--no-judge')
    gate.judge.evaluate.assert_not_called()
    gate.worker.run.reset_mock()
    gate.bd.rows = deepcopy(initial)
    gate.bd.calls.clear()
    gate.judge.evaluate.return_value = verdict
    assert observe() == baseline
    gate.judge.evaluate.assert_called_once()
    assert not gate.bundles
    decision, outcome = gate.events()
    assert decision['effective_action'] == 'baseline'
    assert decision['observed_issue_id'] == 'demo-1'
    expected = 'human' if verdict.failure else verdict.answers.route.value
    if verdict.answers and (verdict.answers.route_confidence < .8 or verdict.answers.action_risk >= 1.5):
        expected = 'human'
    assert decision['intended_action'] == ('proceed' if expected in ('claude', 'codex') else expected)
    assert decision['backend'] == (expected if expected in ('claude', 'codex') else None)
    assert outcome['decision_id'] == decision['decision_id']
    assert outcome['actual_claimed_id'] == 'demo-1'
    assert outcome['accuracy_eligible'] is True
    assert outcome['attribution_mismatch'] is False
    assert outcome['observed_status'] == 'closed'


@pytest.mark.parametrize('claim', ['other', 'absent', 'ambiguous', 'resumed'])
def test_shadow_records_only_attributable_outcomes(gate, claim):
    gate.config.values['judge']['mode'] = 'shadow'
    if claim == 'resumed':
        gate.bd.rows['demo-1'].update(status='in_progress', assignee='original')
    else:
        gate.bd.rows['other'] = dict(ready_issue('other'), status='open', labels=[])

        def worker(*a, **kw):
            if claim in ('other', 'ambiguous'):
                gate.bd.rows['other']['status'] = 'closed'
            if claim == 'ambiguous':
                gate.bd.rows['demo-1']['status'] = 'closed'
            return 0

        gate.worker.run.side_effect = worker
    result = gate.invoke('--iterations', '1')
    assert result.exit_code == 0, result.output + str(result.exception)
    decision, outcome = gate.events()
    assert decision['observed_issue_id'] == 'demo-1'
    assert outcome['actual_claimed_id'] == {'other': 'other', 'resumed': 'demo-1'}.get(claim)
    assert outcome['attribution_mismatch'] is (claim == 'other')
    assert outcome['accuracy_eligible'] is (claim == 'resumed')
    assert outcome['observed_status'] == ('closed' if claim == 'resumed' else None)
    assert not any(c in gate.bd.calls for c in ('claim', 'release', 'atomic'))


@pytest.mark.parametrize('flags', [('--dry-run',), ('--no-judge', '--tasks', '1')])
def test_shadow_preview_and_kill_switch_never_observe(gate, flags):
    gate.config.values['judge']['mode'] = 'shadow'
    result = gate.invoke(*flags)
    assert result.exit_code == 0, result.output + str(result.exception)
    gate.judge.evaluate.assert_not_called()
    assert gate.events() == []
    assert 'Bound issue contract' not in result.output
    assert not gate.bundles


@pytest.mark.parametrize('failure', ['provider', 'preparation', 'decision_log', 'outcome_log'])
def test_shadow_errors_never_stop_baseline(gate, monkeypatch, failure):
    gate.config.values['judge'].update(mode='shadow', failure_mode='closed')
    error = RuntimeError('private exception body')
    if failure == 'provider':
        gate.judge.evaluate.side_effect = error
    elif failure == 'preparation':
        monkeypatch.setattr(grind_mod, 'plan_routes', Mock(side_effect=error))
    else:
        monkeypatch.setattr(grind_mod, 'write_decision' if failure == 'decision_log' else 'write_shadow_outcome',
                            Mock(side_effect=JudgeLogError(LogFailure.IO_ERROR)))
    result = gate.invoke('--tasks', '1')
    assert result.exit_code == 0, result.output + str(result.exception)
    gate.worker.run.assert_called_once()
    assert gate.bd.rows['demo-1']['status'] == 'closed'
    assert gate.bd.calls == []
    assert 'private exception body' not in result.output
    if failure == 'provider':
        assert gate.events()[0]['failure'] == 'service_error'
        assert gate.events()[0]['effective_action'] == 'baseline'


def test_shadow_missing_key_uses_adapter_without_enforcing(gate, monkeypatch):
    from ortus.core.judge_typesafe import TypeSafeJudge
    gate.config.values['judge'].update(mode='shadow', failure_mode='closed')
    monkeypatch.setattr(grind_mod, 'TypeSafeJudge', lambda cfg: TypeSafeJudge(cfg, environ={}))
    result = gate.invoke('--tasks', '1')
    assert result.exit_code == 0, result.output + str(result.exception)
    assert gate.events()[0]['failure'] == 'key_missing'
    assert gate.events()[0]['intended_action'] == 'human'
    gate.worker.run.assert_called_once()


def test_shadow_metadata_redacts_actual_claim(gate, monkeypatch):
    gate.config.values['judge']['mode'] = 'shadow'
    monkeypatch.setenv('SAMPLE_SECRET', 'private-claim')
    gate.bd.rows['private-claim'] = dict(ready_issue('private-claim'), status='open', labels=[])
    gate.worker.run.side_effect = lambda *a, **kw: gate.bd.rows['private-claim'].update(status='closed') or 0
    result = gate.invoke('--iterations', '1')
    assert result.exit_code == 0, result.output + str(result.exception)
    assert 'private-claim' not in (gate.repo / 'logs/jev-decisions.jsonl').read_text()
    assert gate.events()[1]['accuracy_eligible'] is False


def test_shadow_observes_before_worker_without_claim_or_route_preparation(gate):
    gate.config.values['judge']['mode'] = 'shadow'

    def evaluate(state):
        assert state.issue_id == 'demo-1'
        assert gate.bd.rows['demo-1']['status'] == 'open'
        assert gate.bd.calls == []
        assert not gate.bundles
        gate.worker.run.assert_not_called()
        return answer('human')

    gate.judge.evaluate.side_effect = evaluate
    result = gate.invoke('--tasks', '1')
    assert result.exit_code == 0, result.output + str(result.exception)
    gate.judge.evaluate.assert_called_once()
    gate.worker.run.assert_called_once()


def test_shadow_empty_queue_does_not_request(gate):
    gate.config.values['judge']['mode'] = 'shadow'
    gate.bd.rows['demo-1']['status'] = 'blocked'
    result = gate.invoke()
    assert result.exit_code == 0, result.exception
    gate.judge.evaluate.assert_not_called()
    assert gate.events() == []


@pytest.mark.parametrize('error', [KeyboardInterrupt, RuntimeError])
def test_shadow_worker_failure_records_only_observed_claim(gate, error):
    gate.config.values['judge']['mode'] = 'shadow'

    def worker(*args, **kwargs):
        gate.bd.rows['demo-1'].update(status='in_progress', assignee='worker')
        raise error()

    gate.worker.run.side_effect = worker
    result = gate.invoke('--iterations', '1')
    assert result.exit_code != 0
    assert gate.bd.rows['demo-1']['status'] == 'in_progress'
    assert gate.bd.rows['demo-1']['assignee'] == 'worker'
    assert gate.bd.calls == []
    assert gate.events()[1]['observed_status'] == 'in_progress'
    assert gate.events()[1]['actual_claimed_id'] == 'demo-1'
