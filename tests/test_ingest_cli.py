"""Tests for the `ortus ingest` verb: validate-then-create, or create nothing."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from tests._shims import ready_issue_args
from tests.conftest import copy_bd_workspace

pytestmark = pytest.mark.integration
runner = CliRunner()


def _ready_fields() -> dict[str, str]:
    """The fixture leaf's packet, keyed by issue field.

    Reuses the one hand-authored readiness schema v1 body the suite already
    trusts, so these tests cannot drift from the contract the validator
    enforces for every other fixture.
    """
    args = ready_issue_args()
    flags = dict(zip(args[::2], args[1::2]))
    return {
        "description": flags["--description"],
        "design": flags["--design"],
        "acceptance_criteria": flags["--acceptance"],
    }


def _write_packet(directory: Path, **overrides: str) -> Path:
    """Write a packet directory, replacing named sections with `overrides`."""
    directory.mkdir(parents=True, exist_ok=True)
    fields = dict(_ready_fields())
    fields.update(overrides)
    names = {
        "description": "description.md",
        "design": "design.md",
        "acceptance_criteria": "acceptance.md",
    }
    for field, text in fields.items():
        (directory / names[field]).write_text(text, encoding="utf-8")
    return directory


def _open_ids(repo: Path) -> list[str]:
    """Every issue id in the workspace, closed ones included."""
    listing = subprocess.run(
        ["bd", "list", "--all", "--limit", "0", "--json"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [str(item["id"]) for item in json.loads(listing or "[]")]


_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    """Drop CSI sequences rich injects between option glyphs on CI."""
    return _ANSI.sub("", text)


def _flat(text: str) -> str:
    """Help output with rich's wrapping and colour codes folded away.

    Both panels wrap to whatever width the runner picked, so a phrase the help
    must carry can land across two lines. Flattening asserts the wording rather
    than the terminal width of the machine running the suite. Typer also force-
    colours when GITHUB_ACTIONS is set, splitting ``--packet`` as styled dashes.
    """
    return " ".join(_strip_ansi(text).split())


def test_ingest_help_describes_one_command_without_subcommands() -> None:
    """AC-1: ingest is a single verb, not a group with create/fill/ready."""
    result = runner.invoke(app, ["ingest", "--help"])
    assert result.exit_code == 0, result.stdout + result.stderr
    flat = _flat(result.stdout)
    assert "ortus ingest" in flat
    for absent in ("create", "fill", "ready"):
        assert f"ingest {absent}" not in flat
    for flag in ("--packet", "--stdin", "--title", "--type", "--priority"):
        assert flag in flat


def test_top_level_help_sends_a_filing_agent_to_ingest() -> None:
    """AC-1: `ortus --help` alone names ingest as the way to file a bead."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.stdout + result.stderr
    flat = _flat(result.stdout)
    assert "readiness schema v1 bead from a packet" in flat
    assert "in place of a multiline bd create" in flat


def test_ingest_help_documents_packet_shape_spec_and_exit_codes() -> None:
    """AC-2: the verb's own help is the discovery surface for a sidecar."""
    result = runner.invoke(app, ["ingest", "--help"])
    assert result.exit_code == 0, result.stdout + result.stderr
    flat = _flat(result.stdout)
    for needle in ("description.md", "design.md", "acceptance.md", "ortus spec"):
        assert needle in flat, needle
    assert "Exit 0 means the bead exists" in flat
    assert "nonzero exit" in flat and "no bead was written" in flat


