"""Deterministic AC runner: packet commands executed in a disposable shared clone."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from ortus.core import checks, readiness
from ortus.core.checks import (
    parse_criterion_checks,
    render_tracker_comment,
    run_checks,
)
from ortus.core.git import GitClient


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "ortus-tests@example.invalid"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Ortus Tests"], cwd=repo, check=True)
    (repo / "source.py").write_text("BASELINE = True\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "baseline"], cwd=repo, check=True, capture_output=True
    )
    return repo


def _packet(checks_body: str) -> str:
    return (
        "## Observable criteria\n\n"
        "- AC-1: something observable.\n\n"
        "## Criterion checks\n\n"
        f"{checks_body}\n"
    )


def test_runs_every_criterion_in_the_shared_clone(tmp_path: Path) -> None:
    """AC-1: one record per AC-N, executed in a clone of the given ref."""
    repo = _repo(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "work"], cwd=repo, check=True)
    (repo / "branch-only.txt").write_text("on the branch\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "branch work"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)

    result = run_checks(
        repo,
        _packet("- AC-1: `test -f branch-only.txt`\n- AC-2: `touch scribble.txt`"),
        "work",
        timeout_seconds=30,
        scratch_root=tmp_path,
    )

    assert result.ref == "work"
    assert [r.criterion_id for r in result.results] == ["AC-1", "AC-2"]
    assert all(
        r.verdict == checks.VERDICT_PASS and r.exit_code == 0 for r in result.results
    )
    assert result.ok
    # The commands ran in the clone of `work`, not the live tree: main has
    # neither the branch file nor the scribble a criterion created.
    assert not (repo / "branch-only.txt").exists()
    assert not (repo / "scribble.txt").exists()


def test_clone_is_shared_never_worktree_or_archive(tmp_path: Path) -> None:
    """AC-2: `git clone --shared` produces the tree — with vcs metadata, not a
    worktree entry, and never overwriting an existing target."""
    repo = _repo(tmp_path)
    git = GitClient(repo)
    target = tmp_path / "clone"

    assert git.clone_shared("main", target) == ""

    alternates = target / ".git" / "objects" / "info" / "alternates"
    assert alternates.is_file(), "--shared borrows objects via alternates"
    assert "objects" in alternates.read_text()
    assert (target / ".git").is_dir(), "a worktree has a .git file, a clone a dir"
    assert (target / "source.py").read_text() == "BASELINE = True\n"
    worktrees = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert str(target) not in worktrees
    assert "already exists" in git.clone_shared("main", target)
    # The materialized tree keeps its .git — the reason archives are banned —
    # so a criterion can observe it.
    result = run_checks(
        repo,
        _packet("- AC-1: `test -d .git`"),
        "main",
        timeout_seconds=30,
        scratch_root=tmp_path,
    )
    assert result.ok


def test_failure_carries_exit_code_and_output(tmp_path: Path) -> None:
    """AC-3: a failing command keeps its exit code and its (non-ASCII) output."""
    repo = _repo(tmp_path)

    result = run_checks(
        repo,
        _packet("- AC-1: `echo naïve diagnostic ✓; exit 3`"),
        "main",
        timeout_seconds=30,
        scratch_root=tmp_path,
    )

    (record,) = result.results
    assert record.verdict == checks.VERDICT_FAIL
    assert record.exit_code == 3
    assert "naïve diagnostic ✓" in record.output
    assert not result.ok


def _alive(pid: int) -> bool:
    """True while `pid` exists and is not a zombie awaiting its reaper."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        state = stat.rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state != "Z"


def test_timeout_kills_the_process_group(tmp_path: Path) -> None:
    """AC-4: a wedged check is reported as timed out — distinct from failed —
    and its whole process group dies with it."""
    repo = _repo(tmp_path)
    pid_file = tmp_path / "child.pid"

    result = run_checks(
        repo,
        _packet(f"- AC-1: `sleep 30 & echo $! > {pid_file}; wait`"),
        "main",
        timeout_seconds=0.5,
        scratch_root=tmp_path,
    )

    (record,) = result.results
    assert record.verdict == checks.VERDICT_TIMEOUT
    assert record.exit_code is None
    assert not result.ok
    child = int(pid_file.read_text().strip())
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and _alive(child):
        time.sleep(0.05)
    assert not _alive(child), "the backgrounded sleep must die with its group"


