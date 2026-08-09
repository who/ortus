"""Ortus-owned finalization and restart recovery (ortus-pzfd.5).

A passing verdict is the only thing that authorizes finalization, and the
agent never performs it. Ortus writes the final record, closes exactly the
assigned issue, commits only the transaction-owned paths plus the generated
tracker exports, and synchronizes the integration branch — in that order, with
each boundary journaled after it lands.

The journal is what makes a killed run recoverable: a restart replays only the
boundaries that never completed. These tests drive each boundary directly,
because the interesting failures (killed between close and commit, killed
between commit and push) are exactly the ones a happy-path test can't reach.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.commands import grind as grind_mod
from ortus.core import sandbox as sandbox_mod
from ortus.core.bd import BdClient, BdError
from ortus.core.git import GitClient
from ortus.core.profiles import Phase
from ortus.core.sandbox import SandboxInfo
from ortus.core.transaction import (
    CandidateJournal,
    JournalStore,
    candidate_diff,
)
from tests._shims import ready_issue_args
from tests.conftest import copy_bd_workspace

pytestmark = [pytest.mark.integration, pytest.mark.slow]
runner = CliRunner()

CANDIDATE = "candidate.py"
FINALIZATION_MARKER = grind_mod._FINALIZATION_MARKER


def _fake_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sandbox_mod, "smoke_test", lambda: SandboxInfo(platform="Linux", binary="bwrap")
    )


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )


def _seed(tmp_path: Path, name: str, *, remote: bool = False) -> tuple[Path, str]:
    """A bd workspace with a committed git baseline and one ready leaf.

    The workspace is a ~25ms copy of the session's `leaf` template rather than
    a `bd init` plus a `bd create` at roughly a second each (ortus-apmf); the
    baseline commit stays per-test, since each test mutates its own repo.
    """
    workspace = copy_bd_workspace(tmp_path / name, "leaf")
    repo, issue_id = workspace.path, workspace.issues[0]
    (repo / ".gitignore").write_text(
        "logs/\n.cache/\n.beads/*\n!.beads/issues.jsonl\n!.beads/interactions.jsonl\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "fixture baseline")
    if remote:
        bare = tmp_path / f"{name}-origin.git"
        subprocess.run(
            ["git", "init", "--bare", str(bare)], check=True, capture_output=True
        )
        _git(repo, "remote", "add", "origin", str(bare))
        _git(repo, "push", "-u", "origin", "main")
    return repo, issue_id


def _issue(repo: Path, issue_id: str) -> dict:
    shown = subprocess.run(
        ["bd", "show", issue_id, "--json"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    data = json.loads(shown)
    return data[0] if isinstance(data, list) else data


def _comment_bodies(repo: Path, issue_id: str) -> list[str]:
    raw = subprocess.run(
        ["bd", "comments", issue_id, "--json"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    entries = json.loads(raw or "[]")
    bodies = []
    for entry in entries:
        if isinstance(entry, dict):
            for key in ("body", "text", "comment", "content"):
                if entry.get(key):
                    bodies.append(str(entry[key]))
                    break
    return bodies


def _committed_paths(repo: Path) -> set[str]:
    out = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return {line.strip() for line in out.splitlines() if line.strip()}


def _subjects(repo: Path) -> list[str]:
    return subprocess.run(
        ["git", "log", "--format=%s"], cwd=repo, capture_output=True, text=True
    ).stdout.splitlines()


def _finalization_commits(repo: Path, issue_id: str) -> list[str]:
    """Subjects of the commits this transaction made.

    Matched on the leading issue id rather than a fixed phrase: the subject now
    carries the issue title, which each test is free to change.
    """
    return [line for line in _subjects(repo) if line.startswith(f"{issue_id}: ")]


def _head_message(repo: Path) -> str:
    return subprocess.run(
        ["git", "log", "-1", "--format=%B"], cwd=repo, capture_output=True, text=True
    ).stdout


class PassingRunner:
    """Implementation writes one candidate file; the verifier passes it."""

    extra_env: dict[str, str] = {}

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.phases: list[str] = []

    def run(
        self,
        prompt: str,
        *,
        repo: Path,
        log_path: Path,
        profile: object,
        **kwargs: object,
    ) -> int:
        phase = profile.phase  # type: ignore[union-attr]
        self.phases.append(phase.value)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.touch(exist_ok=True)
        if phase is Phase.IMPLEMENT:
            (repo / CANDIDATE).write_text("SHIPPED = True\n")
        elif phase is Phase.VERIFY:
            journal = JournalStore(repo).load()
            assert journal is not None
            payload = {
                "schema": 1,
                "candidate_hash": journal.candidate_hash,
                "decision": "pass",
                "criteria": [
                    {"id": "AC-1", "status": "pass", "evidence": "verified"}
                ],
                "commands": ["uv run pytest tests/test_grind_finalization.py -q"],
                "reviewed_files": [CANDIDATE],
                "reviewed_interfaces": ["SHIPPED"],
                "risks": ["none"],
                "findings": ["none"],
                "codegraph": ["fallback recorded"],
            }
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {
                                "type": "agent_message",
                                "text": "ORTUS_VERDICT: " + json.dumps(payload),
                            },
                        }
                    )
                    + "\n"
                )
        return 0


class NeverRuns:
    """Any spawn during a pure finalization replay is a contract violation."""

    extra_env: dict[str, str] = {}

    def run(self, *args: object, **kwargs: object) -> int:
        raise AssertionError("finalization replay must not spawn an agent")


def _install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, backend_runner: object
) -> None:
    _fake_sandbox(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    monkeypatch.setattr(grind_mod, "_make_runner", lambda *a, **k: backend_runner)


def _stage_pending_journal(
    repo: Path,
    issue_id: str,
    *,
    landed: tuple[str, ...] = (),
    close_issue: bool = False,
    add_report_comment: bool = False,
) -> CandidateJournal:
    """Reproduce a run killed part-way through finalization.

    `landed` names the boundaries whose journal entries were written before the
    kill; `close_issue` / `add_report_comment` reproduce the *observable* side
    effects independently, so tests can also cover the nastier case where the
    step landed but its journal entry never did.
    """
    subprocess.run(
        ["bd", "update", issue_id, "--status", "in_progress"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    (repo / CANDIDATE).write_text("SHIPPED = True\n")
    # bd rewrites its generated exports as work lands. They are this
    # transaction's own output, so finalization must carry them even though
    # they are never part of the reviewed candidate.
    with (repo / ".beads" / "issues.jsonl").open("a", encoding="utf-8") as fh:
        fh.write("\n")
    store = JournalStore(repo)
    paths = frozenset({CANDIDATE})
    digest, diff_ref = store.save_diff(candidate_diff(repo, paths))
    if add_report_comment:
        subprocess.run(
            ["bd", "comments", "add", issue_id, f"{FINALIZATION_MARKER}\n\nreplayed"],
            cwd=repo,
            check=True,
            capture_output=True,
        )
    if close_issue:
        subprocess.run(
            ["bd", "close", issue_id, "--reason", "already closed"],
            cwd=repo,
            check=True,
            capture_output=True,
        )
    packet = _issue(repo, issue_id)
    packet_digest, packet_ref = store.save_packet(issue_id, packet)
    head = GitClient(repo=repo).head_oid()
    journal = CandidateJournal.start(
        repo=repo,
        issue_id=issue_id,
        base_head=head,
        baseline_paths=(),
        packet_hash=packet_digest,
        packet_ref=packet_ref,
        profiles={"implementation": "fixture", "verification": "fixture"},
    ).with_candidate(
        paths, phase="verified-pass", candidate_hash=digest, diff_ref=diff_ref
    )
    journal = journal.finish_verification(
        store.save_report(digest, "fixture verifier report"), phase="verified-pass"
    )
    for step in landed:
        journal = journal.with_finalization(step)
    store.save(journal)
    return journal


# ---------------------------------------------------------------------------
# AC-4 — a current-hash pass lets Ortus alone finalize
# ---------------------------------------------------------------------------


def test_pass_finalizes_report_close_commit_and_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-4: the whole lifecycle, performed by Ortus, in one iteration."""
    repo, issue_id = _seed(tmp_path, "fin1", remote=True)
    # `bd init` lands its own commit, so the seeded baseline is not a fixed
    # number of commits. Assert on the delta this transaction adds.
    baseline_commits = len(_subjects(repo))
    backend = PassingRunner(repo)
    _install(monkeypatch, tmp_path, backend)

    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    assert backend.phases == [Phase.IMPLEMENT.value, Phase.VERIFY.value]
    assert _issue(repo, issue_id)["status"] == "closed"

    bodies = _comment_bodies(repo, issue_id)
    assert sum(FINALIZATION_MARKER in body for body in bodies) == 1
    assert any("Ortus verifier report" in body for body in bodies)

    committed = _committed_paths(repo)
    assert CANDIDATE in committed
    # Path-scoped: only the transaction's own paths, never a `git add -A` sweep.
    assert committed <= {CANDIDATE, ".beads/issues.jsonl", ".beads/interactions.jsonl"}
    assert _subjects(repo)[0] == f"{issue_id}: {_issue(repo, issue_id)['title']}"

    # Exactly one close and one commit, and the integration branch is on origin.
    assert len(_subjects(repo)) == baseline_commits + 1
    assert len(_finalization_commits(repo, issue_id)) == 1
    ahead = subprocess.run(
        ["git", "rev-list", "--count", "origin/main..main"],
        cwd=repo,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert ahead == "0", "finalization must leave main synchronized with origin"
    assert JournalStore(repo).load() is None
    assert "finalization: main synchronized with origin" in "\n".join(
        p.read_text(encoding="utf-8") for p in (repo / "logs").glob("grind-*.log")
    )


def test_pass_without_a_remote_finalizes_locally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-4: no-remote repos still close and commit; the sync step no-ops."""
    repo, issue_id = _seed(tmp_path, "fin2")
    _install(monkeypatch, tmp_path, PassingRunner(repo))

    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert _issue(repo, issue_id)["status"] == "closed"
    assert CANDIDATE in _committed_paths(repo)
    assert JournalStore(repo).load() is None


# ---------------------------------------------------------------------------
# AC-5 — a failed boundary retains a recoverable journal
# ---------------------------------------------------------------------------


def test_failure_at_commit_retains_a_recoverable_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-5: a commit that fails leaves report+close journaled and stops."""
    repo, issue_id = _seed(tmp_path, "fin3")
    _install(monkeypatch, tmp_path, PassingRunner(repo))
    monkeypatch.setattr(
        GitClient, "commit_paths", lambda self, paths, message: False
    )

    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    combined = result.stdout + result.stderr
    assert "finalization blocked — path-scoped commit" in combined
    journal = JournalStore(repo).load()
    assert journal is not None
    assert journal.finalized("report") and journal.finalized("close")
    assert not journal.finalized("commit")
    assert journal.phase == "finalized-close"
    assert _issue(repo, issue_id)["status"] == "closed"


def test_failure_at_push_retains_a_recoverable_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-5: a rejected push retries once through pull --rebase, then stops
    with the commit journaled so a restart resumes at the push."""
    repo, issue_id = _seed(tmp_path, "fin4", remote=True)
    _install(monkeypatch, tmp_path, PassingRunner(repo))
    attempts: list[str] = []

    def _refuse_push(self: GitClient, branch: str) -> bool:
        attempts.append(f"push:{branch}")
        return False

    def _refuse_pull(self: GitClient, branch: str) -> bool:
        attempts.append(f"pull:{branch}")
        return False

    monkeypatch.setattr(GitClient, "push", _refuse_push)
    monkeypatch.setattr(GitClient, "pull_rebase", _refuse_pull)

    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    assert attempts == ["push:main", "pull:main"]
    assert "are NOT on origin" in (result.stdout + result.stderr)
    journal = JournalStore(repo).load()
    assert journal is not None
    assert journal.finalized("commit") and not journal.finalized("sync")
    assert _issue(repo, issue_id)["status"] == "closed"
    assert CANDIDATE in _committed_paths(repo)


def test_failure_when_unrelated_edits_coexist_names_them_and_commits_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-5: uncommitted work outside the transaction halts finalization with a
    precise path list instead of being swept into a `git add -A` commit."""
    repo, issue_id = _seed(tmp_path, "fin5")
    _stage_pending_journal(repo, issue_id)
    (repo / "operator-notes.txt").write_text("unrelated local work\n")
    baseline_subjects = _subjects(repo)
    _install(monkeypatch, tmp_path, NeverRuns())

    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 1, result.stdout + result.stderr

    # Read the log rather than stderr: output.error hard-wraps its hint.
    log = "\n".join(
        p.read_text(encoding="utf-8") for p in (repo / "logs").glob("grind-*.log")
    )
    assert "candidate path set changed after the passing verdict" in log
    assert "operator-notes.txt" in log
    assert _issue(repo, issue_id)["status"] == "in_progress"
    assert _subjects(repo) == baseline_subjects, "nothing may be committed"
    assert (repo / "operator-notes.txt").read_text() == "unrelated local work\n"
    journal = JournalStore(repo).load()
    assert journal is not None and journal.phase == "finalization-blocked"


# ---------------------------------------------------------------------------
# AC-6 — a restart at any partial boundary is idempotent
# ---------------------------------------------------------------------------


def test_restart_after_verdict_before_report_finalizes_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-6: the whole finalization replays with no agent spawn at all."""
    repo, issue_id = _seed(tmp_path, "fin6", remote=True)
    _stage_pending_journal(repo, issue_id)
    _install(monkeypatch, tmp_path, NeverRuns())

    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    assert _issue(repo, issue_id)["status"] == "closed"
    assert sum(
        FINALIZATION_MARKER in body for body in _comment_bodies(repo, issue_id)
    ) == 1
    committed = _committed_paths(repo)
    assert CANDIDATE in committed
    assert ".beads/issues.jsonl" in committed, "generated tracker exports ride along"
    assert JournalStore(repo).load() is None


def test_restart_after_report_before_close_adds_no_duplicate_comment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-6: the report boundary already landed, so the replay must not
    re-comment — including when only the observable comment survived."""
    repo, issue_id = _seed(tmp_path, "fin7")
    _stage_pending_journal(repo, issue_id, add_report_comment=True)
    _install(monkeypatch, tmp_path, NeverRuns())

    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    assert sum(
        FINALIZATION_MARKER in body for body in _comment_bodies(repo, issue_id)
    ) == 1, "a replay must not duplicate the finalization record"
    assert _issue(repo, issue_id)["status"] == "closed"
    assert CANDIDATE in _committed_paths(repo)


def test_restart_after_close_before_commit_commits_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-6: the close already landed; the replay resumes at the commit and
    issues no second `bd close`."""
    repo, issue_id = _seed(tmp_path, "fin8", remote=True)
    _stage_pending_journal(
        repo, issue_id, landed=("report", "close"), close_issue=True
    )
    _install(monkeypatch, tmp_path, NeverRuns())

    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    issue = _issue(repo, issue_id)
    assert issue["status"] == "closed"
    assert issue["close_reason"] == "already closed", (
        "the original close must not be overwritten by a replayed one"
    )
    assert len(_finalization_commits(repo, issue_id)) == 1
    assert CANDIDATE in _committed_paths(repo)
    assert JournalStore(repo).load() is None


def test_restart_after_commit_before_push_only_pushes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-6: a run killed after the commit resumes at the push alone."""
    repo, issue_id = _seed(tmp_path, "fin9", remote=True)
    _stage_pending_journal(
        repo, issue_id, landed=("report", "close"), close_issue=True
    )
    _git(repo, "add", CANDIDATE)
    _git(repo, "commit", "-m", f"{issue_id}: verified candidate")
    store = JournalStore(repo)
    journal = store.load()
    assert journal is not None
    store.save(journal.with_finalization("commit", GitClient(repo=repo).head_oid()))
    _install(monkeypatch, tmp_path, NeverRuns())

    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    assert len(_finalization_commits(repo, issue_id)) == 1
    ahead = subprocess.run(
        ["git", "rev-list", "--count", "origin/main..main"],
        cwd=repo,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert ahead == "0"
    assert JournalStore(repo).load() is None


def test_restart_is_idempotent_when_run_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-6: replaying an already-finalized transaction is a no-op."""
    repo, issue_id = _seed(tmp_path, "fin10", remote=True)
    _stage_pending_journal(repo, issue_id)
    _install(monkeypatch, tmp_path, NeverRuns())

    first = runner.invoke(app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"])
    assert first.exit_code == 0, first.stdout + first.stderr
    subjects_after_first = _subjects(repo)

    second = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert second.exit_code == 0, second.stdout + second.stderr

    assert _subjects(repo) == subjects_after_first
    assert sum(
        FINALIZATION_MARKER in body for body in _comment_bodies(repo, issue_id)
    ) == 1
    assert _issue(repo, issue_id)["status"] == "closed"


def test_restart_after_a_resolved_blocker_finalizes_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-5/AC-6: a blocker is transient, so clearing it must let the *same*
    transaction finish.

    The nasty shape: killed after the close, an unrelated operator edit blocks
    the commit. The operator does what the error hint says — removes the edit —
    and re-runs. If the blocked journal stopped being routable, that second run
    would skip the pending commit and leave verified work stranded behind an
    already-closed issue.
    """
    repo, issue_id = _seed(tmp_path, "fin11", remote=True)
    _stage_pending_journal(
        repo,
        issue_id,
        landed=("report", "close"),
        close_issue=True,
        add_report_comment=True,
    )
    (repo / "operator-notes.txt").write_text("unrelated local work\n")
    _install(monkeypatch, tmp_path, NeverRuns())

    blocked = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert blocked.exit_code == 1, blocked.stdout + blocked.stderr
    assert not _finalization_commits(repo, issue_id)
    journal = JournalStore(repo).load()
    assert journal is not None and journal.phase == "finalization-blocked"
    assert journal.finalized("close") and not journal.finalized("commit")

    (repo / "operator-notes.txt").unlink()

    resumed = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert resumed.exit_code == 0, resumed.stdout + resumed.stderr

    assert len(_finalization_commits(repo, issue_id)) == 1
    assert CANDIDATE in _committed_paths(repo)
    assert _issue(repo, issue_id)["status"] == "closed"
    assert sum(
        FINALIZATION_MARKER in body for body in _comment_bodies(repo, issue_id)
    ) == 1, "the replay must not duplicate the finalization record"
    assert JournalStore(repo).load() is None


def test_blocked_finalization_never_selects_another_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-5: an outstanding finalization holds the queue.

    `NeverRuns` turns any worker spawn into a failure, so this asserts grind
    neither finalizes nor moves on to the unrelated ready issue while a blocked
    transaction still owes a commit.
    """
    repo, issue_id = _seed(tmp_path, "fin12", remote=True)
    _stage_pending_journal(
        repo, issue_id, landed=("report", "close"), close_issue=True
    )
    (repo / "operator-notes.txt").write_text("unrelated local work\n")
    subprocess.run(
        [
            "bd",
            "create",
            "--silent",
            "--title",
            "unrelated ready issue",
            "--type",
            "task",
            "--priority",
            "1",
            *ready_issue_args(),
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    _install(monkeypatch, tmp_path, NeverRuns())

    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 1, result.stdout + result.stderr

    # Re-running while the blocker stands must keep holding, not drift onto the
    # other issue just because the first attempt already reported the problem.
    again = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert again.exit_code == 1, again.stdout + again.stderr

    assert not _finalization_commits(repo, issue_id)
    assert (repo / "operator-notes.txt").read_text() == "unrelated local work\n"
    journal = JournalStore(repo).load()
    assert journal is not None and journal.issue_id == issue_id


# ---------------------------------------------------------------------------
# ortus-irbj — the commit message says what changed and where it came from
# ---------------------------------------------------------------------------


def _finalize_by_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    *,
    title: str | None = None,
    comments: tuple[str, ...] = (),
    corrections: int = 0,
) -> tuple[Path, str, CandidateJournal]:
    """Drive one finalization from a staged journal, with no agent spawn.

    The message is built at the commit boundary, so the replay path exercises
    it exactly as a fresh pass does while keeping the test off the worker.
    `comments` are posted in order before finalization runs, standing in for
    what a worker recorded; `corrections` reproduces a run that went back and
    edited the code after a rejection.
    """
    repo, issue_id = _seed(tmp_path, name)
    if title is not None:
        subprocess.run(
            ["bd", "update", issue_id, "--title", title],
            cwd=repo,
            check=True,
            capture_output=True,
        )
    for comment in comments:
        subprocess.run(
            ["bd", "comments", "add", issue_id, comment],
            cwd=repo,
            check=True,
            capture_output=True,
        )
    journal = _stage_pending_journal(repo, issue_id)
    if corrections:
        journal = replace(journal, corrections=corrections)
        JournalStore(repo).save(journal)
    _install(monkeypatch, tmp_path, NeverRuns())
    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    return repo, issue_id, journal


def test_commit_subject_names_the_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-1: the subject is the id plus the authored title, not a fixed phrase."""
    repo, issue_id, _ = _finalize_by_replay(tmp_path, monkeypatch, "fin13")

    subject = _subjects(repo)[0]
    assert subject == f"{issue_id}: {_issue(repo, issue_id)['title']}"
    assert "verified candidate" not in subject


CHANGES_COMMENT = (
    "**Changes**:\n"
    f"- {CANDIDATE} - added the SHIPPED flag the loader reads at import time\n"
    "- docs/testing.md - documented the flag\n"
    "\n"
    "**Verification**: 4 passed\n"
    "\n"
    "**CodeGraph v1**:\n"
    f"modified: SHIPPED@{CANDIDATE}:1 (2 callers, 0 cross-module)\n"
    "new: none\n"
    "oos_callers: none"
)

#: Every word the finalization body must never carry: the mechanics of how the
#: change was produced belong in the bd record, not in `git log`.
PROVENANCE_WORDS = ("Attempt:", "Corrections:", "Verifier report:")


def test_commit_body_describes_the_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-2/AC-3: the body is the authored change description, with no mechanics."""
    repo, _, _ = _finalize_by_replay(
        tmp_path, monkeypatch, "fin14", comments=(CHANGES_COMMENT,)
    )

    subject, _, body = _head_message(repo).partition("\n\n")
    assert subject and "\n" not in subject
    assert "Exercise the behavior owned by this test." in body
    assert "added the SHIPPED flag the loader reads at import time" in body
    assert "docs/testing.md - documented the flag" in body
    for word in PROVENANCE_WORDS:
        assert word not in body


def test_commit_body_ignores_comments_that_are_not_change_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-5: a plan gap or a stopped-work note is never read as the description."""
    plan_gap = "PLAN-GAP: the packet contradicts reality\n\n**Changes**:\n- nothing\n"
    blocked = "BLOCKED: waiting on a decision\n\n**Changes**:\n- also nothing\n"
    repo, _, _ = _finalize_by_replay(
        tmp_path,
        monkeypatch,
        "fin14b",
        comments=(CHANGES_COMMENT, plan_gap, blocked),
    )

    body = _head_message(repo).partition("\n\n")[2]
    assert "added the SHIPPED flag the loader reads at import time" in body
    assert "nothing" not in body


def test_commit_body_falls_back_to_the_codegraph_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-4: an empty `**Changes**` block degrades to the structural record."""
    empty_bullets = (
        "**Changes**:\n"
        "   \n"
        "**Verification**: 4 passed\n"
        "\n"
        "**CodeGraph v1**:\n"
        f"modified: SHIPPED@{CANDIDATE}:1 (2 callers, 0 cross-module)\n"
        "new: Loader@loader.py:7 (class)\n"
    )
    repo, _, _ = _finalize_by_replay(
        tmp_path, monkeypatch, "fin14c", comments=(empty_bullets,)
    )

    body = _head_message(repo).partition("\n\n")[2]
    assert f"Modified: SHIPPED@{CANDIDATE}:1" in body
    assert "Added: Loader@loader.py:7 (class)" in body


def test_commit_body_falls_back_to_the_owned_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-4: with nothing authored, the journal's own paths describe the commit."""
    repo, _, _ = _finalize_by_replay(tmp_path, monkeypatch, "fin14d")

    body = _head_message(repo).partition("\n\n")[2]
    assert "Files touched:" in body
    assert f"- {CANDIDATE}" in body
    for word in PROVENANCE_WORDS:
        assert word not in body


def test_commit_body_skips_a_stale_changes_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-4: bullets written before an edit round are not committed as current."""
    repo, _, _ = _finalize_by_replay(
        tmp_path,
        monkeypatch,
        "fin14e",
        comments=(CHANGES_COMMENT,),
        corrections=1,
    )

    body = _head_message(repo).partition("\n\n")[2]
    assert "added the SHIPPED flag the loader reads at import time" not in body
    assert f"Modified: SHIPPED@{CANDIDATE}:1" in body


def test_commit_body_takes_the_refreshed_changes_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-4: one block per round makes the newest one describe what ships."""
    refreshed = (
        "**Changes**:\n"
        f"- {CANDIDATE} - narrowed the SHIPPED flag to the loader entry point\n"
        "\n"
        "**Verification**: 5 passed\n"
    )
    repo, _, _ = _finalize_by_replay(
        tmp_path,
        monkeypatch,
        "fin14f",
        comments=(CHANGES_COMMENT, refreshed),
        corrections=1,
    )

    body = _head_message(repo).partition("\n\n")[2]
    assert "narrowed the SHIPPED flag to the loader entry point" in body
    assert "added the SHIPPED flag" not in body


def test_commit_degrades_without_packet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-3: an unreadable packet costs the title, never the commit."""
    repo, issue_id = _seed(tmp_path, "fin15")
    _stage_pending_journal(repo, issue_id)
    _install(monkeypatch, tmp_path, NeverRuns())

    # Only the packet read fails. `status` keeps answering — a tracker that
    # could not report the issue's status at all would block finalization long
    # before the commit, which is a different failure than this one.
    original_show = BdClient.show
    monkeypatch.setattr(
        BdClient,
        "status",
        lambda self, iid: str(original_show(self, iid).get("status") or ""),
    )
    monkeypatch.setattr(
        BdClient,
        "show",
        lambda self, iid: (_ for _ in ()).throw(
            BdError(["bd", "show", iid], 1, "tracker unavailable")
        ),
    )

    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    subject, _, body = _head_message(repo).partition("\n\n")
    assert subject == f"{issue_id}: verified candidate"
    # The description does not come from the packet, so it survives the read
    # failure that costs the title.
    assert f"- {CANDIDATE}" in body
    assert CANDIDATE in _committed_paths(repo)
    assert JournalStore(repo).load() is None


def _journal_with_paths(*paths: str) -> CandidateJournal:
    return CandidateJournal(
        issue_id="repo-1",
        base_head="abc123",
        baseline_paths=(),
        baseline_fingerprints={},
        candidate_paths=paths,
        candidate_hash="a" * 64,
    )


def test_commit_message_survives_an_unencodable_path() -> None:
    """AC-4: a path recovered with surrogateescape must not break the commit."""
    journal = _journal_with_paths("src/caf\udce9.py", CANDIDATE)

    message = grind_mod._commit_message(
        "repo-1", {"title": "Handle odd filenames"}, grind_mod._paths_summary(journal)
    )

    assert message.startswith("repo-1: Handle odd filenames")
    assert f"- {CANDIDATE}" in message
    message.encode("utf-8")  # the git call would raise on a lone surrogate


def test_commit_message_without_a_description_is_still_a_commit() -> None:
    """AC-4: a tracker-only close has no owned paths and still gets committed."""
    journal = _journal_with_paths()

    message = grind_mod._commit_message(
        "repo-1", {"title": "Retire a stale flag"}, grind_mod._paths_summary(journal)
    )

    assert message == "repo-1: Retire a stale flag\n"


def test_commit_subject_truncates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-4: a long title is truncated, and the body stays a separate paragraph."""
    long_title = "Rewrite " + "the finalization commit message machinery " * 4
    repo, issue_id, _ = _finalize_by_replay(
        tmp_path, monkeypatch, "fin16", title=long_title
    )

    lines = _head_message(repo).splitlines()
    subject = lines[0]
    assert len(long_title) > grind_mod._COMMIT_SUBJECT_LIMIT
    assert len(subject) <= grind_mod._COMMIT_SUBJECT_LIMIT
    assert subject.startswith(f"{issue_id}: Rewrite the finalization")
    assert subject.endswith("...")
    assert lines[1] == "", "the body must be separated by a blank line"
    assert f"- {CANDIDATE}" in lines
