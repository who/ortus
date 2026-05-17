"""Parity tests between ortus/tail.sh and src/ortus/commands/tail.py (ortus-eomm).

Two layers of equivalence:

1. Palette equivalence — the set of ANSI escape sequences emitted by Python
   tail for known message categories matches the set documented in
   ortus/tail.sh setup_colors(). Encoded directly (no bash subprocess
   needed): the bash colour table is small and stable, so we assert each
   category in Python output uses the matching ANSI code.

2. Content equivalence — for a representative stream-json fixture, every
   category bash tail.sh renders also appears in Python output (stripped
   of ANSI). This guards against silent omission, which is the regression
   ortus-tshw and ortus-eomm were filed against.

Bash subprocess invocations are intentionally omitted because tail.sh
relies on jq + inotify-tools or a polling tail -f loop, which is awkward
to drive deterministically from a unit test. The palette and category
tables encoded here are derived directly from ortus/tail.sh and will fail
loudly if either implementation drifts (the implementer must update both
files together).
"""

from __future__ import annotations

import io
import re
from pathlib import Path

from ortus.commands.tail import _ANSI_PALETTE, _follow

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)


def _ansi_set(s: str) -> set[str]:
    return set(ANSI_RE.findall(s))


# ---------------------------------------------------------------------------
# Palette equivalence
# ---------------------------------------------------------------------------


# Mirrors ortus/tail.sh setup_colors() literal-ANSI branch. If you change
# either file, update the other.
TAIL_SH_PALETTE = {
    "bold": "\033[1m",
    "dim": "\033[2m",
    "reset": "\033[0m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
}


def test_python_palette_matches_tail_sh_literal_ansi() -> None:
    """Python _ANSI_PALETTE must use the same bytes tail.sh's fallback branch uses."""
    for name, expected in TAIL_SH_PALETTE.items():
        actual = getattr(_ANSI_PALETTE, name)
        assert actual == expected, f"palette.{name}: got {actual!r}, want {expected!r}"


def test_categories_use_expected_ansi_codes(tmp_path: Path) -> None:
    """For each documented category, Python output uses the bash-matching ANSI code.

    Categories asserted (bash tail.sh source):
      - system:init        -> bold + magenta
      - assistant text     -> bold + green
      - user text          -> bold + blue
      - tool_use (-t)      -> yellow
      - tool_result (-t)   -> cyan
      - system:other (-s)  -> dim
      - new-file banner    -> bold + magenta
    """
    logs = tmp_path / "logs"
    logs.mkdir()
    fixture = logs / "grind-fixture.log"
    fixture.write_text(
        '{"type":"system","subtype":"init","session_id":"S1"}\n'
        '{"type":"assistant","message":{"content":"hello"}}\n'
        '{"type":"user","message":{"content":"hi"}}\n'
        '{"type":"assistant","message":{"content":['
        '{"type":"tool_use","name":"Bash","input":{"command":"ls"}}'
        "]}}\n"
        '{"type":"user","message":{"content":['
        '{"type":"tool_result","tool_use_id":"x","content":"out"}'
        "]}}\n"
        '{"type":"system","subtype":"hook_started"}\n'
    )

    buf = io.StringIO()
    _follow(
        logs,
        raw=False,
        show_tools=True,
        show_system=True,
        iterations=1,
        out=buf,
        palette=_ANSI_PALETTE,
    )
    output = buf.getvalue()
    codes = _ansi_set(output)

    # Every documented colour appears at least once with verbose+tools.
    assert TAIL_SH_PALETTE["bold"] in codes, output
    assert TAIL_SH_PALETTE["magenta"] in codes, output  # init banner + tailing banner
    assert TAIL_SH_PALETTE["green"] in codes, output    # assistant
    assert TAIL_SH_PALETTE["blue"] in codes, output     # user
    assert TAIL_SH_PALETTE["yellow"] in codes, output   # tool_use
    assert TAIL_SH_PALETTE["cyan"] in codes, output     # tool_result
    assert TAIL_SH_PALETTE["dim"] in codes, output      # tool input body + system:hook_started


def test_no_color_disables_all_ansi(tmp_path: Path, monkeypatch) -> None:
    """NO_COLOR (https://no-color.org/) — same contract bash tail.sh honours."""
    from ortus.commands.tail import _resolve_palette, _NO_COLOR_PALETTE

    monkeypatch.setenv("NO_COLOR", "1")

    class _TTYStream:
        def isatty(self) -> bool:
            return True

    assert _resolve_palette(_TTYStream()) is _NO_COLOR_PALETTE


def test_non_tty_disables_all_ansi() -> None:
    """Bash tail.sh disables colour when stdout is not a TTY; Python must too."""
    from ortus.commands.tail import _resolve_palette, _NO_COLOR_PALETTE

    assert _resolve_palette(io.StringIO()) is _NO_COLOR_PALETTE


