"""Shared test fixtures.

Per FR-034 (Testing Strategy): claude_mock fixture loads a canned scenario
by key. Scenarios live in tests/fixtures/canned-claude-responses/<key>.py
and behave as drop-in replacements for the real claude binary. Resolution
goes through tests._shims.shim_path so each scenario returns a path the
host OS can execute directly (POSIX: the .py file with +x; Windows: a
generated .bat wrapper). See ortus-f4bu.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import re
import signal
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable

import pytest

from tests._shims import normalize_git_branch, ready_issue_args, shim_path

_DEPENDENCY_MARKERS = ("fast", "integration", "network", "live_provider")
_HERMETIC_TEST_BUDGET_SECONDS = 5.0
#: Multiplier applied to that budget, so a slow machine cannot redden a healthy
#: build. Hosted runners vary by roughly 3x run to run — the same commit on the
#: same leg has taken 706s and 2232s — and an absolute budget measures the
#: runner rather than the test. CI raises this; a developer machine is stable
#: enough to keep the tight number that makes the guard useful.
_DURATION_BUDGET_SCALE = float(os.environ.get("ORTUS_TEST_BUDGET_SCALE", "1") or "1")
_DURATION_BUDGET = _HERMETIC_TEST_BUDGET_SECONDS * _DURATION_BUDGET_SCALE
_CI_GATE_WORKFLOW = Path(__file__).parent.parent / ".github" / "workflows" / "test.yml"
# The gate invocation, including its backslash-continued flag lines.
_CI_GATE_COMMAND_RE = re.compile(
    r'uv run pytest -m "fast or integration"(?:[^\n]*\\\n)*[^\n]*'
)


def ci_gate_command() -> str:
    """Return the comprehensive CI gate invocation, verbatim from the workflow.

    Read out of `.github/workflows/test.yml` rather than restated here, so a
    test asserting what the gate does cannot drift from what it actually runs.
    """
    body = _CI_GATE_WORKFLOW.read_text(encoding="utf-8")
    command = _CI_GATE_COMMAND_RE.search(body)
    if command is None:
        raise RuntimeError(
            f"no `fast or integration` gate command found in {_CI_GATE_WORKFLOW}"
        )
    return command.group(0)


def ci_gate_flags() -> tuple[str, ...]:
    """Return the duration and timeout flags the comprehensive CI gate applies.

    The per-test timeout is reproduced by every worker and verifier sweep, so a
    hung test is caught before main (ortus-q3lh). The duration budget is
    deliberately *not*: those sweeps run with `-n auto`, and contending workers
    inflate durations enough to manufacture breaches, so CI stays the single
    authority on how fast a test is (ortus-3ehq). Both flags are still parsed
    out of the workflow, so a change to the gate cannot silently desync them.
    """
    command = ci_gate_command()
    timeout = re.search(r"--test-timeout=\d+", command)
    if timeout is None or "--enforce-duration-budget" not in command:
        raise RuntimeError(
            "the CI gate command no longer carries both a per-test timeout and "
            f"the duration budget: {command!r}"
        )
    return (timeout.group(0), "--enforce-duration-budget")


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


@pytest.fixture(autouse=True)
def _codegraph_policy(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pin the CodeGraph policy to `auto` unless a test opts into the default.

    `required` is the built-in default, so a bd workspace built
    without a `.codegraph/` index now aborts at the probe — and whether it
    aborts would otherwise depend on which host runs the suite. Lifecycle
    tests are not about CodeGraph, so they keep the best-effort policy;
    tests that are about the policy carry `@pytest.mark.codegraph_default`
    and see the real resolved default.
    """
    if "codegraph_default" in request.keywords:
        return
    from ortus.core import config as _config

    monkeypatch.setitem(_config.DEFAULTS, "codegraph", "auto")


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
        and report.duration > _DURATION_BUDGET
        and item.get_closest_marker("network") is None
        and item.get_closest_marker("live_provider") is None
        and item.get_closest_marker("slow") is None
    ):
        report.outcome = "failed"
        report.longrepr = (
            f"{item.nodeid} took {report.duration:.2f}s; hermetic tests over "
            f"{_DURATION_BUDGET:.0f}s must be optimized or marked slow"
        )


