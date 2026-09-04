"""Backend selection and Codex runner contract."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from ortus.core.agent import (
    BACKEND_BINARIES,
    BACKENDS,
    OPENCODE_PERMISSION_ENV,
    OPENCODE_READONLY_PERMISSION,
    AgentProfile,
    BackendError,
    CodexRunner,
    GrokRunner,
    OpenCodeRunner,
    compose_worker_prompt,
    make_runner,
    resolve_backend,
    wrap_grok_prompt,
    Phase,
)
from ortus.core.claude import ClaudeRunner
from ortus.core.codegraph import CodeGraphCapability
from ortus.core.local_backend import OPENCODE_PROVIDER_ID, LocalConfig
from ortus.core.profiles import SUPPORTED_EFFORTS, ProfileError, validate_profile_values


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
#
# `local` is opencode under its older name: the same `[local]` table, the
# same runner, the same prompt. The Codex-driven engine it once named, with
# the loopback shim that flattened namespace tools for it, is retired.

LOCAL_TABLE = (
    "[local]\n"
    'base_url = "http://127.0.0.1:11434/v1"\n'
    'model = "qwen3:4b"\n'
    'api_key_env = "LLAMA_API_KEY"\n'
)


def _fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep the developer's own ~/.ortusrc and ~/.opencode out of the picture."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


def _opencode_on_path(monkeypatch: pytest.MonkeyPatch) -> str:
    """PATH holds opencode at a fixed place and nothing else; returns that place."""
    path = "/usr/bin/opencode"
    monkeypatch.setattr(
        shutil, "which", lambda name, *a, **k: path if name == "opencode" else None
    )
    return path


def _install_opencode(home: Path, script: str = "#!/bin/sh\nexit 0\n") -> Path:
    """A stand-in opencode where the installer puts it, off PATH."""
    binary = home / ".opencode" / "bin" / "opencode"
    binary.parent.mkdir(parents=True)
    binary.write_text(script)
    binary.chmod(0o755)
    return binary


def test_local_is_a_legal_backend(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert BACKENDS == ("claude", "codex", "grok", "local", "opencode")
    assert BACKEND_BINARIES["local"] == BACKEND_BINARIES["opencode"] == "opencode"
    assert resolve_backend("local", repo=tmp_path, home=home) == "local"
    with pytest.raises(BackendError, match="claude, codex, grok, local"):
        resolve_backend("other", repo=tmp_path, home=home)


def test_local_is_the_opencode_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`local` launches exactly what `opencode` launches; nothing of codex's remains."""
    _fake_home(tmp_path, monkeypatch)
    binary = _opencode_on_path(monkeypatch)
    (tmp_path / ".ortusrc").write_text(LOCAL_TABLE)
    local = make_runner("local", repo=tmp_path)
    opencode = make_runner("opencode", repo=tmp_path)
    assert type(local) is OpenCodeRunner
    assert not isinstance(local, ClaudeRunner)
    assert not isinstance(local, CodexRunner)
    assert local.local == opencode.local == LocalConfig(
        "http://127.0.0.1:11434/v1", "qwen3:4b", api_key_env="LLAMA_API_KEY"
    )
    for readonly in (False, True):
        argv = local.build_argv("work", readonly=readonly)
        assert argv == opencode.build_argv("work", readonly=readonly)
        assert argv[:2] == [binary, "run"]
        assert "exec" not in argv and "-c" not in argv and "--sandbox" not in argv
        assert "ortus_local" not in " ".join(argv)
    assert local.launch_env(readonly=True) == opencode.launch_env(readonly=True)
    task = "Work bd issue demo-123."
    prompt = compose_worker_prompt("local", task)
    assert prompt == compose_worker_prompt("opencode", task)
    assert prompt != compose_worker_prompt("codex", task)
    assert "/goal" not in prompt and "Codex sandbox note" not in prompt


def test_make_runner_other_backends_ignore_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_home(tmp_path, monkeypatch)
    (tmp_path / ".ortusrc").write_text(LOCAL_TABLE)
    assert type(make_runner("claude", repo=tmp_path)) is ClaudeRunner
    assert type(make_runner("codex", repo=tmp_path)) is CodexRunner
    assert type(make_runner("grok", repo=tmp_path)) is GrokRunner