def test_ingest_ready_packet_creates_issue_that_validates(tmp_path: Path) -> None:
    """AC-2: a readiness-shaped packet becomes a bead `ortus validate` calls READY."""
    repo = copy_bd_workspace(tmp_path / "repo", "bare").path
    packet = _write_packet(tmp_path / "packet")

    result = runner.invoke(
        app, ["ingest", str(repo), "--packet", str(packet), "--title", "Filed by ingest"]
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    issue_id = result.stdout.strip()
    assert issue_id and issue_id in _open_ids(repo)
    assert f"done (created {issue_id})" in _flat(result.stderr)

    verdict = runner.invoke(app, ["validate", str(repo), issue_id])
    assert verdict.exit_code == 0, verdict.stdout + verdict.stderr
    assert verdict.stdout.splitlines() == [f"READY {issue_id}"]


def test_ingest_unready_packet_creates_nothing(tmp_path: Path) -> None:
    """AC-3: an incomplete packet exits nonzero with a validate-style row."""
    repo = copy_bd_workspace(tmp_path / "repo", "bare").path
    before = _open_ids(repo)
    packet = _write_packet(tmp_path / "packet", design="## Scope\nTBD")

    result = runner.invoke(
        app, ["ingest", str(repo), "--packet", str(packet), "--title", "Half a packet"]
    )

    assert result.exit_code == 1, result.stdout + result.stderr
    row = result.stdout.strip()
    assert row.startswith("UNREADY <packet>: "), row
    assert "design/readiness schema" in row
    assert _open_ids(repo) == before


def test_ingest_stdin_json_packet_creates_issue(tmp_path: Path) -> None:
    """The JSON path carries the same fields, plus title/type/priority."""
    repo = copy_bd_workspace(tmp_path / "repo", "bare").path
    payload = dict(_ready_fields())
    payload.update({"title": "Filed from stdin", "issue_type": "bug", "priority": 1})

    result = runner.invoke(
        app, ["ingest", str(repo), "--stdin"], input=json.dumps(payload)
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    issue_id = result.stdout.strip()
    shown = json.loads(
        subprocess.run(
            ["bd", "show", issue_id, "--json"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )[0]
    assert shown["title"] == "Filed from stdin"
    assert shown["issue_type"] == "bug"
    assert shown["priority"] == 1


def test_ingest_flags_override_packet_fields(tmp_path: Path) -> None:
    """A typed flag is more current than the field baked into the packet."""
    repo = copy_bd_workspace(tmp_path / "repo", "bare").path
    payload = dict(_ready_fields())
    payload.update({"title": "packet title", "issue_type": "task", "priority": 3})

    result = runner.invoke(
        app,
        ["ingest", str(repo), "--stdin", "--title", "flag title", "--priority", "0"],
        input=json.dumps(payload),
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    shown = json.loads(
        subprocess.run(
            ["bd", "show", result.stdout.strip(), "--json"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )[0]
    assert shown["title"] == "flag title"
    assert shown["priority"] == 0


def test_ingest_missing_packet_section_errors_before_validation(tmp_path: Path) -> None:
    """A transport gap is reported as one, and no bead is written."""
    repo = copy_bd_workspace(tmp_path / "repo", "bare").path
    before = _open_ids(repo)
    packet = _write_packet(tmp_path / "packet")
    (packet / "acceptance.md").unlink()

    result = runner.invoke(
        app, ["ingest", str(repo), "--packet", str(packet), "--title", "No acceptance"]
    )

    assert result.exit_code == 1, result.stdout + result.stderr
    assert "acceptance.md or acceptance_criteria.md" in _flat(result.stderr)
    assert result.stdout.strip() == ""
    assert _open_ids(repo) == before


def test_ingest_accepts_the_acceptance_criteria_file_alias(tmp_path: Path) -> None:
    """Either spelling of the acceptance file is a complete packet."""
    repo = copy_bd_workspace(tmp_path / "repo", "bare").path
    packet = _write_packet(tmp_path / "packet")
    (packet / "acceptance.md").rename(packet / "acceptance_criteria.md")

    result = runner.invoke(
        app, ["ingest", str(repo), "--packet", str(packet), "--title", "Alias packet"]
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    assert result.stdout.strip() in _open_ids(repo)


def test_ingest_non_json_stdin_creates_nothing(tmp_path: Path) -> None:
    """Garbage on stdin is a named error, never a bd call."""
    repo = copy_bd_workspace(tmp_path / "repo", "bare").path
    before = _open_ids(repo)

    result = runner.invoke(app, ["ingest", str(repo), "--stdin"], input="not json at all")

    assert result.exit_code == 1, result.stdout + result.stderr
    assert "stdin is not JSON" in _flat(result.stderr)
    assert _open_ids(repo) == before


def test_ingest_without_a_source_is_a_usage_error(tmp_path: Path) -> None:
    """Neither --packet nor --stdin: say so rather than file an empty bead."""
    repo = copy_bd_workspace(tmp_path / "repo", "bare").path
    before = _open_ids(repo)

    result = runner.invoke(app, ["ingest", str(repo), "--title", "Nothing to read"])

    assert result.exit_code == 2, result.stdout + result.stderr
    flat = _flat(result.stderr)
    assert "--packet" in flat and "--stdin" in flat
    assert _open_ids(repo) == before


def test_ingest_refuses_an_epic(tmp_path: Path) -> None:
    """The readiness epic exemption does not make an epic ingestable."""
    repo = copy_bd_workspace(tmp_path / "repo", "bare").path
    before = _open_ids(repo)
    packet = _write_packet(tmp_path / "packet", design="## Scope\nTBD")

    result = runner.invoke(
        app,
        [
            "ingest",
            str(repo),
            "--packet",
            str(packet),
            "--title", "Container",
            "--type", "epic",
        ],
    )

    assert result.exit_code == 1, result.stdout + result.stderr
    assert "epics are containers" in _flat(result.stderr)
    assert _open_ids(repo) == before


def test_ingest_without_a_title_creates_nothing(tmp_path: Path) -> None:
    """Title has no default: bd would take one silently, so ingest asks."""
    repo = copy_bd_workspace(tmp_path / "repo", "bare").path
    before = _open_ids(repo)
    packet = _write_packet(tmp_path / "packet")

    result = runner.invoke(app, ["ingest", str(repo), "--packet", str(packet)])

    assert result.exit_code == 1, result.stdout + result.stderr
    assert "no title" in _flat(result.stderr)
    assert _open_ids(repo) == before
