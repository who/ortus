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

import errno
import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.commands import grind as grind_mod
from ortus.core import compose as compose_mod
from ortus.core import sandbox as sandbox_mod
from ortus.core.bd import BdClient, BdError
from ortus.core.git import CommitResult, GitClient
from ortus.core.profiles import Phase
from ortus.core.sandbox import SandboxInfo
from ortus.core.transaction import (
    CandidateJournal,
    JournalStore,
    candidate_diff,
)
from tests._shims import (
    install_machine_checks,
    post_completion_comment,
    ready_issue_args,
)
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
            journal = JournalStore(self.repo).load()
            assert journal is not None
            post_completion_comment(self.repo, journal.issue_id, {"AC-1": "pass"})
        elif phase is Phase.VERIFY:
            journal = JournalStore(self.repo).load()
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
    """Any worker spawn during a finalization replay is a contract violation.

    The commit-message pass is the exception, and only when the replay resumes
    at a boundary before it: that message has not been written yet, so the
    replay is entitled to ask for one. It is recorded rather than performed,
    which leaves the deterministic body in place and keeps every assertion
    about the replay's *other* boundaries about them alone.
    """

    extra_env: dict[str, str] = {}

    def __init__(self) -> None:
        self.composed: list[object] = []

    def run(self, *args: object, **kwargs: object) -> int:
        profile = kwargs.get("profile")
        if getattr(profile, "phase", None) is Phase.FINALIZE:
            self.composed.append(profile)
            return 0
        raise AssertionError("finalization replay must not spawn a worker")


