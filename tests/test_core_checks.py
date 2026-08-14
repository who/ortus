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


def _packet(
    checks_body: str, observable: str = "- AC-1: something observable."
) -> str:
    return (
        "## Observable criteria\n\n"
        f"{observable}\n\n"
        "## Criterion checks\n\n"
        f"{checks_body}\n"
    )


def _branched_repo(tmp_path: Path) -> Path:
    """A repo whose `work` branch adds a file its merge base with main lacks."""
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
    return repo


def test_runs_every_criterion_in_the_shared_clone(tmp_path: Path) -> None:
    """AC-1: one record per AC-N, executed in a clone of the given ref."""
    repo = _branched_repo(tmp_path)

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
    """AC-6: a criterion without a runnable command indicts the packet; the
    runner never guesses, and the parseable rest still runs."""
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
    assert all(
        "no runnable command" in f.message for f in result.packet_failures
    )
    assert [r.criterion_id for r in result.results] == ["AC-2"]
    assert not result.ok

    duplicated, failures = parse_criterion_checks(
        _packet("- AC-1: `true`\n- AC-1: `false`")
    )
    assert [c.criterion_id for c in duplicated] == ["AC-1"]
    assert failures and "more than once" in failures[0].message


def test_observable_line_command_parses_without_checks_heading() -> None:
    """One-heading packet: commands on Observable lines are the checks."""
    parsed, failures = parse_criterion_checks(
        "## Observable criteria\n\n"
        "- AC-1 (proves-new): Preview performs no writes. "
        "`uv run pytest tests/test_demo.py::test_preview -q`\n"
        "- AC-2: Normal execution is unchanged. "
        "`uv run pytest tests/test_demo.py::test_run -q`\n"
    )
    assert failures == ()
    assert [c.criterion_id for c in parsed] == ["AC-1", "AC-2"]
    assert parsed[0].command == "uv run pytest tests/test_demo.py::test_preview -q"
    assert parsed[0].kind == "proves-new"
    assert parsed[1].command == "uv run pytest tests/test_demo.py::test_run -q"
    assert parsed[1].kind is None


def test_parse_criterion_checks_prefers_checks_heading() -> None:
    """When Criterion checks is present, Observable commands are ignored."""
    parsed, failures = parse_criterion_checks(
        "## Observable criteria\n\n"
        "- AC-1: something. `uv run pytest tests/wrong.py -q`\n\n"
        "## Criterion checks\n\n"
        "- AC-1: `true`\n"
    )
    assert failures == ()
    assert [c.command for c in parsed] == ["true"]


def test_bare_and_mixed_span_commands_parse() -> None:
    """Backticks are optional; a command-looking span wins among pins."""
    parsed, failures = parse_criterion_checks(
        _packet(
            "- AC-1: uv run pytest tests/test_demo.py -q\n"
            "- AC-2: `rg -n uses: .github/workflows/test.yml` shows no `setup-uv@v3`",
            observable="- AC-1: a.\n- AC-2: b.",
        )
    )
    assert failures == ()
    assert [c.criterion_id for c in parsed] == ["AC-1", "AC-2"]
    assert parsed[0].command == "uv run pytest tests/test_demo.py -q"
    assert parsed[1].command == "rg -n uses: .github/workflows/test.yml"


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
    assert checks._CRITERION_KIND is readiness._CRITERION_KIND
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


def test_red_on_base_green_on_branch_passes(tmp_path: Path) -> None:
    """AC-2 (l2u9.2): `proves-new` passes only as fail-on-base AND
    pass-on-branch, and the record carries both exit codes."""
    repo = _branched_repo(tmp_path)

    result = run_checks(
        repo,
        _packet(
            "- AC-1: `test -f branch-only.txt`",
            observable="- AC-1 (proves-new): the branch adds the file.",
        ),
        "work",
        base_ref="main",
        timeout_seconds=30,
        scratch_root=tmp_path,
    )

    (record,) = result.results
    assert record.kind == "proves-new"
    assert record.verdict == checks.VERDICT_PASS
    assert record.exit_code == 0
    assert record.base_exit_code == 1
    assert result.ok


def test_passes_on_both_trees_is_vacuous(tmp_path: Path) -> None:
    """AC-1 (l2u9.2): a `proves-new` criterion passing on both trees proves
    nothing — reported as vacuous, never as a pass."""
    repo = _branched_repo(tmp_path)
    packet = _packet(
        "- AC-1: `true`",
        observable="- AC-1 (proves-new): supposedly new behavior.",
    )

    result = run_checks(
        repo, packet, "work", base_ref="main", timeout_seconds=30,
        scratch_root=tmp_path,
    )

    (record,) = result.results
    assert record.verdict == checks.VERDICT_VACUOUS
    assert record.exit_code == 0 and record.base_exit_code == 0
    assert not result.ok
    text = render_tracker_comment(result)
    assert "AC-1 (proves-new): vacuous" in text
    assert text.startswith("Deterministic AC run @ work: fail")
    # A merge base identical to the branch head (empty change) makes every
    # proves-new criterion vacuous by construction.
    empty_change = run_checks(
        repo, packet, "main", base_ref="main", timeout_seconds=30,
        scratch_root=tmp_path,
    )
    assert empty_change.results[0].verdict == checks.VERDICT_VACUOUS


