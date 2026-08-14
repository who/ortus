"""Backend selection and Codex runner contract."""

from __future__ import annotations

from pathlib import Path

import pytest

from ortus.core.agent import (
    AgentProfile,
    BackendError,
    CodexRunner,
    GrokRunner,
    compose_worker_prompt,
    make_runner,
    resolve_backend,
    wrap_grok_prompt,
    Phase,
)
from ortus.core.claude import ClaudeRunner
from ortus.core.codegraph import CodeGraphCapability


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


def test_codex_profile_routes_model_and_reasoning_effort() -> None:
    profile = AgentProfile("codex", Phase.IMPLEMENT, "gpt-5.2-codex", "xhigh")
    argv = CodexRunner().build_argv("work", profile=profile)
    assert argv[argv.index("-m") + 1] == "gpt-5.2-codex"
    assert argv[argv.index("-c") + 1] == "model_reasoning_effort=xhigh"


def test_codex_unset_profile_preserves_old_argv() -> None:
    plain = CodexRunner().build_argv("work")
    unset = CodexRunner().build_argv(
        "work", profile=AgentProfile("codex", Phase.VERIFY)
    )
    assert unset == plain
    assert "-m" not in unset and "-c" not in unset


def test_codex_gets_explicit_bounded_codegraph_registration() -> None:
    capability = CodeGraphCapability("/opt/tools/codegraph")
    argv = CodexRunner(codegraph=capability).build_argv("orient")
    overrides = [argv[index + 1] for index, value in enumerate(argv) if value == "-c"]
    joined = "\n".join(overrides)
    assert 'mcp_servers.codegraph.command="/opt/tools/codegraph"' in joined
    assert 'mcp_servers.codegraph.args=["serve", "--mcp"]' in joined
    assert "codegraph_explore" in joined and "codegraph_impact" in joined
    assert "env" not in joined.lower() and "token" not in joined.lower()
    assert "--dangerously-bypass-hook-trust" not in argv


def test_codex_codegraph_registration_supports_read_only_posture() -> None:
    runner = CodexRunner(
        codegraph=CodeGraphCapability("codegraph"), sandbox_mode="read-only"
    )
    argv = runner.build_argv("verify graph only")
    assert argv[argv.index("--sandbox") + 1] == "read-only"


def test_codex_readonly_is_per_verifier_invocation() -> None:
    runner = CodexRunner()
    verify = runner.build_argv("verify", readonly=True)
    implement = runner.build_argv("implement")
    assert verify[verify.index("--sandbox") + 1] == "read-only"
    assert implement[implement.index("--sandbox") + 1] == "workspace-write"


def test_codex_readonly_does_not_wrap_runtime_filesystem(tmp_path: Path) -> None:
    runner = CodexRunner()
    argv = runner.build_argv("verify", readonly=True)

    assert runner._readonly_argv(argv, tmp_path) == argv
    assert argv[argv.index("--sandbox") + 1] == "read-only"


def test_make_runner_grok_is_not_a_claude_subclass() -> None:
    runner = make_runner("grok")
    assert isinstance(runner, GrokRunner)
    assert not isinstance(runner, ClaudeRunner)
    assert not isinstance(runner, CodexRunner)


def test_wrap_grok_prompt_unknown_q1_is_plan_gap() -> None:
    with pytest.raises(BackendError, match="PLAN-GAP"):
        wrap_grok_prompt("T", q1="MAYBE")


def test_compose_worker_prompt_grok_follows_q1_expands() -> None:
    assert compose_worker_prompt("grok", "close one") == "/goal close one"
    assert wrap_grok_prompt("close one") == "/goal close one"
    assert "/goal" not in wrap_grok_prompt("close one", q1="VERBATIM")