def _install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, backend_runner: object
) -> None:
    _fake_sandbox(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    monkeypatch.setattr(grind_mod, "_make_runner", lambda *a, **k: backend_runner)
    # Verification is the machine pipeline now; these tests are about what
    # happens AFTER a green verdict, so the AC runner is scripted green.
    install_machine_checks(monkeypatch)


def _refuse_commit(
    monkeypatch: pytest.MonkeyPatch,
    *,
    detail: str = "hook refused: [ERROR] src/odd [name].py is unformatted",
) -> None:
    """Fail the path-scoped commit the way a refusing pre-commit hook does.

    The detail carries brackets on purpose: it reaches a Rich console, which
    would read them as markup and drop the explanation.
    """
    monkeypatch.setattr(
        GitClient,
        "commit_paths",
        lambda self, paths, message: CommitResult(
            ok=False, command="commit", returncode=1, detail=detail
        ),
    )


def _log_text(repo: Path) -> str:
    return "\n".join(
        p.read_text(encoding="utf-8") for p in (repo / "logs").glob("grind-*.log")
    )


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

    # No finalize-phase model run (compose retired) and no verify-phase agent
    # spawn (the machine pipeline judged the branch): one worker in total.
    assert backend.phases == [Phase.IMPLEMENT.value]
    assert _issue(repo, issue_id)["status"] == "closed"

    bodies = _comment_bodies(repo, issue_id)
    assert sum(FINALIZATION_MARKER in body for body in bodies) == 1
    assert any("Ortus machine verification record" in body for body in bodies)

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
    """AC-5: a refused amend leaves report+close journaled and stops.

    The candidate itself is committed at the machine-verification boundary,
    so finalization's git writes in the workspace are the fold and the
    message amend — the amend is the one every landing crosses (the capture
    message is deliberately below contract), so it is the boundary refused.
    """
    repo, issue_id = _seed(tmp_path, "fin3")
    _install(monkeypatch, tmp_path, PassingRunner(repo))
    monkeypatch.setattr(GitClient, "amend_message", lambda self, message: False)

    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    combined = " ".join((result.stdout + result.stderr).split())
    assert "blocked — could not amend the worker's commit message" in combined
    journal = JournalStore(repo).load()
    assert journal is not None
    assert journal.finalized("report") and journal.finalized("close")
    assert not journal.finalized("commit")
    assert _issue(repo, issue_id)["status"] == "closed"


def test_blocker_keeps_its_first_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-1/AC-2/AC-4: a refused commit says which git command refused and what
    it said — the cause the old message threw away (ortus-pgqg). The commit the
    default pipeline performs is the capture commit at the machine boundary."""
    repo, _issue_id = _seed(tmp_path, "fin3a")
    _install(monkeypatch, tmp_path, PassingRunner(repo))
    _refuse_commit(monkeypatch)

    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    log = _log_text(repo)
    rejected = [
        line
        for line in log.splitlines()
        if "machine verification rejected" in line
    ]
    assert rejected, log
    assert "could not commit uncommitted candidate paths" in rejected[0]
    assert "git commit exited 1" in rejected[0]
    assert "src/odd [name].py is unformatted" in rejected[0]
    # The console keeps the bracketed text a Rich console would otherwise eat.
    console = " ".join((result.stdout + result.stderr).split())
    assert "[ERROR] src/odd [name].py is unformatted" in console


def test_blocker_keeps_the_recovery_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-5: the run really is resumable, so the sentence that says so stays."""
    repo, _issue_id = _seed(tmp_path, "fin3b")
    _install(monkeypatch, tmp_path, PassingRunner(repo))
    _refuse_commit(monkeypatch)

    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    # Normalized: output.error hard-wraps its hint at the console width.
    console = " ".join((result.stdout + result.stderr).split())
    assert "run `ortus grind` again; it resumes this issue at verification" in (
        console
    )


def test_successful_commit_logging_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-7: the new detail belongs to failures only; a commit that lands logs
    exactly what it logged before."""
    repo, issue_id = _seed(tmp_path, "fin3c")
    _install(monkeypatch, tmp_path, PassingRunner(repo))

    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    log = _log_text(repo)
    # The candidate itself is committed at the machine-verification boundary
    # now (the capture commit), so finalization's own commit carries only the
    # transaction's late files — the tracker exports the close rewrote.
    assert (
        f"captured 1 uncommitted candidate path(s) onto ortus/{issue_id}" in log
    )
    committed = next(
        (
            line
            for line in log.splitlines()
            if f"finalization: committed owned paths for {issue_id}:" in line
        ),
        "",
    )
    assert committed, log
    assert "git commit exited" not in log
    assert "finalization: HALT" not in log
    assert CANDIDATE in _committed_paths(repo)


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


def test_finalization_push_announces_ref_remote_and_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-1 (ortus-m1sj): the sync push names ref, remote, and commit range on
    the console before the attempt, then confirms — a push is the one act that
    changes the world outside the machine, so it must be unmistakable."""
    repo, _issue_id = _seed(tmp_path, "fin-announce", remote=True)
    old = _git(repo, "rev-parse", "origin/main").stdout.strip()
    _install(monkeypatch, tmp_path, PassingRunner(repo))

    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    new = _git(repo, "rev-parse", "main").stdout.strip()
    console = " ".join((result.stdout + result.stderr).split())
    assert f"pushing main → origin ({old[:7]}..{new[:7]}, 1 commit)" in console
    assert "pushed main → origin" in console
    # AC-5: the console joined; the log's own line did not change.
    assert "finalization: main synchronized with origin" in _log_text(repo)


def test_push_retry_announces_rebase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-2 (ortus-m1sj): a rejected first push narrates the rebase-and-retry
    on the console, and the retried push announces itself again."""
    repo, _issue_id = _seed(tmp_path, "fin-retry", remote=True)
    _install(monkeypatch, tmp_path, PassingRunner(repo))
    attempts: list[str] = []
    real_push = GitClient.push

    def _flaky_push(self: GitClient, branch: str) -> bool:
        attempts.append(f"push:{branch}")
        if len(attempts) == 1:
            return False
        return real_push(self, branch)

    def _fake_pull(self: GitClient, branch: str) -> bool:
        attempts.append(f"pull:{branch}")
        return True

    monkeypatch.setattr(GitClient, "push", _flaky_push)
    monkeypatch.setattr(GitClient, "pull_rebase", _fake_pull)

    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    assert attempts == ["push:main", "pull:main", "push:main"]
    console = " ".join((result.stdout + result.stderr).split())
    assert "push rejected; rebasing on origin and retrying" in console
    assert console.count("pushing main → origin (") == 2
    assert "pushed main → origin" in console
    # AC-5: the pre-existing log narrative is byte-for-byte unchanged.
    assert (
        "finalization: push rejected; pulling --rebase and retrying once"
        in _log_text(repo)
    )


def test_first_push_says_all_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-4 (ortus-m1sj): with no origin/<branch> tracking ref to anchor a
    range, the announcement says "all history" instead of inventing one."""
    repo, _issue_id = _seed(tmp_path, "fin-first")
    bare = tmp_path / "fin-first-origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare)],
        check=True,
        capture_output=True,
    )
    _git(repo, "remote", "add", "origin", str(bare))
    _install(monkeypatch, tmp_path, PassingRunner(repo))

    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    console = " ".join((result.stdout + result.stderr).split())
    assert "pushing main → origin (all history)" in console
    assert "pushed main → origin" in console


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


