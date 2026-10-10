"""A broken hook is not a decision: every hook-side failure allows the call.

The incident behind this file: a worker ran `cd test && node --test ...`,
Claude's Bash tool kept that directory, and every later PreToolUse input
arrived from `<seat>/test`. The hook required the repository root exactly,
answered each call with an exit-2 deny, and the worker looped for half an
hour in a shadow seat that was never meant to change its behavior.
"""
from __future__ import annotations

import io
import json
import sys
import time
from uuid import uuid4

import pytest

from ortus.core import judge_hook as hook
from ortus.core.claude import ClaudeRunner
from ortus.core.judge import JudgeConfig, JudgeMode
from ortus.core.judge_hooks import HookRun
from ortus.core.judge_log import write_tool_decision
from ortus.core.judge_replay import read_events, validate_event
from ortus.core.judge_tools import ToolAction, ToolDecision

#: About the length of the `bd comments add` line the incident log refused.
LONG_COMMENT = "Investigated the converter edge cases, " * 11


@pytest.fixture
def seat(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "test").mkdir(parents=True)
    (repo / "src").mkdir()
    private = tmp_path / "run"
    private.mkdir(mode=0o700)
    path = private / "context.json"
    data = {"version": 1, "repo": str(repo.resolve()), "issue_id": "test-1",
            "session_id": "session-private", "judge": {"failure_mode": "closed"}}
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    monkeypatch.setenv(hook.CONTEXT_ENV, str(path.resolve()))
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    return path, data


def set_mode(seat, mode):
    path, data = seat
    data["judge"]["mode"] = mode
    path.write_text(json.dumps(data))


def run(monkeypatch, raw: bytes) -> int:
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(raw)))
    return hook.main()


def call(monkeypatch, data, *, cwd=None, command="pwd", **changes) -> int:
    body = {"session_id": data["session_id"], "cwd": cwd or data["repo"],
            "hook_event_name": "PreToolUse", "tool_use_id": "tool-1",
            "tool_name": "Bash", "tool_input": {"command": command}, **changes}
    return run(monkeypatch, json.dumps(body).encode())


def allowed(capsys) -> str:
    """No permission decision on stdout: Claude's own flow decides."""
    captured = capsys.readouterr()
    assert captured.out == ""
    return captured.err


def denied(capsys) -> str:
    output = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    return output["permissionDecisionReason"]


def record(seat) -> dict:
    path, _ = seat
    records = [json.loads(p.read_text()) for p in path.parent.glob("tool-*.json")]
    assert len(records) == 1
    return records[0]


def replayed(tmp_path, body, mode) -> dict:
    """The published record as the parent logs it, read back through replay."""
    repo = tmp_path / "log"
    repo.mkdir(exist_ok=True)
    write_tool_decision(repo, JudgeConfig(enabled=True, mode=mode), uuid4(), "test-1", body)
    events = list(read_events(repo / "logs" / "jev-decisions.jsonl"))
    return [e for e in events if e["event"] == "tool_decision"][-1]


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
def test_incident_subdirectory_cwd_with_long_bd_comment_is_allowed(
    seat, monkeypatch, capsys, mode,
):
    set_mode(seat, mode)
    _, data = seat
    command = f'bd comments add test-1 "{LONG_COMMENT}"'
    assert 400 <= len(command) <= 460
    assert call(monkeypatch, data, cwd=f"{data['repo']}/test", command=command) == 0
    assert "fail open" not in allowed(capsys)
    body = record(seat)
    # The subdirectory is accepted, so nothing failed on the hook side.
    assert "hook_failure" not in body
    assert body["effective_action"] == "allow"


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
def test_incident_relative_operands_resolve_against_the_subdirectory(
    seat, monkeypatch, capsys, mode,
):
    set_mode(seat, mode)
    _, data = seat
    # `../src` from `test` is inside the repository; from the root it would not be.
    assert call(monkeypatch, data, cwd=f"{data['repo']}/test", command="rm -rf ../src") == 0
    allowed(capsys)
    assert record(seat)["action"] == "allow"


def test_incident_relative_escape_from_subdirectory_is_still_policy(seat, monkeypatch, capsys):
    _, data = seat
    assert call(monkeypatch, data, cwd=f"{data['repo']}/test", command="rm -rf ..") == 0
    assert denied(capsys) == "policy_denied"
    assert record(seat)["reason"] == "recursive_root_deletion"


def test_incident_symlinked_cwd_into_repo_is_accepted(seat, monkeypatch, capsys, tmp_path):
    _, data = seat
    alias = tmp_path / "alias"
    alias.symlink_to(f"{data['repo']}/test")
    assert call(monkeypatch, data, cwd=str(alias)) == 0
    allowed(capsys)
    assert "hook_failure" not in record(seat)


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
def test_cwd_outside_repo_fails_open_with_reason(seat, monkeypatch, capsys, tmp_path, mode):
    set_mode(seat, mode)
    _, data = seat
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert call(monkeypatch, data, cwd=str(elsewhere)) == 0
    assert "done (fail open: invalid_input:cwd_outside_repo)" in allowed(capsys)
    body = record(seat)
    assert body["hook_failure"] == "invalid_input:cwd_outside_repo"
    assert (body["action"], body["effective_action"], body["tool"]) == ("allow", "allow", "Bash")
    logged = replayed(tmp_path, body, JudgeMode(mode))
    assert logged["hook_failure"] == "invalid_input:cwd_outside_repo"
    assert validate_event(logged) == logged


