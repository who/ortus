"""Exercise gate decisions through the grind CLI without a provider or worker process."""

from copy import deepcopy
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.commands import grind as grind_mod
from ortus.core.agent import BackendError
from ortus.core.codegraph import CodeGraphMode, CodeGraphProbe
from ortus.core.config import Config
from ortus.core.judge import JudgeAnswers, JudgeRoute
from ortus.core.judge_routing import ExecutionBundle, RoutePlan
from ortus.core.judge_typesafe import JudgeFailure, JudgeVerdict
from ortus.core.profiles import Phase
from tests.test_readiness import ready_issue


class Tracker:
    def __init__(self):
        self.rows = {"demo-1": dict(ready_issue(), status="open", labels=[])}
        self.calls = []
        self.issue_comments = {}

    def show(self, issue_id):
        return deepcopy(self.rows[issue_id])

    def list_all(self):
        return [self.show(i) for i in self.rows]

    def list_ready(self, *, exclude_labels=()):
        return [r for r in self.list_all() if r['status'] == 'open'
                and not set(r['labels']).intersection(exclude_labels)]

    def in_progress_ids(self, *, exclude_labels=()):
        return {i for i, r in self.rows.items() if r['status'] == 'in_progress'
                and not set(r['labels']).intersection(exclude_labels)}

    def count_by_status(self, status, *, exclude_labels=()):
        return sum(r['status'] == status and not set(r['labels']).intersection(exclude_labels)
                   for r in self.rows.values())

    def closed_ids(self):
        return {i for i, r in self.rows.items() if r['status'] == 'closed'}

    def require_atomic_claims(self):
        self.calls.append('atomic')

    def claim(self, issue_id, actor):
        self.calls.append('claim')
        self.rows[issue_id].update(status='in_progress', assignee=actor)
        return self.show(issue_id)

    def release_claim(self, issue_id, actor):
        self.calls.append('release')
        row = self.rows[issue_id]
        if row['status'] != 'in_progress' or row['assignee'] != actor:
            raise BackendError('claim changed')
        row.update(status='open', assignee='')

    def add_label(self, issue_id, label):
        self.calls.append('label')
        self.rows[issue_id]['labels'].append(label)

    def add_comment(self, issue_id, text):
        self.calls.append(('comment', text))
        self.issue_comments.setdefault(issue_id, []).append({'text': text})

    def lessons(self, **kwargs):
        return ()

    def comments(self, issue_id):
        return deepcopy(self.issue_comments.get(issue_id, []))

    def open_ids(self, **kwargs):
        return set()


def answer(route):
    return JudgeVerdict(answers=JudgeAnswers(JudgeRoute(route), .99, .01, .99, 0, .99))


