"""Hermetic end-to-end smoke harness against the local-dev Python ortus build
(ortus-inam).

Distinct from `test_e2e_smoke.py` (which uses Typer's in-process `CliRunner`),
this module shells out to the *built* ortus binary via `uv run --project <repo>`
so the harness always exercises whatever is at HEAD of this branch — wheel
metadata, entry-point shim, sandbox spawn surface, the lot.

Fast tests run by default and stay under ~30s wall-clock total. Slow tests
(`@pytest.mark.slow`) exercise verbs that need a real `claude` and are skipped
unless `./scripts/smoke-local.sh --slow` (or `pytest --slow`) is passed.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Callable

import pytest

from .conftest import _link_claude_auth, requires_claude_auth
from ._shims import normalize_git_branch

pytestmark = pytest.mark.smoke

# ---------------------------------------------------------------------------
# Resolve the repo root once; the harness must invoke the local-dev build, not
# whatever happens to be on PATH. `uv run --project <root>` is the modern
# uv-native pattern and matches the toolchain locked into pyproject.toml.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(
    subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
)


OrtusCallable = Callable[..., subprocess.CompletedProcess[str]]


@pytest.fixture()
def local_ortus() -> OrtusCallable:
    """Return a callable that invokes the local-dev ortus binary.

    Usage:
        proc = local_ortus("init", str(tmp_repo))
        proc = local_ortus("check", str(tmp_repo), check=False)
        proc = local_ortus("human", str(tmp_repo), env={...}, cwd=str(tmp_repo))
    """

    def _run(
        *args: str,
        check: bool = False,
        env: dict[str, str] | None = None,
        cwd: str | Path | None = None,
        timeout: float = 60.0,
    ) -> subprocess.CompletedProcess[str]:
        cmd = ["uv", "run", "--project", str(_REPO_ROOT), "ortus", *args]
        return subprocess.run(
            cmd,
            check=check,
            capture_output=True,
            text=True,
            env=env,
            cwd=str(cwd) if cwd is not None else None,
            timeout=timeout,
        )

    return _run


@pytest.fixture()
def random_prefix() -> str:
    """Per-test random bd workspace prefix.

    The `smoke` base makes test-created bd issues visually distinguishable
    from real ones if state ever leaks; the hex suffix ensures uniqueness
    across runs and parallel pytest workers.

    Randomizing surfaces any code path (in prompts, scripts, or helpers)
    that assumes a default `bd-` prefix shape. See ortus-vidr / ortus-5w6r.
    """
    return "smoke" + secrets.token_hex(3)


@pytest.fixture()
def tmp_repo(tmp_path: Path, random_prefix: str) -> Path:
    """Hermetic project root, pre-`ortus init`-ed with a random bd prefix.

    Tests that need a bare directory (i.e. that want to exercise `ortus init`
    themselves on an empty dir) should use `tmp_path` directly and pass an
    explicit `--prefix`.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    proc = subprocess.run(
        [
            "uv", "run", "--project", str(_REPO_ROOT), "ortus", "init",
            str(repo), "--prefix", random_prefix,
        ],
        check=False,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    if proc.returncode != 0:
        # check=True swallows stderr/stdout into a stringified
        # CalledProcessError that only shows the exit code, which has burned
        # multiple Windows CI iterations on this fixture (ortus-rlob). Fail
        # loudly with the actual diagnostic streams so the next iteration
        # has the real cause.
        pytest.fail(
            f"`ortus init` exited {proc.returncode} during tmp_repo setup\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )
    # `ortus init` git-inits on the git default branch (`master` on most
    # installs); align the fixture with grind's default integration branch so
    # the branch-discipline guard (ortus-6fu6) sees an on-`main` state after a
    # worker commits, instead of halting on a stray `master`. Production ortus
    # repos already live on `main`.
    normalize_git_branch(repo)
    return repo


# ---------------------------------------------------------------------------
# Skip-helpers: many checks need third-party binaries (bd, claude, jq, bwrap).
# Rather than guard every test by hand, surface clear skip reasons up front.
# ---------------------------------------------------------------------------


def _require(*binaries: str) -> None:
    missing = [b for b in binaries if shutil.which(b) is None]
    if missing:
        pytest.skip(f"required binaries not on PATH: {', '.join(missing)}")


def _plan_log_tail(repo: Path, *, lines: int = 40) -> str:
    """Tail of the most recent plan-*.log, for assertion messages.

    A zero-issue `ortus plan` says nothing about *why* on stdout — the cause
    lives in the session log (e.g. every Bash tool call failing on a sandbox
    EPERM, ortus-jke7). Inlining the tail makes the failure self-diagnosing
    instead of sending the next reader digging through pytest tmpdirs.
    """
    logs = sorted((repo / "logs").glob("plan-*.log"))
    if not logs:
        return f"(no plan-*.log under {repo / 'logs'})"
    try:
        text = logs[-1].read_text(errors="replace")
    except OSError as exc:  # pragma: no cover - diagnostics only
        return f"({logs[-1]}: {exc})"
    return f"{logs[-1]}:\n" + "\n".join(text.splitlines()[-lines:])


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Reroute $HOME so the user's real ~/.claude/settings.json can't influence
    the test outcome — `ortus check`'s hook-precheck reads it.

    Auth files are symlinked back in via `_link_claude_auth` so slow tests
    that shell out to claude don't fail with "Not logged in" (ortus-v0uw).
    Capture `real_home` BEFORE the monkeypatch so the symlink resolves to
    the operator's actual ~/.claude/.
    """
    real_home = Path.home()
    fake_home = tmp_path_factory.mktemp("fake-home")
    monkeypatch.setenv("HOME", str(fake_home))
    _link_claude_auth(real_home, fake_home)
    return fake_home


# ---------------------------------------------------------------------------
# Sanity: confirm the harness really is hitting the local-dev build.
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_uv_build_produces_dynamic_version(tmp_path: Path) -> None:
    # Regression guard for ortus-qyjf: pyproject.toml used to hardcode
    # `version = "0.1.0"`, so every tag built a 0.1.0 wheel and PyPI rejected
    # subsequent uploads with "file already exists". After moving to hatch-vcs,
    # `uv build` must derive the version from the git state.
    _require("uv", "git")
    proc = subprocess.run(
        ["uv", "build", "--out-dir", str(tmp_path)],
        cwd=str(_REPO_ROOT),
        check=False,
        capture_output=True,
        text=True,
        timeout=120.0,
    )
    if proc.returncode != 0:
        pytest.fail(
            f"`uv build` exited {proc.returncode}\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )
    wheels = sorted(tmp_path.glob("ortus-*.whl"))
    assert wheels, f"no wheel produced; dist contents: {list(tmp_path.iterdir())}"
    # Wheel filename: ortus-<version>-py3-none-any.whl. Parse out <version>.
    wheel_version = wheels[0].name.removeprefix("ortus-").split("-", 1)[0]

    # Compare against what hatch-vcs should derive from git. On a tagged HEAD
    # this is the tag (minus the leading `v`); otherwise hatch-vcs emits a
    # `<next>.devN+g<sha>` form. Either way: it must not be the stale 0.1.0.
    describe = subprocess.run(
        ["git", "describe", "--tags", "--always"],
        cwd=str(_REPO_ROOT),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if describe.startswith("v") and "-" not in describe:
        # Clean tagged commit: wheel version must match the tag exactly.
        assert wheel_version == describe[1:], (
            f"wheel reports version {wheel_version!r}; expected {describe[1:]!r} "
            f"from `git describe`"
        )
    else:
        # Untagged or post-tag commit: hatch-vcs emits a dev marker.
        assert ".dev" in wheel_version, (
            f"wheel version {wheel_version!r} lacks `.dev` marker on an untagged "
            f"commit (git describe: {describe!r}) — dynamic versioning is wired wrong"
        )


def test_local_ortus_version_matches_installed(local_ortus: OrtusCallable) -> None:
    # Version is dynamic (hatch-vcs sets it from the git tag at build time), so
    # we can't read a static string out of pyproject.toml. The local-dev build
    # invoked via `uv run` should report the same version that this interpreter
    # sees via `importlib.metadata`. If they diverge, the harness is hitting a
    # stale install.
    from importlib.metadata import version as _pkg_version

    proc = local_ortus("--version", check=True)
    expected = _pkg_version("ortus")
    assert expected in proc.stdout, (
        f"local ortus reports {proc.stdout!r}; expected version {expected!r} "
        f"from the test interpreter's installed ortus package — harness may be "
        f"hitting a stale binary"
    )


# ---------------------------------------------------------------------------
# init verb
# ---------------------------------------------------------------------------


def test_init_creates_expected_files(tmp_repo: Path) -> None:
    """AC #6: init creates .beads/, .claude/settings.json, .ortusrc, AGENTS.md.

    The `tmp_repo` fixture has already invoked `ortus init` with a random
    prefix; verifying the post-init state is what this test cares about.
    """
    _require("bd")
    expected = [
        tmp_repo / ".beads",
        tmp_repo / ".claude" / "settings.json",
        tmp_repo / ".ortusrc",
        tmp_repo / "AGENTS.md",
    ]
    missing = [str(p) for p in expected if not p.exists()]
    assert not missing, (
        f"ortus init did not create expected artifacts: {missing}. "
        f"The init verb's create-on-init logic may have regressed."
    )


def test_init_settings_shape(tmp_repo: Path) -> None:
    """AC #6: settings.json declares bd in sandbox.excludedCommands and does
    not silently turn off hooks."""
    _require("bd")
    data = json.loads((tmp_repo / ".claude" / "settings.json").read_text())
    excluded = data.get("sandbox", {}).get("excludedCommands", [])
    assert "bd" in excluded, (
        f".claude/settings.json sandbox.excludedCommands is {excluded!r}; "
        f"expected 'bd' present. ortus init's settings rendering may have drifted."
    )
    assert "bd *" in excluded, (
        f".claude/settings.json sandbox.excludedCommands is {excluded!r}; "
        f"expected 'bd *' present (covers `bd <subcmd>` invocations)."
    )
    assert not data.get("disableAllHooks", False), (
        ".claude/settings.json sets disableAllHooks=true — ortus init must "
        "never ship a disabled-hooks template (would break bd prime SessionStart)."
    )


def test_init_idempotency_force(local_ortus: OrtusCallable, tmp_repo: Path) -> None:
    """AC #6: re-running init without --force is refused; --force succeeds.

    The `tmp_repo` fixture has already invoked init once; this test verifies
    that a second init refuses and a third init with --force succeeds.
    """
    _require("bd")
    again = local_ortus("init", str(tmp_repo))
    assert again.returncode != 0, (
        "second `ortus init` without --force exited 0; expected refusal "
        "since .beads/ already exists (would silently clobber state)."
    )
    forced = local_ortus("init", str(tmp_repo), "--force")
    assert forced.returncode == 0, (
        f"`ortus init --force` exited {forced.returncode}.\n"
        f"stdout:\n{forced.stdout}\nstderr:\n{forced.stderr}"
    )


# ---------------------------------------------------------------------------
# check verb
# ---------------------------------------------------------------------------


def test_check_green_path(local_ortus: OrtusCallable, tmp_repo: Path) -> None:
    """AC #6: check on a healthy just-init'd repo with all binaries present.

    Skips if any of {bd, claude, jq, bwrap} is missing — those are check's
    own external prereqs and their absence is not a smoke-harness failure.
    """
    _require("bd", "claude", "jq", "bwrap")
    proc = local_ortus("check", str(tmp_repo))
    assert proc.returncode == 0, (
        f"`ortus check` exited {proc.returncode} on a fresh init'd repo with "
        f"all prereq binaries present. Some downstream check has regressed.\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert "PASS" in proc.stdout, (
        f"`ortus check` output did not contain 'PASS'; the table rendering "
        f"may have changed.\nstdout:\n{proc.stdout}"
    )


def test_check_disabled_hooks_fails(
    local_ortus: OrtusCallable, tmp_repo: Path
) -> None:
    """AC #6: disableAllHooks=true → check exits 1 with hook-precheck error."""
    _require("bd")
    settings = tmp_repo / ".claude" / "settings.json"
    data = json.loads(settings.read_text())
    data["disableAllHooks"] = True
    settings.write_text(json.dumps(data))
    proc = local_ortus("check", str(tmp_repo))
    assert proc.returncode == 1, (
        f"`ortus check` with disableAllHooks=true exited {proc.returncode}; "
        f"expected 1. hooks precheck may have regressed.\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    combined = proc.stdout + proc.stderr
    assert "hooks" in combined.lower(), (
        f"`ortus check` did not mention 'hooks' in its failure output; the "
        f"diagnostic may have lost its actionable cue.\noutput:\n{combined}"
    )


def test_check_missing_bd_fails(
    local_ortus: OrtusCallable, tmp_repo: Path, tmp_path: Path
) -> None:
    """AC #6: with bd masked from PATH, check exits non-zero and flags bd."""
    _require("bd")
    # Build a PATH stub that contains *only* uv + git + jq + bwrap + claude —
    # explicitly NOT bd. The subprocess will then see bd as missing.
    # Windows symlinks need admin/dev mode; copy as a fallback so the
    # PATH-masking still exercises the bd-presence check.
    stub = tmp_path / "stub-bin"
    stub.mkdir()
    for needed in ("uv", "git", "jq", "bwrap", "claude", "sh", "bash", "env"):
        src = shutil.which(needed)
        if src is None:
            continue
        dest = stub / Path(src).name
        try:
            os.symlink(src, dest)
        except OSError:
            try:
                shutil.copy2(src, dest)
            except OSError:
                continue
    env = {
        **os.environ,
        "PATH": str(stub),
    }
    proc = local_ortus("check", str(tmp_repo), env=env)
    assert proc.returncode != 0, (
        f"`ortus check` with bd masked from PATH exited 0; expected non-zero. "
        f"bd-presence check may have regressed.\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    combined = proc.stdout + proc.stderr
    assert "bd" in combined, (
        f"`ortus check` did not mention 'bd' in its failure output; the "
        f"diagnostic may have lost its actionable cue.\noutput:\n{combined}"
    )


# ---------------------------------------------------------------------------
# tail verb (no external binaries required)
# ---------------------------------------------------------------------------


def test_tail_runs_against_seeded_log(tmp_repo: Path) -> None:
    """AC #6: tail runs without crashing and surfaces content from a seeded
    grind-*.log fixture.

    `ortus tail` polls forever (no --iterations on the CLI surface), so we
    drive it via Popen and SIGTERM after the first read, then verify the
    accumulated stdout. The verb flushes after every line, so partial
    output survives the kill.
    """
    _require("bd")
    # tmp_repo is already inited by the fixture.
    logs = tmp_repo / "logs"
    logs.mkdir(exist_ok=True)
    (logs / "grind-smoke.log").write_text(
        '{"type":"assistant","message":{"content":"smoke harness saw this"}}\n'
    )
    proc = subprocess.Popen(
        ["uv", "run", "--project", str(_REPO_ROOT), "ortus", "tail", str(tmp_repo)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=3.0)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            stdout, stderr = proc.communicate(timeout=3.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
    assert "smoke harness saw this" in stdout, (
        f"`ortus tail` did not surface seeded log content within 3s; the "
        f"log-filter or flush may have regressed.\n"
        f"stdout:\n{stdout}\nstderr:\n{stderr}"
    )


# ---------------------------------------------------------------------------
# human verb
# ---------------------------------------------------------------------------


def test_human_writes_todo(
    local_ortus: OrtusCallable, tmp_repo: Path
) -> None:
    """AC #6: human emits HUMAN-TODO.md listing each flagged issue."""
    _require("bd")
    issue_id = subprocess.run(
        [
            "bd", "create", "--silent",
            "--title", "smoke decision needed",
            "--type", "task", "--priority", "2", "--labels", "human",
        ],
        cwd=str(tmp_repo), check=True, capture_output=True, text=True,
    ).stdout.strip()
    proc = local_ortus("human", str(tmp_repo))
    assert proc.returncode == 0, (
        f"`ortus human` exited {proc.returncode}.\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    todo = tmp_repo / "HUMAN-TODO.md"
    assert todo.is_file(), (
        f"`ortus human` did not write HUMAN-TODO.md at {todo}. The file-emit "
        f"path may have regressed."
    )
    body = todo.read_text(encoding="utf-8")
    assert issue_id in body, (
        f"HUMAN-TODO.md does not mention the seeded issue {issue_id!r}; "
        f"the human-flag selection logic may have regressed.\nbody:\n{body}"
    )
    assert "smoke decision needed" in body, (
        f"HUMAN-TODO.md does not contain the seeded title; rendering may "
        f"have regressed.\nbody:\n{body}"
    )


# ---------------------------------------------------------------------------
# triage verb — interactive, skipped per AC #8
# ---------------------------------------------------------------------------


def test_triage_skipped_interactive() -> None:
    pytest.skip(
        "interactive; AskUserQuestion can't be mocked headless. "
        "Covered by tests/test_triage.py with monkeypatched prompts."
    )


# ---------------------------------------------------------------------------
# Slow tests — gated behind `@pytest.mark.slow`. Run via `--slow`.
# These spend real claude API budget.
# ---------------------------------------------------------------------------


@pytest.mark.slow
@requires_claude_auth
def test_plan_decompose_tiny_prd(
    local_ortus: OrtusCallable, tmp_repo: Path, random_prefix: str
) -> None:
    """AC #7: plan decomposes a synthetic 3-task PRD into bd issues.

    Also asserts every created issue uses the workspace's actual (random)
    prefix — regression guard for ortus-5w6r (plan-prompt hardcoding `bd-`).
    """
    _require("bd", "claude")
    prd = tmp_repo / "tiny.prd.md"
    prd.write_text(
        "# Tiny PRD\n\n"
        "Build a calculator with exactly three tasks:\n"
        "1. Implement add(a, b)\n"
        "2. Implement subtract(a, b)\n"
        "3. Write unit tests for both\n"
    )
    proc = local_ortus("plan", str(tmp_repo), str(prd), timeout=300.0)
    assert proc.returncode == 0, (
        f"`ortus plan` exited {proc.returncode} on tiny PRD.\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}\n"
        f"--- plan session log tail ---\n{_plan_log_tail(tmp_repo)}"
    )
    issues = json.loads(
        subprocess.run(
            ["bd", "list", "--status", "open", "--json"],
            cwd=str(tmp_repo), check=True, capture_output=True, text=True,
        ).stdout
    )
    assert len(issues) >= 3, (
        f"`ortus plan` produced {len(issues)} issues; expected ≥ 3 from a "
        f"3-task PRD. Decomposition logic may have regressed.\n"
        f"--- plan session log tail ---\n{_plan_log_tail(tmp_repo)}"
    )
    wrong_prefix = [
        i["id"] for i in issues if not i["id"].startswith(f"{random_prefix}-")
    ]
    assert not wrong_prefix, (
        f"`ortus plan` created issues whose IDs do not match the workspace's "
        f"prefix {random_prefix!r}: {wrong_prefix}. The plan-prompt likely "
        f"hardcoded `bd-` (or another prefix) — see ortus-5w6r."
    )


@pytest.mark.slow
@requires_claude_auth
def test_grind_one_task(local_ortus: OrtusCallable, tmp_repo: Path) -> None:
    """AC #7: grind --tasks 1 against a seeded fixture closes one issue."""
    _require("bd", "claude")
    subprocess.run(
        [
            "bd", "create", "--silent",
            "--title", "Write hello-world to README.md",
            "--description", "Append the line 'hello world' to README.md.",
            "--type", "task", "--priority", "2",
        ],
        cwd=str(tmp_repo), check=True, capture_output=True, text=True,
    )
    open_before = json.loads(
        subprocess.run(
            ["bd", "list", "--status", "open", "--json"],
            cwd=str(tmp_repo), check=True, capture_output=True, text=True,
        ).stdout
    )
    proc = local_ortus("grind", str(tmp_repo), "--tasks", "1", timeout=600.0)
    assert proc.returncode == 0, (
        f"`ortus grind` exited {proc.returncode}.\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    open_after = json.loads(
        subprocess.run(
            ["bd", "list", "--status", "open", "--json"],
            cwd=str(tmp_repo), check=True, capture_output=True, text=True,
        ).stdout
    )
    assert len(open_after) == len(open_before) - 1, (
        f"`ortus grind --tasks 1` did not close exactly one issue: "
        f"before={len(open_before)} after={len(open_after)}. grind loop may "
        f"have regressed."
    )