def test_deleted_cwd_inside_repo_fails_open_as_unresolvable(seat, monkeypatch, capsys):
    _, data = seat
    assert call(monkeypatch, data, cwd=f"{data['repo']}/gone") == 0
    assert "invalid_input:cwd_unresolvable" in allowed(capsys)
    assert record(seat)["hook_failure"] == "invalid_input:cwd_unresolvable"


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
def test_oversize_input_fails_open(seat, monkeypatch, capsys, tmp_path, mode):
    set_mode(seat, mode)
    assert run(monkeypatch, b" " * (hook.MAX_INPUT_BYTES + 1)) == 0
    assert "done (fail open: oversize_input)" in allowed(capsys)
    body = record(seat)
    assert (body["hook_failure"], body["tool"]) == ("oversize_input", "unknown")
    assert validate_event(replayed(tmp_path, body, JudgeMode(mode)))


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
def test_malformed_json_fails_open(seat, monkeypatch, capsys, mode):
    set_mode(seat, mode)
    assert run(monkeypatch, b'{"session_id": SECRET') == 0
    err = allowed(capsys)
    assert "done (fail open: invalid_input:malformed_json)" in err
    assert "SECRET" not in err
    assert record(seat)["hook_failure"] == "invalid_input:malformed_json"


def test_malformed_json_without_context_still_fails_open(seat, monkeypatch, capsys):
    monkeypatch.delenv(hook.CONTEXT_ENV)
    assert run(monkeypatch, b"{") == 0
    assert "done (fail open: invalid_context)" in allowed(capsys)


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
def test_sdk_missing_fails_open_with_failure_recorded(
    seat, monkeypatch, capsys, tmp_path, mode,
):
    set_mode(seat, mode)
    _, data = seat
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    # A seat whose SDK is not installed: the client import inside the request fails.
    monkeypatch.setitem(sys.modules, "typesafe_sdk", None)
    assert call(monkeypatch, data, command="node --test") == 0
    allowed(capsys)
    body = record(seat)
    # failure_mode is closed, and still a provider that never answered allows.
    assert (body["failure"], body["action"], body["effective_action"]) == (
        "sdk_missing", "allow", "allow")
    assert validate_event(replayed(tmp_path, body, JudgeMode(mode)))


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
def test_watchdog_fails_open_with_reason(seat, monkeypatch, capsys, mode):
    set_mode(seat, mode)
    _, data = seat

    def hang(*args, **kwargs):
        time.sleep(5)
    monkeypatch.setattr(hook, "decide_tool", hang)
    monkeypatch.setattr(hook, "WATCHDOG_SECONDS", 0.05)
    assert call(monkeypatch, data) == 0
    assert "done (fail open: watchdog)" in allowed(capsys)
    assert record(seat)["hook_failure"] == "watchdog"


def test_unwritable_failure_record_still_fails_open(seat, monkeypatch, capsys, tmp_path):
    _, data = seat

    def broken(*args, **kwargs):
        raise OSError("inbox gone")
    monkeypatch.setattr(hook, "_publish", broken)
    assert call(monkeypatch, data, cwd=str(tmp_path)) == 0
    assert "invalid_input:cwd_outside_repo" in allowed(capsys)


def test_shadow_local_policy_is_logged_as_baseline_and_applied_as_allow(
    seat, monkeypatch, capsys, tmp_path,
):
    set_mode(seat, "shadow")
    _, data = seat
    assert call(monkeypatch, data, command="bd list | head -5") == 0
    allowed(capsys)
    body = record(seat)
    assert body["reason"] == "wrapped_bd"
    assert (body["action"], body["baseline_action"], body["effective_action"]) == (
        "deny_call", "deny_call", "allow")
    logged = replayed(tmp_path, body, JudgeMode.SHADOW)
    assert logged["effective_action"] == "allow"
    assert validate_event(logged) == logged


def test_shadow_model_park_is_never_applied(seat, monkeypatch, capsys):
    path, data = seat
    set_mode(seat, "shadow")
    monkeypatch.setattr(hook, "decide_tool", lambda *a, **k: ToolDecision(
        ToolAction.PARK_BEAD, "judged", 0.1, 0.1, 0.8))
    assert call(monkeypatch, data) == 0
    allowed(capsys)
    assert not list(path.parent.glob("human-*.json"))


def test_enforce_local_policy_is_still_denied(seat, monkeypatch, capsys):
    set_mode(seat, "enforce")
    _, data = seat
    assert call(monkeypatch, data, command="bd list | head -5") == 0
    assert denied(capsys) == "policy_denied"
    body = record(seat)
    assert (body["reason"], body["effective_action"]) == ("wrapped_bd", "deny_call")


def test_parent_logs_failure_records_as_replayable_tool_decisions(tmp_path):
    runner = ClaudeRunner()
    registration = HookRun(tmp_path, "demo-1", JudgeConfig(), str(uuid4()), runner)
    try:
        context = hook.load_context(runner.extra_env)
        hook.write_failure_record(context, "Bash", "invalid_input:cwd_outside_repo")
        assert registration.poll() is False
        events = list(read_events(tmp_path / "logs" / "jev-decisions.jsonl"))
        logged = [e for e in events if e["event"] == "tool_decision"]
        assert len(logged) == 1
        assert logged[0]["hook_failure"] == "invalid_input:cwd_outside_repo"
        assert logged[0]["effective_action"] == "allow"
    finally:
        registration.close()