@pytest.fixture
def gate(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    repo.mkdir()
    (repo / '.beads').mkdir()
    tracker = Tracker()
    config = Config({'codegraph': 'off', 'judge': {'enabled': True}})
    monkeypatch.setattr(grind_mod, 'load_config', lambda **kw: config)
    monkeypatch.setattr(grind_mod, 'resolve_backend', lambda *a, **kw: 'claude')
    monkeypatch.setattr(grind_mod, '_make_bd', lambda repo: tracker)
    git = Mock()
    git.is_git_repo.return_value = True
    git.has_commits.return_value = True
    git.current_branch.return_value = 'main'
    git.branch_tip.return_value = 'abc'
    git.dirty_paths.return_value = set()
    monkeypatch.setattr(grind_mod, '_make_git', lambda repo: git)
    monkeypatch.setattr(grind_mod, '_enforce_branch_discipline', lambda *a, **kw: None)
    monkeypatch.setattr(grind_mod, '_checkpoint_codex_preflight', lambda *a, **kw: None)
    monkeypatch.setattr(grind_mod.hooks, 'check_hooks_enabled', lambda *a: None)
    monkeypatch.setattr(grind_mod.sandbox, 'smoke_test', lambda: None)
    probe = CodeGraphProbe(CodeGraphMode.OFF, False, False, False)
    adapter = Mock()
    adapter.probe.return_value = probe
    monkeypatch.setattr(grind_mod, '_make_codegraph', lambda: adapter)
    monkeypatch.setattr(grind_mod, '_stale_completion_contract_diagnostic', lambda *a, **kw: None)
    worker = Mock(extra_env={})
    worker.run.side_effect = lambda *a, **kw: tracker.rows['demo-1'].update(status='closed') or 0
    monkeypatch.setattr(grind_mod, '_make_runner', lambda *a, **kw: worker)
    bundles = {}

    def plan(cfg, judge, baseline, *, overrides):
        return RoutePlan(JudgeRoute(baseline), (JudgeRoute.CLAUDE, JudgeRoute.CODEX),
                         tuple(JudgeRoute), overrides)

    def prepare(plan, route, **kwargs):
        tracker.calls.append('preflight-' + route.value)
        selected = Mock(extra_env={})
        selected.run.side_effect = lambda *a, **kw: worker.run(*a, **kw)
        bundle = ExecutionBundle(route, selected,
                                 *(config.resolve_profile(route.value, p) for p in
                                   (Phase.IMPLEMENT, Phase.VERIFY, Phase.FINALIZE)), probe)
        bundles[route] = bundle
        return bundle

    monkeypatch.setattr(grind_mod, 'plan_routes', plan)
    monkeypatch.setattr(grind_mod, 'prepare_route', prepare)
    judge = Mock()
    judge.evaluate.return_value = answer('codex')
    monkeypatch.setattr(grind_mod, 'TypeSafeJudge', lambda cfg: judge)
    monkeypatch.delenv('ORTUS_JUDGE_ENABLED', raising=False)
    monkeypatch.setattr(grind_mod.Path, 'home', classmethod(lambda cls: tmp_path / 'home'))

    def invoke(*args):
        return CliRunner().invoke(app, ['grind', str(repo), '--idle-sleep', '0', *args])

    def events():
        path = repo / 'logs' / 'jev-decisions.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    return SimpleNamespace(repo=repo, bd=tracker, config=config, worker=worker,
                           judge=judge, bundles=bundles, invoke=invoke, events=events)


def test_proceed_binds_selected_runner_and_logs_close(gate):
    result = gate.invoke('--tasks', '1')
    assert result.exit_code == 0, result.output + str(result.exception)
    gate.judge.evaluate.assert_called_once()
    bundle = gate.bundles[JudgeRoute.CODEX]
    prompt = bundle.runner.run.call_args.args[0]
    assert 'Bound issue contract v1' in prompt and '"demo-1"' in prompt
    assert not prompt.startswith('/goal ')
    assert bundle.runner.extra_env['BEADS_ACTOR'] == gate.bd.rows['demo-1']['assignee']
    assert bundle.runner.run.call_args.kwargs['profile'].backend == 'codex'
    assert bundle.runner.run.call_args.kwargs['timeout'] == 5400
    assert callable(bundle.runner.run.call_args.kwargs['on_poll'])
    assert gate.bd.calls.index('claim') > gate.bd.calls.index('preflight-codex')
    assert 'release' not in gate.bd.calls
    decision, outcome = gate.events()
    assert decision['effective_action'] == 'proceed'
    assert outcome['observed_status'] == 'closed'
    assert outcome['decision_id'] == decision['decision_id']


@pytest.mark.parametrize('resumed', [False, True])
def test_skip_preserves_inherited_work(gate, resumed):
    if resumed:
        gate.bd.rows['demo-1'].update(status='in_progress', assignee='previous-worker')
    (gate.repo / 'candidate.py').write_text('pending work')
    gate.judge.evaluate.return_value = answer('skip')
    result = gate.invoke('--iterations', '4')
    assert result.exit_code == 0, result.output + str(result.exception)
    gate.worker.run.assert_not_called()
    row = gate.bd.rows['demo-1']
    assert row['status'] == ('in_progress' if resumed else 'open')
    assert ('release' in gate.bd.calls) == (not resumed)
    assert 'human' not in row['labels']
    assert not any(isinstance(c, tuple) for c in gate.bd.calls)
    assert (gate.repo / 'candidate.py').read_text() == 'pending work'
    assert 'iters_run=0' in next((gate.repo / 'logs').glob('grind-*')).read_text()
    assert gate.events()[1]['observed_status'] == row['status']