def test_commit_body_has_no_file_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-3: the body never lists the committed files.

    `git show --stat` prints them from the commit itself, so a list in the body
    could only agree with it or contradict it. With nothing authored the body
    is the issue objective and nothing else.
    """
    repo, _, _ = _finalize_by_replay(tmp_path, monkeypatch, "fin14d")

    body = _head_message(repo).partition("\n\n")[2]
    assert "Files touched:" not in body
    assert CANDIDATE not in body
    for word in PROVENANCE_WORDS:
        assert word not in body


def test_missing_change_bullets_is_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-4: a commit body thinner than intended is visible in the run log.

    The worker prompt asks for a `**Changes**` block and this run had none, so
    the operator log has to say the commit message degraded — otherwise the
    only trace is a commit nobody notices is thin.
    """
    repo, issue_id, _ = _finalize_by_replay(tmp_path, monkeypatch, "fin14g")

    logs = "\n".join(
        p.read_text(encoding="utf-8") for p in (repo / "logs").glob("grind-*.log")
    )
    assert f"no usable **Changes** bullets for {issue_id}" in logs


def test_authored_bullets_are_not_logged_as_a_degradation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-4: the degradation line only fires when the bullets are missing."""
    repo, issue_id, _ = _finalize_by_replay(
        tmp_path, monkeypatch, "fin14h", comments=(CHANGES_COMMENT,)
    )

    logs = "\n".join(
        p.read_text(encoding="utf-8") for p in (repo / "logs").glob("grind-*.log")
    )
    assert f"no usable **Changes** bullets for {issue_id}" not in logs


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

    subject = _head_message(repo).partition("\n\n")[0]
    assert subject == f"{issue_id}: verified candidate"
    # A tracker that cannot answer costs the title and the objective both, and
    # nothing else authored a body — but the owned paths are still committed.
    assert CANDIDATE in _committed_paths(repo)
    assert JournalStore(repo).load() is None


def test_commit_message_survives_an_unencodable_path() -> None:
    """AC-4: text recovered with surrogateescape must not break the commit."""
    message = grind_mod._commit_message(
        "repo-1",
        {"title": "Handle odd filenames"},
        grind_mod._bounded_block(["src/caf\udce9.py - renamed the loader"]),
    )

    assert message.startswith("repo-1: Handle odd filenames")
    assert "renamed the loader" in message
    message.encode("utf-8")  # the git call would raise on a lone surrogate


def test_commit_message_without_a_description_is_still_a_commit() -> None:
    """AC-4: a change nobody described still gets committed, subject only."""
    message = grind_mod._commit_message("repo-1", {"title": "Retire a stale flag"}, "")

    assert message == "repo-1: Retire a stale flag\n"


def test_commit_subject_shortens_on_word_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-1: an over-long title ends on a whole word, with no ellipsis.

    A subject cut mid-word behind a `...` reads as a corrupted log line and
    tells a reader nothing the whole words before it did not already say.
    """
    long_title = "Rewrite " + "the finalization commit message machinery " * 4
    repo, issue_id, _ = _finalize_by_replay(
        tmp_path, monkeypatch, "fin16", title=long_title
    )

    lines = _head_message(repo).splitlines()
    subject = lines[0]
    assert len(long_title) > grind_mod._COMMIT_SUBJECT_LIMIT
    assert len(subject) <= grind_mod._COMMIT_SUBJECT_LIMIT
    assert subject.startswith(f"{issue_id}: Rewrite the finalization")
    assert "..." not in subject
    assert long_title.startswith(subject.partition(": ")[2] + " ")
    assert lines[1] == "", "the body must be separated by a blank line"


def test_commit_subject_keeps_issue_prefix() -> None:
    """AC-5: the greppable `<id>: ` prefix survives any title length."""
    for title in ("", "short", "word " * 40, "supercalifragilistic" * 8):
        subject = grind_mod._commit_subject("repo-1", title)
        assert subject.startswith("repo-1: ")
        assert len(subject) <= grind_mod._COMMIT_SUBJECT_LIMIT


def test_commit_subject_deduplicates_component_prefix() -> None:
    """AC-2: the component the id already names is not repeated after it."""
    assert (
        grind_mod._commit_subject("ortus-4b2p", "ortus: retire the stale flag")
        == "ortus-4b2p: retire the stale flag"
    )
    # A leading component the id does not name is information, so it stays.
    assert (
        grind_mod._commit_subject("ortus-4b2p", "grind: retire the stale flag")
        == "ortus-4b2p: grind: retire the stale flag"
    )
    # No colon, nothing to de-duplicate.
    assert (
        grind_mod._commit_subject("ortus-4b2p", "retire the stale flag")
        == "ortus-4b2p: retire the stale flag"
    )
    # A title that is only the component keeps it rather than becoming empty.
    assert grind_mod._commit_subject("ortus-4b2p", "ortus:") == "ortus-4b2p: ortus:"


def test_commit_subject_first_word_over_limit() -> None:
    """AC-6: a single word wider than the budget is cut, never dropped."""
    subject = grind_mod._commit_subject("repo-1", "x" * 200)

    assert subject.startswith("repo-1: x")
    assert len(subject) == grind_mod._COMMIT_SUBJECT_LIMIT