def test_make_runner_local_without_table_is_actionable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_home(tmp_path, monkeypatch)
    with pytest.raises(BackendError, match="local.model"):
        make_runner("local")
    (tmp_path / ".ortusrc").write_text('backend = "codex"\n')
    with pytest.raises(BackendError, match="local.model"):
        make_runner("local", repo=tmp_path)


# --- opencode backend --------------------------------------------------------
#
# The served model `[local]` names, driven by the opencode CLI.

OPENCODE_ARGV_PREFIX = ["opencode", "run", "--format", "json"]


def test_opencode_runner_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-1: make_runner routes opencode to a sibling runner with the spike argv.

    On PATH, the executable resolves to where PATH has it and nothing else
    changes: `argv[0]` is that absolute path, the rest is the spike argv.
    """
    _fake_home(tmp_path, monkeypatch)
    binary = _opencode_on_path(monkeypatch)
    (tmp_path / ".ortusrc").write_text(LOCAL_TABLE)
    home = tmp_path / "home"
    assert BACKEND_BINARIES["opencode"] == "opencode"
    assert resolve_backend("opencode", repo=tmp_path, home=home) == "opencode"
    runner = make_runner("opencode", repo=tmp_path)
    assert isinstance(runner, OpenCodeRunner)
    assert not isinstance(runner, ClaudeRunner)
    assert not isinstance(runner, CodexRunner)
    assert not isinstance(runner, GrokRunner)
    assert runner.opencode_binary == binary
    assert runner.local == LocalConfig(
        "http://127.0.0.1:11434/v1", "qwen3:4b", api_key_env="LLAMA_API_KEY"
    )
    argv = runner.build_argv("work")
    assert argv == [binary, *OPENCODE_ARGV_PREFIX[1:], "-m", "ortuslocal/qwen3:4b", "work"]
    assert argv[argv.index("-m") + 1] == f"{OPENCODE_PROVIDER_ID}/qwen3:4b"
    # Nothing codex-shaped leaks across: no exec, no -c overrides, no sandbox flag.
    assert "exec" not in argv and "-c" not in argv and "--sandbox" not in argv
    # A served id with slashes and a colon still rides behind the provider.
    served = "0bserverx/Qwen3.8-27B-GGUF:Q4_K_M"
    argv = OpenCodeRunner(LocalConfig("http://127.0.0.1:8080/v1", served)).build_argv("w")
    assert argv[argv.index("-m") + 1] == f"{OPENCODE_PROVIDER_ID}/{served}"


def test_opencode_prompt_has_no_goal() -> None:
    """AC-2: the opencode worker prompt is the plain objective, never /goal."""
    task = "Work bd issue demo-123. Do not invoke goal.sh or ralph.sh."
    prompt = compose_worker_prompt("opencode", task)
    assert prompt.startswith(task)
    assert "/goal" not in prompt
    assert "PLAN-GAP" in prompt
    assert "Codex sandbox note" not in prompt
    argv = OpenCodeRunner(LocalConfig("http://127.0.0.1:8080/v1", "m")).build_argv(prompt)
    assert argv[-1] == prompt
    assert "/goal" not in " ".join(argv)


def test_opencode_profile_routes_model_and_variant() -> None:
    local = LocalConfig("http://127.0.0.1:8080/v1", "configured-model")
    profile = AgentProfile("opencode", Phase.IMPLEMENT, "profile-model", "high")
    argv = OpenCodeRunner(local).build_argv("work", profile=profile)
    assert argv.count("-m") == 1
    assert argv[argv.index("-m") + 1] == f"{OPENCODE_PROVIDER_ID}/profile-model"
    assert "configured-model" not in " ".join(argv)
    assert argv[argv.index("--variant") + 1] == "high"
    assert argv[-1] == "work"
    # Effort only: the configured model still rides as the sole `-m`.
    effort_only = AgentProfile("opencode", Phase.VERIFY, None, "low")
    argv = OpenCodeRunner(local).build_argv("work", profile=effort_only)
    assert argv[argv.index("-m") + 1] == f"{OPENCODE_PROVIDER_ID}/configured-model"
    assert argv[argv.index("--variant") + 1] == "low"
    # An unset profile leaves the plain argv alone.
    plain = OpenCodeRunner(local).build_argv("work")
    unset = OpenCodeRunner(local).build_argv(
        "work", profile=AgentProfile("opencode", Phase.VERIFY)
    )
    assert unset == plain
    assert "--variant" not in plain


def test_opencode_efforts_are_variant_names() -> None:
    assert SUPPORTED_EFFORTS["opencode"] == frozenset(
        {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
    )
    profile = validate_profile_values(
        "opencode", Phase.IMPLEMENT, model="m", reasoning_effort="max"
    )
    assert profile.reasoning_effort == "max"
    with pytest.raises(ProfileError, match="profiles.opencode.implement"):
        validate_profile_values("opencode", Phase.IMPLEMENT, reasoning_effort="hgih")


def test_opencode_resume_maps_to_session() -> None:
    runner = OpenCodeRunner(LocalConfig("http://127.0.0.1:8080/v1", "m"))
    argv = runner.build_argv("work", resume="ses_1")
    assert argv[argv.index("--session") + 1] == "ses_1"
    assert argv[-1] == "work"
    assert "--session" not in runner.build_argv("work")


def test_opencode_readonly_posture_is_permission_denial(tmp_path: Path) -> None:
    runner = OpenCodeRunner(LocalConfig("http://127.0.0.1:8080/v1", "m"))
    verify = runner.build_argv("verify", readonly=True)
    assert verify == runner.build_argv("verify")
    assert runner._readonly_argv(verify, tmp_path) == verify
    assert "bwrap" not in verify
    assert runner.preflight_readonly(tmp_path) is None
    posture = json.loads(runner.launch_env(readonly=True)[OPENCODE_PERMISSION_ENV])
    assert posture == {"edit": "deny", "write": "deny", "bash": "deny"}
    assert posture == OPENCODE_READONLY_PERMISSION
    assert OPENCODE_PERMISSION_ENV not in runner.launch_env()
    # The implement posture is untouched: opencode auto-approves headless.
    assert "--auto" not in runner.build_argv("implement")


def test_opencode_readonly_permission_reaches_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launches: list[tuple[list[str], dict[str, str], bool]] = []

    def fake_spawn(argv: list[str], **kwargs: object) -> int:
        launches.append((list(argv), dict(kwargs["extra_env"]), bool(kwargs["readonly"])))  # type: ignore[arg-type]
        return 0

    monkeypatch.setattr("ortus.core.agent._spawn_logged", fake_spawn)
    runner = OpenCodeRunner(
        LocalConfig("http://127.0.0.1:8080/v1", "m"), extra_env={"BEADS_DIR": "/x"}
    )
    assert runner.run("verify", repo=tmp_path, log_path=tmp_path / "log", readonly=True) == 0
    assert runner.run("implement", repo=tmp_path, log_path=tmp_path / "log") == 0
    verify_argv, verify_env, verify_flag = launches[0]
    implement_argv, implement_env, implement_flag = launches[1]
    assert verify_flag and not implement_flag
    assert json.loads(verify_env[OPENCODE_PERMISSION_ENV]) == OPENCODE_READONLY_PERMISSION
    assert OPENCODE_PERMISSION_ENV not in implement_env
    assert verify_env["BEADS_DIR"] == implement_env["BEADS_DIR"] == "/x"
    assert verify_argv[:4] == implement_argv[:4] == OPENCODE_ARGV_PREFIX


def test_opencode_argv_never_contains_key_material(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLAMA_API_KEY", "sk-live-secret")
    local = LocalConfig("http://127.0.0.1:8080/v1", "m", api_key_env="LLAMA_API_KEY")
    runner = OpenCodeRunner(local)
    argv = runner.build_argv("work", readonly=True)
    assert "sk-live-secret" not in " ".join(argv)
    assert "sk-live-secret" not in " ".join(runner.launch_env(readonly=True).values())
    # Not even the variable name: opencode.json resolves it, not argv.
    assert "LLAMA_API_KEY" not in " ".join(argv)


def test_opencode_codegraph_is_store_only() -> None:
    runner = OpenCodeRunner(LocalConfig("http://127.0.0.1:8080/v1", "m"))
    runner.configure_codegraph(CodeGraphCapability("codegraph"))
    argv = runner.build_argv("orient")
    assert "mcp" not in " ".join(argv).lower()
    assert runner.codegraph is not None


def test_make_runner_opencode_without_table_is_actionable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_home(tmp_path, monkeypatch)
    with pytest.raises(BackendError, match="local.model"):
        make_runner("opencode")
    (tmp_path / ".ortusrc").write_text('backend = "opencode"\n')
    with pytest.raises(BackendError, match="local.model"):
        make_runner("opencode", repo=tmp_path)


def test_opencode_binary_fallback_launches_from_the_install_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-1: off PATH but at `~/.opencode/bin/opencode`, the runner resolves and launches it.

    The stand-in prints its own path, so the log proves which executable
    `subprocess.Popen` received: the absolute install path, not a name the
    child's PATH would have had to find.
    """
    home = _fake_home(tmp_path, monkeypatch)
    installed = _install_opencode(home, "#!/bin/sh\nprintf 'launched %s\\n' \"$0\"\n")
    (tmp_path / ".ortusrc").write_text(LOCAL_TABLE)
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
    runner = make_runner("opencode", repo=tmp_path)
    assert isinstance(runner, OpenCodeRunner)
    assert runner.opencode_binary == str(installed.resolve())
    argv = runner.build_argv("work")
    assert Path(argv[0]).is_absolute() and argv[0] == runner.opencode_binary
    assert argv[1:] == [*OPENCODE_ARGV_PREFIX[1:], "-m", "ortuslocal/qwen3:4b", "work"]
    # `local` is the same backend under its older name: the same executable.
    assert make_runner("local", repo=tmp_path).opencode_binary == runner.opencode_binary

    repo = tmp_path / "repo"
    repo.mkdir()
    log = tmp_path / "worker.log"
    assert runner.run("work", repo=repo, log_path=log) == 0
    assert log.read_text().strip() == f"launched {installed.resolve()}"


