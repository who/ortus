"""Integration tests for leftover-claim treatment under the on-main contract.

Since the grind-roots rework the worker owns selection and lifecycle: it
continues a leftover in_progress claim or claims from `bd ready`, and grind
judges only observable bd status after the process exits. A claimed-but-
unclosed issue is therefore not an orphan — it is a live claim left for the
next context window, and the next grind invocation is that window.

`--orphan-policy` still exists, but only the startup sweep consults it, and
`revert` is coerced to `warn` there: reverting a live unfinished claim would
discard the routing the worker recorded in bd. `escalate` still hands the
issue to a human when the operator asked for that policy.
"""

from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.commands import grind as grind_mod
from ortus.core import sandbox as sandbox_mod
from ortus.core.claude import ClaudeRunner
from ortus.core.sandbox import SandboxInfo
from tests._shims import make_inline_python_shim
from tests.conftest import copy_bd_workspace


pytestmark = [pytest.mark.integration, pytest.mark.slow]
runner = CliRunner()


def _stub_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sandbox_mod, "smoke_test", lambda: SandboxInfo(platform="Linux", binary="bwrap")
    )


def _seed_repo(tmp_path: Path) -> tuple[Path, str]:
    """Returns (repo, issue_id) — one ready issue.

    A ~25ms copy of the session's `leaf` template, which already carries the
    ready issue, the `main` branch grind's guard requires, and the enabled
    .claude, rather than a `bd init` plus a `bd create` (ortus-apmf).
    """
    workspace = copy_bd_workspace(tmp_path / "orphan-policy", "leaf")
    repo = workspace.path
    (repo / ".gitignore").write_text(
        "logs/\n.cache/\n.beads/ortus.flock\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "fixture baseline"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo, workspace.issues[0]


def _claim_only_shim(tmp_path: Path) -> Path:
    return make_inline_python_shim(
        tmp_path,
        "claude-claims",
        textwrap.dedent(
            """\
            import json
            import subprocess
            ready = json.loads(subprocess.run(
                ["bd", "ready", "--json"], check=True, capture_output=True, text=True
            ).stdout)
            first = next((i["id"] for i in ready if i.get("issue_type") != "epic"), None)
            if first:
                subprocess.run(
                    ["bd", "update", first, "--status", "in_progress"],
                    check=True, stdout=subprocess.DEVNULL,
                )
                print(f"claude (claim-test) claimed {first} and bailed", flush=True)
            """
        ),
    )


def _no_op_shim(tmp_path: Path) -> Path:
    """A fake claude that does nothing — isolates the startup sweep's effect
    from any per-iteration mutation."""
    return make_inline_python_shim(
        tmp_path,
        "claude-noop",
        'print("fake-claude (sweep-test) did nothing", flush=True)\n',
    )


def _install_shim(monkeypatch: pytest.MonkeyPatch, shim: Path) -> None:
    monkeypatch.setattr(
        grind_mod, "_make_runner", lambda: ClaudeRunner(claude_binary=str(shim))
    )


def _force_fake_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))


