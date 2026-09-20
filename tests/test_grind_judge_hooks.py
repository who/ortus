"""Run-scoped Claude hooks, bound escalation and opt-out compatibility."""

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
from unittest.mock import Mock

import pytest

from ortus.commands import grind as grind_mod
from ortus.core.agent import BackendError
from ortus.core.claude import ClaudeRunner
from ortus.core.config import Config
from ortus.core.judge import JudgeConfig, parse_judge_config
from ortus.core.judge_hook import CONTEXT_ENV, load_context, write_human_signal
from ortus.core.judge_hooks import HookRun, check_pre_tool
from tests._shims import make_inline_python_shim
from tests.test_grind_judge import gate, answer  # noqa: F401


@pytest.fixture
def hooks_gate(gate):
    gate.config.values['judge'].update(enabled=False, pre_tool=True)
    gate.worker.hook_settings = None
    gate.worker.hook_session_id = None
    return gate


def publish(runner):
    context = load_context(runner.extra_env)
    write_human_signal(context)
    return context


@pytest.mark.parametrize('resumed', [False, True])
def test_bound_human_signal_stops_once_and_preserves_work(hooks_gate, resumed):
    gate = hooks_gate
    if resumed:
        gate.bd.rows['demo-1'].update(status='in_progress', assignee='previous')
    candidate = gate.repo / 'candidate.py'
    candidate.write_text('pending')
    directories = []

    def worker(prompt, **kwargs):
        assert 'Bound issue contract v1' in prompt
        assert gate.bd.rows['demo-1']['status'] == 'in_progress'
        context = publish(gate.worker)
        directories.append(context.directory)
        publish(gate.worker)
        assert kwargs['reap_when']() is True
        assert kwargs['reap_when']() is True
        # Tracker mutation occurs only after the process returns.
        assert gate.bd.rows['demo-1']['labels'] == []
        return 143

    gate.worker.run.side_effect = worker
    result = gate.invoke('--iterations', '4')
    assert result.exit_code == 0, result.output + str(result.exception)
    assert gate.worker.run.call_count == 1
    gate.judge.evaluate.assert_not_called()
    assert gate.bd.rows['demo-1']['status'] == 'in_progress'
    assert gate.bd.rows['demo-1']['labels'] == ['human']
    assert [c for c in gate.bd.calls if isinstance(c, tuple)] == [
        ('comment', 'judge pre_tool: human reason=needs_human')]
    assert candidate.read_text() == 'pending'
    assert 'release' not in gate.bd.calls
    assert all(not path.exists() for path in directories)
    assert CONTEXT_ENV not in gate.worker.extra_env
    assert gate.worker.hook_settings is None


@pytest.mark.parametrize('ending', ['normal', 'timeout', 'interrupt', 'crash'])
def test_every_worker_exit_cleans_private_files(hooks_gate, ending):
    gate = hooks_gate
    directories = []

    def worker(*args, **kwargs):
        directories.append(load_context(gate.worker.extra_env).directory)
        if ending == 'timeout':
            raise subprocess.TimeoutExpired('worker', 1)
        if ending == 'interrupt':
            raise KeyboardInterrupt
        if ending == 'crash':
            raise OSError('worker failed')
        return 1

    gate.worker.run.side_effect = worker
    result = gate.invoke('--iterations', '1')
    assert result.exit_code == {'normal': 0, 'timeout': 0, 'interrupt': 130, 'crash': 1}[ending]
    assert len(directories) == 1
    assert not directories[0].exists()
    assert gate.bd.rows['demo-1']['status'] == 'in_progress'
    assert gate.bd.rows['demo-1']['labels'] == []


def test_signal_on_fast_exit_is_still_escalated(hooks_gate):
    gate = hooks_gate
    gate.worker.run.side_effect = lambda *a, **kw: publish(gate.worker) and 0
    result = gate.invoke()
    assert result.exit_code == 0, result.exception
    assert gate.bd.rows['demo-1']['labels'] == ['human']


@pytest.mark.parametrize('backend', ['codex', 'grok', 'opencode', 'local'])
def test_unsupported_backend_fails_before_claim(hooks_gate, monkeypatch, backend):
    gate = hooks_gate
    monkeypatch.setattr(grind_mod, 'resolve_backend', lambda *a, **kw: backend)
    result = gate.invoke()
    assert result.exit_code == 1
    assert 'supports only Claude' in result.output
    assert not gate.bd.calls
    gate.worker.run.assert_not_called()


def test_docker_without_context_mount_fails_before_claim(hooks_gate):
    result = hooks_gate.invoke('--docker')
    assert result.exit_code == 1
    assert 'Docker hook context access is unavailable' in result.output
    assert not hooks_gate.bd.calls


