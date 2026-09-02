"""Harness-side reaping of a worker held by the /goal Stop hook (ortus-wg2w).

A Claude worker that takes the goal prompt's PLAN-GAP exit comments, labels
its issue human, and tries to stop. The /goal condition grind composes reads
"closed and in sync", so the Stop hook re-prompts the worker on every stop,
and nothing on the harness side used to notice: only grok had a reap_when
callback, and it watched the done bar alone. The worker re-answered the hook
until --worker-timeout killed it, and the run recorded a timeout for what was
a correct exit.

The pure reason helper is driven with fakes for each branch: a claim newly
flagged human reaps, a claim already flagged when the window began does not,
a tracker error never reaps, and the done bar still reaps. One integration
test then runs grind with a fake claude that claims its issue, flags it human,
and hangs, and reads the reap out of the grind log: the flagged-claim line is
present and the TIMEOUT line is not.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.commands import grind as grind_mod
from ortus.core import sandbox as sandbox_mod
from ortus.core.bd import BdError
from ortus.core.claude import ClaudeRunner
from ortus.core.sandbox import SandboxInfo
from tests._shims import make_inline_python_shim
from tests.conftest import copy_bd_workspace

runner = CliRunner()


# --- the reason helper, driven with fakes ----------------------------------


class _FakeBd:
    """Tracker double: in_progress ids with their labels, and a closed count.

    ``fail`` selects the failure the real client would surface. ``list`` and
    ``exclude`` answer with an empty set, which is how ``BdClient`` swallows a
    failed query; ``show`` raises, which is how the confirmation read fails.
    """

    def __init__(
        self,
        claims: dict[str, list[str]],
        *,
        closed: int = 0,
        fail: str = "",
    ) -> None:
        self.claims = claims
        self.closed = closed
        self.fail = fail

    def count_by_status(
        self, status: str, *, exclude_labels: tuple[str, ...] = ()
    ) -> int:
        del status, exclude_labels
        return self.closed

    def in_progress_ids(self, *, exclude_labels: tuple[str, ...] = ()) -> set[str]:
        if self.fail == "list":
            return set()
        if exclude_labels and self.fail == "exclude":
            return set()
        return {
            issue_id
            for issue_id, labels in self.claims.items()
            if not any(label in exclude_labels for label in labels)
        }

    def show(self, issue_id: str) -> dict[str, object]:
        if self.fail == "show":
            raise BdError(["bd", "show", issue_id], 1, "tracker down")
        return {"id": issue_id, "labels": list(self.claims[issue_id])}


class _RaisingBd(_FakeBd):
    """A tracker whose listing itself blows up, not just answers empty."""

    def in_progress_ids(self, *, exclude_labels: tuple[str, ...] = ()) -> set[str]:
        raise RuntimeError("tracker down")


class _InSyncGit:
    def __init__(self, *, ahead: int = 0) -> None:
        self.ahead = ahead

    def remote_tip(self, branch: str) -> str:
        del branch
        return "abc"

    def local_ahead_of_remote(self, branch: str) -> int:
        del branch
        return self.ahead

    def dirty_paths(self) -> frozenset[str] | None:
        return frozenset()


def _reason(
    bd: object,
    *,
    flagged_at_start: frozenset[str] | None = frozenset(),
    baseline_closed: int | None = 5,
) -> str | None:
    return grind_mod._reap_reason(
        bd,  # type: ignore[arg-type]
        _InSyncGit(),  # type: ignore[arg-type]
        baseline_closed=baseline_closed,
        flagged_at_start=flagged_at_start,
        integration_branch="main",
    )


def test_reap_reason_fires_when_claim_gains_human_label() -> None:
    """The claim was unflagged when the window began and is flagged now."""
    bd = _FakeBd({"ortus-x": ["human"]}, closed=5)
    assert _reason(bd) == "claim flagged human (ortus-x)"


def test_reap_reason_ignores_claim_flagged_before_window() -> None:
    """A leftover flagged claim is not this worker's; it never trips the reap."""
    bd = _FakeBd({"ortus-x": ["human"]}, closed=5)
    assert _reason(bd, flagged_at_start=frozenset({"ortus-x"})) is None


def test_reap_reason_ignores_unflagged_claim() -> None:
    bd = _FakeBd({"ortus-x": ["cli"]}, closed=5)
    assert _reason(bd) is None


@pytest.mark.parametrize("fail", ["list", "show"])
def test_reap_reason_is_none_on_tracker_error(fail: str) -> None:
    """A poll that bd did not answer must never kill a live worker."""
    bd = _FakeBd({"ortus-x": ["human"]}, closed=5, fail=fail)
    assert _reason(bd) is None
    assert _reason(_RaisingBd({"ortus-x": ["human"]}, closed=5)) is None


def test_reap_reason_does_not_trust_a_failed_excluding_query() -> None:
    """When only the excluding listing fails, every claim looks flagged by
    the set difference; the confirmation read by name says otherwise."""
    bd = _FakeBd({"ortus-x": ["cli"]}, closed=5, fail="exclude")
    assert _reason(bd) is None


