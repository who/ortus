"""Backend selection and Codex runner contract."""

from __future__ import annotations

from pathlib import Path

import pytest

from ortus.core.agent import (
    BackendError,
    CodexRunner,
    compose_worker_prompt,
    make_runner,
    resolve_backend,
)
from ortus.core.claude import ClaudeRunner


def test_claude_is_the_default(tmp_path: Path) -> None:
    assert resolve_backend(repo=tmp_path, home=tmp_path / "home") == "claude"
    assert isinstance(make_runner("claude"), ClaudeRunner)


def test_backend_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".ortusrc").write_text('backend = "codex"\n')
    assert resolve_backend(repo=tmp_path, home=tmp_path / "home") == "codex"
    monkeypatch.setenv("ORTUS_BACKEND", "claude")
    assert resolve_backend(repo=tmp_path, home=tmp_path / "home") == "claude"
    assert resolve_backend("codex", repo=tmp_path, home=tmp_path / "home") == "codex"


def test_unknown_backend_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(BackendError, match="unknown backend"):
        resolve_backend("other", repo=tmp_path, home=tmp_path / "home")


def test_codex_exec_gets_plain_prompt_not_slash_goal() -> None:
    task = "Work bd issue demo-123. Do not invoke goal.sh or ralph.sh."
    prompt = compose_worker_prompt("codex", task)
    argv = CodexRunner().build_argv(prompt)
    assert prompt.startswith(task)
    assert "outer Ortus process will commit and push" in prompt
    assert argv[:2] == ["codex", "exec"]
    assert argv[2] == prompt
    assert "/goal" not in " ".join(argv)
    assert "--json" in argv
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv


def test_claude_keeps_goal_contract() -> None:
    assert compose_worker_prompt("claude", "close one") == "/goal close one"
