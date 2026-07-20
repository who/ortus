"""Shared test fixtures.

Per FR-034 (Testing Strategy): claude_mock fixture loads a canned scenario
by key. Scenarios live in tests/fixtures/canned-claude-responses/<key>.py
and behave as drop-in replacements for the real claude binary. Resolution
goes through tests._shims.shim_path so each scenario returns a path the
host OS can execute directly (POSIX: the .py file with +x; Windows: a
generated .bat wrapper). See ortus-f4bu.
"""

from __future__ import annotations

import os
import signal
import shutil
import subprocess
from pathlib import Path
from typing import Callable

import pytest

from tests._shims import normalize_git_branch, ready_issue_args, shim_path

_DEPENDENCY_MARKERS = ("fast", "integration", "network", "live_provider")
_HERMETIC_TEST_BUDGET_SECONDS = 5.0


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--enforce-duration-budget",
        action="store_true",
        help="fail when a hermetic test takes over 5s without @pytest.mark.slow",
    )
    parser.addoption(
        "--test-timeout",
        type=float,
        default=0.0,
        help="fail a test after N seconds and name its node id (0 disables)",
    )


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Give every test one enforceable dependency class.

    Network and live-provider tests are always explicit. Existing integration
    markers remain authoritative; all other tests are fast hermetic tests.
    """
    for item in items:
        classes = [
            name for name in _DEPENDENCY_MARKERS if item.get_closest_marker(name)
        ]
        if not classes:
            # Smoke/regression tests exercise command or subprocess boundaries
            # even when their provider/binary is canned, so keep them out of
            # the bounded unit loop.
            if item.get_closest_marker("smoke") or item.get_closest_marker(
                "regression"
            ):
                item.add_marker(pytest.mark.integration)
            else:
                item.add_marker(pytest.mark.fast)
        elif len(classes) > 1:
            raise pytest.UsageError(
                f"{item.nodeid} has multiple dependency-class markers: {classes}"
            )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item: pytest.Item):
    """Bound individual tests while preserving pytest's normal report path."""
    seconds = item.config.getoption("--test-timeout")
    if seconds <= 0:
        yield
        return

    def _expired(_signum: int, _frame: object) -> None:
        raise TimeoutError(f"currently running {item.nodeid}: exceeded {seconds:g}s")

    previous = signal.signal(signal.SIGALRM, _expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    outcome = yield
    report = outcome.get_result()
    if (
        report.when == "call"
        and report.passed
        and item.config.getoption("--enforce-duration-budget")
        and report.duration > _HERMETIC_TEST_BUDGET_SECONDS
        and item.get_closest_marker("network") is None
        and item.get_closest_marker("live_provider") is None
        and item.get_closest_marker("slow") is None
    ):
        report.outcome = "failed"
        report.longrepr = (
            f"{item.nodeid} took {report.duration:.2f}s; hermetic tests over "
            f"{_HERMETIC_TEST_BUDGET_SECONDS:.0f}s must be optimized or marked slow"
        )


# The canonical bash/Copier implementation is archived. Keep its historical
# tests in-tree with the final-bash sources, but do not collect them once the
# canonical ``ortus/*.sh`` launchers have been removed. The Python CLI suites
# below are now the release gate.
if not (Path(__file__).parent.parent / "ortus" / "goal.sh").is_file():
    collect_ignore = [
        "test_agent_cli_question.py",
        "test_argv_parity.py",
        "test_backend_sh.py",
        "test_bd_preflight_sh.py",
        "test_codex_config_template.py",
        "test_copier_setup_task.py",
        "test_hook_gate_backend_sh.py",
        "test_instruction_files_per_backend.py",
        "test_interview_sh.py",
        "test_m5_backend_acceptance.py",
        "test_tail_backend_detection.py",
        "test_tail_parity.py",
        "test_tail_render_parity.py",
        "test_triage_sh.py",
    ]

_FIXTURES = Path(__file__).parent / "fixtures"
_CANNED_DIR = _FIXTURES / "canned-claude-responses"

# Auth files the real claude binary reads from ~/.claude/. The hermetic
# fake-HOME used by smoke tests would otherwise hide these and make claude
# exit with "Not logged in" (ortus-v0uw). We allow-list the minimum set —
# do NOT mirror the whole directory, since hermeticity for memory paths,
# settings, sessions, etc. is the whole point of fake HOME.
_CLAUDE_AUTH_FILES = (
    ".credentials.json",  # current claude (>=1.x): hidden, 0600
    "credentials.json",  # older/forward-compat variant
    "auth.json",  # older/forward-compat variant
)


def _link_claude_auth(real_home: Path, fake_home: Path) -> None:
    """Symlink (or copy) the operator's claude auth files into a fake HOME.

    Pierces hermeticity for auth files only. Bd state, claude memory paths,
    settings overrides, and session state continue to resolve under
    `fake_home` so each test still gets a clean slate.

    Windows symlinks require either administrator privileges or Developer
    Mode; CI runners typically have neither. Fall back to shutil.copy2 so
    the auth file is at least present (it is read-only state from the
    test's perspective, so a copy is functionally equivalent).

    No-op if the operator has no ~/.claude/ or no auth files (caller should
    then skip the test — see `claude_authenticated`).
    """
    import shutil

    real_claude = real_home / ".claude"
    if not real_claude.exists():
        return
    fake_claude = fake_home / ".claude"
    fake_claude.mkdir(parents=True, exist_ok=True)
    for name in _CLAUDE_AUTH_FILES:
        src = real_claude / name
        if not src.exists():
            continue
        dest = fake_claude / name
        if dest.exists() or dest.is_symlink():
            continue
        try:
            dest.symlink_to(src)
        except OSError:
            # Windows without symlink privilege, or any other symlink
            # failure. A plain copy preserves the contents the test needs.
            shutil.copy2(src, dest)


def claude_authenticated() -> bool:
    """Return True when claude auth is reachable on this host.

    Evaluated at collection time via `pytest.mark.skipif`, so it must not
    depend on monkeypatched env vars. We check the operator's real home
    directory for any of the allow-listed auth files. The override env var
    `ORTUS_SKIP_CLAUDE_AUTH_CHECK=1` forces the check to pass (for future
    CI runs with a mocked claude).
    """
    if os.environ.get("ORTUS_SKIP_CLAUDE_AUTH_CHECK") == "1":
        return True
    real_claude = Path.home() / ".claude"
    if not real_claude.exists():
        return False
    return any(
        (real_claude / name).is_file() and (real_claude / name).stat().st_size > 0
        for name in _CLAUDE_AUTH_FILES
    )


requires_claude_auth = pytest.mark.skipif(
    not claude_authenticated(),
    reason=(
        "claude auth not available; slow tests require operator login. "
        "Run `claude login` so ~/.claude/.credentials.json exists, or set "
        "ORTUS_SKIP_CLAUDE_AUTH_CHECK=1 to override (e.g. when claude is mocked)."
    ),
)


# ---------------------------------------------------------------------------
# Nested-sandbox guard (ortus-fjkr).
#
# When the @slow live-claude tests run from inside an already-sandboxed claude
# session (an ortus grind/goal loop), the *inner* `claude -p` session's Bash
# tool dies with:
#
#   Sandbox is required but failed to initialize: EPERM: operation not
#   permitted, listen /tmp/claude-<uid>/srt-mux-<n>.sock
#
# The failure is intermittent — the inner session may complete zero, some, or
# all of its bd writes — so a run that reaches the assertions reports "plan
# produced 0 issues", which reads as a decomposition regression when the real
# cause is the sandbox. Two guards keep that from happening:
#
#   1. `requires_no_nested_sandbox` skips up front when we can see we are a
#      child claude session, so the outcome is consistent rather than flaky.
#   2. `skip_if_srt_mux_eperm` inspects the session log after the fact and
#      converts a sandbox-caused failure into a skip naming the EPERM, for any
#      nesting shape the env-var check does not catch.
# ---------------------------------------------------------------------------

_SRT_MUX_EPERM_MARKERS = ("srt-mux", "Sandbox is required but failed to initialize")


def nested_claude_sandbox() -> bool:
    """Return True when this process is itself inside a claude session.

    `CLAUDE_CODE_CHILD_SESSION` is exported by claude into the environment of
    processes it spawns, so its presence means any `claude -p` we launch would
    be doubly nested — the srt-mux EPERM condition. `ORTUS_ALLOW_NESTED_SANDBOX=1`
    forces the tests to run anyway (e.g. to reproduce the bug on purpose).
    """
    if os.environ.get("ORTUS_ALLOW_NESTED_SANDBOX") == "1":
        return False
    return bool(os.environ.get("CLAUDE_CODE_CHILD_SESSION"))


requires_no_nested_sandbox = pytest.mark.skipif(
    nested_claude_sandbox(),
    reason=(
        "nested claude sandbox: this pytest run is itself inside a claude "
        "session (CLAUDE_CODE_CHILD_SESSION is set), where the inner `claude -p` "
        "Bash tool intermittently dies on 'Sandbox is required but failed to "
        "initialize: EPERM ... listen /tmp/claude-<uid>/srt-mux-<n>.sock' "
        "(ortus-fjkr). Run these tests from a plain shell, or set "
        "ORTUS_ALLOW_NESTED_SANDBOX=1 to override."
    ),
)


def skip_if_srt_mux_eperm(log_text: str, *, what: str) -> None:
    """Skip instead of failing when `log_text` shows the srt-mux EPERM.

    Called from the assertion path of the live tests so a sandbox-caused
    failure is never reported as a decomposition/loop regression (ortus-fjkr).
    """
    if any(marker in log_text for marker in _SRT_MUX_EPERM_MARKERS):
        pytest.skip(
            f"{what} failed because the inner claude session's Bash tool hit the "
            f"srt-mux sandbox EPERM ('Sandbox is required but failed to "
            f"initialize', ortus-fjkr), not because the logic regressed. "
            f"Session log tail:\n{log_text}"
        )


@pytest.fixture()
def claude_mock() -> Callable[[str], Path]:
    """Return a callable that resolves a canned scenario name → shim path.

    Usage:
        def test_x(claude_mock, monkeypatch):
            from ortus.commands import grind as gm
            from ortus.core.claude import ClaudeRunner
            shim = claude_mock("grind-empty-queue")
            monkeypatch.setattr(gm, "_make_runner",
                                lambda: ClaudeRunner(claude_binary=str(shim)))
    """

    def _resolve(scenario: str) -> Path:
        source = _CANNED_DIR / f"{scenario}.py"
        if not source.is_file():
            available = sorted(p.stem for p in _CANNED_DIR.glob("*.py"))
            raise FileNotFoundError(
                f"no canned claude scenario named {scenario!r}; available: {available}"
            )
        return shim_path(scenario)

    return _resolve


@pytest.fixture()
def seeded_3_issues(tmp_path: Path) -> Path:
    """A fresh bd workspace with 1 epic + 2 children (1 ready, 1 blocked).

    Returns the repo root.
    """
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    repo = tmp_path / "seeded"
    repo.mkdir()
    subprocess.run(
        ["bd", "init", "--prefix", "seed"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    # `bd init` lands the incidental git repo on `master`; grind's branch guard
    # (ortus-6fu6) pins to the `main` integration branch, so align the fixture.
    normalize_git_branch(repo)
    # Epic
    epic = subprocess.run(
        [
            "bd",
            "create",
            "--silent",
            "--title",
            "Test epic",
            "--type",
            "epic",
            "--priority",
            "1",
        ],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    # Ready child
    ready = subprocess.run(
        [
            "bd",
            "create",
            "--silent",
            "--title",
            "Child ready",
            "--type",
            "task",
            "--priority",
            "2",
            "--parent",
            epic,
            *ready_issue_args(),
        ],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    # Blocked child (depends on the ready one being closed first)
    blocked = subprocess.run(
        [
            "bd",
            "create",
            "--silent",
            "--title",
            "Child blocked",
            "--type",
            "task",
            "--priority",
            "2",
            "--parent",
            epic,
            *ready_issue_args(),
        ],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    # Make `blocked` depend on `ready` (ready blocks blocked).
    subprocess.run(
        ["bd", "dep", "add", blocked, ready],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    return repo
