"""Cross-platform fake-claude shim resolution.

Tests that drive ortus through a fake `claude` binary historically wrote
POSIX shell scripts (`#!/usr/bin/env bash`) under tests/fixtures/. Those
work on Linux/macOS because the kernel honors the shebang, but on Windows
`CreateProcess` doesn't understand shebangs and fails with OSError 193
("not a valid Win32 application"). See ortus-f4bu.

The fix: every shim is now a Python file (portable across OSes). For
invocation, this module exposes two helpers:

  shim_path(stem)          -- resolves a bundled shim by stem (e.g.
                              "fake-claude") and returns a path that the
                              host OS can execute directly. On POSIX that
                              is the .py file itself (with +x bit). On
                              Windows it is a freshly-generated .bat
                              wrapper that invokes `sys.executable <py>`.

  make_inline_python_shim  -- write an ad-hoc Python shim body to a tmp
                              dir and return an OS-executable path. Used
                              by tests that need bespoke per-test shims.

Keeping the resolution one-call-away from tests means future shims (or
new test fixtures) only need to do the Python rewrite once; the OS-aware
wrapping happens here.
"""

from __future__ import annotations

import stat
import subprocess
import sys
from pathlib import Path

_FIXTURES = Path(__file__).parent / "fixtures"
_BIN_DIR = _FIXTURES / "bin"
_CANNED_DIR = _FIXTURES / "canned-claude-responses"

IS_WINDOWS = sys.platform == "win32"


def machine_run(
    verdict: str = "pass",
    *,
    criteria: tuple[str, ...] = ("AC-1",),
    ref: str = "fixture-ref",
    output: str = "",
):
    """One scripted AC-runner result: every criterion carries `verdict`."""

    from ortus.core.checks import CheckRunResult, CriterionResult

    return CheckRunResult(
        ref=ref,
        results=tuple(
            CriterionResult(
                criterion_id=criterion_id,
                command="uv run pytest tests/test_grind.py -q",
                exit_code=0 if verdict == "pass" else 1,
                duration_seconds=0.1,
                output=output,
                verdict=verdict,
            )
            for criterion_id in criteria
        ),
    )


def install_machine_checks(
    monkeypatch, sequence: list | None = None, default=None
) -> list[dict]:
    """Script grind's AC runner the way tests script worker backends.

    Each call pops the next result from `sequence`; an exhausted sequence
    returns `default` (a green single-criterion run unless given). Returns the
    recorded calls so tests can assert what ref and criteria were judged.
    """

    from ortus.commands import grind as grind_mod

    calls: list[dict] = []
    remaining = list(sequence or [])

    def _scripted(repo, acceptance_criteria, ref, *, base_ref=None, **kwargs):
        calls.append(
            {
                "repo": repo,
                "acceptance_criteria": acceptance_criteria,
                "ref": ref,
                "base_ref": base_ref,
            }
        )
        if remaining:
            return remaining.pop(0)
        return default if default is not None else machine_run(ref=str(ref))

    monkeypatch.setattr(grind_mod, "_run_machine_checks", _scripted)
    return calls


def claims_comment_body(
    claims: dict[str, str] | None = None, *, changes: str = "- fixture change"
) -> str:
    """A completion comment carrying **Changes** and the **Claims v1** block."""

    lines = [
        "**Changes**:",
        changes,
        "",
        "**Verification**: fixture checks run",
        "",
        "**Claims v1**:",
    ]
    for criterion_id, status in (claims or {"AC-1": "pass"}).items():
        lines.append(f"{criterion_id}: {status}")
    return "\n".join(lines)


def post_completion_comment(
    repo: Path,
    issue_id: str,
    claims: dict[str, str] | None = None,
    *,
    changes: str = "- fixture change",
) -> None:
    """Post the completion comment a real worker leaves, claims block included."""

    subprocess.run(
        ["bd", "comments", "add", issue_id, claims_comment_body(claims, changes=changes)],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )


