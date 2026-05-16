"""Integration tests for the 8-verb CLI skeleton (q075.2 acceptance criteria)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.core.repo import FR003_NO_BEADS_ERROR

runner = CliRunner()

VERBS = ["init", "plan", "grind", "interview", "tail", "triage", "human", "check"]


def test_top_help_lists_all_eight_verbs() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for verb in VERBS:
        assert verb in result.stdout, f"--help missing verb {verb!r}"


@pytest.mark.parametrize("verb", VERBS)
def test_verb_help_works(verb: str) -> None:
    result = runner.invoke(app, [verb, "--help"])
    assert result.exit_code == 0, result.stdout
    assert f"ortus {verb}" in result.stdout


def test_version_flag_prints_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.startswith("ortus ")


@pytest.mark.parametrize(
    "verb",
    # Verbs that exit 2 directly (no repo precheck before stub).
    ["init", "check"],
)
def test_stub_verbs_exit_two_with_message(verb: str) -> None:
    result = runner.invoke(app, [verb, "/tmp/no-such-dir-stub-test"])
    assert result.exit_code == 2
    assert "not implemented" in result.stderr


def test_grind_nonexistent_repo_emits_fr003_error(tmp_path: Path) -> None:
    bogus = tmp_path / "no-beads-here"
    bogus.mkdir()
    result = runner.invoke(app, ["grind", str(bogus)])
    assert result.exit_code == 1
    # The error message must match the FR-003 mandated string verbatim.
    assert FR003_NO_BEADS_ERROR in result.stderr


def test_repo_arg_with_beads_dir_proceeds_to_stub(tmp_path: Path) -> None:
    repo = tmp_path / "fake-repo"
    (repo / ".beads").mkdir(parents=True)
    # grind should pass FR-003 and hit the stub (exit 2).
    result = runner.invoke(app, ["grind", str(repo)])
    assert result.exit_code == 2
    assert "not implemented" in result.stderr