@pytest.fixture(scope="session")
def _empty_git_config(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """An empty file to stand in for the operator's ~/.gitconfig."""
    path = tmp_path_factory.mktemp("git-identity") / "gitconfig"
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def isolated_beads_tracker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep test `bd` subprocesses off an inherited host tracker.

    `ortus grind` sets BEADS_DIR on its workers. Criterion-check pytest
    inherits that pin and would otherwise init or mutate the host database.
    """
    monkeypatch.delenv("BEADS_DIR", raising=False)


@pytest.fixture(autouse=True)
def neutralized_git_identity(
    monkeypatch: pytest.MonkeyPatch, _empty_git_config: Path
) -> None:
    """Hide the ambient global git identity from every test (ortus-q3lh).

    A developer machine has `user.email` in ~/.gitconfig and a CI runner does
    not, so a fixture that shells out to `git commit` without configuring its
    own identity passes locally and fails on the runner — a class that landed
    twelve failures through verification phases that all reported pass. Every
    test now sees the runner's condition, so such a fixture fails loudly here
    instead of on main.

    `GIT_CONFIG_GLOBAL` is pointed at an empty file rather than HOME being
    cleared, because clearing HOME breaks unrelated tooling the tests invoke.
    Autouse so a new test cannot opt out by omission, which is exactly how the
    gap was introduced. `monkeypatch` restores the previous environment even
    when a test raises.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(_empty_git_config))


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


# ---------------------------------------------------------------------------
# bd workspace templates (ortus-apmf).
#
# Every `bd` call is a fresh Go process opening a Dolt database, so `bd init`
# measures ~1.6s and `bd create` ~1.2s where git doing comparable filesystem
# work measures 2-3ms. Paying that per test put the grind-family suite on a
# flat multi-second setup floor before a single assertion ran. Instead the
# session pays one `bd init`, bakes the fixture issues into a few template
# workspaces, and each test acquires its workspace by copying the template it
# needs — measured at ~25ms including the re-rooting below.
#
# Copies rather than one shared workspace: grind tests mutate issue state, so
# sharing would couple them, and at 25ms isolation is effectively free.
# ---------------------------------------------------------------------------

BD_TEMPLATE_PREFIX = "tmpl"


@dataclass(frozen=True)
class BdWorkspace:
    """A bd workspace and the ids of the issues baked into it."""

    path: Path
    issues: tuple[str, ...]


_TEMPLATES: dict[str, BdWorkspace] = {}
_TEMPLATE_DIGESTS: dict[str, dict[str, str]] = {}
_bd_init_calls = 0


def bd_init_calls() -> int:
    """How many times this session has run `bd init` to build a template.

    Per process, so under `-n auto` each xdist worker builds its own templates
    and pays its own single init — a handful of inits per run rather than one
    per test, which is the saving either way.
    """
    return _bd_init_calls


@lru_cache(maxsize=1)
def _templates_root() -> Path:
    """A session-lifetime directory holding the template workspaces.

    Deliberately not `tmp_path_factory`: `copy_bd_workspace` is called from
    plain module-level helpers in the test modules, which have no fixture to
    request. `atexit` removes the tree even when pytest exits on a signal.
    """
    root = Path(tempfile.mkdtemp(prefix="ortus-bd-templates-"))
    atexit.register(shutil.rmtree, root, True)
    return root


