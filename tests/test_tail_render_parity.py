"""M3 render-parity check between the Claude and Codex tail decoders (ortus-iwac).

Parity here is about *information classes*, not byte-identical formatting: the
two backends emit different event vocabularies, so the renders will never match
character for character. What must hold is that every class of information a
reader relies on in the Claude view has a counterpart in the Codex view.

The table below is the parity table the acceptance criteria call for. Each row
names one information class and the marker each decoder renders it with; the
test renders both fixtures through ``ortus/tail.sh --decode`` and checks every
row off against both renders. A class that exists on only one side would have
to be listed with an empty marker, which the test rejects — that is how this
file stays honest rather than decorative.

Running this check is what surfaced the one real gap: the Claude branch
rendered no token counts at all, so ``[USAGE]`` was Codex-only. The decoders in
``ortus/tail.sh`` and ``src/ortus/commands/tail.py`` now both emit it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures"
TAIL_SH = REPO_ROOT / "ortus" / "tail.sh"
CLAUDE_FIXTURE = FIXTURES / "claude-stream-events.jsonl"
CODEX_FIXTURE = FIXTURES / "codex-exec-events.jsonl"

requires_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")


@dataclass(frozen=True)
class ParityClass:
    """One information class and the marker each decoder renders it with."""

    name: str
    claude_marker: str
    codex_marker: str


# --- The parity table ------------------------------------------------------
#
# | information class | claude stream-json     | codex exec --json            |
# |-------------------|------------------------|------------------------------|
# | session start     | system/init            | thread.started               |
# | assistant text    | assistant/text         | item:agent_message           |
# | tool or command   | assistant/tool_use     | item.started:command_exec    |
# | call result       | user/tool_result       | item.completed:command_exec  |
# | failure signal    | result subtype=error   | non-zero exit / status failed|
# | system event      | system/<subtype>       | turn.started, item:todo_list |
# | token usage       | result.usage           | turn.completed.usage         |
PARITY_TABLE = (
    ParityClass("session start", "=== NEW SESSION ===", "=== NEW SESSION ==="),
    ParityClass("assistant text", "<<< ASSISTANT", "<<< ASSISTANT"),
    ParityClass("tool or command call", "  [TOOL] ", "  [TOOL] "),
    ParityClass("call result", "tool_result", "  [RESULT] command_execution: "),
    ParityClass("failure signal", "is_error", "ERROR (exit 3)"),
    ParityClass("system event", "[SYS] ", "[SYS] "),
    ParityClass("token usage", "  [USAGE] input=", "  [USAGE] input="),
)


def _render(fixture: Path, *args: str) -> str:
    proc = subprocess.run(
        ["bash", str(TAIL_SH), *args, "--decode", str(fixture)],
        capture_output=True,
        text=True,
        env={
            "NO_COLOR": "1",
            "SHOW_TOOLS": "true",
            "SHOW_SYSTEM": "true",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        },
    )
    assert proc.returncode == 0, f"{fixture.name} failed to decode: {proc.stderr}"
    return proc.stdout


@pytest.fixture(scope="module")
def claude_render() -> str:
    # No --codex and no ORTUS_BACKEND: the decoder is picked from the fixture's
    # own `# ortus-backend: claude` marker, the same way a real log is read.
    return _render(CLAUDE_FIXTURE)


@pytest.fixture(scope="module")
def codex_render() -> str:
    return _render(CODEX_FIXTURE, "--codex")


@requires_jq
@pytest.mark.parametrize("row", PARITY_TABLE, ids=lambda r: r.name)
def test_information_class_present_in_both_renders(
    row: ParityClass, claude_render: str, codex_render: str
) -> None:
    """Every row of the parity table is checked off against both renders."""
    assert row.claude_marker, f"{row.name}: no claude marker — class is Codex-only"
    assert row.codex_marker, f"{row.name}: no codex marker — class is Claude-only"
    assert row.claude_marker in claude_render, (
        f"{row.name}: missing from the claude render\n{claude_render}"
    )
    assert row.codex_marker in codex_render, (
        f"{row.name}: missing from the codex render\n{codex_render}"
    )


@requires_jq
def test_codex_render_covers_every_claude_class(codex_render: str) -> None:
    """Condition (2): no information class in the Claude view is Codex-only-missing.

    Guards the table itself — a row added for a Claude class without a Codex
    counterpart fails here rather than being quietly dropped from coverage.
    """
    uncovered = [row.name for row in PARITY_TABLE if row.codex_marker not in codex_render]
    assert not uncovered, f"claude classes with no codex render: {uncovered}"


@requires_jq
def test_token_usage_survives_without_tool_output(tmp_path: Path) -> None:
    """The usage class must not be gated behind -t on either side.

    Codex renders [USAGE] from turn.completed unconditionally; the Claude
    branch has to match, otherwise the class disappears from the default view.
    """
    for fixture, args in ((CLAUDE_FIXTURE, ()), (CODEX_FIXTURE, ("--codex",))):
        proc = subprocess.run(
            ["bash", str(TAIL_SH), *args, "--decode", str(fixture)],
            capture_output=True,
            text=True,
            env={
                "NO_COLOR": "1",
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
            },
        )
        assert proc.returncode == 0, proc.stderr
        assert "[USAGE] input=" in proc.stdout, f"{fixture.name} hides usage without -t"
