"""tail.sh decoder selection from the log's own backend marker (ortus-36w3).

goal.sh stamps `# ortus-backend: <name>` as the first line of every log it
creates; tail.sh reads that marker to pick between the Claude stream-json and
Codex `codex exec --json` decoders. The contract under test:

  1. a marked log decodes with the right decoder and no flag;
  2. a markerless or unknown-marker log is a hard error, not a wrong-decoder
     render;
  3. --codex / ORTUS_BACKEND still force a decoder for raw logs captured
     outside goal.sh;
  4. the marker line itself never shows up in the rendered output.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures"
CODEX_FIXTURE = FIXTURES / "codex-exec-events.jsonl"

TAIL_SHS = [REPO_ROOT / "ortus" / "tail.sh", REPO_ROOT / "template" / "ortus" / "tail.sh"]
GOAL_SHS = [REPO_ROOT / "ortus" / "goal.sh", REPO_ROOT / "template" / "ortus" / "goal.sh"]

CLAUDE_EVENT = (
    '{"type":"assistant","message":{"content":[{"type":"text","text":"hello from claude"}]}}'
)
CODEX_EVENT = (
    '{"type":"item.completed","item":{"id":"i","type":"agent_message","text":"hello from codex"}}'
)


def _decode(tail_sh: Path, path: Path, *args: str, **env_extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(tail_sh), *args, "--decode", str(path)],
        capture_output=True,
        text=True,
        env={
            "NO_COLOR": "1",
            "SHOW_TOOLS": "true",
            "SHOW_SYSTEM": "true",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
            **env_extra,
        },
    )


def _log(tmp_path: Path, name: str, marker: str | None, body: str) -> Path:
    path = tmp_path / name
    head = f"# ortus-backend: {marker}\n" if marker is not None else ""
    path.write_text(head + body + "\n", encoding="utf-8")
    return path


@pytest.mark.parametrize("tail_sh", TAIL_SHS, ids=lambda p: p.parts[-3])
@pytest.mark.parametrize(
    "marker,event,expected",
    [("claude", CLAUDE_EVENT, "hello from claude"), ("codex", CODEX_EVENT, "hello from codex")],
)
def test_marker_selects_the_decoder_without_a_flag(
    tail_sh: Path, tmp_path: Path, marker: str, event: str, expected: str
) -> None:
    proc = _decode(tail_sh, _log(tmp_path, f"goal-{marker}.log", marker, event))
    assert proc.returncode == 0, proc.stderr
    assert expected in proc.stdout


def test_markerless_log_errors_instead_of_guessing(tmp_path: Path) -> None:
    proc = _decode(TAIL_SHS[0], _log(tmp_path, "goal-raw.log", None, CODEX_EVENT))
    assert proc.returncode != 0
    assert "no '# ortus-backend:' marker" in proc.stderr
    # No wrong-decoder garbage: the Claude branch would have silently emitted
    # nothing, but the point is that nothing is rendered at all.
    assert proc.stdout == ""


def test_unknown_marker_errors_and_names_the_value(tmp_path: Path) -> None:
    proc = _decode(TAIL_SHS[0], _log(tmp_path, "goal-weird.log", "gemini", CODEX_EVENT))
    assert proc.returncode != 0
    assert "unknown backend marker 'gemini'" in proc.stderr
    assert proc.stdout == ""


def test_explicit_flag_overrides_detection_for_raw_logs(tmp_path: Path) -> None:
    """A raw `codex exec --json` capture has no marker; --codex still decodes it."""
    proc = _decode(TAIL_SHS[0], CODEX_FIXTURE, "--codex")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip()


def test_env_backend_overrides_a_conflicting_marker(tmp_path: Path) -> None:
    """ORTUS_BACKEND is the operator's escape hatch — it wins over the marker."""
    log = _log(tmp_path, "goal-claude.log", "claude", CODEX_EVENT)
    proc = _decode(TAIL_SHS[0], log, ORTUS_BACKEND="codex")
    assert proc.returncode == 0, proc.stderr
    assert "hello from codex" in proc.stdout


@pytest.mark.parametrize("tail_sh", TAIL_SHS, ids=lambda p: p.parts[-3])
def test_marker_line_is_not_rendered(tail_sh: Path, tmp_path: Path) -> None:
    proc = _decode(tail_sh, _log(tmp_path, "goal-claude.log", "claude", CLAUDE_EVENT))
    assert "ortus-backend" not in proc.stdout


@pytest.mark.parametrize("goal_sh", GOAL_SHS, ids=lambda p: p.parts[-3])
def test_goal_sh_stamps_the_marker_before_the_first_log_write(goal_sh: Path) -> None:
    """The marker must be line 1 of the log, so detection can read a short head."""
    source = goal_sh.read_text(encoding="utf-8")
    stamp = source.index("printf '# ortus-backend: %s\\n' \"$ORTUS_BACKEND\" > \"$LOG_FILE\"")
    # `>` (truncate), not `>>`, and it precedes the definition of log().
    assert stamp < source.index("log() {")
    # The format tail.sh greps for and the format goal.sh writes must agree.
    tail_src = (goal_sh.parent / "tail.sh").read_text(encoding="utf-8")
    assert re.search(r"s/\^# ortus-backend: \\\(\[a-z\]\[a-z0-9_-\]\*\\\)\$/\\1/p", tail_src)