@pytest.mark.parametrize('flag', ['disableAllHooks', 'allowManagedHooksOnly'])
def test_local_disabled_hooks_fail_before_claim(hooks_gate, flag):
    path = hooks_gate.repo / '.claude' / 'settings.local.json'
    path.parent.mkdir()
    path.write_text(json.dumps({flag: True}))
    result = hooks_gate.invoke()
    assert result.exit_code == 1
    assert 'disable run-scoped hooks' in result.output
    assert not hooks_gate.bd.calls


def test_disabled_hooks_still_fail_without_precheck_mock(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    path = tmp_path / '.claude' / 'settings.json'
    path.parent.mkdir()
    path.write_text('{"disableAllHooks": true}')
    with pytest.raises(BackendError, match='disable hooks'):
        check_pre_tool(tmp_path, 'claude')


def test_routed_unsupported_backend_releases_unused_claim(hooks_gate):
    gate = hooks_gate
    gate.config.values['judge']['enabled'] = True
    gate.judge.evaluate.return_value = answer('codex')
    result = gate.invoke()
    assert result.exit_code == 1, result.exception
    gate.worker.run.assert_not_called()
    assert gate.bd.rows['demo-1']['status'] == 'open'
    assert 'release' in gate.bd.calls


@pytest.mark.parametrize('args', [('--dry-run',), ('--condition', 'custom')])
def test_preview_and_custom_condition_never_claim(hooks_gate, args):
    result = hooks_gate.invoke(*args)
    assert result.exit_code == (0 if args[0] == '--dry-run' else 1)
    assert not hooks_gate.bd.calls
    hooks_gate.worker.run.assert_not_called()


def test_context_preflight_failure_releases_claim(hooks_gate, monkeypatch):
    def fail(*args, **kwargs):
        raise BackendError('hook context preflight failed')
    monkeypatch.setattr(grind_mod, 'HookRun', fail)
    result = hooks_gate.invoke()
    assert result.exit_code == 1
    assert hooks_gate.bd.rows['demo-1']['status'] == 'open'
    hooks_gate.worker.run.assert_not_called()


def test_registration_composes_settings_and_quotes_interpreter(tmp_path, monkeypatch):
    runner = ClaudeRunner(extra_env={'CACHE_SETTING': 'kept'})
    settings = tmp_path / '.claude' / 'settings.json'
    settings.parent.mkdir()
    original = '{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"true"}]}]}}'
    settings.write_text(original)
    monkeypatch.setenv('TYPESAFE_API_KEY', 'never-persist-this')
    registration = HookRun(tmp_path, 'demo-1', JudgeConfig(pre_tool=True), 'run-1', runner)
    try:
        assert registration.directory.stat().st_mode & 0o777 == 0o700
        assert registration.directory.is_relative_to(
            Path(tempfile.gettempdir()).resolve())
        assert not registration.directory.is_relative_to(tmp_path.resolve())
        for path in (registration.context_path, registration.settings_path):
            assert path.stat().st_mode & 0o777 == 0o600
            assert 'never-persist-this' not in path.read_text()
        body = json.loads(registration.settings_path.read_text())
        assert set(body['hooks']) == {'PreToolUse'}
        hook = body['hooks']['PreToolUse'][0]
        assert hook['matcher'] == '*'
        assert shlex.split(hook['hooks'][0]['command']) == [
            os.path.abspath(sys.executable), '-m', 'ortus.core.judge_hook']
        argv = runner.build_argv('/goal do it')
        assert argv[argv.index('--settings') + 1] == str(registration.settings_path)
        assert argv[argv.index('--session-id') + 1] == registration.session_id
        assert settings.read_text() == original
    finally:
        registration.close()
    assert runner.extra_env == {'CACHE_SETTING': 'kept'}
    assert '--settings' not in runner.build_argv('ordinary')
    assert not registration.directory.exists()


@pytest.mark.parametrize('field', ['issue_id', 'session_hash', 'run_id', 'call_id', 'reason', 'version'])
def test_foreign_or_malformed_signals_cannot_escalate(tmp_path, field):
    runner = ClaudeRunner()
    registration = HookRun(tmp_path, 'demo-1', JudgeConfig(), 'run-1', runner)
    try:
        context = publish(runner)
        path = next(context.directory.glob('human-*.json'))
        body = json.loads(path.read_text())
        body[field] = 'foreign'
        path.write_text(json.dumps(body))
        assert registration.poll() is False
        bd = Mock()
        registration.escalate(bd)
        bd.add_label.assert_not_called()
        write_human_signal(context)
        assert registration.poll() is True
        assert len(registration.seen) == 1
        assert registration.poll() is True
        assert len(registration.seen) == 1
    finally:
        registration.close()


@pytest.mark.parametrize('tool,arguments,expect_human', [
    ('Bash', {'command': 'opaque | command'}, True),
    ('Read', {'file_path': '.env'}, False),
])
def test_fake_claude_executes_hook_and_parent_reaps(tmp_path, tool, arguments, expect_human):
    script = """
import json, os, subprocess, sys, time
from pathlib import Path
argv = sys.argv
settings = json.loads(Path(argv[argv.index('--settings') + 1]).read_text())
command = settings['hooks']['PreToolUse'][0]['hooks'][0]['command']
body = json.loads(os.environ['TEST_TOOL'])
body.update(hook_event_name='PreToolUse', session_id=argv[argv.index('--session-id') + 1],
            cwd=os.getcwd(), tool_use_id='call-1')
result = subprocess.run(command, shell=True, input=json.dumps(body), text=True, capture_output=True)
assert result.returncode == 0, result.stderr
assert json.loads(result.stdout)['hookSpecificOutput']['permissionDecision'] == 'deny'
print('hook-denied', flush=True)
if os.environ['TEST_WAIT'] == 'yes':
    time.sleep(30)
"""
    shim = make_inline_python_shim(tmp_path, 'hook-claude', script)
    runner = ClaudeRunner(claude_binary=str(shim), extra_env={
        'TEST_TOOL': json.dumps({'tool_name': tool, 'tool_input': arguments}),
        'TEST_WAIT': 'yes' if expect_human else 'no',
    })
    registration = HookRun(tmp_path, 'demo-1', JudgeConfig(pre_tool=True), 'run-1', runner)
    started = time.monotonic()
    try:
        rc = runner.run('/goal work', repo=tmp_path, log_path=tmp_path / 'worker.log',
                        timeout=10, reap_when=registration.poll, reap_poll=.05)
        registration.poll()
        assert time.monotonic() - started < 5
        assert (rc != 0) == expect_human
        assert registration.requested == expect_human
        if not expect_human:
            assert 'hook-denied' in (tmp_path / 'worker.log').read_text()
        bd = Mock()
        registration.escalate(bd)
        assert bd.add_label.call_count == int(expect_human)
    finally:
        registration.close()


def test_pre_tool_config_is_independent_and_validated():
    assert not parse_judge_config(Config({}), environ={}).pre_tool
    config = parse_judge_config(Config({'judge': {'pre_tool': True}}), environ={})
    assert config.pre_tool and not config.enabled
    from ortus.core.profiles import ProfileError
    with pytest.raises(ProfileError):
        parse_judge_config(Config({'judge': {'pre_tool': 'sometimes'}}), environ={})


def test_parent_tracker_failure_still_removes_registration(hooks_gate, monkeypatch):
    gate = hooks_gate
    directories = []

    def worker(*args, **kwargs):
        directories.append(publish(gate.worker).directory)
        return 0

    gate.worker.run.side_effect = worker
    monkeypatch.setattr(gate.bd, 'add_label', Mock(side_effect=BackendError('tracker failed')))
    result = gate.invoke()
    assert result.exit_code == 1
    assert 'tracker failed' in result.output
    assert not directories[0].exists()
    assert gate.bd.rows['demo-1']['status'] == 'in_progress'


def test_missing_inbox_stops_worker_without_silent_fallback(tmp_path):
    runner = ClaudeRunner()
    registration = HookRun(tmp_path, 'demo-1', JudgeConfig(), 'run-1', runner)
    try:
        for path in registration.directory.iterdir():
            path.unlink()
        registration.directory.rmdir()
        assert registration.poll() is True
        with pytest.raises(BackendError, match='inbox unavailable'):
            registration.escalate(Mock())
    finally:
        registration.close()


def test_preflight_import_failure_cleans_created_directory(tmp_path, monkeypatch):
    runner = ClaudeRunner()
    directories = []

    def fail(*args, **kwargs):
        directories.append(Path(kwargs['env'][CONTEXT_ENV]).parent)
        return subprocess.CompletedProcess(args[0], 1)

    monkeypatch.setattr('ortus.core.judge_hooks.subprocess.run', fail)
    with pytest.raises(BackendError, match='preflight failed'):
        HookRun(tmp_path, 'demo-1', JudgeConfig(), 'run-1', runner)
    assert len(directories) == 1 and not directories[0].exists()
    assert runner.extra_env == {}


def test_temporary_root_inside_repository_is_refused(tmp_path, monkeypatch):
    runner = ClaudeRunner()
    inside = tmp_path / 'inside-tmp'
    inside.mkdir()
    monkeypatch.setattr(tempfile, 'tempdir', str(inside))
    with pytest.raises(BackendError, match='inside the repository'):
        HookRun(tmp_path, 'demo-1', JudgeConfig(), 'run-1', runner)
    assert list(inside.iterdir()) == []
    assert runner.extra_env == {}