@pytest.mark.parametrize('resumed', [False, True])
def test_human_route_runs_the_baseline_and_keeps_the_answer(gate, resumed):
    if resumed:
        gate.bd.rows['demo-1'].update(status='in_progress', assignee='previous-worker')
    gate.judge.evaluate.return_value = answer('human')
    result = gate.invoke('--tasks', '1')
    assert result.exit_code == 0, result.output + str(result.exception)
    assert gate.bundles[JudgeRoute.CLAUDE].runner.run.called
    assert not gate.bundles[JudgeRoute.CODEX].runner.run.called
    assert gate.bd.rows['demo-1']['labels'] == []
    assert not any(isinstance(c, tuple) for c in gate.bd.calls)
    decision = gate.events()[0]
    assert (decision['intended_action'], decision['effective_action']) == ('human', 'proceed')
    assert (decision['backend'], decision['reason']) == ('claude', 'routed')


@pytest.mark.parametrize('failure', list(JudgeFailure))
@pytest.mark.parametrize('mode', ['open', 'closed'])
def test_service_failure_policy(gate, failure, mode):
    gate.config.values['judge']['failure_mode'] = mode
    gate.judge.evaluate.return_value = JudgeVerdict(failure=failure)
    result = gate.invoke('--tasks', '1')
    assert result.exit_code == 0, result.output + str(result.exception)
    assert gate.worker.run.call_count == (mode == 'open')
    assert gate.events()[0]['failure'] == failure.value
    if mode == 'open':
        assert gate.bundles[JudgeRoute.CLAUDE].runner.run.called
    else:
        assert gate.bd.rows['demo-1']['status'] == 'open'
        assert gate.bd.rows['demo-1']['labels'] == ['human']


@pytest.mark.parametrize('resumed', [False, True])
def test_interrupt_releases_only_unused_fresh_claim(gate, resumed):
    if resumed:
        gate.bd.rows['demo-1'].update(status='in_progress', assignee='previous-worker')
    gate.judge.evaluate.side_effect = KeyboardInterrupt
    result = gate.invoke()
    assert result.exit_code != 0
    assert gate.bd.rows['demo-1']['status'] == ('in_progress' if resumed else 'open')
    gate.worker.run.assert_not_called()


def test_timeout_preserves_consumed_claim_and_records_outcome(gate):
    gate.worker.run.side_effect = subprocess.TimeoutExpired('worker', 1)
    result = gate.invoke('--worker-timeout', '1', '--iterations', '1')
    assert result.exit_code == 0, result.output + str(result.exception)
    assert gate.bd.rows['demo-1']['status'] == 'in_progress'
    assert 'release' not in gate.bd.calls
    assert gate.events()[1]['observed_status'] == 'in_progress'


@pytest.mark.parametrize('change', ['closed', 'owner', 'human'])
def test_mutation_during_judgment_halts_without_overwriting(gate, change):
    def evaluate(state):
        row = gate.bd.rows['demo-1']
        if change == 'closed':
            row['status'] = 'closed'
        elif change == 'owner':
            row['assignee'] = 'other-worker'
        else:
            row['labels'].append('human')
        return answer('codex')
    gate.judge.evaluate.side_effect = evaluate
    result = gate.invoke()
    assert result.exit_code != 0
    gate.worker.run.assert_not_called()
    row = gate.bd.rows['demo-1']
    if change == 'closed':
        assert row['status'] == 'closed'
    elif change == 'owner':
        assert row['assignee'] == 'other-worker'
    else:
        assert row['labels'] == ['human']


def test_attribution_ignores_unrelated_close(gate):
    gate.bd.rows['other'] = dict(ready_issue('other'), status='blocked', labels=[])
    gate.worker.run.side_effect = lambda *a, **kw: gate.bd.rows['other'].update(status='closed') or 0
    result = gate.invoke('--tasks', '1', '--iterations', '1')
    assert result.exit_code == 0, result.output + str(result.exception)
    assert gate.events()[1]['observed_status'] == 'in_progress'
    assert '0 landed' in result.output


