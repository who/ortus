"""Focused cross-backend verifier isolation contract."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from ortus.core.agent import (
    OPENCODE_PERMISSION_ENV,
    BackendError,
    CodexRunner,
    GrokRunner,
    OpenCodeRunner,
    compose_worker_prompt,
    make_runner,
    resolve_backend,
    wrap_grok_prompt,
)
from ortus.core.claude import ClaudeRunner, _readonly_wrapper
from ortus.core.local_backend import LocalConfig
from ortus.core.profiles import Phase, ProfileError, validate_profile_values


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


def test_all_is_rejected_as_a_run_backend(tmp_path: Path) -> None:
    """`all` provisions at init time; it must never resolve as a run backend."""
    home = tmp_path / "home"
    with pytest.raises(BackendError, match="init provisioning option"):
        resolve_backend("all", repo=tmp_path, home=home)


def test_ortusrc_backend_all_is_rejected(tmp_path: Path) -> None:
    """Config validation refuses the token before any verb can act on it."""
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".ortusrc").write_text('backend = "all"\n')
    with pytest.raises(ProfileError, match="init provisioning option"):
        resolve_backend(None, repo=repo, home=home)


def test_ortus_backend_env_all_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("ORTUS_BACKEND", "all")
    with pytest.raises(BackendError, match="init provisioning option"):
        resolve_backend(None, repo=tmp_path, home=home)


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
    assert "PLAN-GAP" in prompt
    assert "outer Ortus process will commit and push" not in prompt


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


def test_runner_run_accepts_resume_kwarg() -> None:
    """f2he.5 AC-3: runners still accept resume=; grind just must not pass it."""
    for cls in (ClaudeRunner, GrokRunner, CodexRunner, OpenCodeRunner):
        assert "resume" in inspect.signature(cls.run).parameters


def test_codex_has_no_separate_handshake_agent() -> None:
    """AC-4: Codex no longer launches a child just to scrape a handshake log."""
    assert not hasattr(CodexRunner, "run_codegraph_handshake")
    assert "on_poll" in inspect.signature(CodexRunner.run).parameters


def test_opencode_readonly_posture_is_technically_enforced(tmp_path: Path) -> None:
    """The verify posture is opencode's own permission table, carried per launch."""
    runner = OpenCodeRunner(LocalConfig("http://127.0.0.1:8080/v1", "m"))
    argv = runner.build_argv("verify", readonly=True)
    assert argv[:2] == ["opencode", "run"]
    assert runner._readonly_argv(argv, tmp_path) == argv
    assert "bwrap" not in argv
    posture = json.loads(runner.launch_env(readonly=True)[OPENCODE_PERMISSION_ENV])
    assert posture["bash"] == "deny"
    assert posture["edit"] == "deny"
    assert posture["write"] == "deny"
    assert OPENCODE_PERMISSION_ENV not in runner.launch_env()


def test_make_runner_opencode_is_a_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    (tmp_path / ".ortusrc").write_text('[local]\nmodel = "m"\n')
    runner = make_runner("opencode", repo=tmp_path)
    assert type(runner) is OpenCodeRunner
    assert not isinstance(runner, ClaudeRunner)
    assert not isinstance(runner, CodexRunner)
    assert not isinstance(runner, GrokRunner)
    # `local` is opencode under its older name: the same sibling, never codex.
    assert type(make_runner("local", repo=tmp_path)) is OpenCodeRunner


def test_compose_worker_prompt_opencode() -> None:
    prompt = compose_worker_prompt("opencode", "T")
    assert prompt.startswith("T")
    assert "/goal" not in prompt
    assert "PLAN-GAP" in prompt
