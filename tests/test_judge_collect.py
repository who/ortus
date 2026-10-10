"""Collection mode: every Jev site asked in shadow, only code baselines applied."""

import json
from unittest.mock import Mock
from uuid import uuid4

import pytest

from ortus.commands import grind as grind_mod
from ortus.core import judge_collect, judge_readiness
from ortus.core.agent import BackendError
from ortus.core.config import Config
from ortus.core.judge import JudgeConfig, JudgeMode, parse_judge_config
from ortus.core.judge_collect import (
    COLLECT_SITES, collecting_config, collection_arming, collection_note,
)
from ortus.core.judge_hook import load_context
from ortus.core.judge_log import ROUTE_LOG_NAME, OutcomeStatus
from ortus.core.judge_post import Outcome, OutcomeVerdict, WorkerOutcome, apply_outcome
from ortus.core.judge_replay import join_events, read_events, summarize
from ortus.core.judge_stuck import StuckAction, baseline_stuck_action
from ortus.core.judge_triage import TriageClass
from ortus.core.profiles import ProfileError
from tests.test_grind_judge import answer, gate as gate  # noqa: F401 - shared CLI fixture
from tests.test_judge_triage import _FakeBd, _parked, _triage, _vector


def _collect(table, environ):
    return parse_judge_config(Config({'judge': table}), environ=environ).collect


def test_parse_collect_key_defaults_off_and_rejects_garbage():
    assert _collect({}, {}) is False
    assert _collect({'collect': True}, {}) is True
    assert _collect({'collect': False}, {}) is False
    with pytest.raises(ProfileError, match='judge.collect'):
        _collect({'collect': 'yes'}, {})


@pytest.mark.parametrize('configured', [True, False])
@pytest.mark.parametrize('value,expected', [
    ('1', True), ('true', True), (' TRUE ', True), ('0', False), ('false', False),
])
def test_env_overrides_collect_in_both_directions(configured, value, expected):
    assert _collect({'collect': configured}, {'ORTUS_JEV_COLLECT': value}) is expected


@pytest.mark.parametrize('configured', [True, False])
@pytest.mark.parametrize('value', ['', 'yes', 'on', '2'])
def test_unparseable_env_falls_back_to_the_configured_collect(configured, value):
    assert _collect({'collect': configured}, {'ORTUS_JEV_COLLECT': value}) is configured


def test_arming_is_empty_and_the_seat_untouched_without_collect():
    config = JudgeConfig(enabled=True)
    assert collection_arming(None, config, 'claude') == {}
    assert collecting_config(config, {}) is config


def test_arming_forces_shadow_on_every_site(monkeypatch):
    monkeypatch.setattr(judge_collect, 'check_pre_tool', lambda *a, **kw: None)
    config = JudgeConfig(enabled=True, mode=JudgeMode.ENFORCE, collect=True)
    arming = collection_arming(None, config, 'claude')
    assert arming == dict.fromkeys(COLLECT_SITES, True)
    seat = collecting_config(config, arming)
    assert seat.enabled and seat.mode is JudgeMode.SHADOW
    assert seat.pre_tool and seat.post_turn and seat.include_log_tail
    note = collection_note(config, arming)
    assert note.startswith('judge collect: shadow on ' + ', '.join(COLLECT_SITES))
    assert 'mode=enforce overridden' in note


@pytest.mark.parametrize('backend,docker', [('codex', False), ('claude', True)])
def test_arming_reports_pre_tool_unavailable_without_halting(backend, docker):
    config = JudgeConfig(collect=True)
    arming = collection_arming(None, config, backend, docker=docker)
    assert arming['pre_tool'] is False
    assert all(armed for site, armed in arming.items() if site != 'pre_tool')
    assert not collecting_config(config, arming).pre_tool
    note = collection_note(config, arming)
    assert 'pre_tool unavailable' in note and 'overridden' not in note


def test_arming_keeps_an_explicit_pre_tool_refusal():
    config = JudgeConfig(collect=True, pre_tool=True)
    assert collection_arming(None, config, 'codex')['pre_tool'] is True
    with pytest.raises(BackendError):
        grind_mod.check_pre_tool(None, 'codex')


def _seat():
    return collecting_config(
        JudgeConfig(enabled=True, collect=True), dict.fromkeys(COLLECT_SITES, True),
    )


def test_stuck_applies_the_baseline_under_collect(tmp_path, monkeypatch):
    monkeypatch.setattr(grind_mod, '_replan_window_count', lambda bd, issue_id: 0)
    decision, applied = grind_mod._decide_stuck(
        Mock(), tmp_path, _seat(), uuid4(), 'demo-1',
        OutcomeVerdict(Outcome.NEEDS_HUMAN, .99), 1, False, lambda line: None,
    )
    assert decision.action is StuckAction.ESCALATE
    assert applied is baseline_stuck_action(1) is StuckAction.CONTINUE


