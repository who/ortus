"""Bracketed caller text survives every `ortus.core.output` helper.

Rich reads `[local]` as a style tag and drops it, so a message naming a TOML
table used to reach the terminal with a hole in it. The helpers own the
escape; callers pass plain text and never need to know the renderer.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console
from rich.text import Text

from ortus.core import output

Buffers = tuple[io.StringIO, io.StringIO]


@pytest.fixture
def consoles(monkeypatch: pytest.MonkeyPatch) -> Buffers:
    """Stdout and stderr buffers behind file-backed consoles, no terminal."""
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(
        output, "_out", Console(file=out, force_terminal=False, width=200)
    )
    monkeypatch.setattr(
        output, "_err", Console(file=err, force_terminal=False, width=200)
    )
    return out, err


def test_warn_prints_bracketed_text(consoles: Buffers) -> None:
    """AC-1: `[local]` reaches stderr intact behind the warn prefix."""
    _, err = consoles
    output.warn("[local] table missing")
    assert err.getvalue() == "warn: [local] table missing\n"


def test_error_prints_bracketed_text_and_hint(consoles: Buffers) -> None:
    """AC-2: the message and the hint both keep their bracketed spans."""
    _, err = consoles
    output.error("invalid [local] field", hint="fix the [local] table")
    assert (
        err.getvalue() == "error: invalid [local] field\n       fix the [local] table\n"
    )


def test_success_prints_bracketed_text(consoles: Buffers) -> None:
    """A dotted table name is a tag to Rich too; success keeps it whole."""
    out, _ = consoles
    output.success("[profiles.codex.plan] written")
    assert out.getvalue() == "✓ [profiles.codex.plan] written\n"


def test_table_cell_keeps_bracketed_text(consoles: Buffers) -> None:
    """AC-3: a `[local]` row name and a detail naming the table both render."""
    out, _ = consoles
    output.table(
        ["Check", "Details"],
        [["[local]", "[local] table missing — run ortus init --backend local"]],
    )
    rendered = out.getvalue()
    assert "[local]" in rendered
    assert "[local] table missing" in rendered
    assert "\\" not in rendered


def test_table_styled_text_cell_passes_through(consoles: Buffers) -> None:
    """A `Text` cell is the caller's own styling (the check glyph), not markup."""
    out, _ = consoles
    output.table(["", "Check"], [[Text("✓", style="green"), "bd"]])
    rendered = out.getvalue()
    assert "✓" in rendered
    assert "[green]" not in rendered
    assert "\\" not in rendered


def test_closing_tag_prints_literally(consoles: Buffers) -> None:
    """Text carrying closing tags is caller text and prints as written."""
    _, err = consoles
    output.warn("hook printed [/] and [dim]x[/dim]")
    assert "hook printed [/] and [dim]x[/dim]" in err.getvalue()


def test_plain_text_is_unchanged(consoles: Buffers) -> None:
    """Escaping only touches brackets: bracket-free lines are byte-identical."""
    out, err = consoles
    output.success("did the thing")
    output.warn("watch out")
    output.error("boom", hint="try X")
    assert out.getvalue() == "✓ did the thing\n"
    assert err.getvalue() == "warn: watch out\nerror: boom\n       try X\n"