def test_output_is_file_captured_and_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-5: stdout/stderr go to a file — never a pipe or filter — and the
    record's output is bounded on read, keeping head and tail."""
    repo = _repo(tmp_path)
    sinks: list[tuple[object, object]] = []
    real_popen = subprocess.Popen

    def spying_popen(*args: object, **kwargs: object):
        # Only the executor spawns through the shell; GitClient's plumbing
        # (subprocess.run) also lands here and is not under test.
        if kwargs.get("shell"):
            sinks.append((kwargs.get("stdout"), kwargs.get("stderr")))
        return real_popen(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(checks.subprocess, "Popen", spying_popen)

    result = run_checks(
        repo,
        _packet("- AC-1: `seq 1 20000`"),
        "main",
        timeout_seconds=30,
        output_limit=500,
        scratch_root=tmp_path,
    )

    (record,) = result.results
    assert record.verdict == checks.VERDICT_PASS
    assert len(record.output) <= 500 + len(checks._TRUNCATION_MARKER)
    assert checks._TRUNCATION_MARKER.strip() in record.output
    assert record.output.startswith("1\n"), "the head of the output survives"
    assert record.output.rstrip().endswith("20000"), "the tail survives too"
    assert sinks, "the executor must have spawned through Popen"
    for stdout, stderr in sinks:
        assert stdout is not subprocess.PIPE
        assert hasattr(stdout, "fileno"), "capture goes straight to a file"
        assert stderr is subprocess.STDOUT


def test_unparseable_criterion_is_a_packet_failure(tmp_path: Path) -> None:
    """AC-6: a criterion without exactly one backticked command indicts the
    packet; the runner never guesses, and the parseable rest still runs."""
    repo = _repo(tmp_path)

    result = run_checks(
        repo,
        _packet(
            "- AC-1: run the tests somehow\n"
            "- AC-2: `true`\n"
            "- AC-3: `first` then `second`"
        ),
        "main",
        timeout_seconds=30,
        scratch_root=tmp_path,
    )

    assert [f.criterion_id for f in result.packet_failures] == ["AC-1", "AC-3"]
    assert all("backticked command" in f.message for f in result.packet_failures)
    assert [r.criterion_id for r in result.results] == ["AC-2"]
    assert not result.ok

    duplicated, failures = parse_criterion_checks(
        _packet("- AC-1: `true`\n- AC-1: `false`")
    )
    assert [c.criterion_id for c in duplicated] == ["AC-1"]
    assert failures and "more than once" in failures[0].message


def test_rendering_carries_commands_and_verdicts(tmp_path: Path) -> None:
    """AC-7: the tracker comment states every command, verdict, and exit code,
    and an empty run reads as unverified, never as success."""
    repo = _repo(tmp_path)

    result = run_checks(
        repo,
        _packet(
            "- AC-1: `true`\n- AC-2: `echo boom; exit 9`\n- AC-3: `sleep 30`"
        ),
        "main",
        timeout_seconds=1.0,
        scratch_root=tmp_path,
    )
    text = render_tracker_comment(result)

    assert text.startswith("Deterministic AC run @ main: fail (1/3 criteria passed)")
    assert "`true`" in text and "`echo boom; exit 9`" in text and "`sleep 30`" in text
    assert "AC-1: pass" in text and "exit 0" in text
    assert "AC-2: fail" in text and "exit 9" in text and "boom" in text
    assert "AC-3: timeout" in text and "no exit code" in text

    empty = run_checks(repo, "", "main", scratch_root=tmp_path)
    assert not empty.ok and not empty.results
    assert "nothing verified" in render_tracker_comment(empty)


def test_grammar_is_shared_with_readiness() -> None:
    """AC-8: the runner parses with readiness's own objects, so the grammars
    cannot drift apart by coincidence."""
    assert checks._CRITERION_ID is readiness._CRITERION_ID
    assert checks._CODE_SPAN is readiness._CODE_SPAN
    assert (
        checks._CHECKS_HEADING
        == readiness._section("criterion_mapped_checks").heading
    )
    # Only the section readiness validates is read: observable prose and
    # targeted-test backticks contribute neither commands nor failures.
    acceptance = (
        "## Observable criteria\n\n- AC-1: the runner runs.\n\n"
        "## Criterion checks\n\n- AC-1: `true`\n\n"
        "## Targeted tests\n\n`uv run pytest tests/test_demo.py -q`\n"
    )
    parsed, failures = parse_criterion_checks(acceptance)
    assert [c.command for c in parsed] == ["true"]
    assert failures == ()


def test_env_prep_failure_is_not_a_criterion_failure(tmp_path: Path) -> None:
    """AC-9: environment preparation runs before the first criterion, and its
    failure names the command instead of producing any criterion verdict."""
    repo = _repo(tmp_path)

    result = run_checks(
        repo,
        _packet("- AC-1: `true`"),
        "main",
        timeout_seconds=30,
        scratch_root=tmp_path,
        sync_command="echo sync exploded; exit 7",
    )

    assert result.results == ()
    assert result.packet_failures == ()
    assert result.environment is not None
    assert result.environment.exit_code == 7
    assert "echo sync exploded; exit 7" in result.environment.reason
    assert "sync exploded" in result.environment.output
    assert not result.ok
    assert "environment preparation failed" in render_tracker_comment(result)
    # The default convention: uv-managed trees sync, everything else skips,
    # and an explicit "" always skips.
    assert checks._sync_command_for(tmp_path, None) is None
    (tmp_path / "uv.lock").write_text("")
    assert checks._sync_command_for(tmp_path, None) == checks.DEFAULT_SYNC_COMMAND
    assert checks._sync_command_for(tmp_path, "") is None
