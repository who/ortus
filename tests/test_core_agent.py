"""Backend selection and Codex runner contract."""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from ortus.core.agent import (
    BACKEND_BINARIES,
    BACKENDS,
    AgentProfile,
    BackendError,
    CodexRunner,
    GrokRunner,
    LocalRunner,
    compose_worker_prompt,
    make_runner,
    resolve_backend,
    wrap_grok_prompt,
    Phase,
)
from ortus.core.claude import ClaudeRunner
from ortus.core.codegraph import CodeGraphCapability
from ortus.core.local_backend import LocalConfig


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
    assert "PLAN-GAP" in prompt
    assert "outer Ortus process will commit and push" not in prompt
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


# --- local backend -----------------------------------------------------------

LOCAL_TABLE = (
    "[local]\n"
    'base_url = "http://127.0.0.1:11434/v1"\n'
    'model = "qwen3:4b"\n'
    'api_key_env = "LLAMA_API_KEY"\n'
)

#: The provider overrides in their pinned order, as LocalRunner emits them.
PROVIDER_PAIRS = [
    "-c",
    'model_providers.ortus_local.name="ortus_local"',
    "-c",
    'model_providers.ortus_local.base_url="http://127.0.0.1:11434/v1"',
    "-c",
    'model_providers.ortus_local.wire_api="responses"',
    "-c",
    'model_providers.ortus_local.env_key="LLAMA_API_KEY"',
    "-c",
    'model_provider="ortus_local"',
]


def _fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep the developer's own ~/.ortusrc out of the layered config."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