def test_commit_subject_non_ascii_title() -> None:
    """AC-7: shortening never splits a multi-byte character."""
    title = "Précis über die Änderungen an dem Änderungsprotokoll " * 3
    subject = grind_mod._commit_subject("repo-1", title)

    assert len(subject) <= grind_mod._COMMIT_SUBJECT_LIMIT
    assert subject.startswith("repo-1: Précis über die")
    subject.encode("utf-8").decode("utf-8")


def test_one_shortener_serves_both_subject_producers() -> None:
    """ortus-frht AC-8: the two producers cut a subject the same way.

    A second copy in this module is how the composed subject came to be
    rejected for length while the deterministic one was quietly shortened.
    """

    assert grind_mod.shortened is compose_mod.shortened
    assert not hasattr(grind_mod, "_shortened")


# ---------------------------------------------------------------------------
# ortus-u1gs — one bounded read-only pass writes the message
# ---------------------------------------------------------------------------

COMPOSED_SUBJECT = "Ship the candidate flag the loader reads at import"
COMPOSED_BODY = (
    "The loader had no way to tell a shipped build from a working tree, so "
    "every caller guessed from the environment and two of them guessed "
    "differently.\n\n"
    "`candidate.py` now carries a single flag the loader reads once at import "
    "time, and everything downstream reads that instead of the environment.\n\n"
    "The flag is a plain module constant rather than a lookup, because a value "
    "resolved at import cannot disagree with itself later in the same process. "
    "Nothing rewrites it at runtime, which is the point."
)


