"""Tests for ortus/interview.sh backend gating (ortus-nyd9).

interview.sh is driven by AskUserQuestion, a Claude Code surface with no Codex
equivalent (ortus-0a1k). Under a codex-selected project it must refuse up
front rather than launch a session that stalls on a question the backend
cannot ask (NFR-004/NFR-005). These tests pin that gate, and pin that the
claude path is untouched by it.
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
INTERVIEW_SH = REPO_ROOT / "ortus" / "interview.sh"
TEMPLATE_INTERVIEW_SH = REPO_ROOT / "template" / "ortus" / "interview.sh"

# The gate must fire without waiting on anything. Well under this bound in
# practice; the point of the timeout is that a hang fails the test loudly
# instead of wedging the suite.
BOUND_SECONDS = 20


def run_interview(*args: str, env: dict[str, str] | None = None, stdin: int = subprocess.DEVNULL):
    return subprocess.run(
        [BASH, str(INTERVIEW_SH), *args],
        capture_output=True,
        text=True,
        timeout=BOUND_SECONDS,
        stdin=stdin,
        env={"PATH": "/usr/bin:/bin", "NO_COLOR": "1", **(env or {})},
    )


@pytest.mark.parametrize(
    ("args", "env"),
    [
        (("--backend", "codex"), {}),
        (("--backend=codex",), {}),
        ((), {"ORTUS_BACKEND": "codex"}),
    ],
    ids=["flag", "flag-equals", "env"],
)
def test_interview_refuses_under_codex(args: tuple[str, ...], env: dict[str, str]) -> None:
    """Every route to selecting codex hits the same fast, explicit refusal."""
    result = run_interview(*args, env=env)
    assert result.returncode == 3
    assert "not available under the 'codex' backend" in result.stderr
    assert "AskUserQuestion" in result.stderr
    # It must never have reached the feature search or the selection prompt.
    assert "Searching for features" not in result.stdout


def test_interview_refusal_is_bounded_with_idle_stdin() -> None:
    """NFR-004: an stdin that never answers must not turn the gate into a hang.

    subprocess.PIPE with nothing written is the harshest version of the
    interactive case: the pipe stays open forever. A `read` reached before the
    gate would block until the timeout and raise TimeoutExpired.
    """
    result = run_interview("--backend", "codex", stdin=subprocess.PIPE)
    assert result.returncode == 3


def test_interview_rejects_unknown_backend() -> None:
    """Validation stays in backend.sh, so the message matches every launcher."""
    result = run_interview("--backend", "nope")
    assert result.returncode == 1
    assert "unknown backend 'nope'" in result.stderr


def test_interview_claude_path_is_not_gated() -> None:
    """AC (3): under claude the gate is transparent — the flow proceeds.

    `bd` is absent from the controlled PATH, so the run cannot complete; what
    matters is that it got past the gate into the feature search rather than
    exiting 3.
    """
    result = run_interview("--backend", "claude")
    assert result.returncode != 3
    assert "not available under the 'claude' backend" not in result.stderr


def test_interview_help_documents_the_backend_flag() -> None:
    result = run_interview("--help")
    assert result.returncode == 0
    assert "--backend" in result.stdout
    # The help slice must reach the end of the header, including the new code.
    assert "3 - Selected backend cannot run this flow" in result.stdout


def test_interview_sh_mirrored_to_template() -> None:
    assert INTERVIEW_SH.read_text(encoding="utf-8") == TEMPLATE_INTERVIEW_SH.read_text(
        encoding="utf-8"
    )