def ready_issue_args() -> list[str]:
    """Return bd-create fields for a minimal readiness-schema-v1 test leaf."""

    return [
        "--description",
        """## Objective
Exercise the behavior owned by this test.

## Behavioral context
The fixture supplies one executable leaf so grind can run the tested worker path.""",
        "--design",
        """## Readiness schema
v1

## Scope
Run the fixture's single bounded worker scenario.

## Non-goals
No production feature implementation.

## Concrete locations
Exercise `src/ortus/commands/grind.py` in `grind()` through the fixture worker interface.

## Resolved decisions
Use the existing fake-worker scenario and observable Beads state.

## Compatibility constraints
Keep the fixture hermetic on supported platforms.

## Ordered steps
1. Let grind claim the fixture leaf.
2. Run the configured worker scenario.
3. Assert the resulting state and logs.

## Dependencies
None — the fixture leaf is standalone; its consumer is `grind()`.

## Edge cases
The individual test defines timeout, orphan, close, or no-op behavior.

## Plan-gap guidance
If fixture behavior contradicts the grind contract, record PLAN-GAP and stop.""",
        "--acceptance",
        """## Observable criteria
- AC-1: The configured grind scenario reaches its asserted state.

## Criterion checks
- AC-1: Run `uv run pytest tests/test_grind.py -q`.

## Targeted tests
Run `uv run pytest tests/test_grind.py -q`.""",
    ]


def _ensure_executable(path: Path) -> None:
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _write_bat_wrapper(py_path: Path) -> Path:
    """Write a sibling .bat shim that runs `python <py_path> %*` on Windows.

    The .bat is regenerated on every call so it always points at the
    *current* interpreter (sys.executable can change between test
    sessions, e.g. when running under different uv-managed venvs).
    """
    bat = py_path.with_suffix(".bat")
    bat.write_text(
        f'@echo off\r\n"{sys.executable}" "{py_path}" %*\r\n',
        encoding="utf-8",
    )
    return bat


def _wrap_for_os(py_path: Path) -> Path:
    if IS_WINDOWS:
        return _write_bat_wrapper(py_path)
    _ensure_executable(py_path)
    return py_path


def shim_path(stem: str) -> Path:
    """Resolve a bundled shim by stem; return an OS-executable path.

    Looks for `<stem>.py` under tests/fixtures/bin/ first, then under
    tests/fixtures/canned-claude-responses/. Raises FileNotFoundError
    if neither location has the shim.
    """
    for d in (_BIN_DIR, _CANNED_DIR):
        py = d / f"{stem}.py"
        if py.is_file():
            return _wrap_for_os(py)
    raise FileNotFoundError(f"no shim named {stem!r} under {_BIN_DIR} or {_CANNED_DIR}")


def normalize_git_branch(repo: Path, branch: str = "main") -> None:
    """Rename the current git branch of `repo` to `branch` (default "main").

    `bd init` incidentally `git init`s the workspace and lands it on the git
    default branch — `master` on most installs. grind's branch-discipline guard
    (ortus-6fu6) pins the working tree to its integration branch (`main` by
    default) and halts if it can't, so integration fixtures that drive grind
    must sit on that branch. Production ortus repos already do; this aligns the
    bd-init'd test repos so the guard sees a clean on-integration-branch state
    instead of tripping on the incidental `master`. No-op if already on
    `branch`.
    """
    proc = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        # An unborn repository has no resolvable HEAD yet, but its symbolic
        # ref still chooses the branch that the first worker commit creates.
        subprocess.run(
            ["git", "symbolic-ref", "HEAD", f"refs/heads/{branch}"],
            cwd=str(repo),
            check=True,
            capture_output=True,
        )
        return
    if proc.stdout.strip() == branch:
        return
    subprocess.run(
        ["git", "branch", "-m", branch],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )


def make_inline_python_shim(out_dir: Path, stem: str, body: str) -> Path:
    """Write an ad-hoc Python shim and return an OS-executable path.

    The body is a Python source snippet (no shebang needed; this helper
    prepends one). The shim is written under out_dir/<stem>.py; on
    Windows a sibling .bat wrapper is generated.
    """
    py = out_dir / f"{stem}.py"
    if not body.startswith("#!"):
        body = "#!/usr/bin/env python3\n" + body
    py.write_text(body, encoding="utf-8")
    return _wrap_for_os(py)
