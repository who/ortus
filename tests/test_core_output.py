"""Tests for core/output.py — rich-based formatters (NFR-005)."""

from __future__ import annotations

import datetime as dt
import io
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from ortus.core import output


def _patched_consoles(monkeypatch: pytest.MonkeyPatch) -> tuple[io.StringIO, io.StringIO]:
    """Replace stdout/stderr consoles with file-backed Consoles for capture."""
    from rich.console import Console

    out_buf = io.StringIO()
    err_buf = io.StringIO()
    monkeypatch.setattr(output, "_out", Console(file=out_buf, force_terminal=False))
    monkeypatch.setattr(output, "_err", Console(file=err_buf, force_terminal=False))
    return out_buf, err_buf


def test_info_writes_to_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    out, err = _patched_consoles(monkeypatch)
    output.info("hello")
    assert "hello" in out.getvalue()
    assert err.getvalue() == ""


def test_success_writes_to_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    out, err = _patched_consoles(monkeypatch)
    output.success("did the thing")
    assert "did the thing" in out.getvalue()
    assert err.getvalue() == ""


def test_warn_writes_to_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    out, err = _patched_consoles(monkeypatch)
    output.warn("watch out")
    assert "watch out" in err.getvalue()
    assert out.getvalue() == ""


def test_error_with_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    out, err = _patched_consoles(monkeypatch)
    output.error("boom", hint="try X")
    assert "boom" in err.getvalue()
    assert "try X" in err.getvalue()
    assert out.getvalue() == ""


def test_error_without_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    out, err = _patched_consoles(monkeypatch)
    output.error("solo error")
    assert "solo error" in err.getvalue()


def test_progress_writes_to_stderr_with_canonical_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    out, err = _patched_consoles(monkeypatch)
    output.progress("init", "creating .beads/ workspace")
    rendered = err.getvalue()
    assert "[ortus init] creating .beads/ workspace" in rendered
    assert out.getvalue() == ""


def test_progress_timestamp_format_precedes_verb_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    """A line opens with `[YYYY-MM-DD HH:MM:SS]`, zero-padded, then the verb tag."""
    out, err = _patched_consoles(monkeypatch)
    output.progress("grind", "picking next issue")
    rendered = err.getvalue().strip()
    assert re.fullmatch(
        r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] \[ortus grind\] picking next issue",
        rendered,
    ), rendered


def test_progress_timestamp_matches_log_format(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The console stamp is byte-identical to the one the grind log file writes."""
    from ortus.commands import grind as grind_cmd

    # A single-digit month/day/hour instant, so a missing zero-pad shows up.
    frozen = dt.datetime(2026, 8, 8, 1, 2, 3)

    class _FakeDatetime:
        @staticmethod
        def now(tz: object = None) -> dt.datetime:
            return frozen

    out, err = _patched_consoles(monkeypatch)
    monkeypatch.setattr(output, "_dt", SimpleNamespace(datetime=_FakeDatetime))
    monkeypatch.setattr(grind_cmd, "_dt", SimpleNamespace(datetime=_FakeDatetime))

    output.progress("grind", "picking next issue")
    log_path = tmp_path / "grind.log"
    grind_cmd._log_writer(log_path)("picking next issue")

    console_stamp = re.match(r"\[[^\]]+\]", err.getvalue().strip())
    log_stamp = re.match(r"\[[^\]]+\]", log_path.read_text(encoding="utf-8"))
    assert console_stamp and log_stamp
    assert console_stamp.group(0) == log_stamp.group(0) == "[2026-08-08 01:02:03]"


def test_progress_timestamp_is_local_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stamp is the local wall clock — the clock the grind log file prints."""
    out, err = _patched_consoles(monkeypatch)
    before = dt.datetime.now().replace(microsecond=0)
    output.progress("grind", "picking next issue")
    after = dt.datetime.now()
    stamp = re.match(r"\[([\d\-: ]+)\]", err.getvalue().strip())
    assert stamp, err.getvalue()
    parsed = dt.datetime.strptime(stamp.group(1), "%Y-%m-%d %H:%M:%S")
    assert before <= parsed <= after

    # And it is taken from a naive local `now()`, never `now(timezone.utc)`.
    tz_args: list[object] = []

    class _FakeDatetime:
        @staticmethod
        def now(tz: object = None) -> dt.datetime:
            tz_args.append(tz)
            return dt.datetime(2026, 8, 8, 13, 28, 45)

    out2, err2 = _patched_consoles(monkeypatch)
    monkeypatch.setattr(output, "_dt", SimpleNamespace(datetime=_FakeDatetime))
    output.progress("grind", "picking next issue")
    assert "[2026-08-08 13:28:45] [ortus grind] picking next issue" in err2.getvalue()
    assert tz_args == [None]


def test_progress_goes_to_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    """Progress stays on stderr so machine-readable stdout is untouched."""
    out, err = _patched_consoles(monkeypatch)
    output.progress("grind", "picking next issue")
    assert "[ortus grind] picking next issue" in err.getvalue()
    assert out.getvalue() == ""


def test_progress_escapes_markup_in_phase(monkeypatch: pytest.MonkeyPatch) -> None:
    """A phase containing `[` must not be interpreted as a rich tag."""
    out, err = _patched_consoles(monkeypatch)
    output.progress("plan", "writing [1/3] of N issues")
    rendered = err.getvalue()
    assert "[ortus plan] writing [1/3] of N issues" in rendered


def test_table_writes_to_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    out, err = _patched_consoles(monkeypatch)
    output.table(["col1", "col2"], [["a", 1], ["b", 2]])
    rendered = out.getvalue()
    assert "col1" in rendered
    assert "a" in rendered
    assert "2" in rendered
    assert err.getvalue() == ""
