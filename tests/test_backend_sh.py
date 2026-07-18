"""Tests for ortus/lib/backend.sh — the claude-only backend adapter.

Each case sources the lib in a fresh bash, calls a function, and prints the
resulting array one element per line so the assertions compare argv elements
rather than a re-split string.
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


# FR-005 — CODEX_HOME must be project-local so a codex run reads the generated
# .codex/config.toml and never the operator's global ~/.codex.
def test_backend_env_points_codex_home_at_the_project() -> None:
    proc = run_bash(
        'cd /tmp && backend_env && echo "$CODEX_HOME"',
        env={"ORTUS_BACKEND": "codex"},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "/tmp/.codex"


def test_backend_env_is_a_no_op_for_claude() -> None:
    """Claude discovers .claude/ in cwd on its own; exporting CODEX_HOME for it
    would be noise, and must not happen (NFR-001 zero-change default)."""
    proc = run_bash('backend_env && echo "[${CODEX_HOME:-unset}]"')
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "[unset]"


def test_backend_env_respects_an_explicit_codex_home() -> None:
    proc = run_bash(
        'backend_env && echo "$CODEX_HOME"',
        env={"ORTUS_BACKEND": "codex", "CODEX_HOME": "/elsewhere/.codex"},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "/elsewhere/.codex"


def test_backend_argv_sets_codex_home_before_refusing_an_unimplemented_backend() -> None:
    """backend_argv calls backend_env first, so the codex argv branch (FR-004)
    inherits the project-local config directory without extra wiring."""
    proc = run_bash(
        'cd /tmp && { backend_argv goal P || true; }; echo "$CODEX_HOME"',
        env={"ORTUS_BACKEND": "codex"},
    )
    assert proc.stdout.strip() == "/tmp/.codex"


# FR-002 — precedence is flag > ORTUS_BACKEND > copier-generated default.
# The table is every reachable combination of the three inputs; `None` means
# "input absent at this layer", which is what makes the next layer win.
RESOLUTION_CASES = [
    # (flag, ORTUS_BACKEND, ORTUS_BACKEND_DEFAULT, expected)
    ("codex", "claude", "claude", "codex"),  # flag beats env
    ("codex", None, "claude", "codex"),  # flag beats generated default
    ("claude", "codex", "codex", "claude"),  # flag wins in both directions
    (None, "codex", "claude", "codex"),  # env beats generated default
    (None, "claude", "codex", "claude"),
    (None, None, "codex", "codex"),  # generated default is the last resort
    (None, None, "claude", "claude"),
    (None, None, None, "claude"),  # unrendered template still resolves
]


@pytest.mark.parametrize("flag,env_backend,generated,expected", RESOLUTION_CASES)
def test_resolve_backend_precedence(flag, env_backend, generated, expected) -> None:
    env = {}
    if env_backend is not None:
        env["ORTUS_BACKEND"] = env_backend
    if generated is not None:
        env["ORTUS_BACKEND_DEFAULT"] = generated
    proc = run_bash(f'resolve_backend "{flag or ""}"', env=env)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == expected


@pytest.mark.parametrize("layer", ["flag", "env", "generated"])
def test_resolve_backend_rejects_a_bogus_value_at_any_layer(layer) -> None:
    """An invalid backend fails identically however it arrived — validation
    lives in resolve_backend, not in each launcher's flag parser."""
    flag = "bogus" if layer == "flag" else ""
    env = {}
    if layer == "env":
        env["ORTUS_BACKEND"] = "bogus"
    if layer == "generated":
        env["ORTUS_BACKEND_DEFAULT"] = "bogus"
    proc = run_bash(f'resolve_backend "{flag}" || echo rc=$?', env=env)
    assert "rc=1" in proc.stdout
    assert "unknown backend 'bogus'" in proc.stderr
    assert "claude, codex" in proc.stderr, "error must name the valid choices"


def _run_launcher(script: str, *args: str, env=None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [BASH, str(REPO_ROOT / "ortus" / script), *args],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=REPO_ROOT,
        env={"PATH": "/usr/bin:/bin", "HOME": "/tmp", **(env or {})},
    )


def test_goal_sh_backend_flag_beats_env() -> None:
    proc = _run_launcher("goal.sh", "--backend", "codex", "--dry-run", env={"ORTUS_BACKEND": "claude"})
    assert proc.returncode == 0, proc.stderr
    assert "ORTUS_BACKEND=codex" in proc.stdout


def test_goal_sh_falls_back_to_env_then_generated_default() -> None:
    proc = _run_launcher("goal.sh", "--dry-run", env={"ORTUS_BACKEND": "codex"})
    assert "ORTUS_BACKEND=codex" in proc.stdout
    proc = _run_launcher("goal.sh", "--dry-run")
    assert "ORTUS_BACKEND=claude" in proc.stdout


def test_goal_sh_rejects_a_bogus_backend() -> None:
    proc = _run_launcher("goal.sh", "--backend", "bogus", "--dry-run")
    assert proc.returncode == 1
    assert "unknown backend 'bogus'" in proc.stderr
    assert "claude, codex" in proc.stderr


def test_idea_sh_resolves_identically() -> None:
    """Acceptance #3: idea.sh shares goal.sh's resolution, including the
    failure mode — it does not reimplement precedence or validation."""
    proc = _run_launcher("idea.sh", "--backend", "bogus", "--prd", "prd/nope.md")
    assert proc.returncode == 1
    assert "unknown backend 'bogus'" in proc.stderr
    assert "claude, codex" in proc.stderr

    proc = _run_launcher("idea.sh", "--backend")
    assert proc.returncode == 1
    assert "--backend requires a value" in proc.stderr


def test_template_mirror_is_byte_identical() -> None:
    """Parity: the distributable mirror must not drift from the working copy."""
    assert TEMPLATE_BACKEND_SH.is_file()
    assert TEMPLATE_BACKEND_SH.read_bytes() == BACKEND_SH.read_bytes()