def test_guards_existing_broken_base(tmp_path: Path) -> None:
    """AC-3 (l2u9.2): `guards-existing` passes only on both trees; a base
    failure is reported as broken-base, indicting the criterion or the base."""
    repo = _branched_repo(tmp_path)

    result = run_checks(
        repo,
        _packet(
            "- AC-1: `test -f branch-only.txt`\n- AC-2: `test -f source.py`",
            observable=(
                "- AC-1 (guards-existing): the file was always there.\n"
                "- AC-2 (guards-existing): the baseline survives."
            ),
        ),
        "work",
        base_ref="main",
        timeout_seconds=30,
        scratch_root=tmp_path,
    )

    broken, guarded = result.results
    assert broken.verdict == checks.VERDICT_BROKEN_BASE
    assert broken.base_exit_code == 1 and broken.exit_code == 0
    assert guarded.verdict == checks.VERDICT_PASS
    assert guarded.base_exit_code == 0 and guarded.exit_code == 0
    assert not result.ok
    assert "AC-1 (guards-existing): broken-base" in render_tracker_comment(result)


def test_untagged_criterion_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-4 (l2u9.2): an untagged criterion runs branch-only — no merge base
    consulted, no base fields set, the rendered line exactly as before."""
    repo = _branched_repo(tmp_path)
    monkeypatch.setattr(
        GitClient,
        "merge_base",
        lambda self, a, b: pytest.fail("untagged criteria must not take a merge base"),
    )

    result = run_checks(
        repo,
        _packet("- AC-1: `test -f branch-only.txt`"),
        "work",
        base_ref="main",
        timeout_seconds=30,
        scratch_root=tmp_path,
    )

    (record,) = result.results
    assert record.verdict == checks.VERDICT_PASS
    assert record.kind is None
    assert record.base_exit_code is None and record.base_output == ""
    assert result.ok
    line = next(
        l for l in render_tracker_comment(result).splitlines()
        if l.startswith("- AC-1")
    )
    assert line == (
        f"- AC-1: pass — exit 0 in {record.duration_seconds:.1f}s — "
        "`test -f branch-only.txt`"
    )


def test_timeout_is_never_the_required_failure(tmp_path: Path) -> None:
    """AC-6 (l2u9.2): a `proves-new` command that times out on the base is a
    timeout — a wedged command is not the failure the proof requires."""
    repo = _branched_repo(tmp_path)

    result = run_checks(
        repo,
        # Quick pass where the file exists (the branch), wedged where it
        # does not (the merge base).
        _packet(
            "- AC-1: `test -f branch-only.txt || sleep 30`",
            observable="- AC-1 (proves-new): the new file exists.",
        ),
        "work",
        base_ref="main",
        timeout_seconds=0.5,
        scratch_root=tmp_path,
    )

    (record,) = result.results
    assert record.verdict == checks.VERDICT_TIMEOUT
    assert record.exit_code == 0, "the branch run itself passed"
    assert record.base_exit_code is None, "the base run timed out"
    assert not result.ok


def test_rendering_states_both_exit_codes(tmp_path: Path) -> None:
    """AC-7 (l2u9.2): the tracker comment states base and branch exit codes
    for every tagged criterion."""
    repo = _branched_repo(tmp_path)

    result = run_checks(
        repo,
        _packet(
            "- AC-1: `test -f branch-only.txt`\n"
            "- AC-2: `test -f source.py`\n"
            "- AC-3: `true`",
            observable=(
                "- AC-1 (proves-new): the branch adds the file.\n"
                "- AC-2 (guards-existing): the baseline survives.\n"
                "- AC-3 (proves-new): supposedly new behavior."
            ),
        ),
        "work",
        base_ref="main",
        timeout_seconds=30,
        scratch_root=tmp_path,
    )
    text = render_tracker_comment(result)

    assert "- AC-1 (proves-new): pass — base exit 1, branch exit 0" in text
    assert "- AC-2 (guards-existing): pass — base exit 0, branch exit 0" in text
    assert "- AC-3 (proves-new): vacuous — base exit 0, branch exit 0" in text


def test_tagged_criteria_without_a_merge_base_fail_the_environment(
    tmp_path: Path,
) -> None:
    """A tagged packet with no establishable merge base is an environment
    failure — reported, never improvised around by running branch-only."""
    repo = _branched_repo(tmp_path)
    packet = _packet(
        "- AC-1: `true`",
        observable="- AC-1 (proves-new): needs a base to prove anything.",
    )

    unsupplied = run_checks(
        repo, packet, "work", timeout_seconds=30, scratch_root=tmp_path
    )
    assert unsupplied.results == ()
    assert unsupplied.environment is not None
    assert "no base ref was supplied" in unsupplied.environment.output

    unresolvable = run_checks(
        repo, packet, "work", base_ref="no-such-branch",
        timeout_seconds=30, scratch_root=tmp_path,
    )
    assert unresolvable.results == ()
    assert unresolvable.environment is not None
    assert "no merge base" in unresolvable.environment.output