def test_reap_reason_fires_on_done_bar() -> None:
    """A closed count that grew with HEAD in sync reaps, as it did for grok."""
    bd = _FakeBd({}, closed=6)
    assert _reason(bd) == "done bar met (closed 5->6, in sync)"


def test_reap_reason_prefers_done_bar_over_flag() -> None:
    bd = _FakeBd({"ortus-x": ["human"]}, closed=6)
    assert _reason(bd) == "done bar met (closed 5->6, in sync)"


def test_reap_reason_skips_checks_without_baselines() -> None:
    """A baseline that could not be read disables its check rather than
    guessing; with neither baseline the poll is a no-op."""
    bd = _FakeBd({"ortus-x": ["human"]}, closed=6)
    assert _reason(bd, flagged_at_start=None, baseline_closed=None) is None
    assert _reason(bd, flagged_at_start=None) == "done bar met (closed 5->6, in sync)"
    assert _reason(bd, baseline_closed=None) == "claim flagged human (ortus-x)"


def test_flagged_claims_is_empty_when_nothing_is_in_progress() -> None:
    assert grind_mod._flagged_claims(_FakeBd({})) == set()  # type: ignore[arg-type]


# --- grind wires the reap for claude ---------------------------------------


def _stub_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sandbox_mod, "smoke_test", lambda: SandboxInfo(platform="Linux", binary="bwrap")
    )


def _seed_repo(tmp_path: Path) -> tuple[Path, str]:
    """Returns (repo, issue_id): a committed tree with one ready leaf."""
    workspace = copy_bd_workspace(tmp_path / "reap", "leaf")
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


def _force_fake_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))


def _grind_log(repo: Path) -> str:
    return sorted((repo / "logs").glob("grind-*.log"))[-1].read_text(encoding="utf-8")


def _bd_show(repo: Path, issue_id: str) -> dict:
    import json

    proc = subprocess.run(
        ["bd", "show", issue_id, "--json"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )
    data = json.loads(proc.stdout)
    return data[0] if isinstance(data, list) else data


class _RecordingRunner:
    extra_env: dict[str, str] = {}

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def run(self, prompt: str, **kwargs: object) -> int:
        self.calls.append({"prompt": prompt, **kwargs})
        return 0


@pytest.mark.integration
def test_claude_implement_spawn_gets_reap_when(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claude spawn carries a reap callback, as only the grok spawn did."""
    repo, _issue_id = _seed_repo(tmp_path)
    recorded = _RecordingRunner()
    _stub_sandbox(monkeypatch)
    _force_fake_home(monkeypatch, tmp_path)
    monkeypatch.setattr(grind_mod, "_make_runner", lambda *a, **k: recorded)

    result = runner.invoke(
        app,
        ["grind", str(repo), "--iterations", "1", "--idle-sleep", "0"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert recorded.calls
    reap_when = recorded.calls[0].get("reap_when")
    assert callable(reap_when)
    # Nothing changed under the recorded (no-op) worker, so the poll stays quiet.
    assert reap_when() is False


# A worker that claims the first ready issue, takes the PLAN-GAP exit
# (labels it human), and then behaves like a worker held by the Stop hook:
# it never exits on its own.
_FLAG_THEN_HANG = textwrap.dedent(
    """\
    import json, subprocess, time
    ready = json.loads(subprocess.run(
        ["bd", "ready", "--json"], check=True, capture_output=True, text=True
    ).stdout)
    first = next((i["id"] for i in ready if i.get("issue_type") != "epic"), None)
    if first:
        subprocess.run(
            ["bd", "update", first, "--status", "in_progress"],
            check=True, stdout=subprocess.DEVNULL,
        )
        subprocess.run(
            ["bd", "label", "add", first, "human"],
            check=True, stdout=subprocess.DEVNULL,
        )
        print(f"flagged {first} human, now held by the hook", flush=True)
    time.sleep(120)
    """
)


@pytest.mark.integration
@pytest.mark.slow
def test_flagged_claim_is_reaped_within_a_poll_not_timed_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-1: a claude worker whose claim gains the human label is reaped by
    the harness and logged as a flagged-claim reap; the watchdog never fires."""
    repo, issue_id = _seed_repo(tmp_path)
    _stub_sandbox(monkeypatch)
    _force_fake_home(monkeypatch, tmp_path)
    shim = make_inline_python_shim(tmp_path, "claude-flag-hang", _FLAG_THEN_HANG)
    monkeypatch.setattr(
        grind_mod, "_make_runner", lambda *a, **k: ClaudeRunner(claude_binary=str(shim))
    )

    # The watchdog is left far above the poll cadence so the only way this
    # worker dies inside the test's budget is the flagged-claim reap.
    result = runner.invoke(
        app,
        [
            "grind",
            str(repo),
            "--iterations",
            "1",
            "--idle-sleep",
            "0",
            "--worker-timeout",
            "90",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    log = _grind_log(repo)
    assert f"claim flagged human ({issue_id}); reaping worker" in log, log
    assert "TIMEOUT" not in log, log
    shown = _bd_show(repo, issue_id)
    assert shown["status"] == "in_progress"
    assert "human" in (shown.get("labels") or [])