class CommittingRunner(PassingRunner):
    """A worker that commits its candidate on the issue branch it was handed.

    The branch-scoped contract: the commit, with the worker's own message, is
    the deliverable, and the verifier judges the committed range.
    """

    def __init__(self, repo: Path, message: str) -> None:
        super().__init__(repo)
        self.message = message

    def run(self, prompt: str, **kwargs: object) -> int:
        profile = kwargs.get("profile")
        if getattr(profile, "phase", None) is not Phase.IMPLEMENT:
            return super().run(prompt, **kwargs)  # type: ignore[arg-type]
        self.phases.append(Phase.IMPLEMENT.value)
        log_path = kwargs["log_path"]
        workspace = kwargs["repo"]
        assert isinstance(log_path, Path)
        assert isinstance(workspace, Path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.touch(exist_ok=True)
        (workspace / CANDIDATE).write_text("SHIPPED = True\n")
        _git(workspace, "add", CANDIDATE)
        _git(workspace, "commit", "-m", self.message)
        journal = JournalStore(self.repo).load()
        assert journal is not None
        post_completion_comment(self.repo, journal.issue_id, {"AC-1": "pass"})
        return 0


def test_worker_commit_message_survives_finalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid message the worker wrote lands verbatim; the transaction's own
    late files fold into the worker's commit instead of stacking a second."""
    repo, issue_id = _seed(tmp_path, "fin-compose1")
    message = (
        f"{issue_id}: {COMPOSED_SUBJECT}\n\n{COMPOSED_BODY}"
    )
    backend = CommittingRunner(repo, message)
    _install(monkeypatch, tmp_path, backend)

    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    subject, _, body = _head_message(repo).partition("\n\n")
    assert subject == f"{issue_id}: {COMPOSED_SUBJECT}"
    assert "reads once at import" in body
    # One commit per issue: the tracker exports the close rewrote were folded
    # into the worker's commit, not stacked on top of it.
    assert len(_finalization_commits(repo, issue_id)) == 1
    committed = _committed_paths(repo)
    assert CANDIDATE in committed
    # bd writes its exports asynchronously: folded into this commit, dirty
    # awaiting the next sweep, or not yet rewritten at all — timing, not
    # behavior. What must hold is that the export file is never lost.
    assert (repo / ".beads" / "issues.jsonl").exists()
    # No finalize-phase model run (compose retired) and no verify-phase agent
    # (the machine pipeline judged the branch): one worker spawn total.
    assert backend.phases == [Phase.IMPLEMENT.value]


def test_restart_after_compose_reuses_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-2: a journaled message is committed verbatim, with no second call."""
    repo, issue_id = _seed(tmp_path, "fin-compose2")
    journal = _stage_pending_journal(
        repo, issue_id, landed=("report", "close"), close_issue=True
    )
    message = f"{issue_id}: {COMPOSED_SUBJECT}\n\n{COMPOSED_BODY}\n"
    JournalStore(repo).save(journal.with_finalization("compose", message))
    backend = NeverRuns()
    _install(monkeypatch, tmp_path, backend)

    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    assert _head_message(repo).strip() == message.strip()
    assert backend.composed == [], "a journaled message must not be recomposed"
    assert len(_finalization_commits(repo, issue_id)) == 1


def test_legacy_journal_without_compose_finalizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-3: a journal written before this boundary existed still finalizes."""
    repo, issue_id = _seed(tmp_path, "fin-compose3")
    _stage_pending_journal(
        repo, issue_id, landed=("report", "close"), close_issue=True
    )
    store = JournalStore(repo)
    stored = json.loads(store.path.read_text(encoding="utf-8"))
    assert "compose" not in stored["finalization"]
    _install(monkeypatch, tmp_path, NeverRuns())

    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    assert len(_finalization_commits(repo, issue_id)) == 1
    assert store.load() is None


def test_uncommitted_candidate_falls_back_to_the_deterministic_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker that left its candidate uncommitted still lands one commit,
    with the body Ortus builds deterministically; the retired compose pass is
    never spawned for it."""
    repo, issue_id = _seed(tmp_path, "fin-compose4")
    backend = PassingRunner(repo)
    _install(monkeypatch, tmp_path, backend)

    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    subject, _, body = _head_message(repo).partition("\n\n")
    assert subject == f"{issue_id}: {_issue(repo, issue_id)['title']}"
    assert "Exercise the behavior owned by this test." in body
    assert len(_finalization_commits(repo, issue_id)) == 1
    assert backend.phases == [Phase.IMPLEMENT.value]
    logs = sorted((repo / "logs").glob("grind-*.log"))
    assert logs, "the run wrote no log"
    assert "commit-message pass retired" in logs[-1].read_text(encoding="utf-8")


def test_subject_shortening_is_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ortus-frht AC-1/AC-4: the message lands, and the repair says so.

    The commit keeps the explanation the worker wrote — only the tail of the
    subject is spent — and the run log states the length that was written, so a
    worker drifting past the limit is visible rather than silently patched.
    """
    repo, issue_id = _seed(tmp_path, "fin-compose6")
    long_subject = COMPOSED_SUBJECT + " time on every supported platform"
    backend = CommittingRunner(
        repo, f"{issue_id}: {long_subject}\n\n{COMPOSED_BODY}"
    )
    _install(monkeypatch, tmp_path, backend)

    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    message = _head_message(repo)
    subject = message.splitlines()[0]
    written = len(f"{issue_id}: ") + len(long_subject)
    assert written > grind_mod._COMMIT_SUBJECT_LIMIT
    assert len(subject) <= grind_mod._COMMIT_SUBJECT_LIMIT
    assert subject.startswith(f"{issue_id}: Ship the candidate flag")
    assert "..." not in subject
    # The body is the part worth keeping, and it is untouched.
    assert "reads once at import" in message
    assert "Exercise the behavior owned by this test." not in message

    logs = sorted((repo / "logs").glob("grind-*.log"))
    assert logs, "the run wrote no log"
    log = logs[-1].read_text(encoding="utf-8")
    assert f"shortened from {written} characters" in log
    assert "the deterministic body stands" not in log


def test_worker_message_that_is_wrong_is_replaced_by_amend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A message that is wrong rather than long — here, no body at all — is
    replaced by the deterministic assembly via amend, so the commit still
    lands as one commit carrying the worker's tree with a usable message."""
    repo, issue_id = _seed(tmp_path, "fin-compose5")
    backend = CommittingRunner(repo, f"{issue_id}: bare subject with no body")
    _install(monkeypatch, tmp_path, backend)

    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert _issue(repo, issue_id)["status"] == "closed"
    message = _head_message(repo)
    assert message.startswith(f"{issue_id}: ")
    # The deterministic assembly replaced the bodiless message.
    assert "bare subject with no body" not in message
    assert CANDIDATE in _committed_paths(repo)
    assert len(_finalization_commits(repo, issue_id)) == 1

    logs = sorted((repo / "logs").glob("grind-*.log"))
    log = logs[-1].read_text(encoding="utf-8")
    assert "worker commit message rejected" in log


# The compose-pass mutation-guard test retired with the pass itself:
# `guard_read_only` keeps its unit coverage in tests/test_core_compose.py, and
# nothing spawns a finalize-phase model run any longer, so there is no
# in-pipeline writer left for an end-to-end guard test to catch.


# ---------------------------------------------------------------------------
# ortus-9yh9 — a check that rebuilds committed build output is not misconduct
# ---------------------------------------------------------------------------


GENERATED = "dist/app.js"
BUILT = b"// built from candidate.py\n"
REBUILT = b"// rebuilt at review time\n"


def _log(repo: Path) -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for path in (repo / "logs").glob("grind-*.log")
    )


def _enable_reviewer(repo: Path) -> None:
    """Turn the agent reviewer on for this workspace, committed so the flag
    file never reads as inherited work.

    The seal and mutation-guard properties belong to the agent reviewer, which
    is a default-off configured step now; these tests exercise it through the
    flag exactly as an operator would.
    """

    config = repo / ".ortusrc"
    existing = config.read_text(encoding="utf-8") if config.exists() else ""
    config.write_text(existing + "\nreviewer = true\n", encoding="utf-8")
    _git(repo, "add", ".ortusrc")
    _git(repo, "commit", "-m", "fixture: enable the reviewer flag")


