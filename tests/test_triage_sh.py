"""Tests for ortus/triage.sh — the `ortus triage` deprecation shim (ortus-4md0).

The shell harness used to launch `claude --allowedTools AskUserQuestion,... -p`.
Per ortus-sr0b that surface is interactive-only: under -p the calls return
is_error and the agent exits, which the wrapper read as success — the operator
saw "triage complete" while the human queue was never drained. These tests pin
the retirement so the broken flow cannot come back.
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
TRIAGE_SH = REPO_ROOT / "ortus" / "triage.sh"
TEMPLATE_TRIAGE_SH = REPO_ROOT / "template" / "ortus" / "triage.sh"


def run_triage(*args: str, path: str) -> subprocess.CompletedProcess:
    """Run triage.sh with a controlled PATH so `ortus` presence is explicit."""
    return subprocess.run(
        [BASH, str(TRIAGE_SH), *args],
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": path},
    )


@pytest.fixture()
def fake_ortus(tmp_path: Path) -> Path:
    """A stub `ortus` that echoes its argv, so we can assert on delegation."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "ortus"
    stub.write_text('#!/usr/bin/env bash\necho "ortus $*"\nexit 7\n', encoding="utf-8")
    stub.chmod(0o755)
    return bindir


def test_triage_sh_does_not_use_askuserquestion() -> None:
    """The regression itself: the broken interactive surface is gone."""
    # Strip comments: the header explains why AskUserQuestion was dropped, so
    # only executable lines are evidence of the flow still being wired up.
    code = "\n".join(
        line
        for line in TRIAGE_SH.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "AskUserQuestion" not in code
    assert "--allowedTools" not in code


def test_triage_sh_delegates_to_ortus_verb(fake_ortus: Path) -> None:
    result = run_triage(path=f"{fake_ortus}:/usr/bin:/bin")
    assert "ortus triage" in result.stdout
    assert "deprecated" in result.stderr


def test_triage_sh_forwards_args_and_exit_code(fake_ortus: Path) -> None:
    result = run_triage("/some/repo", path=f"{fake_ortus}:/usr/bin:/bin")
    assert "ortus triage /some/repo" in result.stdout
    # exec means the verb's exit code is the script's exit code.
    assert result.returncode == 7


def test_triage_sh_errors_clearly_when_cli_missing() -> None:
    result = run_triage(path="/usr/bin:/bin")
    assert result.returncode == 1
    assert "not on PATH" in result.stderr
    assert "uv tool install ortus" in result.stderr


def test_triage_sh_help_exits_zero_without_delegating() -> None:
    result = run_triage("--help", path="/usr/bin:/bin")
    assert result.returncode == 0
    assert "deprecation shim" in result.stdout


@pytest.mark.parametrize(
    ("args", "env"),
    [
        (("--backend", "codex"), {}),
        (("--backend=codex",), {}),
        ((), {"ORTUS_BACKEND": "codex"}),
    ],
    ids=["flag", "flag-equals", "env"],
)
def test_triage_sh_refuses_under_codex(
    fake_ortus: Path, args: tuple[str, ...], env: dict[str, str]
) -> None:
    """ortus-nyd9: the context phase is still `claude -p`, so codex refuses.

    The stub `ortus` is on PATH, so reaching the delegation would show up as
    "ortus triage" on stdout — its absence is the evidence the gate fired
    first rather than quietly running Claude under a codex project (NFR-005).
    """
    result = subprocess.run(
        [BASH, str(TRIAGE_SH), *args],
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": f"{fake_ortus}:/usr/bin:/bin", **env},
    )
    assert result.returncode == 3
    assert "not available under the 'codex' backend" in result.stderr
    assert "ortus triage" not in result.stdout


def test_triage_sh_backend_flag_is_not_forwarded(fake_ortus: Path) -> None:
    """--backend is consumed here; the verb never sees a flag it doesn't take."""
    result = run_triage(
        "--backend", "claude", "/some/repo", path=f"{fake_ortus}:/usr/bin:/bin"
    )
    assert "ortus triage /some/repo" in result.stdout
    assert "--backend" not in result.stdout


def test_triage_sh_help_documents_the_backend_flag() -> None:
    result = run_triage("--help", path="/usr/bin:/bin")
    assert result.returncode == 0
    assert "--backend" in result.stdout
    assert "3   Selected backend cannot run this flow" in result.stdout


def test_triage_sh_mirrored_to_template() -> None:
    assert TRIAGE_SH.read_text(encoding="utf-8") == TEMPLATE_TRIAGE_SH.read_text(
        encoding="utf-8"
    )
