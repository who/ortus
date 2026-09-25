"""Exercise the private hook protocol without a provider or tracker service."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from ortus.core import judge_hook as hook
from ortus.core.judge_tools import ToolAction, ToolDecision


@pytest.fixture
def run_context(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    private = tmp_path / "run"
    private.mkdir(mode=0o700)
    path = private / "context.json"
    data = {"version": 1, "repo": str(repo.resolve()), "issue_id": "test-1",
            "session_id": "session-private", "judge": {"failure_mode": "open"}}
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    monkeypatch.setenv(hook.CONTEXT_ENV, str(path.resolve()))
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    return path, data


def payload(data, **changes):
    body = {"session_id": data["session_id"], "cwd": data["repo"],
            "hook_event_name": "PreToolUse", "tool_use_id": "tool-1",
            "tool_name": "Read", "tool_input": {"file_path": "safe.txt"}}
    body.update(changes)
    return body


def invoke(monkeypatch, data, **changes):
    raw = json.dumps(payload(data, **changes)).encode()
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(raw)))
    return hook.main()


def reason(capsys):
    captured = capsys.readouterr()
    body = json.loads(captured.out)
    assert set(body) == {"hookSpecificOutput"}
    result = body["hookSpecificOutput"]
    assert result["hookEventName"] == "PreToolUse"
    assert result["permissionDecision"] == "deny"
    assert "done (" in captured.err
    return result["permissionDecisionReason"]


def save(path, data):
    path.write_text(json.dumps(data))


def test_subprocess_allow_preserves_native_permissions(run_context):
    _, data = run_context
    result = subprocess.run([sys.executable, "-m", "ortus.core.judge_hook"],
                            input=json.dumps(payload(data)), text=True,
                            capture_output=True, timeout=10)
    assert result.returncode == 0
    assert result.stdout == ""
    assert "done (normal permission flow)" in result.stderr


def parked(*args, **kwargs):
    return ToolDecision(ToolAction.PARK_BEAD, "judged", 0.1, 0.1, 0.8)


@pytest.mark.parametrize("tool,args", [
    ("Bash", {"command": "rm -rf /"}),
    ("Bash", {"command": "rm -rf /etc/hosts"}),
    ("Bash", {"command": "git push --force"}),
    ("Bash", {"command": "git reset --hard"}),
    ("Read", {"file_path": ".env"}),
])
def test_hard_denial_precedes_provider(run_context, monkeypatch, capsys, tool, args):
    _, data = run_context
    monkeypatch.setattr(hook, "decide_tool", lambda *a, **k: pytest.fail("provider called"))
    assert invoke(monkeypatch, data, tool_name=tool, tool_input=args) == 0
    assert reason(capsys) == "policy_denied"


def test_ordinary_calls_are_not_denied_by_local_policy(run_context, monkeypatch, capsys):
    # The regression this phase was stopping grind for: a pipeline and a search
    # continue the native permission flow instead of ending the window.
    _, data = run_context
    for args in ({"command": "ls && cat AGENTS.md | head -100"}, {"command": "git status"}):
        assert invoke(monkeypatch, data, tool_name="Bash", tool_input=args) == 0
        assert capsys.readouterr().out == ""


def test_park_signal_contains_only_safe_identity(run_context, monkeypatch, capsys):
    path, data = run_context
    monkeypatch.setattr(hook, "decide_tool", parked)
    assert invoke(monkeypatch, data, tool_name="Bash",
                  tool_input={"command": "echo SECRET; pwd"}, issue_id="forged") == 0
    assert reason(capsys) == "needs_human"
    records = list(path.parent.glob("human-*.json"))
    assert len(records) == 1
    record = json.loads(records[0].read_text())
    assert record["issue_id"] == "test-1"
    assert record["reason"] == "needs_human"
    assert len(record["call_id"]) == 32
    assert data["session_id"] not in records[0].read_text()
    assert "SECRET" not in records[0].read_text()
    assert records[0].stat().st_mode & 0o777 == 0o600
    assert not list(path.parent.glob("*.tmp"))


@pytest.mark.parametrize("changes", [
    {"session_id": "other"}, {"session_id": None}, {"cwd": "/"},
    {"cwd": "."}, {"hook_event_name": "PostToolUse"}, {"tool_name": ""},
    {"tool_use_id": ""}, {"tool_input": []},
])
def test_bad_hook_schema(run_context, monkeypatch, capsys, changes):
    _, data = run_context
    assert invoke(monkeypatch, data, **changes) == 2
    assert reason(capsys) == "invalid_input"


@pytest.mark.parametrize("raw", [b"", b"[]", b"null", b"SECRET", b"\xff",
                                  b'{"a":1,"a":2}', b'{"a":NaN}',
                                  b" " * (hook.MAX_INPUT_BYTES + 1)])
def test_invalid_json_is_bounded_and_not_echoed(run_context, monkeypatch, capsys, raw):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(raw)))
    assert hook.main() == 2
    captured = capsys.readouterr()
    assert "SECRET" not in captured.out + captured.err
    assert json.loads(captured.out)["hookSpecificOutput"]["permissionDecisionReason"] == "invalid_input"


def test_reader_accepts_exact_limit_and_reads_only_sentinel():
    class Recording(io.BytesIO):
        def read(self, size=-1):
            assert size == hook.MAX_INPUT_BYTES + 1
            return super().read(size)
    assert hook.read_object(Recording(b"{}" + b" " * (hook.MAX_INPUT_BYTES - 2))) == {}
    with pytest.raises(hook.InvalidInput):
        hook.read_object(Recording(b"{}" + b" " * hook.MAX_INPUT_BYTES))


@pytest.mark.parametrize("change", [
    {"version": True}, {"version": 2}, {"repo": "."}, {"issue_id": "../bad"},
    {"session_id": ""}, {"allowed_roots": ["relative"]},
    {"judge": {"endpoint": "https://bad.invalid"}}, {"credential": "SECRET"},
])
def test_invalid_context_fails_closed(run_context, monkeypatch, capsys, change):
    path, data = run_context
    save(path, {**data, **change})
    assert invoke(monkeypatch, data) == 2
    assert reason(capsys) == "invalid_context"


@pytest.mark.parametrize("target", ["file", "directory", "symlink", "missing"])
def test_context_requires_private_regular_file(run_context, monkeypatch, capsys, target):
    path, data = run_context
    if target == "file":
        path.chmod(0o644)
    elif target == "directory":
        path.parent.chmod(0o755)
    elif target == "symlink":
        alias = path.parent / "alias"
        alias.symlink_to(path)
        monkeypatch.setenv(hook.CONTEXT_ENV, str(alias))
    else:
        monkeypatch.delenv(hook.CONTEXT_ENV)
    assert invoke(monkeypatch, data) == 2
    assert reason(capsys) == "invalid_context"


@pytest.mark.parametrize("mode,expected", [("open", "allow"), ("closed", "denied_call")])
def test_missing_provider_key_follows_policy(run_context, monkeypatch, capsys, mode, expected):
    path, data = run_context
    data["judge"]["failure_mode"] = mode
    save(path, data)
    assert invoke(monkeypatch, data) == 0
    if expected == "allow":
        assert capsys.readouterr().out == ""
    else:
        assert reason(capsys) == expected
    # A provider that never answered says nothing about the bead, so neither
    # failure mode parks it.
    assert not list(path.parent.glob("human-*.json"))


def test_internal_failure_never_fails_open_or_leaks(run_context, monkeypatch, capsys):
    _, data = run_context
    def crash(*args, **kwargs):
        print("SECRET")
        print("SECRET", file=sys.stderr)
        raise RuntimeError("SECRET")
    monkeypatch.setattr(hook, "decide_tool", crash)
    assert invoke(monkeypatch, data) == 2
    captured = capsys.readouterr()
    assert "SECRET" not in captured.out + captured.err
    assert json.loads(captured.out)["hookSpecificOutput"]["permissionDecisionReason"] == "internal_failure"


def test_watchdog_bypasses_provider_exception_handler(run_context, monkeypatch, capsys):
    _, data = run_context
    def hang(*args, **kwargs):
        try:
            time.sleep(5)
        except Exception:
            return ToolDecision(ToolAction.ALLOW, "service_failure")
    monkeypatch.setattr(hook, "decide_tool", hang)
    monkeypatch.setattr(hook, "WATCHDOG_SECONDS", 0.05)
    assert invoke(monkeypatch, data) == 2
    assert reason(capsys) == "watchdog"


@pytest.mark.slow
def test_subprocess_watchdog_covers_idle_stdin(run_context):
    with subprocess.Popen([sys.executable, "-m", "ortus.core.judge_hook"],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True) as child:
        child.wait(timeout=10)
        stdout, stderr = child.communicate()
    assert child.returncode == 2
    assert json.loads(stdout)["hookSpecificOutput"]["permissionDecisionReason"] == "watchdog"
    assert "done (watchdog)" in stderr


def test_park_signal_write_failure_is_blocking(run_context, monkeypatch, capsys):
    _, data = run_context
    def fail(*args):
        raise OSError("SECRET")
    monkeypatch.setattr(hook, "decide_tool", parked)
    monkeypatch.setattr(hook, "write_human_signal", fail)
    assert invoke(monkeypatch, data, tool_name="Unknown") == 2
    assert reason(capsys) == "signal_failure"


def test_concurrent_signals_have_unique_complete_records(run_context):
    path, _ = run_context
    context = hook.load_context(os.environ)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: hook.write_human_signal(context), range(24)))
    records = [json.loads(p.read_text()) for p in path.parent.glob("human-*.json")]
    assert len({r["call_id"] for r in records}) == 24
    assert not list(path.parent.glob("*.tmp"))


@pytest.mark.parametrize("action", list(ToolAction))
def test_shadow_suppresses_model_refusals(run_context, monkeypatch, capsys, action):
    path, data = run_context
    data["judge"]["mode"] = "shadow"
    save(path, data)
    monkeypatch.setattr(hook, "decide_tool",
                        lambda *a, **k: ToolDecision(action, "judged", 0.2, 0.4, 0.4))
    assert invoke(monkeypatch, data) == 0
    assert capsys.readouterr().out == ""
    assert not list(path.parent.glob("human-*.json"))
    record = json.loads(next(path.parent.glob("tool-*.json")).read_text())
    # The vector is still recorded; only its application is withheld.
    assert (record["action"], record["effective_action"]) == (action.value, "allow")


@pytest.mark.parametrize("tool_input,expected", [
    ({"command": "rm -rf /"}, "recursive_root_deletion"),
    ({"command": "git push --force"}, "force_push"),
])
def test_shadow_preserves_local_policy(run_context, monkeypatch, capsys, tool_input, expected):
    path, data = run_context
    data["judge"]["mode"] = "shadow"
    save(path, data)
    assert invoke(monkeypatch, data, tool_name="Bash", tool_input=tool_input) == 0
    assert reason(capsys) == "policy_denied"
    record = json.loads(next(path.parent.glob("tool-*.json")).read_text())
    assert record["reason"] == expected
    assert record["effective_action"] == "deny_call"


def test_park_only_on_park_bead_and_one_call_on_deny(run_context, monkeypatch, capsys):
    path, data = run_context
    for action, expected in ((ToolAction.DENY_CALL, "denied_call"),
                             (ToolAction.PARK_BEAD, "needs_human")):
        for stale in path.parent.glob("*-*.json"):
            stale.unlink()
        monkeypatch.setattr(hook, "decide_tool",
                            lambda *a, **k: ToolDecision(action, "judged", 0.1, 0.5, 0.4))
        assert invoke(monkeypatch, data) == 0
        assert reason(capsys) == expected
        parks = list(path.parent.glob("human-*.json"))
        assert len(parks) == int(action is ToolAction.PARK_BEAD)
        record = json.loads(next(path.parent.glob("tool-*.json")).read_text())
        assert record["effective_action"] == action.value
        assert record["vector"] == {"allow": 0.1, "deny_call": 0.5, "park_bead": 0.4}


def test_every_decision_publishes_one_record_with_no_arguments(run_context, monkeypatch):
    path, data = run_context
    assert invoke(monkeypatch, data, tool_name="Bash",
                  tool_input={"command": "cat SECRET-FILE && pwd"}) == 0
    records = list(path.parent.glob("tool-*.json"))
    assert len(records) == 1
    raw = records[0].read_text()
    assert "SECRET-FILE" not in raw
    record = json.loads(raw)
    assert record["tool"] == "Bash"
    assert (record["action"], record["effective_action"]) == ("allow", "allow")
    assert record["reason"] == "service_failure"
    assert record["failure"] == "key_missing"
    assert record["vector"] is None
    assert records[0].stat().st_mode & 0o777 == 0o600


def test_trusted_allowed_root_is_used(run_context, monkeypatch, capsys, tmp_path):
    path, data = run_context
    outside = tmp_path / "allowed"
    outside.mkdir()
    data["allowed_roots"] = [str(outside)]
    save(path, data)
    assert invoke(monkeypatch, data, tool_input={"file_path": str(outside / "safe")}) == 0
    assert capsys.readouterr().out == ""


def test_no_tracker_commands_in_direct_process(run_context, tmp_path):
    _, data = run_context
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "tracker-called"
    fake = bin_dir / "bd"
    fake.write_text('#!/bin/sh\nprintf called > "' + str(marker) + '"\nexit 1\n')
    fake.chmod(0o700)
    result = subprocess.run([sys.executable, "-m", "ortus.core.judge_hook"],
                            input=json.dumps(payload(data, tool_name="Bash",
                                                     tool_input={"command": "rm -rf /"})),
                            text=True, capture_output=True, timeout=10,
                            env={**os.environ, "PATH": str(bin_dir)})
    assert result.returncode == 0
    assert not marker.exists()
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_input_cannot_add_allowed_roots(run_context, monkeypatch, capsys):
    _, data = run_context
    assert invoke(monkeypatch, data, tool_name="Bash",
                  tool_input={"command": "rm -rf /etc/hosts"},
                  allowed_roots=["/"], repo="/", judge={"failure_mode": "open"}) == 0
    assert reason(capsys) == "policy_denied"


def test_ambient_config_cannot_override_parent_snapshot(run_context, monkeypatch, capsys):
    path, data = run_context
    data["judge"]["failure_mode"] = "closed"
    save(path, data)
    monkeypatch.setenv("ORTUS_JUDGE_ENABLED", "false")
    monkeypatch.setenv("ORTUS_JUDGE_MODEL", "invalid")
    assert invoke(monkeypatch, data) == 0
    assert reason(capsys) == "denied_call"


def test_failed_atomic_publish_cleans_temporary_file(run_context, monkeypatch):
    path, _ = run_context
    context = hook.load_context(os.environ)
    def fail(*args):
        raise OSError("publish failed")
    monkeypatch.setattr(hook.os, "replace", fail)
    with pytest.raises(OSError):
        hook.write_human_signal(context)
    assert list(path.parent.iterdir()) == [path]


def test_provider_output_cannot_pollute_allow_protocol(run_context, monkeypatch, capsys):
    _, data = run_context
    def noisy(*args, **kwargs):
        print("SECRET")
        print("SECRET", file=sys.stderr)
        return ToolDecision(ToolAction.ALLOW, "judged_safe")
    monkeypatch.setattr(hook, "decide_tool", noisy)
    assert invoke(monkeypatch, data) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "SECRET" not in captured.err