def _blob(repo: Path, relative: str) -> bytes:
    return subprocess.run(
        ["git", "show", f"HEAD:{relative}"], cwd=repo, capture_output=True, check=True
    ).stdout


class RebuildingRunner(PassingRunner):
    """A candidate carrying build output whose checks rebuild it under review.

    The shape the guard used to blame the reviewer for: the implementation
    ships a source file plus the artifact built from it, and the read-only
    verifier's own checks write that artifact again with different bytes. The
    inherited verdict declares only `CANDIDATE` as reviewed — a reviewer reads
    the source, not the transpiled sibling it never opened.
    """

    def __init__(
        self,
        repo: Path,
        *,
        built: bytes = BUILT,
        rebuilt: bytes = REBUILT,
        artifact: str = GENERATED,
        also_edits: str = "",
        adds_path: str = "",
        removes_artifact: bool = False,
    ) -> None:
        super().__init__(repo)
        self.built = built
        self.rebuilt = rebuilt
        self.artifact = artifact
        self.also_edits = also_edits
        self.adds_path = adds_path
        self.removes_artifact = removes_artifact

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
        if phase is Phase.IMPLEMENT:
            (repo / self.artifact).parent.mkdir(parents=True, exist_ok=True)
            (repo / self.artifact).write_bytes(self.built)
        elif phase is Phase.VERIFY:
            if self.removes_artifact:
                (repo / self.artifact).unlink()
            else:
                (repo / self.artifact).write_bytes(self.rebuilt)
            if self.also_edits:
                (repo / self.also_edits).write_text("SHIPPED = True\n# edited\n")
            if self.adds_path:
                (repo / self.adds_path).write_text("scratch from the reviewer\n")
        return super().run(
            prompt, repo=repo, log_path=log_path, profile=profile, **kwargs
        )


def test_rebuilt_artifact_is_restored_and_run_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-1: the reviewer's checks rebuilt a candidate path, so the run goes on.

    Before this, the re-diff saw only that a byte moved and rejected the
    verdict for reviewer tampering — systematically, on every candidate that
    carries build output.
    """
    repo, issue_id = _seed(tmp_path, "fin-seal1")
    _enable_reviewer(repo)
    backend = RebuildingRunner(repo)
    _install(monkeypatch, tmp_path, backend)

    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    # No finalize-phase model run: the compose pass is retired.
    assert backend.phases == [
        Phase.IMPLEMENT.value,
        Phase.VERIFY.value,
    ]
    assert "mutated the candidate" not in _log(repo)
    assert _issue(repo, issue_id)["status"] == "closed"
    assert {CANDIDATE, GENERATED} <= _committed_paths(repo)
    assert JournalStore(repo).load() is None


def test_restored_paths_are_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-2: an artifact that differs every run is a finding, not a silence."""
    repo, _ = _seed(tmp_path, "fin-seal2")
    _enable_reviewer(repo)
    _install(monkeypatch, tmp_path, RebuildingRunner(repo))

    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    log = _log(repo)
    assert f"restored {GENERATED}" in log
    assert "the verifier's checks rebuilt it during read-only review" in log


def test_verifier_source_edit_still_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-3: the seal is not weakened for what the reviewer says it reviewed.

    Restoring a rebuilt artifact must not become a licence to edit the code
    under review; that property is the whole reason the phase is read-only.
    """
    repo, issue_id = _seed(tmp_path, "fin-seal3")
    _enable_reviewer(repo)
    _install(monkeypatch, tmp_path, RebuildingRunner(repo, also_edits=CANDIDATE))

    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    assert "verifier mutated the candidate during read-only review" in _log(repo)
    assert _issue(repo, issue_id)["status"] == "in_progress"
    # The candidate parks committed on the issue branch (the capture
    # commit); what a rejection must never do is land it on main.
    main_subjects = _git(repo, "log", "--format=%s", "main").stdout.splitlines()
    assert [s for s in main_subjects if s.startswith(f"{issue_id}: ")] == []
    journal = JournalStore(repo).load()
    assert journal is not None and journal.phase == "verification-rejected"


def test_failed_restore_is_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-4: a candidate Ortus cannot put back is one no reviewer saw.

    And the diagnosis names the restore, not the reviewer: an unwritable mount
    is not misconduct.
    """
    repo, issue_id = _seed(tmp_path, "fin-seal4")
    _enable_reviewer(repo)
    _install(monkeypatch, tmp_path, RebuildingRunner(repo))

    def _unwritable(*args: object, **kwargs: object) -> None:
        raise OSError(errno.EROFS, "Read-only file system")

    monkeypatch.setattr(grind_mod, "restore_sealed_path", _unwritable)

    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    log = _log(repo)
    assert "could not restore the candidate after read-only review" in log
    assert GENERATED in log
    assert "verifier mutated the candidate" not in log
    assert _issue(repo, issue_id)["status"] == "in_progress"
    # The candidate parks committed on the issue branch (the capture
    # commit); what a rejection must never do is land it on main.
    main_subjects = _git(repo, "log", "--format=%s", "main").stdout.splitlines()
    assert [s for s in main_subjects if s.startswith(f"{issue_id}: ")] == []


