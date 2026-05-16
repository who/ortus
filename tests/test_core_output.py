"""Tests for core/output.py — rich-based formatters (NFR-005)."""

from __future__ import annotations

import io

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


def test_table_writes_to_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    out, err = _patched_consoles(monkeypatch)
    output.table(["col1", "col2"], [["a", 1], ["b", 2]])
    rendered = out.getvalue()
    assert "col1" in rendered
    assert "a" in rendered
    assert "2" in rendered
    assert err.getvalue() == ""