@pytest.mark.parametrize('args', [('--dry-run',), ('--condition', 'custom')])
def test_preview_and_legacy_do_not_claim_or_request(gate, args):
    result = gate.invoke(*args)
    assert result.exit_code == (0 if args[0] == '--dry-run' else 1), result.exception
    assert not gate.bd.calls
    gate.judge.evaluate.assert_not_called()
    assert gate.events() == []


def test_no_ready_queue_makes_no_request(gate):
    gate.bd.rows['demo-1']['status'] = 'blocked'
    result = gate.invoke()
    assert result.exit_code == 0, result.exception
    gate.judge.evaluate.assert_not_called()
    assert not gate.bd.calls


def test_disabled_flag_overrides_enabled_config(gate):
    result = gate.invoke('--no-judge', '--tasks', '1')
    assert result.exit_code == 0, result.output + str(result.exception)
    gate.judge.evaluate.assert_not_called()
    gate.worker.run.assert_called_once()
    assert 'Bound issue contract' not in gate.worker.run.call_args.args[0]
    assert gate.events() == []


def test_route_preflight_failure_flags_human_before_claim(gate, monkeypatch):
    from ortus.core.judge_routing import RoutePreparationError
    monkeypatch.setattr(grind_mod, 'prepare_route', Mock(side_effect=RoutePreparationError('unavailable')))
    result = gate.invoke()
    assert result.exit_code == 1
    assert gate.bd.rows['demo-1']['status'] == 'open'
    assert gate.bd.rows['demo-1']['labels'] == ['human']
    assert 'claim' not in gate.bd.calls
    gate.judge.evaluate.assert_not_called()
    gate.worker.run.assert_not_called()


def test_log_failure_releases_unused_claim_and_never_fails_open(gate, monkeypatch):
    from ortus.core.judge_log import JudgeLogError, LogFailure
    monkeypatch.setattr(grind_mod, 'write_decision', Mock(side_effect=JudgeLogError(LogFailure.IO_ERROR)))
    result = gate.invoke()
    assert result.exit_code == 1
    assert gate.bd.rows['demo-1']['status'] == 'open'
    assert 'judge log failed' in result.output
    gate.worker.run.assert_not_called()


def test_cleanup_failure_is_visible(gate, monkeypatch):
    gate.judge.evaluate.return_value = answer('skip')
    monkeypatch.setattr(gate.bd, 'release_claim', Mock(side_effect=BackendError('cannot release')))
    result = gate.invoke()
    assert result.exit_code == 1
    assert 'judge claim cleanup failed' in result.output
    assert gate.bd.rows['demo-1']['status'] == 'in_progress'
    gate.worker.run.assert_not_called()


def test_interrupt_after_launch_keeps_claim_and_logs_observation(gate):
    gate.worker.run.side_effect = KeyboardInterrupt
    result = gate.invoke()
    assert result.exit_code != 0
    assert gate.bd.rows['demo-1']['status'] == 'in_progress'
    assert 'release' not in gate.bd.calls
    assert gate.events()[1]['observed_status'] == 'in_progress'


def test_prompt_failure_cleans_up_before_any_worker(gate, monkeypatch):
    monkeypatch.setattr(grind_mod, '_compose_work_prompt', Mock(side_effect=BackendError('prompt cannot bind')))
    result = gate.invoke()
    assert result.exit_code == 1
    assert gate.bd.rows['demo-1']['status'] == 'open'
    gate.worker.run.assert_not_called()


def test_claim_rechecked_after_prompt_preparation(gate, monkeypatch):
    def lessons(*args):
        gate.bd.rows['demo-1']['assignee'] = 'competitor'
        return ()
    monkeypatch.setattr(grind_mod, '_selected_lessons', lessons)
    result = gate.invoke()
    assert result.exit_code == 1
    assert gate.bd.rows['demo-1']['assignee'] == 'competitor'
    gate.worker.run.assert_not_called()


def test_ready_leaf_leaves_other_unready_packets_alone(gate):
    gate.bd.rows = {'unready': {'id': 'unready', 'status': 'open', 'labels': [], 'issue_type': 'task'},
                    **gate.bd.rows}
    result = gate.invoke('--tasks', '1')
    assert result.exit_code == 0, result.output + str(result.exception)
    assert gate.bd.rows['unready']['labels'] == []
    assert gate.bd.rows['unready']['status'] == 'open'