def _bd_show(repo: Path, issue_id: str) -> dict:
    """Read one issue's full JSON via `bd show <id> --json`."""
    proc = subprocess.run(
        ["bd", "show", issue_id, "--json"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )
    data = json.loads(proc.stdout)
    if isinstance(data, list):
        return data[0]
    return data


def _bd_labels(repo: Path, issue_id: str) -> list[str]:
    return _bd_show(repo, issue_id).get("labels") or []


def _grind_log(repo: Path) -> str:
    return sorted((repo / "logs").glob("grind-*.log"))[-1].read_text(encoding="utf-8")


def _pre_claim(repo: Path, issue_id: str) -> None:
    """Simulate a prior grind window that claimed the issue and exited."""
    subprocess.run(
        ["bd", "update", issue_id, "--status", "in_progress"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )


# --- per-iteration: a claim left by the worker ends the window --------------


def test_worker_claim_left_in_progress_ends_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default path: the worker claims and exits without closing. Grind judges
    bd status, leaves the claim for the next window, and ends the run."""
    repo, issue_id = _seed_repo(tmp_path)
    _stub_sandbox(monkeypatch)
    _force_fake_home(monkeypatch, tmp_path)
    _install_shim(monkeypatch, _claim_only_shim(tmp_path))

    result = runner.invoke(
        app,
        ["grind", str(repo), "--iterations", "3", "--idle-sleep", "0"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    issue = _bd_show(repo, issue_id)
    assert issue["status"] == "in_progress", (
        f"a live claim must stay claimed for the next window; got {issue['status']}"
    )
    log = _grind_log(repo)
    assert f"left {issue_id} in_progress for the next window" in log
    # One context window per leftover claim: the run ends rather than
    # respawning against the same claim, even with iteration budget left.
    assert "iter 2" not in log


def test_legacy_condition_path_leaves_claim_for_next_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The legacy --condition path judges by snapshot delta rather than a
    targeted `bd show`, and reaches the same verdict: claim stays, run ends."""
    repo, issue_id = _seed_repo(tmp_path)
    _stub_sandbox(monkeypatch)
    _force_fake_home(monkeypatch, tmp_path)
    _install_shim(monkeypatch, _claim_only_shim(tmp_path))

    result = runner.invoke(
        app,
        [
            "grind",
            str(repo),
            "--iterations",
            "1",
            "--idle-sleep",
            "0",
            "-c",
            "Close exactly one bd issue you select yourself.",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    assert _bd_show(repo, issue_id)["status"] == "in_progress"
    log = _grind_log(repo)
    assert f"left {issue_id} in_progress for the next window" in log


# --- startup: a leftover claim from a prior window --------------------------


def test_startup_leftover_claim_is_not_reverted_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cross-restart scenario: a prior window claimed and exited. The new
    grind names the leftover claim at startup and must NOT revert it — a live
    unfinished claim is not an orphan."""
    repo, issue_id = _seed_repo(tmp_path)
    _stub_sandbox(monkeypatch)
    _force_fake_home(monkeypatch, tmp_path)
    _pre_claim(repo, issue_id)
    _install_shim(monkeypatch, _no_op_shim(tmp_path))

    result = runner.invoke(
        app,
        ["grind", str(repo), "--iterations", "1", "--idle-sleep", "0"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    log = _grind_log(repo)
    assert "startup leftover claim(s)" in log, (
        f"startup should name the leftover claim; got log:\n{log}"
    )
    assert issue_id in log
    assert f"revert: {issue_id}" not in log, (
        "revert must never fire on a live claim"
    )
    assert _bd_show(repo, issue_id)["status"] == "in_progress"


def test_startup_revert_policy_is_coerced_to_warn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--orphan-policy=revert on a leftover claim degrades to warn: the claim
    is logged, not mutated. Reverting would erase the routing the worker
    recorded in bd."""
    repo, issue_id = _seed_repo(tmp_path)
    _stub_sandbox(monkeypatch)
    _force_fake_home(monkeypatch, tmp_path)
    _pre_claim(repo, issue_id)
    _install_shim(monkeypatch, _no_op_shim(tmp_path))

    result = runner.invoke(
        app,
        [
            "grind",
            str(repo),
            "--iterations",
            "1",
            "--idle-sleep",
            "0",
            "--orphan-policy",
            "revert",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    log = _grind_log(repo)
    assert f"warn: orphan claim on {issue_id}" in log
    assert f"revert: {issue_id}" not in log
    assert _bd_show(repo, issue_id)["status"] == "in_progress", (
        "revert policy must not mutate a live claim"
    )


def test_startup_escalate_labels_leftover_human(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Escalate stays honored at startup: the operator explicitly asked for
    leftover claims to be handed to the human queue."""
    repo, issue_id = _seed_repo(tmp_path)
    _stub_sandbox(monkeypatch)
    _force_fake_home(monkeypatch, tmp_path)
    _pre_claim(repo, issue_id)
    _install_shim(monkeypatch, _no_op_shim(tmp_path))

    result = runner.invoke(
        app,
        [
            "grind",
            str(repo),
            "--iterations",
            "1",
            "--idle-sleep",
            "0",
            "--orphan-policy",
            "escalate",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    labels = _bd_labels(repo, issue_id)
    assert "human" in labels, (
        f"escalate policy should add 'human' label; got labels={labels}"
    )
    log = _grind_log(repo)
    assert "startup leftover claim(s)" in log
    assert f"escalate: {issue_id}" in log