def test_opencode_missing_binary_is_a_backend_error_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nowhere at all, `make_runner` raises the error every verb already reports.

    The message names both fixes. The other backends never resolve opencode
    and keep launching by name, so a missing opencode cannot touch them.
    """
    home = _fake_home(tmp_path, monkeypatch)
    (tmp_path / ".ortusrc").write_text(LOCAL_TABLE)
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
    for backend in ("opencode", "local"):
        with pytest.raises(BackendError) as info:
            make_runner(backend, repo=tmp_path)
        message = str(info.value)
        assert "opencode CLI not on PATH" in message
        assert f"add {home / '.opencode' / 'bin'} to PATH" in message
        assert "install opencode" in message
    assert make_runner("claude").claude_binary == "claude"
    assert make_runner("codex").codex_binary == "codex"
    assert isinstance(make_runner("grok"), GrokRunner)


def test_opencode_verify_readonly_denies_write_tools_in_the_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-2: the verify launch hands the spawned process the denial, over the operator's shell.

    opencode's permission is tool-level: with edit, write, and bash denied it
    drops those tools from the model's surface, so a verifier holds nothing
    that can touch the tree. This drives the real spawn path with a stand-in
    binary that echoes what it was given, so the proof is about the process
    opencode would be, not a patched function: the denial arrives intact, an
    allow exported in the operator's shell cannot leak into it, the implement
    launch inherits that shell untouched, and no project file is written to
    carry the posture.
    """
    fake = tmp_path / "bin" / "opencode"
    fake.parent.mkdir()
    fake.write_text('#!/bin/sh\nprintf \'%s\\n\' "${OPENCODE_PERMISSION-unset}"\n')
    fake.chmod(0o755)
    repo = tmp_path / "repo"
    repo.mkdir()
    inherited = {"bash": "allow", "write": "allow", "edit": "allow"}
    monkeypatch.setenv(OPENCODE_PERMISSION_ENV, json.dumps(inherited))
    runner = OpenCodeRunner(
        LocalConfig("http://127.0.0.1:8080/v1", "m"), opencode_binary=str(fake)
    )

    verify_log = tmp_path / "verify.log"
    assert runner.run("verify", repo=repo, log_path=verify_log, readonly=True) == 0
    posture = json.loads(verify_log.read_text().strip())
    assert posture == OPENCODE_READONLY_PERMISSION
    assert {posture[tool] for tool in ("edit", "write", "bash")} == {"deny"}
    assert "allow" not in posture.values()

    implement_log = tmp_path / "implement.log"
    assert runner.run("implement", repo=repo, log_path=implement_log) == 0
    assert json.loads(implement_log.read_text().strip()) == inherited

    assert list(repo.iterdir()) == []