def test_multiple_inherited_claims_stop_before_judgment(gate):
    gate.bd.rows['demo-1'].update(status='in_progress', assignee='previous')
    gate.bd.rows['demo-2'] = dict(ready_issue('demo-2'), status='in_progress',
                                assignee='previous', labels=[])
    result = gate.invoke()
    assert result.exit_code == 1
    assert all(row['status'] == 'in_progress' and row['labels'] == ['human']
               for row in gate.bd.rows.values())
    gate.judge.evaluate.assert_not_called()


def test_environment_can_enable_gate_and_flag_can_disable_it(gate, monkeypatch):
    gate.config.values['judge']['enabled'] = False
    monkeypatch.setenv('ORTUS_JUDGE_ENABLED', 'true')
    result = gate.invoke('--tasks', '1')
    assert result.exit_code == 0, result.exception
    gate.judge.evaluate.assert_called_once()


def test_resumed_proceed_keeps_assignee_and_bound_id(gate):
    gate.bd.rows['demo-1'].update(status='in_progress', assignee='previous')
    result = gate.invoke('--tasks', '1')
    assert result.exit_code == 0, result.output + str(result.exception)
    assert gate.bundles[JudgeRoute.CODEX].runner.extra_env['BEADS_ACTOR'] == 'previous'
    assert 'claim' not in gate.bd.calls
    assert gate.events()[1]['observed_status'] == 'closed'


def test_claude_reaping_tracks_only_bound_issue(gate):
    gate.judge.evaluate.return_value = answer('claude')
    gate.bd.rows['other'] = dict(ready_issue('other'), status='blocked', labels=[])
    git = grind_mod._make_git(gate.repo)
    git.remote_tip.return_value = 'abc'
    git.local_ahead_of_remote.return_value = 0

    def worker(*args, **kwargs):
        reap = kwargs['reap_when']
        gate.bd.rows['other'].update(status='closed')
        assert reap() is False
        gate.bd.rows['other'].update(status='in_progress', labels=['human'])
        assert reap() is False
        gate.bd.rows['demo-1']['status'] = 'closed'
        assert reap() is True
        return 0

    gate.worker.run.side_effect = worker
    result = gate.invoke('--tasks', '1')
    assert result.exit_code == 0, result.output + str(result.exception)
    assert gate.events()[1]['observed_status'] == 'closed'


def test_claude_reaps_bound_human_without_releasing_it(gate):
    gate.judge.evaluate.return_value = answer('claude')

    def worker(*args, **kwargs):
        gate.bd.rows['demo-1']['labels'].append('human')
        assert kwargs['reap_when']() is True
        return 0

    gate.worker.run.side_effect = worker
    result = gate.invoke()
    assert result.exit_code == 0, result.output + str(result.exception)
    assert gate.bd.rows['demo-1']['status'] == 'in_progress'
    assert 'release' not in gate.bd.calls


def leftover(gate, *, windows=0):
    """A claim grind will resume, carrying `windows` recorded no-close windows."""
    gate.bd.rows['demo-1'].update(status='in_progress', assignee='prior')
    if windows:
        gate.bd.add_comment('demo-1', f'{grind_mod._NO_CLOSE_MARKER_PREFIX}{windows}')
        gate.bd.calls.clear()
    gate.worker.run.side_effect = lambda *a, **kw: 0


def stuck_events(gate):
    return [e for e in gate.events() if e['event'] == 'stuck_decision']