def test_local_is_a_legal_backend(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert BACKENDS == ("claude", "codex", "grok", "local")
    assert BACKEND_BINARIES["local"] == "codex"
    assert resolve_backend("local", repo=tmp_path, home=home) == "local"
    with pytest.raises(BackendError, match="claude, codex, grok, local"):
        resolve_backend("other", repo=tmp_path, home=home)


def test_local_argv_carries_provider_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_home(tmp_path, monkeypatch)
    (tmp_path / ".ortusrc").write_text(LOCAL_TABLE)
    runner = make_runner("local", repo=tmp_path)
    assert isinstance(runner, LocalRunner)
    assert isinstance(runner, CodexRunner)
    assert runner.codex_binary == "codex"
    assert runner.local == LocalConfig(
        "http://127.0.0.1:11434/v1", "qwen3:4b", api_key_env="LLAMA_API_KEY"
    )
    argv = runner.build_argv("work")
    plain = CodexRunner().build_argv("work")
    assert argv[: len(plain)] == plain
    assert argv[len(plain) :] == PROVIDER_PAIRS + ["-m", "qwen3:4b"]


def test_make_runner_other_backends_ignore_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_home(tmp_path, monkeypatch)
    (tmp_path / ".ortusrc").write_text(LOCAL_TABLE)
    assert type(make_runner("claude", repo=tmp_path)) is ClaudeRunner
    assert type(make_runner("codex", repo=tmp_path)) is CodexRunner
    assert type(make_runner("grok", repo=tmp_path)) is GrokRunner


def test_local_profile_model_wins_over_config() -> None:
    local = LocalConfig("http://127.0.0.1:8080/v1", "configured-model")
    profile = AgentProfile("local", Phase.IMPLEMENT, "profile-model", "high")
    argv = LocalRunner(local).build_argv("work", profile=profile)
    assert argv.count("-m") == 1
    assert argv[argv.index("-m") + 1] == "profile-model"
    assert "configured-model" not in argv
    # The inherited effort pair precedes the provider pairs.
    assert argv.index("model_reasoning_effort=high") < argv.index(
        'model_providers.ortus_local.name="ortus_local"'
    )
    # Effort only: the configured model still rides along as the sole `-m`.
    effort_only = AgentProfile("local", Phase.VERIFY, None, "low")
    argv = LocalRunner(local).build_argv("work", profile=effort_only)
    assert argv.count("-m") == 1
    assert argv[-2:] == ["-m", "configured-model"]


def test_local_readonly_argv(tmp_path: Path) -> None:
    runner = LocalRunner(LocalConfig("http://127.0.0.1:8080/v1", "m"))
    argv = runner.build_argv("verify", readonly=True)
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert 'model_provider="ortus_local"' in argv
    assert argv[-2:] == ["-m", "m"]
    assert runner._readonly_argv(argv, tmp_path) == argv
    assert runner.preflight_readonly(tmp_path) is None
    assert "bwrap" not in argv


def test_local_argv_never_contains_key_material(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLAMA_API_KEY", "sk-live-secret")
    local = LocalConfig("http://127.0.0.1:8080/v1", "m", api_key_env="LLAMA_API_KEY")
    runner = LocalRunner(local)
    argv = runner.build_argv("work", readonly=True)
    assert "sk-live-secret" not in " ".join(argv)
    assert "sk-live-secret" not in " ".join(runner.extra_env.values())
    assert 'model_providers.ortus_local.env_key="LLAMA_API_KEY"' in argv
    # No api_key_env, no env_key pair at all.
    bare = LocalRunner(LocalConfig("http://127.0.0.1:8080/v1", "m")).build_argv("work")
    assert not any(value.startswith("model_providers.ortus_local.env_key") for value in bare)


def _provider_override(argv: list[str], key: str) -> str | None:
    prefix = f"model_providers.ortus_local.{key}="
    for value in argv:
        if value.startswith(prefix):
            return json.loads(value[len(prefix) :])
    return None


def test_local_runner_base_url_targets_shim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With CodeGraph the override names the shim; without it, the server."""
    local = LocalConfig("http://127.0.0.1:8080/v1", "m", api_key_env="LLAMA_API_KEY")
    launches: list[tuple[list[str], str | None, bool]] = []

    def fake_spawn(argv: list[str], **kwargs: object) -> int:
        shim = runner.shim
        listening = False
        if shim is not None:
            with socket.create_connection(("127.0.0.1", shim.port), timeout=1):
                listening = True
        shim_url = None if shim is None else shim.base_url
        launches.append((list(argv), shim_url, listening))
        return 0

    monkeypatch.setattr("ortus.core.claude._spawn_logged", fake_spawn)
    runner = LocalRunner(local, codegraph=CodeGraphCapability("codegraph"))
    assert runner.provider_base_url == local.base_url
    assert runner.run("work", repo=tmp_path, log_path=tmp_path / "log") == 0
    argv, shim_url, listening = launches[0]
    base_url = _provider_override(argv, "base_url")
    assert base_url == shim_url
    assert base_url != local.base_url
    assert base_url.startswith("http://127.0.0.1:")
    assert base_url.endswith("/v1")
    assert listening
    # The key rides the shim's upstream leg, never the codex argv.
    assert _provider_override(argv, "env_key") is None
    assert runner.shim is None
    assert runner.provider_base_url == local.base_url

    runner = LocalRunner(local)
    assert runner.run("work", repo=tmp_path, log_path=tmp_path / "log") == 0
    argv, shim_url, _ = launches[1]
    assert shim_url is None
    assert _provider_override(argv, "base_url") == local.base_url
    assert _provider_override(argv, "env_key") == "LLAMA_API_KEY"
    assert runner.shim is None


def test_compose_worker_prompt_local_matches_codex() -> None:
    task = "Work bd issue demo-123."
    prompt = compose_worker_prompt("local", task)
    assert prompt == compose_worker_prompt("codex", task)
    assert not prompt.startswith("/goal")
    assert "/goal" not in prompt


def test_make_runner_local_without_table_is_actionable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_home(tmp_path, monkeypatch)
    with pytest.raises(BackendError, match="local.model"):
        make_runner("local")
    (tmp_path / ".ortusrc").write_text('backend = "codex"\n')
    with pytest.raises(BackendError, match="local.model"):
        make_runner("local", repo=tmp_path)