def _bd(cwd: Path, *args: str) -> str:
    env = os.environ.copy()
    # Grind workers pin BEADS_DIR to the host tracker. Template init/create
    # must use the workspace under cwd, not that inherited database.
    env.pop("BEADS_DIR", None)
    return subprocess.run(
        ["bd", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _build_bare(path: Path) -> tuple[str, ...]:
    """The one `bd init` this session performs, plus the setup every test shares."""
    global _bd_init_calls
    _bd_init_calls += 1
    _bd(path, "init", "--non-interactive", "--prefix", BD_TEMPLATE_PREFIX)
    # `bd init` lands the incidental git repo on `master`; grind's branch guard
    # (ortus-6fu6) pins to the `main` integration branch. Normalized here rather
    # than at the copy so every copy inherits an already-correct branch.
    normalize_git_branch(path)
    # Ortus finalizes a verified candidate with a real `git commit`, which
    # aborts without an author identity — and `neutralized_git_identity` hides
    # the operator's global one from every test, exactly as a runner would.
    _git(path, "config", "user.email", "ortus-tests@example.invalid")
    _git(path, "config", "user.name", "Ortus Tests")
    # grind refuses to spawn a worker unless bd is excluded from the sandbox.
    # This overwrites the hook settings `bd init` writes, which is what every
    # grind fixture did by hand before.
    settings = path / ".claude" / "settings.json"
    settings.parent.mkdir(exist_ok=True)
    settings.write_text(
        json.dumps({"sandbox": {"excludedCommands": ["bd", "bd *"]}}), encoding="utf-8"
    )
    # Copies inherit this commit, so grind does not treat scaffold files as a
    # recovery handoff (ortus-eji6).
    gitignore = path / ".gitignore"
    ignore = "logs/\n.cache/\n.beads/ortus.flock\n"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.is_file() else ""
    if ignore not in existing:
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        gitignore.write_text(existing + prefix + ignore, encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "fixture baseline")
    return ()


def _build_leaf(path: Path) -> tuple[str, ...]:
    """One ready, executable leaf — the shape most grind tests drive."""
    return (
        _bd(
            path,
            "create",
            "--silent",
            "--title",
            "ready leaf",
            "--type",
            "task",
            "--priority",
            "1",
            *ready_issue_args(),
        ),
    )


def _build_epic(path: Path) -> tuple[str, ...]:
    """1 epic + 2 children, one ready and one blocked behind it."""
    common = ("create", "--silent", "--type")
    epic = _bd(path, *common, "epic", "--title", "Test epic", "--priority", "1")
    children = [
        _bd(
            path,
            *common,
            "task",
            "--title",
            title,
            "--priority",
            "2",
            "--parent",
            epic,
            *ready_issue_args(),
        )
        for title in ("Child ready", "Child blocked")
    ]
    ready, blocked = children
    # `blocked` waits on `ready` being closed first.
    _bd(path, "dep", "add", blocked, ready)
    return (epic, ready, blocked)


_TEMPLATE_BUILDERS: dict[str, Callable[[Path], tuple[str, ...]]] = {
    "bare": _build_bare,
    "leaf": _build_leaf,
    "epic": _build_epic,
}


def bd_template(kind: str = "bare") -> BdWorkspace:
    """Return this session's `kind` template workspace, building it once.

    Never hand the returned path to a test: it is the master that copies are
    made from. Call `copy_bd_workspace` (or use the `bd_workspace` fixture).
    """
    if kind not in _TEMPLATE_BUILDERS:
        raise ValueError(
            f"unknown bd template kind {kind!r}; have {sorted(_TEMPLATE_BUILDERS)}"
        )
    cached = _TEMPLATES.get(kind)
    if cached is not None:
        return cached
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    path = _templates_root() / kind
    if kind == "bare":
        path.mkdir(parents=True)
        issues = _build_bare(path)
    else:
        # Seeded kinds copy the bare template instead of re-running `bd init`,
        # so the session pays exactly one init however many kinds it uses.
        _copy_workspace(bd_template("bare").path, path)
        issues = _TEMPLATE_BUILDERS[kind](path)
    template = BdWorkspace(path, issues)
    _TEMPLATES[kind] = template
    _TEMPLATE_DIGESTS[kind] = _digests(path)
    return template


def copy_bd_workspace(dest: Path, kind: str = "bare") -> BdWorkspace:
    """Copy the `kind` template workspace to `dest` and return the copy.

    This is how a test acquires a bd workspace: ~25ms against ~1.6s for
    `bd init`. `dest` must not exist yet; rooting it in the test's own
    `tmp_path` is what keeps two xdist workers off the same destination.
    """
    template = bd_template(kind)
    _assert_template_pristine(kind)
    dest.parent.mkdir(parents=True, exist_ok=True)
    _copy_workspace(template.path, dest)
    return BdWorkspace(dest, template.issues)


def _copy_workspace(template: Path, dest: Path) -> None:
    """Copy a workspace and prove the copy stands on its own.

    The one absolute path `bd init` writes into a workspace is git's
    `core.hooksPath`, aimed at `<workspace>/.beads/hooks`. A plain copy would
    therefore run — and let git write through — the *template's* hooks, so it
    is re-rooted onto the copy. The scan that follows then proves no other
    reference survived, rather than assuming bd stores no path elsewhere.
    """
    shutil.copytree(template, dest, symlinks=True)
    _git(dest, "config", "core.hooksPath", str(dest / ".beads" / "hooks"))
    leaked = _template_path_references(template, dest)
    if leaked:
        raise AssertionError(
            f"the bd workspace copied to {dest} still points at its template "
            f"{template} from {leaked}. Such a copy is not isolated. Re-root the "
            f"new reference here; do NOT fall back to a per-test `bd init`, which "
            f"would silently restore the cost this harness exists to remove."
        )


def _template_path_references(template: Path, dest: Path) -> list[str]:
    """Files under `dest` whose bytes still contain the template's own path.

    Scanned as bytes over the whole tree, including the embedded Dolt database,
    rather than over a hand-listed set of config files — a path stored somewhere
    unanticipated is then detected instead of assumed absent.
    """
    needle = str(template).encode()
    return [
        str(path.relative_to(dest))
        for path in sorted(dest.rglob("*"))
        if path.is_file() and not path.is_symlink() and needle in path.read_bytes()
    ]


def _digests(path: Path) -> dict[str, str]:
    return {
        str(child.relative_to(path)): hashlib.sha256(child.read_bytes()).hexdigest()
        for child in sorted(path.rglob("*"))
        if child.is_file() and not child.is_symlink()
    }


def _assert_template_pristine(kind: str) -> None:
    """Fail at the next acquisition if a test wrote into a template.

    Content digests rather than size and mtime, so an in-place edit that
    preserves both is still caught. The copy direction has to be enforced:
    a mutated template silently changes every later test's starting state.
    """
    current = _digests(_TEMPLATES[kind].path)
    expected = _TEMPLATE_DIGESTS[kind]
    if current == expected:
        return
    changed = sorted(
        set(current).symmetric_difference(expected)
        | {name for name, digest in current.items() if expected.get(name) != digest}
    )
    raise AssertionError(
        f"the {kind!r} bd template was modified after it was built: {changed}. "
        f"Templates are read-only masters — write only into the copy that "
        f"`copy_bd_workspace` returns."
    )


@pytest.fixture(scope="session")
def bd_template_bare() -> BdWorkspace:
    return bd_template("bare")


@pytest.fixture(scope="session")
def bd_template_leaf() -> BdWorkspace:
    return bd_template("leaf")


@pytest.fixture(scope="session")
def bd_template_epic() -> BdWorkspace:
    return bd_template("epic")


@pytest.fixture()
def bd_workspace(tmp_path: Path) -> Callable[..., BdWorkspace]:
    """Factory for a per-test bd workspace: `bd_workspace("repo", "leaf")`.

    Destination first, matching `copy_bd_workspace`; the difference is that
    this one names a directory under the test's `tmp_path` instead of a path.
    """

    def _make(name: str = "repo", kind: str = "bare") -> BdWorkspace:
        return copy_bd_workspace(tmp_path / name, kind)

    return _make


@pytest.fixture()
def seeded_3_issues(tmp_path: Path) -> Path:
    """A bd workspace with 1 epic + 2 children (1 ready, 1 blocked).

    Returns the repo root.
    """
    return copy_bd_workspace(tmp_path / "seeded", "epic").path
