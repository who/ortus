"""Argv-parity regression test (NFR-001, ortus-rqoy).

The backend adapter refactor moved three `claude ...` invocations out of the
launchers and behind ortus/lib/backend.sh. The whole bet is that the default
path — backend=claude — did not change at all. This test is what makes that
bet checkable: it replays each role through the adapter and compares the
result, element by element, against argv captured from the pre-refactor tree
(tests/fixtures/argv-parity-claude.json).

Element-wise, not joined: `claude -p X --verbose` and `claude --verbose -p X`
join to different strings but so do a hundred harmless whitespace differences,
and joining hides which position moved. Comparing positions catches reordering
and says exactly where.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests._platform import skip_on_windows_bash_shim

pytestmark = skip_on_windows_bash_shim

REPO_ROOT = Path(__file__).parent.parent
BASH = shutil.which("bash") or "/bin/bash"
BACKEND_SH = REPO_ROOT / "ortus" / "lib" / "backend.sh"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "argv-parity-claude.json"

# Every role the adapter implements. Named here rather than derived from the
# fixture so that deleting a case from the fixture fails the suite instead of
# quietly shrinking its coverage.
ALL_ROLES = ("goal", "prd-decompose", "idea-expand")

CASES = json.loads(FIXTURE.read_text())["cases"]


def _adapter_argv(case: dict) -> list[str]:
    """Source backend.sh in a fresh bash and print BACKEND_ARGV one element per
    line. Prompts are passed as positional args ("$@"), never interpolated into
    the script, so no quoting of ours can perturb what the adapter sees."""
    prelude = ""
    if case["claude_cmd"] is not None:
        elements = " ".join(f'"{e}"' for e in case["claude_cmd"])
        prelude = f"CLAUDE_CMD=({elements})\n"

    script = (
        f'set -euo pipefail\nsource "{BACKEND_SH}"\n{prelude}'
        f'backend_argv {case["role"]} "$@"\n'
        f'printf "%s\\n" "${{BACKEND_ARGV[@]}}"\n'
    )
    proc = subprocess.run(
        [BASH, "-c", script, "bash", *case["args"]],
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": "/usr/bin:/bin", "ORTUS_BACKEND": "claude", **case["env"]},
    )
    assert proc.returncode == 0, f"backend_argv failed: {proc.stderr}"
    return proc.stdout.splitlines()


def test_fixture_covers_every_role() -> None:
    """A role with no golden fixture is an untested invocation."""
    assert {c["role"] for c in CASES} == set(ALL_ROLES)


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_claude_argv_matches_pre_refactor_golden(case: dict) -> None:
    actual = _adapter_argv(case)
    expected = case["expect"]

    # Report the first positional divergence rather than dumping two lists and
    # leaving the reader to diff them — a moved flag reads as "position 3" and
    # not as "these two long arrays differ somewhere".
    for i, (got, want) in enumerate(zip(actual, expected)):
        assert got == want, (
            f"argv drift at position {i} for role {case['role']!r}: "
            f"expected {want!r}, got {got!r}\n"
            f"  golden: {expected}\n  actual: {actual}"
        )

    assert len(actual) == len(expected), (
        f"argv length drift for role {case['role']!r}: "
        f"expected {len(expected)} elements, got {len(actual)}\n"
        f"  golden: {expected}\n  actual: {actual}"
    )