def test_triage_applies_the_baseline_park_under_collect(tmp_path):
    bd = _FakeBd({'demo-1': _parked()})
    applied, _ = _triage(
        bd, 'demo-1', 'PLAN-GAP: unclear', tmp_path,
        vector=_vector(planner_fix=0.9, needs_human=0.1), config=_seat(),
        respec=Mock(side_effect=AssertionError('collection never re-specs')),
    )
    assert applied is TriageClass.NEEDS_HUMAN


def test_post_turn_preserves_the_tracker_result_as_baseline(tmp_path):
    bd = Mock()
    bd.show.return_value = {'status': 'in_progress', 'labels': []}
    observation = WorkerOutcome(
        exit_status=0, watchdog=False, observed_status=OutcomeStatus.IN_PROGRESS,
        branch_advanced=False,
    )
    stop = apply_outcome(
        bd, tmp_path, 'demo-1', observation, OutcomeVerdict(Outcome.PLAN_GAP, .99),
        _seat(), uuid4(),
    )
    assert stop is False
    bd.add_label.assert_not_called()
    bd.add_comment.assert_not_called()


def _grind(gate, monkeypatch, backend, *flags):
    monkeypatch.setattr(grind_mod, 'resolve_backend', lambda *a, **kw: backend)
    monkeypatch.setattr(judge_readiness, 'TypeSafeJudge', lambda cfg: gate.judge)
    # Readiness asks only about a packet whose text survives packing.
    gate.bd.rows['demo-1']['title'] = 'Ship the preview flag'
    monkeypatch.setenv('ORTUS_JEV_PROGRESS', 'enforce')
    monkeypatch.delenv('ORTUS_JEV_COLLECT', raising=False)
    gate.config.values['judge'].update(collect=True, mode='enforce')
    gate.judge.evaluate.return_value = answer('human')
    result = gate.invoke('--iterations', '1', *flags)
    assert result.exit_code == 0, result.output + str(result.exception)
    return result


def test_grind_arming_applies_only_baselines(gate, monkeypatch):
    result = _grind(gate, monkeypatch, 'codex')
    # An enforcing seat would have bound a routed bundle; collection runs the
    # baseline worker and never routes, claims for, or parks the bead.
    assert not gate.bundles
    gate.worker.run.assert_called_once()
    assert gate.bd.rows['demo-1']['labels'] == []
    assert not any(c in gate.bd.calls for c in ('claim', 'release', 'atomic'))
    assert 'judge collect: shadow on' in result.output
    assert 'pre_tool unavailable' in result.output
    assert 'loop reaper shadow' in result.output
    events = gate.events()
    decisions = [e for e in events if e['event'] == 'decision']
    assert {e['phase'] for e in decisions} == {'pre_turn', 'semantic_readiness'}
    assert all(e['mode'] == 'shadow' and e['effective_action'] == 'baseline'
               for e in decisions)
    route_log = gate.repo / 'logs' / ROUTE_LOG_NAME
    routes = [json.loads(line) for line in route_log.read_text().splitlines()]
    assert routes and all(e['applied'] is False for e in routes)


def test_grind_arming_hands_the_claude_hook_a_shadow_seat(gate, monkeypatch):
    seats = []

    def worker(*a, **kw):
        seats.append(load_context(gate.worker.extra_env).config)
        gate.bd.rows['demo-1'].update(status='closed')
        return 0

    gate.worker.hook_settings = None
    gate.worker.hook_session_id = None
    gate.worker.run.side_effect = worker
    result = _grind(gate, monkeypatch, 'claude')
    assert 'pre_tool unavailable' not in result.output
    (seat,) = seats
    assert seat.pre_tool and seat.mode is JudgeMode.SHADOW


def test_readiness_decision_gets_a_paired_eligible_outcome(gate, monkeypatch):
    _grind(gate, monkeypatch, 'codex')
    events = gate.events()
    (readiness,) = [e for e in events if e.get('phase') == 'semantic_readiness']
    outcomes = [e for e in events if e['event'] == 'outcome'
                and e['decision_id'] == readiness['decision_id']]
    assert len(outcomes) == 1
    assert outcomes[0]['run_id'] == readiness['run_id']
    assert outcomes[0]['accuracy_eligible'] is True
    data = join_events(read_events(gate.repo / 'logs' / 'jev-decisions.jsonl'))
    summary = summarize(data, {})
    assert summary['excluded_shadow'] == 0
    assert summary['accuracy_eligible'] == summary['decisions'] == 2


def test_readiness_outcome_is_not_written_without_collect(gate, monkeypatch):
    monkeypatch.setattr(judge_readiness, 'TypeSafeJudge', lambda cfg: gate.judge)
    gate.bd.rows['demo-1']['title'] = 'Ship the preview flag'
    gate.config.values['judge']['mode'] = 'shadow'
    result = gate.invoke('--iterations', '1')
    assert result.exit_code == 0, result.output + str(result.exception)
    events = gate.events()
    (readiness,) = [e for e in events if e.get('phase') == 'semantic_readiness']
    assert not [e for e in events if e['event'] == 'outcome'
                and e['decision_id'] == readiness['decision_id']]
