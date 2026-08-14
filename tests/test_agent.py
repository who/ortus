"""Focused cross-backend verifier isolation contract."""

from __future__ import annotations

from pathlib import Path

import pytest

from ortus.core.agent import (
    BackendError,
    CodexRunner,
    GrokRunner,
    compose_worker_prompt,
    make_runner,
    resolve_backend,
    wrap_grok_prompt,
)
from ortus.core.claude import ClaudeRunner, _readonly_wrapper
from ortus.core.profiles import Phase, validate_profile_values


def test_readonly_verifier_postures_are_technically_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex_argv = CodexRunner().build_argv("verify", readonly=True)
    assert codex_argv[codex_argv.index("--sandbox") + 1] == "read-only"

    claude_argv = ClaudeRunner().build_argv("verify", readonly=True)
    assert "--disallowedTools" in claude_argv
    monkeypatch.setattr("ortus.core.claude.platform.system", lambda: "Linux")
    wrapped = _readonly_wrapper(claude_argv, tmp_path)
    assert wrapped[:4] == ["bwrap", "--ro-bind", "/", "/"]
    assert ["--chdir", str(tmp_path.resolve())] == wrapped[
        wrapped.index("--chdir") : wrapped.index("--chdir") + 2
    ]

    grok_argv = GrokRunner().build_argv("verify", readonly=True)
    assert grok_argv[grok_argv.index("--sandbox") + 1] == "read-only"
    assert GrokRunner()._readonly_argv(grok_argv, tmp_path) == grok_argv
    assert grok_argv[0] != "bwrap" and "bwrap" not in grok_argv


def test_resolve_backend_grok_from_ortusrc(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".ortusrc").write_text('backend = "grok"\n')
    assert resolve_backend(None, repo=repo, home=home) == "grok"


def test_grok_is_a_legal_backend(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert resolve_backend("grok", repo=tmp_path, home=home) == "grok"
    runner = make_runner("grok")
    assert isinstance(runner, GrokRunner)
    assert not isinstance(runner, ClaudeRunner)
    with pytest.raises(BackendError, match="unknown backend"):
        resolve_backend("other", repo=tmp_path, home=home)


def test_grok_implement_argv() -> None:
    argv = GrokRunner().build_argv("task")
    assert argv[0] == "grok"
    assert argv[:3] == ["grok", "-p", "task"]
    assert argv[argv.index("--sandbox") + 1] == "workspace"
    assert "--always-approve" in argv
    assert argv[argv.index("--output-format") + 1] == "streaming-json"
    assert "--yolo" not in argv
    assert "-c" not in argv


def test_grok_readonly_argv() -> None:
    argv = GrokRunner().build_argv("task", readonly=True)
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert "--always-approve" in argv
    assert "--yolo" not in argv


def test_compose_worker_prompt_claude() -> None:
    assert compose_worker_prompt("claude", "T") == "/goal T"


def test_compose_worker_prompt_grok() -> None:
    prompt = compose_worker_prompt("grok", "T")
    assert prompt.startswith("/goal ")
    assert prompt == "/goal T"


def test_wrap_grok_prompt_verbatim_never_contains_goal() -> None:
    prompt = wrap_grok_prompt("T", q1="VERBATIM")
    assert "/goal" not in prompt
    assert prompt.startswith("T")
    assert "outer Ortus process will commit and push" in prompt


def test_grok_profile_routes_model_and_effort() -> None:
    profile = validate_profile_values(
        "grok", Phase.IMPLEMENT, model="grok-code", reasoning_effort="xhigh"
    )
    argv = GrokRunner().build_argv("work", profile=profile)
    assert argv[argv.index("-m") + 1] == "grok-code"
    assert argv[argv.index("--effort") + 1] == "xhigh"


def test_grok_resume_maps_to_flag() -> None:
    argv = GrokRunner().build_argv("work", resume="sess-1")
    assert argv[argv.index("--resume") + 1] == "sess-1"


def test_make_runner_grok_is_grok_runner() -> None:
    runner = make_runner("grok")
    assert type(runner) is GrokRunner
    assert not isinstance(runner, CodexRunner)


def test_grok_runner_selection_has_no_codex_else() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "ortus"
    hits: list[str] = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for needle in (
            'else _make_runner("codex")',
            "else _make_runner('codex')",
            'else runner_factory("codex")',
            "else runner_factory('codex')",
        ):
            if needle in text:
                hits.append(f"{path.relative_to(root.parent.parent)}: {needle}")
    assert hits == []


def test_grok_codegraph_is_store_only() -> None:
    from ortus.core.codegraph import CodeGraphCapability

    runner = GrokRunner()
    runner.configure_codegraph(CodeGraphCapability("codegraph"))
    argv = runner.build_argv("orient")
    assert "-c" not in argv
    assert "mcp_servers" not in " ".join(argv)
    assert runner.codegraph is not None