def test_path_set_change_still_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-5: a path the reviewer added is a different failure, and stays fatal."""
    repo, issue_id = _seed(tmp_path, "fin-seal5")
    _enable_reviewer(repo)
    _install(monkeypatch, tmp_path, RebuildingRunner(repo, adds_path="reviewer.txt"))

    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    assert "mutated the candidate path set during read-only review" in _log(repo)
    assert _issue(repo, issue_id)["status"] == "in_progress"
    # The candidate parks committed on the issue branch (the capture
    # commit); what a rejection must never do is land it on main.
    main_subjects = _git(repo, "log", "--format=%s", "main").stdout.splitlines()
    assert [s for s in main_subjects if s.startswith(f"{issue_id}: ")] == []


def test_binary_artifact_restored_byte_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-6: an image goes back byte for byte, never through a text path."""
    image = b"\x89PNG\r\n\x1a\n\x00\x01\xff\xfe built\x00"
    repo, _ = _seed(tmp_path, "fin-seal6")
    _enable_reviewer(repo)
    _install(
        monkeypatch,
        tmp_path,
        RebuildingRunner(
            repo,
            artifact="assets/logo.png",
            built=image,
            rebuilt=b"\x89PNG\r\n\x1a\n\x00\x01\xff\xfe rebuilt\x00\x00",
        ),
    )

    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    assert (repo / "assets/logo.png").read_bytes() == image
    assert _blob(repo, "assets/logo.png") == image


def test_unchanged_review_is_unaffected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-7: a review during which nothing moved logs nothing new."""
    repo, issue_id = _seed(tmp_path, "fin-seal7")
    _enable_reviewer(repo)
    _install(monkeypatch, tmp_path, PassingRunner(repo))

    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    log = _log(repo)
    assert "restored" not in log
    assert "mutated the candidate" not in log
    assert _issue(repo, issue_id)["status"] == "closed"
    assert CANDIDATE in _committed_paths(repo)


def test_commit_carries_the_sealed_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-8: the rebuilt bytes are never adopted, on disk or in the commit."""
    repo, _ = _seed(tmp_path, "fin-seal8")
    _enable_reviewer(repo)
    _install(monkeypatch, tmp_path, RebuildingRunner(repo))

    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    assert _blob(repo, GENERATED) == BUILT
    assert (repo / GENERATED).read_bytes() == BUILT


def test_deleted_artifact_is_restored_not_taken_as_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A check that cleans build output before rebuilding it still leaves the
    candidate whole: the path comes back with its sealed content rather than
    being committed as a deletion nobody reviewed."""
    repo, issue_id = _seed(tmp_path, "fin-seal9")
    _enable_reviewer(repo)
    (repo / GENERATED).parent.mkdir(parents=True)
    (repo / GENERATED).write_bytes(b"// stale build\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "committed build output")
    _install(monkeypatch, tmp_path, RebuildingRunner(repo, removes_artifact=True))

    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    assert f"restored {GENERATED}" in _log(repo)
    assert _issue(repo, issue_id)["status"] == "closed"
    assert (repo / GENERATED).read_bytes() == BUILT
    assert _blob(repo, GENERATED) == BUILT


# ---------------------------------------------------------------------------
# Branch-scoped finalization (ortus-rcdf.1, Phase L0 commit A)
# ---------------------------------------------------------------------------


def test_candidate_lands_on_an_issue_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The commit exists on `ortus/<issue-id>` and the integration branch is
    fast-forwarded to it; the branch survives finalization."""
    repo, issue_id = _seed(tmp_path, "br1")
    _install(monkeypatch, tmp_path, PassingRunner(repo))

    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    branch = f"ortus/{issue_id}"
    tip = _git(repo, "rev-parse", f"refs/heads/{branch}").stdout.strip()
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert tip == head
    assert CANDIDATE in _committed_paths(repo)


def test_run_ends_on_the_integration_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, issue_id = _seed(tmp_path, "br2")
    _install(monkeypatch, tmp_path, PassingRunner(repo))

    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    current = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert current == "main"
    assert _issue(repo, issue_id)["status"] == "closed"