@pytest.mark.parametrize('recorded,human', [(0, False), (1, True)])
@pytest.mark.parametrize('unavailable', ['disabled', 'failing'])
def test_stuck_fail_open_keeps_the_two_window_rule(gate, monkeypatch, recorded,
                                                   human, unavailable):
    """AC-3: with no usable verdict the claim escalates at the threshold and
    resumes below it, exactly as it did before the decision existed."""
    from ortus.core.judge_post import Outcome, OutcomeVerdict

    gate.config.values['judge']['post_turn'] = unavailable == 'failing'
    leftover(gate, windows=recorded)
    verdict = (OutcomeVerdict(failure=JudgeFailure.TIMEOUT) if unavailable == 'failing'
               else OutcomeVerdict(Outcome.CONTINUE, .99))
    monkeypatch.setattr(grind_mod, 'evaluate_outcome', Mock(return_value=verdict))
    result = gate.invoke('--iterations', '1')
    assert result.exit_code == 0, result.output + str(result.exception)
    # The window burned one more than was recorded, so `human` is exactly the
    # threshold being met.
    assert ('human' in gate.bd.rows['demo-1']['labels']) == human
    assert gate.bd.rows['demo-1']['status'] == 'in_progress'
    comments = ' '.join(c['text'] for c in gate.bd.comments('demo-1'))
    assert ('wedged claim escalated' in comments) == human
    assert grind_mod._REPLAN_MARKER_PREFIX not in comments
    if unavailable == 'failing':
        assert stuck_events(gate)[-1]['fail_open'] is True


def test_stuck_replan_resumes_with_the_directive(gate, monkeypatch):
    """AC-4: an unsure continue at one burned window reads as re-plan, and the
    next window's prompt carries the directive the stalled one did not."""
    from ortus.core.judge_post import Outcome, OutcomeVerdict

    gate.config.values['judge']['post_turn'] = True
    leftover(gate)
    monkeypatch.setattr(grind_mod, 'evaluate_outcome',
                        Mock(return_value=OutcomeVerdict(Outcome.CONTINUE, .1)))
    result = gate.invoke('--iterations', '2')
    assert result.exit_code == 0, result.output + str(result.exception)
    # The startup resume logs a fail-open decision of its own; the judged one
    # is the first with a verdict behind it.
    judged = [e for e in stuck_events(gate) if not e['fail_open']]
    assert judged[0]['action'] == 'replan'
    first, second = (call.args[0] for call in gate.worker.run.call_args_list[:2])
    assert grind_mod._REPLAN_HEADER not in first
    assert grind_mod._REPLAN_HEADER in second
    assert 'no new commits on main' in second
    comments = ' '.join(c['text'] for c in gate.bd.comments('demo-1'))
    assert f'{grind_mod._REPLAN_MARKER_PREFIX}1' in comments


def test_stuck_decision_logged_with_vector_windows_and_fail_open(gate, monkeypatch):
    """AC-5: every decision is replayable — vector, action, windows, fail_open."""
    from ortus.core.judge_post import Outcome, OutcomeVerdict
    from ortus.core.judge_replay import validate_event

    gate.config.values['judge']['post_turn'] = True
    leftover(gate)
    monkeypatch.setattr(grind_mod, 'evaluate_outcome',
                        Mock(return_value=OutcomeVerdict(Outcome.NEEDS_HUMAN, .99)))
    result = gate.invoke('--iterations', '1')
    assert result.exit_code == 0, result.output + str(result.exception)
    event = stuck_events(gate)[-1]
    assert event['windows'] == 1 and event['fail_open'] is False
    assert event['branch_advanced'] is False
    assert event['action'] == 'escalate' == event['effective_action']
    assert set(event['vector']) == {'continue', 'replan', 'escalate'}
    assert event['vector']['escalate'] == max(event['vector'].values())
    assert abs(sum(event['vector'].values()) - 1) < 1e-9
    # The record joins the existing log, so replay must still accept it.
    assert all(validate_event(e) for e in gate.events())


def test_stuck_shadow_logs_the_read_action_and_applies_the_baseline(gate, monkeypatch):
    from ortus.core.judge_post import Outcome, OutcomeVerdict

    gate.config.values['judge'].update(post_turn=True, mode='shadow')
    leftover(gate)
    monkeypatch.setattr(grind_mod, 'evaluate_outcome',
                        Mock(return_value=OutcomeVerdict(Outcome.NEEDS_HUMAN, .99)))
    result = gate.invoke('--iterations', '1')
    assert result.exit_code == 0, result.output + str(result.exception)
    event = stuck_events(gate)[-1]
    assert event['action'] == 'escalate' and event['effective_action'] == 'continue'
    assert gate.bd.rows['demo-1']['labels'] == []
