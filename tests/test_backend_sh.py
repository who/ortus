"""Tests for ortus/lib/backend.sh — the claude + codex backend adapter.

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


# FR-004 — the codex goal argv is the Codex row of the Appendix A contract.
CODEX = {"ORTUS_BACKEND": "codex"}


def test_codex_goal_role_matches_reference_argv() -> None:
    assert argv_with_args("goal", "/goal DO THING", env=CODEX) == [
        "codex",
        "exec",
        "/goal DO THING",
        "--json",
        "--sandbox",
        "workspace-write",
        "--dangerously-bypass-approvals-and-sandbox",
    ]


def test_codex_and_claude_goal_argv_carry_a_byte_identical_prompt() -> None:
    """Acceptance #2: the /goal string is the same across backends — only the
    flags around it differ."""
    # printf %q, not a raw print: the prompt carries newlines and quotes, and
    # a raw print would let the comparison pass on a re-split fragment.
    body = 'backend_argv goal "$1"; printf "%q\\n" "${BACKEND_ARGV[2]}"'
    prompt = "/goal close one issue\nline two \"quoted\""

    def quoted_prompt(env):
        script = f'set -euo pipefail\nsource "{BACKEND_SH}"\n{body}\n'
        proc = subprocess.run(
            [BASH, "-c", script, "bash", prompt],
            capture_output=True, text=True, timeout=30,
            env={"PATH": "/usr/bin:/bin", **env},
        )
        assert proc.returncode == 0, proc.stderr
        return proc.stdout

    assert quoted_prompt({}) == quoted_prompt(CODEX)


def test_codex_goal_role_falls_back_to_a_real_inner_sandbox() -> None:
    """FR-010 posture is a variable, not a literal: opting out of the outer
    sandbox swaps the bypass for a real inner sandbox."""
    argv = argv_with_args("goal", "P", env={**CODEX, "ORTUS_CODEX_POSTURE": "inner"})
    assert argv[-3:] == ["workspace-write", "--ask-for-approval", "never"]
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv


def test_codex_posture_is_derived_from_the_outer_sandbox_state() -> None:
    """FR-010: the outer sandbox picks the inner posture. Enforced (the
    default, and what goal.sh's unconditional smoke test guarantees) buys the
    bypass; opting out of the outer layer buys a real inner sandbox."""
    enforced = argv_with_args("goal", "P", env={**CODEX, "ORTUS_OUTER_SANDBOX": "enforced"})
    assert enforced[-1] == "--dangerously-bypass-approvals-and-sandbox"
    # Unset must behave as "enforced" — goal.sh exits rather than launching
    # with a failed smoke test, so reaching the adapter implies the gate passed.
    assert argv_with_args("goal", "P", env=CODEX) == enforced

    opted_out = argv_with_args("goal", "P", env={**CODEX, "ORTUS_OUTER_SANDBOX": "off"})
    assert opted_out[-3:] == ["workspace-write", "--ask-for-approval", "never"]
    assert "--dangerously-bypass-approvals-and-sandbox" not in opted_out


def test_codex_posture_override_wins_over_the_outer_sandbox_state() -> None:
    """The explicit override is for the operator whose wrapper's isolation we
    cannot see; it beats the derived value in both directions."""
    argv = argv_with_args(
        "goal", "P", env={**CODEX, "ORTUS_OUTER_SANDBOX": "off", "ORTUS_CODEX_POSTURE": "bypass"}
    )
    assert argv[-1] == "--dangerously-bypass-approvals-and-sandbox"


def test_codex_rejects_an_unknown_outer_sandbox_state() -> None:
    """NFR-005: a typo'd ORTUS_OUTER_SANDBOX must not silently fall through to
    the bypass — that would be the exact silent degradation FR-010 forbids."""
    proc = run_bash(
        "backend_argv goal P || echo rc=$?", env={**CODEX, "ORTUS_OUTER_SANDBOX": "bogus"}
    )
    assert "rc=1" in proc.stdout
    assert "unknown outer sandbox state 'bogus'" in proc.stderr


def test_codex_goal_role_rejects_an_unknown_posture() -> None:
    proc = run_bash("backend_argv goal P || echo rc=$?", env={**CODEX, "ORTUS_CODEX_POSTURE": "bogus"})
    assert "rc=1" in proc.stdout
    assert "unknown codex sandbox posture 'bogus'" in proc.stderr


def test_codex_goal_role_appends_the_model_only_when_set() -> None:
    with_model = argv_with_args("goal", "P", env={**CODEX, "ORTUS_CODEX_MODEL": "gpt-5"})
    assert with_model[-2:] == ["-m", "gpt-5"]
    assert "-m" not in argv_with_args("goal", "P", env=CODEX)


def test_fast_mode_is_a_documented_no_op_under_codex() -> None:
    """Acceptance #3: --fast notices, does not appear in argv, does not error."""
    proc = run_bash(
        'backend_argv goal P; printf "%s\\n" "${BACKEND_ARGV[@]}"',
        env={**CODEX, "FAST_MODE": "--fast"},
    )
    assert proc.returncode == 0, proc.stderr
    assert "--fast" not in proc.stdout.splitlines()
    assert "no-op under the codex backend" in proc.stderr


def test_codex_stream_flag_is_json() -> None:
    proc = run_bash('backend_stream_flags; printf "%s\\n" "${BACKEND_STREAM_FLAGS[@]}"', env=CODEX)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == ["--json"]


def test_codex_refuses_the_roles_it_does_not_implement_yet() -> None:
    for role in ("prd-decompose", "idea-expand"):
        proc = run_bash(f"backend_argv {role} P || echo rc=$?", env=CODEX)
        assert "rc=1" in proc.stdout
        assert "does not implement role" in proc.stderr


def test_unknown_role_fails_identically_under_codex() -> None:
    proc = run_bash("backend_argv bogus P || echo rc=$?", env=CODEX)
    assert "rc=1" in proc.stdout
    assert "goal, prd-decompose, idea-expand" in proc.stderr


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


def test_backend_argv_sets_codex_home_for_the_codex_branch() -> None:
    """backend_argv calls backend_env first, so the codex argv branch (FR-004)
    inherits the project-local config directory without extra wiring."""
    proc = run_bash(
        'cd /tmp && backend_argv goal P; echo "$CODEX_HOME"',
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


# FR-010, third condition — the outer gate that makes the bypass defensible is
# still the gate. These sit here rather than in test_core_sandbox.py because
# what they protect is the posture contract, not lib/sandbox.sh's internals.


def test_outer_smoke_test_still_blocks_launch_when_the_sandbox_is_broken(tmp_path) -> None:
    """A deliberately broken outer sandbox (no bwrap/sandbox-exec on PATH)
    must abort, not degrade — otherwise the codex bypass would run bare."""
    # A PATH carrying uname (so the platform still resolves) but neither
    # sandbox binary: the sandbox is broken, not the shell.
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    uname = shutil.which("uname")
    assert uname, "uname is required to detect the platform"
    (stub_bin / "uname").symlink_to(uname)

    script = (
        "log() { echo \"$*\"; }\n"
        f'source "{REPO_ROOT / "ortus" / "lib" / "sandbox.sh"}"\n'
        "sandbox_smoke_test\n"
        "echo REACHED_LAUNCH\n"
    )
    proc = subprocess.run(
        [BASH, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": str(stub_bin)},
    )
    assert proc.returncode != 0
    assert "REACHED_LAUNCH" not in proc.stdout
    assert "Sandbox prerequisite missing" in proc.stdout


def test_goal_sh_publishes_the_outer_state_only_after_the_gate() -> None:
    """The export means 'the smoke test passed'. If it moved above the gate it
    would still be there after a failed check — a lie the adapter would trust."""
    body = (REPO_ROOT / "ortus" / "goal.sh").read_text(encoding="utf-8")
    gate = body.index("sandbox_smoke_test\n")
    export = body.index('export ORTUS_OUTER_SANDBOX=')
    assert gate < export


# ortus-3gox — backend_preflight: name the backend AND the fix, and refuse
# before the flock guard so a failed launch leaves no stale lock.
def preflight(*, path: str, home: Path, env: dict[str, str] | None = None):
    script = f'source "{BACKEND_SH}"\nbackend_preflight || echo rc=$?\n'
    return subprocess.run(
        [BASH, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": path, "HOME": str(home), **(env or {})},
    )


def cli_stub(tmp_path: Path, name: str) -> Path:
    """A PATH dir holding a fake backend CLI that always succeeds."""
    bin_dir = tmp_path / f"bin-{name}"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / name
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)
    return bin_dir


@pytest.mark.parametrize(
    ("backend", "binary", "install"),
    [("claude", "claude", "@anthropic-ai/claude-code"), ("codex", "codex", "@openai/codex")],
)
def test_preflight_missing_binary_names_backend_and_install(
    tmp_path: Path, backend: str, binary: str, install: str
) -> None:
    """Acceptance #1: a missing binary exits non-zero naming both."""
    proc = preflight(path="/nonexistent", home=tmp_path, env={"ORTUS_BACKEND": backend})
    assert "rc=1" in proc.stdout
    assert f"'{backend}'" in proc.stderr
    assert binary in proc.stderr
    assert install in proc.stderr


def test_preflight_unauthenticated_claude_names_the_login_command(tmp_path: Path) -> None:
    """Acceptance #2: the CLI is present but carries no credentials."""
    proc = preflight(path=str(cli_stub(tmp_path, "claude")), home=tmp_path)
    assert "rc=1" in proc.stdout
    assert "not authenticated" in proc.stderr
    assert "/login" in proc.stderr


def test_preflight_accepts_an_api_key_as_credentials(tmp_path: Path) -> None:
    """An API key in the environment is a login; refusing it would block a
    perfectly working CI run."""
    proc = preflight(
        path=str(cli_stub(tmp_path, "claude")),
        home=tmp_path,
        env={"ANTHROPIC_API_KEY": "sk-test"},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == ""


def test_preflight_accepts_a_credentials_file(tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / ".credentials.json").write_text("{}")
    proc = preflight(path=str(cli_stub(tmp_path, "claude")), home=tmp_path)
    assert proc.returncode == 0, proc.stderr


def test_goal_sh_preflights_before_taking_the_flock(tmp_path: Path) -> None:
    """Acceptance #3: a refused launch creates no lock file. The check has to
    sit above the flock block for that to hold, so assert the ordering too."""
    body = (REPO_ROOT / "ortus" / "goal.sh").read_text(encoding="utf-8")
    assert body.index("backend_preflight") < body.index("flock -n -x .beads/ralph.flock")

    # A PATH with the shell utilities goal.sh needs to reach the preflight,
    # but no backend CLI.
    bin_dir = tmp_path / "bin-nobackend"
    bin_dir.mkdir()
    for tool in ("dirname", "sed", "date"):
        real = shutil.which(tool)
        if real:
            (bin_dir / tool).symlink_to(real)

    proc = subprocess.run(
        [BASH, str(REPO_ROOT / "ortus" / "goal.sh")],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=tmp_path,
        env={"PATH": str(bin_dir), "HOME": str(tmp_path)},
    )
    assert proc.returncode != 0
    assert "not on PATH" in proc.stderr
    assert not (tmp_path / ".beads").exists()


def test_template_mirror_is_byte_identical() -> None:
    """Parity: the distributable mirror must not drift from the working copy."""
    assert TEMPLATE_BACKEND_SH.is_file()
    assert TEMPLATE_BACKEND_SH.read_bytes() == BACKEND_SH.read_bytes()