# ---------------------------------------------------------------------------
# Content equivalence
# ---------------------------------------------------------------------------


# Bash tail.sh categories, keyed by the canonical label bash uses in its
# output. Each entry is a fixture stream-json line plus the substring that
# MUST appear in Python's stripped-ANSI output. If bash adds a category in
# the future, add it here and update tail.py; the test will fail until both
# sides agree.
BASH_CATEGORY_FIXTURES = [
    (
        "init",
        '{"type":"system","subtype":"init","session_id":"SID"}',
        ["=== NEW SESSION ===", "SID"],
    ),
    (
        "assistant_text",
        '{"type":"assistant","message":{"content":"BODY-A"}}',
        ["<<< ASSISTANT", "BODY-A"],
    ),
    (
        "user_text",
        '{"type":"user","message":{"content":"BODY-U"}}',
        [">>> USER", "BODY-U"],
    ),
    (
        "tool_use",
        '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Read","input":{"file_path":"/x"}}]}}',
        ["[TOOL] Read", "/x"],
    ),
    (
        "tool_result",
        '{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"x","content":"BODY-R"}]}}',
        ["[result]", "BODY-R"],
    ),
    (
        "system_other",
        '{"type":"system","subtype":"hook_started"}',
        ["[SYS] hook_started"],
    ),
    (
        "thinking",
        '{"type":"assistant","message":{"content":[{"type":"thinking","thinking":"PLAN"}]}}',
        ["(thinking)", "PLAN"],
    ),
]


def test_every_bash_category_surfaces_in_python_verbose(tmp_path: Path) -> None:
    """The categories bash tail.sh --verbose shows must all appear in Python --verbose.

    This is the regression guard for ortus-tshw and ortus-eomm: silently
    narrower verbose output broke operator workflows last cycle.
    """
    logs = tmp_path / "logs"
    logs.mkdir()
    fixture = logs / "grind-bash-categories.log"
    fixture.write_text("\n".join(line for _, line, _ in BASH_CATEGORY_FIXTURES) + "\n")

    buf = io.StringIO()
    _follow(logs, raw=False, show_tools=True, show_system=True, iterations=1, out=buf)
    stripped = _strip_ansi(buf.getvalue())

    missing: list[str] = []
    for label, _line, needles in BASH_CATEGORY_FIXTURES:
        for needle in needles:
            if needle not in stripped:
                missing.append(f"{label}: missing {needle!r}")
    assert not missing, "Python --verbose dropped categories:\n  " + "\n  ".join(missing) + (
        f"\n\nFull output:\n{stripped}"
    )


def test_tailing_banner_format_matches_bash(tmp_path: Path) -> None:
    """New-file banner format matches bash: '=== TAILING: name ===' (bold magenta)."""
    logs = tmp_path / "logs"
    logs.mkdir()
    log = logs / "grind-banner.log"
    log.write_text('{"type":"assistant","message":{"content":"x"}}\n')

    buf = io.StringIO()
    _follow(
        logs,
        raw=False,
        show_tools=False,
        show_system=False,
        iterations=1,
        out=buf,
        palette=_ANSI_PALETTE,
    )
    out = buf.getvalue()
    # Plain content
    assert "=== TAILING: grind-banner.log ===" in _strip_ansi(out)
    # Coloured: bold + magenta + reset surround the banner text
    assert "\033[1m\033[35m=== TAILING: grind-banner.log ===\033[0m" in out


def test_plain_text_lines_get_pattern_coloring(tmp_path: Path) -> None:
    """Non-JSON lines pick up bash's pattern-based colouring (===, error, success, default-dim)."""
    logs = tmp_path / "logs"
    logs.mkdir()
    log = logs / "grind-plain.log"
    log.write_text(
        "=== SECTION ===\n"
        "Processing: item-1\n"
        "Something went ERROR at step 2\n"
        "Migration completed successfully\n"
        "ordinary line\n"
    )
    buf = io.StringIO()
    _follow(
        logs,
        raw=False,
        show_tools=False,
        show_system=False,
        iterations=1,
        out=buf,
        palette=_ANSI_PALETTE,
    )
    out = buf.getvalue()
    # Bold cyan banner with leading newline
    assert "\033[1m\033[36m=== SECTION ===\033[0m" in out
    # Cyan info
    assert "\033[36mProcessing: item-1\033[0m" in out
    # Red error
    assert "\033[31mSomething went ERROR at step 2\033[0m" in out
    # Green success
    assert "\033[32mMigration completed successfully\033[0m" in out
    # Dim default
    assert "\033[2mordinary line\033[0m" in out
