"""Tests for bd_preflight in ortus/lib/sandbox.sh — the FR-006 bd exemption gate.

The preflight is the gate that stops the loop from launching into a sandbox
where bd can read the queue but not write it: without it the session burns a
full run and closes nothing, which is PRD Risk 2. Each case sources the lib in
a fresh bash with a stubbed `bd` on PATH, so the assertions are about the
gate's decision rather than about the real database.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tests._platform import skip_on_windows_bash_shim

pytestmark = skip_on_windows_bash_shim

REPO_ROOT = Path(__file__).parent.parent
BASH = shutil.which("bash") or "/bin/bash"
SANDBOX_SH = REPO_ROOT / "ortus" / "lib" / "sandbox.sh"
TEMPLATE_SANDBOX_SH = REPO_ROOT / "template" / "ortus" / "lib" / "sandbox.sh"

CONFIG_OK = """\
approval_policy = "never"
sandbox_mode = "workspace-write"

[sandbox_workspace_write]
network_access = true
"""


def stub_bin(tmp_path: Path, bd_body: str | None = "printf '[]\\n'") -> Path:
    """A PATH dir holding a fake `bd` plus the real binaries the lib calls."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for tool in ("sed", "tail", "tr", "git", "uname"):
        real = shutil.which(tool)
        if real and not (bin_dir / tool).exists():
            (bin_dir / tool).symlink_to(real)
    if bd_body is not None:
        bd = bin_dir / "bd"
        bd.write_text(f"#!/bin/sh\n{bd_body}\n")
        bd.chmod(0o755)
    return bin_dir


