"""Tests for ortus/lib/backend.sh — the claude-only backend adapter.

Each case sources the lib in a fresh bash, calls a function, and prints the
resulting array one element per line so the assertions compare argv elements
rather than a re-split string.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from tests._platform import skip_on_windows_bash_shim

pytestmark = skip_on_windows_bash_shim

REPO_ROOT = Path(__file__).parent.parent
BASH = shutil.which("bash") or "/bin/bash"
BACKEND_SH = REPO_ROOT / "ortus" / "lib" / "backend.sh"
TEMPLATE_BACKEND_SH = REPO_ROOT / "template" / "ortus" / "lib" / "backend.sh"


def run_bash(body: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    script = f'set -euo pipefail\nsource "{BACKEND_SH}"\n{body}\n'
    return subprocess.run(
        [BASH, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": "/usr/bin:/bin", **(env or {})},
    )


def argv_with_args(role: str, *args: str, env=None) -> list[str]:
    """Call backend_argv with the prompt/prd passed as positional args, so no
    caller quoting leaks into the generated script."""
    script = (
        f'set -euo pipefail\nsource "{BACKEND_SH}"\n'
        f'backend_argv {role} "$@"; printf "%s\\n" "${{BACKEND_ARGV[@]}}"\n'
    )
    proc = subprocess.run(
        [BASH, "-c", script, "bash", *args],
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": "/usr/bin:/bin", **(env or {})},
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.splitlines()


def test_all_four_functions_exist_after_sourcing() -> None:
    """Acceptance #1: the four public functions are defined by sourcing alone."""
    for fn in ("backend_argv", "backend_stream_flags", "backend_available", "backend_preflight"):
        proc = run_bash(f"declare -F {fn} >/dev/null && echo yes")
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "yes", f"{fn} not defined"


def test_backend_argv_publishes_an_array_not_a_string() -> None:
    """Acceptance #2: BACKEND_ARGV is a bash array, and elements keep their spaces."""
    proc = run_bash(
        'backend_argv goal "two words"; '
        'declare -p BACKEND_ARGV | head -c 12; echo; '
        'echo "${#BACKEND_ARGV[@]}"'
    )
    assert proc.returncode == 0, proc.stderr
    kind, count = proc.stdout.splitlines()
    assert kind.startswith("declare -a"), f"BACKEND_ARGV is not an array: {kind!r}"
    assert count == "7"


def test_goal_role_matches_claude_reference_argv() -> None:
    """Acceptance #3: goal role == the Claude row of the adapter contract."""
    assert argv_with_args("goal", "/goal DO THING") == [
        "claude",
        "-p",
        "/goal DO THING",
        "--output-format",
        "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
    ]


def test_goal_role_appends_fast_mode_only_when_set() -> None:
    with_fast = argv_with_args("goal", "P", env={"FAST_MODE": "--fast"})
    assert with_fast[-1] == "--fast"
    assert "" not in argv_with_args("goal", "P")


def test_prd_decompose_role_matches_claude_reference_argv() -> None:
    assert argv_with_args("prd-decompose", "PROMPT", "prd/PRD-x.md") == [
        "claude",
        "--allowedTools",
        "Read(prd/PRD-x.md),Bash(bd:*)",
        "--dangerously-skip-permissions",
        "-p",
        "PROMPT",
    ]


def test_idea_expand_role_matches_claude_reference_argv() -> None:
    assert argv_with_args("idea-expand", "expand this") == [
        "claude",
        "--print",
        "expand this",
    ]


def test_stream_flags_match_the_goal_argv() -> None:
    proc = run_bash('backend_stream_flags; printf "%s\\n" "${BACKEND_STREAM_FLAGS[@]}"')
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == ["--output-format", "stream-json", "--verbose"]


def test_claude_cmd_prefix_is_honoured() -> None:
    """A launcher wrapping the CLI (e.g. docker sandbox) keeps its prefix."""
    proc = run_bash(
        'CLAUDE_CMD=(docker sandbox run claude --name ortus-goal --); '
        'backend_argv goal "P"; printf "%s\\n" "${BACKEND_ARGV[@]}"'
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines()[:6] == [
        "docker",
        "sandbox",
        "run",
        "claude",
        "--name",
        "ortus-goal",
    ]


def test_unknown_role_exits_non_zero_naming_valid_roles() -> None:
    proc = run_bash("backend_argv bogus P || echo rc=$?")
    assert "rc=1" in proc.stdout
    assert "goal, prd-decompose, idea-expand" in proc.stderr


def test_unimplemented_backend_is_refused() -> None:
    proc = run_bash("backend_argv goal P || echo rc=$?", env={"ORTUS_BACKEND": "codex"})
    assert "rc=1" in proc.stdout
    assert "not implemented" in proc.stderr


def test_backend_available_is_silent_and_reflects_path() -> None:
    proc = run_bash("backend_available && echo found || echo missing")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() in {"found", "missing"}
    assert proc.stderr == ""


def test_backend_preflight_names_the_missing_cli() -> None:
    """With an empty PATH the claude binary cannot be found; say so."""
    script = f'source "{BACKEND_SH}"\nbackend_preflight || echo rc=$?\n'
    proc = subprocess.run(
        [BASH, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": "/nonexistent"},
    )
    assert "rc=1" in proc.stdout
    assert "claude" in proc.stderr


def test_template_mirror_is_byte_identical() -> None:
    """Parity: the distributable mirror must not drift from the working copy."""
    assert TEMPLATE_BACKEND_SH.is_file()
    assert TEMPLATE_BACKEND_SH.read_bytes() == BACKEND_SH.read_bytes()
