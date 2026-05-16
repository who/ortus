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
    # All Phase 1+2 verbs (init/check/grind/plan) are implemented; remaining
    # stubs (interview/tail/triage/human) hit resolve_repo first and exit 1
    # on missing .beads/, not 2. So no top-level verb currently emits the
    # canonical 'not implemented' message on a path without .beads/.
    [],
)
def test_stub_verbs_exit_two_with_message(verb: str) -> None:  # pragma: no cover
    result = runner.invoke(app, [verb, "/tmp/no-such-dir-stub-test"])
    assert result.exit_code == 2
    assert "not implemented" in result.stderr


def test_remaining_phase3_stubs_exit_with_not_implemented_when_repo_ok(
    tmp_path: Path,
) -> None:
    """Phase 3 stubs hit resolve_repo() first; given a valid repo they reach
    the 'not implemented' message and exit 2."""
    repo = tmp_path / "ok"
    (repo / ".beads").mkdir(parents=True)
    for verb in ("interview", "tail", "triage", "human"):
        result = runner.invoke(app, [verb, str(repo)])
        assert result.exit_code == 2, f"{verb}: expected exit 2, got {result.exit_code}"
        assert "not implemented" in result.stderr, f"{verb}: missing message"


def test_grind_nonexistent_repo_emits_fr003_error(tmp_path: Path) -> None:
    bogus = tmp_path / "no-beads-here"
    bogus.mkdir()
    result = runner.invoke(app, ["grind", str(bogus)])
    assert result.exit_code == 1
    # The error message must match the FR-003 mandated string verbatim.
    assert FR003_NO_BEADS_ERROR in result.stderr


def test_repo_arg_with_beads_dir_proceeds_past_fr003(tmp_path: Path) -> None:
    """A valid repo passes the FR-003 check; grind --dry-run is a cheap way
    to confirm the routing reached the verb without spawning claude."""
    repo = tmp_path / "fake-repo"
    (repo / ".beads").mkdir(parents=True)
    result = runner.invoke(app, ["grind", str(repo), "--dry-run"])
    assert result.exit_code == 0
    assert "/goal" in result.stdout
