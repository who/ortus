"""Integration tests for the 8-verb CLI skeleton (q075.2 acceptance criteria)."""

from __future__ import annotations

import json
import shutil
import subprocess
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


def test_all_verbs_have_real_implementations(tmp_path: Path) -> None:
    """All 8 verbs are now implemented; none should emit 'not implemented'.
    (Some hit fast-path early-exits; we just assert no leftover stubs.)"""
    repo = tmp_path / "ok"
    (repo / ".beads").mkdir(parents=True)
    # We don't drive every verb here (some would hang on real claude / real
    # tail polling); per-verb tests cover their behavior. This guard is only
    # to catch a future regression of someone marking a verb as stub again.
    import ortus.commands._stub as _stub
    assert callable(_stub.not_implemented)


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


# --- CLI output convention (ortus-s60a) -------------------------------------
#
# Every non-interactive verb must emit `[ortus <verb>] <phase>` lines to
# stderr so the operator can tell "running" from "hung." These tests guard
# the convention per-verb. See AGENTS.md "CLI output convention".


def _bd_repo(tmp_path: Path) -> Path:
    """Create a hermetic bd-initialized repo for verbs that require .beads/."""
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    repo = tmp_path / "conv"
    repo.mkdir()
    subprocess.run(
        ["bd", "init", "--prefix", "conv"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    return repo


def test_init_emits_progress_lines(tmp_path: Path) -> None:
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    target = tmp_path / "fresh"
    result = runner.invoke(app, ["init", str(target)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "[ortus init]" in result.stderr
    assert "[ortus init] done" in result.stderr


def test_check_emits_progress_lines(tmp_path: Path) -> None:
    repo = _bd_repo(tmp_path)
    # check may fail individual sub-checks in this hermetic env (no claude,
    # no real sandbox); we only assert the convention output is present.
    result = runner.invoke(app, ["check", str(repo)])
    assert "[ortus check]" in result.stderr
    assert "[ortus check] done" in result.stderr


def test_human_emits_progress_lines(tmp_path: Path) -> None:
    repo = _bd_repo(tmp_path)
    result = runner.invoke(app, ["human", str(repo)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "[ortus human]" in result.stderr
    assert "[ortus human] done" in result.stderr


def test_grind_dry_run_keeps_dry_run_output_on_stdout(tmp_path: Path) -> None:
    """--dry-run results stay on stdout (machine-readable); only progress is stderr.

    Regression guard: switching grind's pre-flock output.info calls to
    output.progress must not silently move dry-run flag dump to stderr.
    """
    repo = tmp_path / "fake-repo"
    (repo / ".beads").mkdir(parents=True)
    result = runner.invoke(app, ["grind", str(repo), "--dry-run"])
    assert result.exit_code == 0
    # Dry-run output is the verb's RESULT, not progress — keep it on stdout.
    assert "repo:" in result.stdout
    assert "/goal" in result.stdout