def test_existing_branch_is_not_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-existing issue branch that is not at the integration head is
    reported and the claim reverted — never reused silently, never reset."""
    repo, issue_id = _seed(tmp_path, "br3")
    _install(monkeypatch, tmp_path, PassingRunner(repo))
    stray = repo / "stray.txt"
    stray.write_text("earlier attempt\n")
    _git(repo, "checkout", "-b", f"ortus/{issue_id}")
    _git(repo, "add", "stray.txt")
    _git(repo, "commit", "-m", "earlier attempt")
    earlier_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()
    # Checking main back out drops stray.txt from the tree: it is committed on
    # the issue branch only, which is exactly the durable home under test.
    _git(repo, "checkout", "main")

    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 1
    kept = _git(repo, "rev-parse", f"refs/heads/ortus/{issue_id}").stdout.strip()
    assert kept == earlier_tip
    assert _issue(repo, issue_id)["status"] == "open"


def test_pass_with_a_remote_pushes_the_fast_forwarded_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sync step still lands the integration branch on origin after the
    fast-forward, so nothing about the remote contract changed."""
    repo, issue_id = _seed(tmp_path, "br4", remote=True)
    _install(monkeypatch, tmp_path, PassingRunner(repo))

    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    local = _git(repo, "rev-parse", "main").stdout.strip()
    remote = _git(repo, "rev-parse", "origin/main").stdout.strip()
    assert local == remote
    branch_tip = _git(
        repo, "rev-parse", f"refs/heads/ortus/{issue_id}"
    ).stdout.strip()
    assert branch_tip == local


# ---------------------------------------------------------------------------
# Worker workspaces (ortus-u4zv.2): disposable clones, never-moving primary
# ---------------------------------------------------------------------------


def test_fetched_branch_fast_forwards_integration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-2: the worker's commits reach `ortus/<id>` in the primary repository
    via the finalization fetch, and the integration branch fast-forwards."""
    repo, issue_id = _seed(tmp_path, "ws2")
    _install(monkeypatch, tmp_path, PassingRunner(repo))

    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    branch_tip = _git(repo, "rev-parse", f"refs/heads/ortus/{issue_id}").stdout.strip()
    main_tip = _git(repo, "rev-parse", "refs/heads/main").stdout.strip()
    assert branch_tip == main_tip
    shown = _git(repo, "show", f"ortus/{issue_id}:{CANDIDATE}").stdout
    assert shown == "SHIPPED = True\n"
    assert "fast-forwarded to" in _log_text(repo)


def test_primary_side_export_churn_is_swept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-4: tracker exports written primary-side during the run fold into the
    landing when they arrive in time, and stay dirty for the next sweep when
    bd's async export loses the race — never lost, never a second commit."""
    repo, issue_id = _seed(tmp_path, "ws4")
    _install(monkeypatch, tmp_path, PassingRunner(repo))

    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # bd writes exports asynchronously, so at this instant the export may be
    # folded into the landing, still dirty awaiting the next sweep, or not
    # yet rewritten at all — every case is fine; what must never happen is a
    # stacked housekeeping commit or a lost export file.
    assert (repo / ".beads" / "issues.jsonl").exists()
    assert len(_finalization_commits(repo, issue_id)) == 1, "one commit, no chore stack"


def test_clone_removed_branch_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-5: the clone is removed after landing; a parked issue's branch
    survives in the primary repository even though its clone is gone too."""
    repo, issue_id = _seed(tmp_path, "ws5a")
    _install(monkeypatch, tmp_path, PassingRunner(repo))
    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert not (repo / "logs" / "grind-workspaces" / issue_id).exists()
    assert _git(repo, "rev-parse", f"refs/heads/ortus/{issue_id}").returncode == 0

    from tests._shims import install_machine_checks, machine_run

    parked_repo, parked_id = _seed(tmp_path, "ws5b")
    _install(monkeypatch, tmp_path, PassingRunner(parked_repo))
    install_machine_checks(
        monkeypatch, default=machine_run("fail", output="stays red")
    )
    result = runner.invoke(
        app,
        [
            "grind",
            str(parked_repo),
            "--tasks",
            "1",
            "--idle-sleep",
            "0",
            "--max-corrections",
            "0",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert not (parked_repo / "logs" / "grind-workspaces" / parked_id).exists()
    shown = _git(parked_repo, "show", f"ortus/{parked_id}:{CANDIDATE}")
    assert shown.returncode == 0, "the parked branch preserves the work"


def test_no_remote_full_flow_in_clone_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-7: a repository with no remote completes the whole flow in a clone."""
    repo, issue_id = _seed(tmp_path, "ws7")
    _install(monkeypatch, tmp_path, PassingRunner(repo))
    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert _issue(repo, issue_id)["status"] == "closed"
    assert CANDIDATE in _committed_paths(repo)
    assert JournalStore(repo).load() is None