def run_preflight(
    tmp_path: Path,
    *,
    bd_body: str | None = "printf '[]\\n'",
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    bin_dir = stub_bin(tmp_path, bd_body)
    script = (
        'log() { echo "$*"; }\n'
        f'source "{SANDBOX_SH}"\n'
        "bd_preflight || { echo REFUSED; exit 1; }\n"
        "echo REACHED_LAUNCH\n"
    )
    return subprocess.run(
        [BASH, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(cwd or tmp_path),
        env={"PATH": str(bin_dir), **(env or {})},
    )


def codex_project(tmp_path: Path, config: str) -> Path:
    """A project dir with a .codex/config.toml, as CODEX_HOME expects."""
    project = tmp_path / "project"
    (project / ".codex").mkdir(parents=True, exist_ok=True)
    (project / ".codex" / "config.toml").write_text(config)
    return project


# --- the live run ------------------------------------------------------------


def test_passes_when_bd_answers_with_a_json_array(tmp_path: Path) -> None:
    proc = run_preflight(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "REACHED_LAUNCH" in proc.stdout
    assert "bd preflight: ok" in proc.stdout


def test_refuses_when_bd_is_absent(tmp_path: Path) -> None:
    proc = run_preflight(tmp_path, bd_body=None)
    assert proc.returncode != 0
    assert "REACHED_LAUNCH" not in proc.stdout
    assert "not found on PATH" in proc.stdout


def test_refuses_when_bd_ready_exits_non_zero(tmp_path: Path) -> None:
    """The over-restrictive-sandbox shape: bd is present but cannot reach dolt."""
    proc = run_preflight(
        tmp_path,
        bd_body="echo 'error: failed to open database: read-only file system' >&2\nexit 1",
    )
    assert proc.returncode != 0
    assert "REACHED_LAUNCH" not in proc.stdout
    assert "'bd ready --json' exited 1" in proc.stdout
    # Loud AND specific: the operator gets bd's own words, not just our verdict.
    assert "read-only file system" in proc.stdout


def test_refuses_when_bd_exits_zero_without_a_json_array(tmp_path: Path) -> None:
    """A degraded bd that answers with prose is as broken as an absent one."""
    proc = run_preflight(tmp_path, bd_body="echo 'no workspace resolved'")
    assert proc.returncode != 0
    assert "did not print a JSON array" in proc.stdout


def test_tolerates_leading_whitespace_in_bd_output(tmp_path: Path) -> None:
    proc = run_preflight(tmp_path, bd_body="printf '\\n  [\\n]\\n'")
    assert proc.returncode == 0, proc.stdout + proc.stderr


# --- the declared codex posture ----------------------------------------------


def test_codex_refuses_when_the_config_is_missing(tmp_path: Path) -> None:
    project = tmp_path / "no-config"
    project.mkdir()
    proc = run_preflight(
        tmp_path,
        cwd=project,
        env={"ORTUS_BACKEND": "codex", "CODEX_HOME": str(project / ".codex")},
    )
    assert proc.returncode != 0
    assert "no Codex config at" in proc.stdout


def test_codex_refuses_on_a_read_only_sandbox_mode(tmp_path: Path) -> None:
    """The declared posture is checked even though the live bd run would pass:
    the preflight runs on the host, outside the sandbox Codex will enforce."""
    project = codex_project(tmp_path, 'sandbox_mode = "read-only"\n')
    proc = run_preflight(
        tmp_path,
        cwd=project,
        env={"ORTUS_BACKEND": "codex", "CODEX_HOME": str(project / ".codex")},
    )
    assert proc.returncode != 0
    assert "REACHED_LAUNCH" not in proc.stdout
    assert 'sandbox_mode = "read-only"' in proc.stdout
    assert "workspace-write" in proc.stdout


def test_codex_passes_on_the_generated_posture(tmp_path: Path) -> None:
    project = codex_project(tmp_path, CONFIG_OK)
    proc = run_preflight(
        tmp_path,
        cwd=project,
        env={"ORTUS_BACKEND": "codex", "CODEX_HOME": str(project / ".codex")},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "REACHED_LAUNCH" in proc.stdout


def test_codex_refuses_on_network_access_false_with_a_remote(tmp_path: Path) -> None:
    """Over-restrictive network_access: bd reads fine, but `bd dolt push` cannot
    run, so the loop would close issues and strand them locally."""
    project = codex_project(tmp_path, CONFIG_OK.replace("network_access = true", "network_access = false"))
    # Stub git so the check does not depend on the test dir being a repo:
    # `git remote` prints a remote → the session-close push is in play.
    git = stub_bin(tmp_path) / "git"
    git.unlink(missing_ok=True)
    git.write_text("#!/bin/sh\n[ \"$1\" = remote ] && echo origin\n")
    git.chmod(0o755)
    proc = run_preflight(
        tmp_path,
        cwd=project,
        env={"ORTUS_BACKEND": "codex", "CODEX_HOME": str(project / ".codex")},
    )
    assert proc.returncode != 0
    assert "network_access = false" in proc.stdout
    assert "strand" in proc.stdout


def test_codex_allows_network_access_false_without_a_remote(tmp_path: Path) -> None:
    """bd's store is local — a local-only project is correctly configured, and
    failing it would block a posture that works."""
    project = codex_project(tmp_path, CONFIG_OK.replace("network_access = true", "network_access = false"))
    bin_dir = stub_bin(tmp_path)
    git = bin_dir / "git"
    git.unlink(missing_ok=True)
    git.write_text("#!/bin/sh\nexit 0\n")  # no remotes configured
    git.chmod(0o755)
    proc = run_preflight(
        tmp_path,
        cwd=project,
        env={"ORTUS_BACKEND": "codex", "CODEX_HOME": str(project / ".codex")},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ok for this local-only project" in proc.stdout


def test_claude_backend_does_not_read_the_codex_config(tmp_path: Path) -> None:
    """A broken .codex/config.toml is irrelevant when codex is not the backend."""
    project = codex_project(tmp_path, 'sandbox_mode = "read-only"\n')
    proc = run_preflight(tmp_path, cwd=project, env={"ORTUS_BACKEND": "claude"})
    assert proc.returncode == 0, proc.stdout + proc.stderr


# --- the loop refuses to start -----------------------------------------------


def test_goal_sh_refuses_to_start_when_the_preflight_fails() -> None:
    """No silent degradation: goal.sh must exit on a failed preflight, and must
    run it before any agent spawn."""
    body = (REPO_ROOT / "ortus" / "goal.sh").read_text(encoding="utf-8")
    assert "bd_preflight || {" in body
    assert "exit 1" in body[body.index("bd_preflight || {") :]
    # Ordering: the gate precedes the invocation it guards.
    assert body.index("bd_preflight") < body.index('log "Invoking')


def test_preflight_is_not_skippable_by_env_var() -> None:
    """A skippable gate is a slower path to the failure it exists to prevent."""
    body = SANDBOX_SH.read_text(encoding="utf-8")
    preflight = body[body.index("bd_preflight() {") :]
    for skip in ("SKIP_BD", "BD_PREFLIGHT_SKIP", "SKIP_PREFLIGHT"):
        assert skip not in preflight


def test_preflight_never_wraps_bd() -> None:
    """Wrapping bd (pipes, xargs, bash -c) defeats host-level allowances — the
    same caveat the Claude excludedCommands guidance carries (FR-006)."""
    body = SANDBOX_SH.read_text(encoding="utf-8")
    preflight = body[body.index("bd_preflight() {") : body.index("_bd_toml_scalar() {")]
    for line in preflight.splitlines():
        code = line.split("#", 1)[0]
        if "bd ready" in code:
            assert "|" not in code and "xargs" not in code, line


@pytest.mark.parametrize(
    "needle",
    ["pipes", "xargs", 'bash -c "bd', "excludedCommands"],
)
def test_codex_config_documents_the_wrapping_caveat(needle: str) -> None:
    """FR-006 acceptance #3: the generated Codex config carries the caveat."""
    # The .codex dir is conditionally named (M4) — glob rather than hardcode.
    template = next((REPO_ROOT / "template").glob("*.codex*/config.toml.jinja"))
    config = template.read_text(encoding="utf-8")
    assert needle in config


def test_template_mirror_is_byte_identical() -> None:
    assert TEMPLATE_SANDBOX_SH.is_file()
    assert TEMPLATE_SANDBOX_SH.read_bytes() == SANDBOX_SH.read_bytes()
