"""Graceful resume of uncommitted candidates after a failed grind (ortus-pzfd.6).

An unsuccessful run — nonzero worker exit, a kill, a verifier rejection, a
malformed verdict — can return with its issue still assigned and its edits still
uncommitted. That pair, the issue plus the worktree, is the handoff artifact: the
next grind resumes the same goal, hands a fresh worker the issue packet and the
complete current git state, and lets it keep, repair, or disown what it inherited
instead of restarting.

Every historical mismatch (journal schema, prior HEAD, path set, candidate hash)
is context in that handoff rather than a startup failure, and nothing here ever
resets, stashes, deletes, or silently commits work the operator left behind.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Callable

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.commands import grind as grind_mod
from ortus.core import sandbox as sandbox_mod
from ortus.core.git import GitClient
from ortus.core.profiles import Phase
from ortus.core.sandbox import SandboxInfo
from ortus.core.transaction import CandidateJournal, JournalStore, candidate_diff
from tests._shims import normalize_git_branch, ready_issue_args

pytestmark = [pytest.mark.integration, pytest.mark.slow]
runner = CliRunner()

CANDIDATE = "candidate.py"
INHERITED = "inherited-work.txt"
OPERATOR = "operator-notes.txt"
FINALIZATION_MARKER = grind_mod._FINALIZATION_MARKER
DECLARATION = grind_mod._UNRELATED_DECLARATION


# ---------------------------------------------------------------------------
# fixture plumbing
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )


def _create_issue(repo: Path, title: str, *, priority: str = "1") -> str:
    return subprocess.run(
        [
            "bd",
            "create",
            "--silent",
            "--title",
            title,
            "--type",
            "task",
            "--priority",
            priority,
            *ready_issue_args(),
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _seed(tmp_path: Path, prefix: str, *, remote: bool = False) -> tuple[Path, str]:
    """A bd workspace on `main` with a committed baseline and one ready leaf."""
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    repo = tmp_path / prefix
    repo.mkdir()
    subprocess.run(
        ["bd", "init", "--non-interactive", "--prefix", prefix],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    normalize_git_branch(repo)
    settings = repo / ".claude" / "settings.json"
    settings.parent.mkdir(exist_ok=True)
    settings.write_text(json.dumps({"sandbox": {"excludedCommands": ["bd", "bd *"]}}))
    (repo / ".gitignore").write_text(
        "logs/\n.cache/\n.beads/*\n!.beads/issues.jsonl\n!.beads/interactions.jsonl\n"
    )
    (repo / OPERATOR).write_text("committed operator baseline\n")
    issue_id = _create_issue(repo, "recovery fixture")
    _git(repo, "config", "user.email", "ortus-tests@example.invalid")
    _git(repo, "config", "user.name", "Ortus Tests")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "fixture baseline")
    if remote:
        bare = tmp_path / f"{prefix}-origin.git"
        subprocess.run(
            ["git", "init", "--bare", str(bare)], check=True, capture_output=True
        )
        _git(repo, "remote", "add", "origin", str(bare))
        _git(repo, "push", "-u", "origin", "main")
    return repo, issue_id


def _install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, backend_runner: object
) -> None:
    monkeypatch.setattr(
        sandbox_mod, "smoke_test", lambda: SandboxInfo(platform="Linux", binary="bwrap")
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    monkeypatch.setattr(grind_mod, "_make_runner", lambda *a, **k: backend_runner)


def _issue(repo: Path, issue_id: str) -> dict:
    data = json.loads(
        subprocess.run(
            ["bd", "show", issue_id, "--json"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    return data[0] if isinstance(data, list) else data


def _claim(repo: Path, issue_id: str) -> None:
    subprocess.run(
        ["bd", "update", issue_id, "--status", "in_progress"],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _comment_bodies(repo: Path, issue_id: str) -> list[str]:
    entries = json.loads(
        subprocess.run(
            ["bd", "comments", issue_id, "--json"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        or "[]"
    )
    bodies = []
    for entry in entries:
        if isinstance(entry, dict):
            for key in ("body", "text", "comment", "content"):
                if entry.get(key):
                    bodies.append(str(entry[key]))
                    break
    return bodies


def _subjects(repo: Path) -> list[str]:
    return subprocess.run(
        ["git", "log", "--format=%s"], cwd=repo, capture_output=True, text=True
    ).stdout.splitlines()


def _finalization_commits(repo: Path, issue_id: str) -> list[str]:
    """Subjects of the commits finalization made for `issue_id`.

    Matched on the leading issue id: the rest of the subject is the issue's
    own title, which these tests don't pin.
    """
    return [line for line in _subjects(repo) if line.startswith(f"{issue_id}: ")]


def _committed_paths(repo: Path) -> set[str]:
    out = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return {line.strip() for line in out.splitlines() if line.strip()}


def _log(repo: Path) -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for path in (repo / "logs").glob("grind-*.log")
    )


def _grind(repo: Path, *extra: str) -> object:
    return runner.invoke(
        app, ["grind", str(repo), "--idle-sleep", "0", *(extra or ("--iterations", "1"))]
    )


class ScriptedRunner:
    """A fake backend with one hook per phase and a record of every prompt."""

    extra_env: dict[str, str] = {}

    def __init__(
        self,
        *,
        implement: Callable[[Path, Path], int] | None = None,
        verify: Callable[[Path, Path], int] | None = None,
    ) -> None:
        self.prompts: list[tuple[str, str]] = []
        self._implement = implement
        self._verify = verify

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
        self.prompts.append((phase.value, prompt))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.touch(exist_ok=True)
        hook = self._implement if phase is Phase.IMPLEMENT else self._verify
        return 0 if hook is None else hook(repo, log_path)

    def prompt_for(self, phase: Phase) -> str:
        return next(text for name, text in self.prompts if name == phase.value)


def _emit(log_path: Path, text: str) -> None:
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": text},
                }
            )
            + "\n"
        )


def _pass_verdict(repo: Path, log_path: Path) -> int:
    """A verdict bound to whatever candidate the transaction currently owns."""
    journal = JournalStore(repo).load()
    assert journal is not None
    _emit(
        log_path,
        "ORTUS_VERDICT: "
        + json.dumps(
            {
                "schema": 1,
                "candidate_hash": journal.candidate_hash,
                "decision": "pass",
                "criteria": [{"id": "AC-1", "status": "pass", "evidence": "verified"}],
                "commands": ["uv run pytest tests/test_grind_recovery.py -q"],
                "reviewed_files": sorted(journal.candidate_paths) or [CANDIDATE],
                "reviewed_interfaces": ["RECOVERED"],
                "risks": ["none"],
                "findings": ["none"],
                "codegraph": ["fallback recorded"],
            }
        ),
    )
    return 0


def _fail_verdict(repo: Path, log_path: Path) -> int:
    """A rejection bound to the same candidate, so a correction attempt follows."""
    journal = JournalStore(repo).load()
    assert journal is not None
    _emit(
        log_path,
        "ORTUS_VERDICT: "
        + json.dumps(
            {
                "schema": 1,
                "candidate_hash": journal.candidate_hash,
                "decision": "fail",
                "criteria": [
                    {"id": "AC-1", "status": "fail", "evidence": "not demonstrated"}
                ],
                "commands": ["uv run pytest tests/test_grind_recovery.py -q"],
                "reviewed_files": sorted(journal.candidate_paths) or [CANDIDATE],
                "reviewed_interfaces": ["RECOVERED"],
                "risks": ["none"],
                "findings": [f"{CANDIDATE}:1 the candidate does not satisfy AC-1"],
                "codegraph": ["fallback recorded"],
            }
        ),
    )
    return 0


def _malformed_verdict(repo: Path, log_path: Path) -> int:
    _emit(log_path, 'ORTUS_VERDICT: {"schema": 1, "decision":')
    return 0


def _stage_journal(
    repo: Path,
    issue_id: str,
    *,
    phase: str,
    paths: frozenset[str],
) -> CandidateJournal:
    """Reproduce a run that left `paths` uncommitted and never finished."""
    _claim(repo, issue_id)
    store = JournalStore(repo)
    digest, diff_ref = store.save_diff(candidate_diff(repo, paths))
    packet_digest, packet_ref = store.save_packet(issue_id, _issue(repo, issue_id))
    journal = CandidateJournal.start(
        repo=repo,
        issue_id=issue_id,
        base_head=GitClient(repo=repo).head_oid(),
        baseline_paths=(),
        packet_hash=packet_digest,
        packet_ref=packet_ref,
        profiles={"implementation": "fixture", "verification": "fixture"},
    ).with_candidate(paths, phase=phase, candidate_hash=digest, diff_ref=diff_ref)
    store.save(journal)
    return journal


# ---------------------------------------------------------------------------
# AC-1 — every abnormal exit preserves the issue association and the work
# ---------------------------------------------------------------------------


def test_nonzero_exit_after_edits_preserves_the_claim_and_the_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-1: a worker that edits and then exits nonzero leaves a resumable
    transaction — the issue association, the candidate paths, and the files."""
    repo, issue_id = _seed(tmp_path, "rec1")

    def implement(repo: Path, log_path: Path) -> int:
        (repo / CANDIDATE).write_text("PARTIAL = True\n")
        return 1

    backend = ScriptedRunner(implement=implement)
    _install(monkeypatch, tmp_path, backend)
    baseline_subjects = _subjects(repo)

    result = _grind(repo)
    assert result.exit_code == 0, result.stdout + result.stderr

    journal = JournalStore(repo).load()
    assert journal is not None
    assert journal.issue_id == issue_id
    assert CANDIDATE in journal.candidate_paths
    assert (repo / CANDIDATE).read_text() == "PARTIAL = True\n"
    assert _subjects(repo) == baseline_subjects, "nothing may be committed"
    assert _issue(repo, issue_id)["status"] != "closed"


def test_parent_interruption_preserves_the_candidate_before_grind_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-1: the worker's process group is killed mid-edit. The journal still
    names the issue and owns the surviving edits when grind returns."""
    repo, issue_id = _seed(tmp_path, "rec2")

    def implement(repo: Path, log_path: Path) -> int:
        (repo / CANDIDATE).write_text("HALF_DONE = True\n")
        raise subprocess.TimeoutExpired("fake-worker", 1)

    _install(monkeypatch, tmp_path, ScriptedRunner(implement=implement))

    result = _grind(repo, "--iterations", "1", "--worker-timeout", "5")
    assert result.exit_code == 0, result.stdout + result.stderr

    journal = JournalStore(repo).load()
    assert journal is not None
    assert journal.issue_id == issue_id
    assert CANDIDATE in journal.candidate_paths
    assert journal.phase == "implementation-timeout"
    assert (repo / CANDIDATE).read_text() == "HALF_DONE = True\n"
    assert journal.evidence and journal.evidence[-1]["timed_out"] is True


# ---------------------------------------------------------------------------
# AC-2 — a later grind resumes that issue before selecting anything new
# ---------------------------------------------------------------------------


def test_resume_before_select_prefers_the_in_progress_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-2: a higher-priority ready issue does not win over an outstanding
    handoff — the resumed issue is the one the worker is given."""
    repo, issue_id = _seed(tmp_path, "rec3")
    tempting = _create_issue(repo, "unrelated newer work", priority="0")
    (repo / INHERITED).write_text("work from the prior engineer\n")
    _stage_journal(
        repo, issue_id, phase="incomplete-candidate", paths=frozenset({INHERITED})
    )
    backend = ScriptedRunner()
    _install(monkeypatch, tmp_path, backend)

    result = _grind(repo)
    assert result.exit_code == 0, result.stdout + result.stderr

    implementation = backend.prompt_for(Phase.IMPLEMENT)
    assert issue_id in implementation
    assert tempting not in implementation
    assert _issue(repo, tempting)["status"] == "open"
    assert f"transaction resume: issue={issue_id}" in _log(repo)


def test_resume_before_select_uses_the_single_claim_when_the_journal_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-2/AC-4: no journal at all still routes. One claimed issue plus
    uncommitted work identifies the goal well enough to resume it."""
    repo, issue_id = _seed(tmp_path, "rec4")
    tempting = _create_issue(repo, "unrelated newer work", priority="0")
    (repo / INHERITED).write_text("work from the prior engineer\n")
    _claim(repo, issue_id)
    backend = ScriptedRunner()
    _install(monkeypatch, tmp_path, backend)

    result = _grind(repo)
    assert result.exit_code == 0, result.stdout + result.stderr

    implementation = backend.prompt_for(Phase.IMPLEMENT)
    assert issue_id in implementation and tempting not in implementation
    assert f"resuming the single claimed issue {issue_id}" in _log(repo)
    assert (repo / INHERITED).read_text() == "work from the prior engineer\n"


# ---------------------------------------------------------------------------
# AC-3 — the fresh worker is told to assess, keep, repair, and continue
# ---------------------------------------------------------------------------


def test_handoff_prompt_supplies_git_state_and_the_disown_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-3: the recovery worker receives the prior phase, the inherited paths,
    the captured diff, and the channel for declaring unrelated work."""
    repo, issue_id = _seed(tmp_path, "rec5")
    (repo / INHERITED).write_text("half-finished work\n")
    journal = _stage_journal(
        repo, issue_id, phase="implementation-timeout", paths=frozenset({INHERITED})
    )
    backend = ScriptedRunner()
    _install(monkeypatch, tmp_path, backend)

    result = _grind(repo)
    assert result.exit_code == 0, result.stdout + result.stderr

    implementation = backend.prompt_for(Phase.IMPLEMENT)
    assert "RECOVERY HANDOFF" in implementation
    assert "stopped at implementation-timeout" in implementation
    assert INHERITED in implementation
    assert "git status" in implementation and "git diff HEAD" in implementation
    assert DECLARATION.as_posix() in implementation
    assert JournalStore(repo).load().candidate_diff_ref in implementation  # type: ignore[union-attr]
    assert journal.issue_packet_ref, "the packet artifact stays the authority"
    log = _log(repo)
    assert "transaction handoff: git status" in log
    assert INHERITED in log


# ---------------------------------------------------------------------------
# AC-4 — schema, HEAD, path, and hash differences are context, not blockers
# ---------------------------------------------------------------------------


def test_moved_state_schema_head_and_hash_drift_stay_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-4: a journal from a newer writer, a HEAD that moved, a candidate that
    changed, and a path set that no longer matches all resolve to notes."""
    repo, issue_id = _seed(tmp_path, "rec6")
    _claim(repo, issue_id)
    (repo / INHERITED).write_text("work the journal never recorded\n")
    store = JournalStore(repo)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        json.dumps(
            {
                "schema": 99,
                "issue_id": issue_id,
                "base_head": "0" * 40,
                "baseline_paths": [],
                "baseline_fingerprints": {},
                "candidate_paths": ["a-file-that-never-existed.py"],
                "candidate_hash": "not-the-current-hash",
                "phase": "incomplete-candidate",
                "invented_by_a_newer_ortus": {"unreadable": True},
            }
        )
    )
    backend = ScriptedRunner()
    _install(monkeypatch, tmp_path, backend)

    result = _grind(repo)
    assert result.exit_code == 0, result.stdout + result.stderr

    log = _log(repo)
    assert "journal schema 99" in log
    assert "invented_by_a_newer_ortus" in log
    assert "HEAD moved" in log
    assert "candidate path set changed" in log
    assert "candidate changed; refreshed to" in log
    # Reported, then resumed anyway: the same issue, with the real worktree.
    assert issue_id in backend.prompt_for(Phase.IMPLEMENT)
    assert (repo / INHERITED).read_text() == "work the journal never recorded\n"
    journal = JournalStore(repo).load()
    assert journal is not None and journal.issue_id == issue_id
    assert INHERITED in journal.candidate_paths


# ---------------------------------------------------------------------------
# AC-5 — unrelated work is never reset, stashed, deleted, or committed
# ---------------------------------------------------------------------------


def test_unrelated_declared_paths_stay_dirty_and_are_never_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-5: the worker judges inherited work unrelated, so the verified commit
    carries only the candidate and the operator's edits survive untouched."""
    repo, issue_id = _seed(tmp_path, "rec7")
    (repo / OPERATOR).write_text("local operator experiment\n")
    (repo / INHERITED).write_text("someone else's scratch file\n")
    _git(repo, "add", INHERITED)
    _claim(repo, issue_id)

    def implement(repo: Path, log_path: Path) -> int:
        (repo / CANDIDATE).write_text("SHIPPED = True\n")
        (repo / DECLARATION).parent.mkdir(parents=True, exist_ok=True)
        (repo / DECLARATION).write_text(f"{OPERATOR}\n{INHERITED}\n")
        return 0

    _install(
        monkeypatch,
        tmp_path,
        ScriptedRunner(implement=implement, verify=_pass_verdict),
    )

    result = _grind(repo, "--tasks", "1")
    assert result.exit_code == 0, result.stdout + result.stderr

    assert _issue(repo, issue_id)["status"] == "closed"
    committed = _committed_paths(repo)
    assert CANDIDATE in committed
    assert OPERATOR not in committed and INHERITED not in committed
    assert (repo / OPERATOR).read_text() == "local operator experiment\n"
    assert (repo / INHERITED).read_text() == "someone else's scratch file\n"
    dirty = GitClient(repo=repo).dirty_paths()
    assert dirty is not None and {OPERATOR, INHERITED} <= dirty
    assert "declared unrelated" in _log(repo)
    assert not (repo / DECLARATION).exists(), "the declaration is consumed"


def test_unrelated_declared_by_a_correction_worker_is_honored_in_the_same_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-5: the disown channel stays open on the correction attempt. A path the
    correction worker judges unrelated leaves the candidate immediately instead
    of being committed now and only honored by some later resume."""
    repo, issue_id = _seed(tmp_path, "rec7b")
    (repo / OPERATOR).write_text("local operator experiment\n")
    attempts = {"implement": 0, "verify": 0}

    def implement(repo: Path, log_path: Path) -> int:
        attempts["implement"] += 1
        (repo / CANDIDATE).write_text(f"SHIPPED = {attempts['implement']}\n")
        if attempts["implement"] > 1:
            # Only the correction worker recognizes the inherited edit as
            # somebody else's; the first worker committed it to the candidate.
            (repo / DECLARATION).parent.mkdir(parents=True, exist_ok=True)
            (repo / DECLARATION).write_text(f"{OPERATOR}\n")
        return 0

    def verify(repo: Path, log_path: Path) -> int:
        attempts["verify"] += 1
        return (
            _fail_verdict(repo, log_path)
            if attempts["verify"] == 1
            else _pass_verdict(repo, log_path)
        )

    _install(monkeypatch, tmp_path, ScriptedRunner(implement=implement, verify=verify))

    result = _grind(repo, "--tasks", "1")
    assert result.exit_code == 0, result.stdout + result.stderr

    assert attempts["implement"] == 2, "a correction attempt must have run"
    assert _issue(repo, issue_id)["status"] == "closed"
    committed = _committed_paths(repo)
    assert CANDIDATE in committed
    assert OPERATOR not in committed, (
        "the correction worker's disown must be honored before the commit"
    )
    assert (repo / OPERATOR).read_text() == "local operator experiment\n"
    dirty = GitClient(repo=repo).dirty_paths()
    assert dirty is not None and OPERATOR in dirty
    assert not (repo / DECLARATION).exists(), "the declaration is consumed"
    assert "declared unrelated" in _log(repo)


def test_unrelated_staged_and_untracked_work_survives_a_failed_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-5: a resume that fails again still touches nothing it does not own."""
    repo, issue_id = _seed(tmp_path, "rec8")
    (repo / OPERATOR).write_text("staged operator work\n")
    _git(repo, "add", OPERATOR)
    (repo / "untracked-notes.md").write_text("untracked operator notes\n")
    _stage_journal(
        repo,
        issue_id,
        phase="verification-rejected",
        paths=frozenset({OPERATOR, "untracked-notes.md"}),
    )
    baseline_subjects = _subjects(repo)
    _install(monkeypatch, tmp_path, ScriptedRunner(verify=_malformed_verdict))

    result = _grind(repo)
    assert result.exit_code == 0, result.stdout + result.stderr

    assert (repo / OPERATOR).read_text() == "staged operator work\n"
    assert (repo / "untracked-notes.md").read_text() == "untracked operator notes\n"
    assert _subjects(repo) == baseline_subjects
    assert (
        subprocess.run(
            ["git", "diff", "--cached", "--quiet", "--", OPERATOR], cwd=repo
        ).returncode
        == 1
    ), "the staged operator change must still be staged"
    assert JournalStore(repo).load() is not None


def test_unrelated_work_stays_disowned_for_the_next_issue_in_the_same_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-5: a disowned path outlives the transaction that disowned it. The next
    issue's candidate must not quietly absorb and commit it."""
    repo, first_id = _seed(tmp_path, "rec12")
    second_id = _create_issue(repo, "second queued issue", priority="2")
    (repo / OPERATOR).write_text("operator work nobody owns\n")
    _claim(repo, first_id)
    seen: list[str] = []

    def implement(repo: Path, log_path: Path) -> int:
        # One candidate file per issue, and the operator's file is disowned once.
        target = repo / f"candidate-{len(seen)}.py"
        seen.append(target.name)
        target.write_text("SHIPPED = True\n")
        if len(seen) == 1:
            (repo / DECLARATION).parent.mkdir(parents=True, exist_ok=True)
            (repo / DECLARATION).write_text(f"{OPERATOR}\n")
        return 0

    _install(
        monkeypatch,
        tmp_path,
        ScriptedRunner(implement=implement, verify=_pass_verdict),
    )

    result = _grind(repo, "--tasks", "2")
    assert result.exit_code == 0, result.stdout + result.stderr

    assert _issue(repo, first_id)["status"] == "closed"
    assert _issue(repo, second_id)["status"] == "closed"
    assert seen == ["candidate-0.py", "candidate-1.py"]
    assert {subject.split(":", 1)[0] for subject in _subjects(repo)[:2]} == {
        first_id,
        second_id,
    }
    everything_committed = subprocess.run(
        ["git", "log", "--name-only", "--format=", "-2"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert OPERATOR not in everything_committed
    assert (repo / OPERATOR).read_text() == "operator work nobody owns\n"


def test_moved_state_readopts_unrelated_work_a_worker_edited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-4/AC-5: disowning is not permanent exile. A file that changed since it
    was disowned was taken back deliberately, so it returns to the candidate."""
    repo, issue_id = _seed(tmp_path, "rec13")
    (repo / OPERATOR).write_text("was declared unrelated\n")
    journal = _stage_journal(
        repo, issue_id, phase="incomplete-candidate", paths=frozenset({OPERATOR})
    )
    store = JournalStore(repo)
    store.save(
        journal.with_handoff(repo=repo, paths={OPERATOR}).with_unrelated({OPERATOR})
    )
    # The next engineer edits exactly that file, which is how adoption is stated.
    (repo / OPERATOR).write_text("picked back up and finished\n")
    _install(monkeypatch, tmp_path, ScriptedRunner(verify=_pass_verdict))

    result = _grind(repo, "--tasks", "1")
    assert result.exit_code == 0, result.stdout + result.stderr

    assert "previously unrelated work was edited" in _log(repo)
    assert _issue(repo, issue_id)["status"] == "closed"
    assert OPERATOR in _committed_paths(repo)
    assert (repo / OPERATOR).read_text() == "picked back up and finished\n"


# ---------------------------------------------------------------------------
# ortus-9ljd — a resumed worker may not disown its own issue's candidate
# ---------------------------------------------------------------------------


def _shipped_and_declares(*declared: str) -> Callable[[Path, Path], int]:
    """An implementation worker that adds a candidate and disowns `declared`."""

    def implement(repo: Path, log_path: Path) -> int:
        (repo / CANDIDATE).write_text("SHIPPED = True\n")
        (repo / DECLARATION).parent.mkdir(parents=True, exist_ok=True)
        (repo / DECLARATION).write_text("".join(f"{path}\n" for path in declared))
        return 0

    return implement


def test_disown_same_issue_refused_and_the_inherited_work_still_lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-1: the resume exists to carry the prior attempt at this issue forward,
    so declaring that attempt unrelated is refused and it stays in the
    candidate — the alternative silently abandons the work being resumed."""
    repo, issue_id = _seed(tmp_path, "rec14")
    (repo / INHERITED).write_text("the prior attempt at this very issue\n")
    _stage_journal(
        repo, issue_id, phase="incomplete-candidate", paths=frozenset({INHERITED})
    )
    _install(
        monkeypatch,
        tmp_path,
        ScriptedRunner(
            implement=_shipped_and_declares(INHERITED), verify=_pass_verdict
        ),
    )

    result = _grind(repo, "--tasks", "1")
    assert result.exit_code == 0, result.stdout + result.stderr

    assert _issue(repo, issue_id)["status"] == "closed"
    assert {CANDIDATE, INHERITED} <= _committed_paths(repo), (
        "the resumed issue's own inherited work must stay in the candidate"
    )
    log = _log(repo)
    assert f"inherited work belonging to {issue_id}" in log
    assert INHERITED in log
    assert not (repo / DECLARATION).exists(), "the declaration is consumed"


def test_disown_foreign_issue_honored_when_nothing_attributes_it_here(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-2: disowning stays available for genuinely foreign leftovers. Work
    stranded by an issue that has since closed belongs to nobody here, so the
    worker's declaration is honored and the file is left untouched."""
    repo, stranding_id = _seed(tmp_path, "rec15")
    claimed_id = _create_issue(repo, "the issue this run resumes", priority="2")
    (repo / INHERITED).write_text("a file stranded by another issue\n")
    _stage_journal(
        repo, stranding_id, phase="incomplete-candidate", paths=frozenset({INHERITED})
    )
    subprocess.run(
        ["bd", "close", stranding_id], cwd=repo, check=True, capture_output=True
    )
    _claim(repo, claimed_id)
    _install(
        monkeypatch,
        tmp_path,
        ScriptedRunner(
            implement=_shipped_and_declares(INHERITED), verify=_pass_verdict
        ),
    )

    result = _grind(repo, "--tasks", "1")
    assert result.exit_code == 0, result.stdout + result.stderr

    assert _issue(repo, claimed_id)["status"] == "closed"
    committed = _committed_paths(repo)
    assert CANDIDATE in committed and INHERITED not in committed
    assert (repo / INHERITED).read_text() == "a file stranded by another issue\n"
    dirty = GitClient(repo=repo).dirty_paths()
    assert dirty is not None and INHERITED in dirty
    assert "declared unrelated" in _log(repo)


def test_disown_mixed_declaration_honors_only_the_foreign_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-3: one declaration, two kinds of path. The issue's own inherited
    candidate is refused; a file that went dirty after the journal was written
    is attributable to nobody, so that half of the declaration still stands."""
    repo, issue_id = _seed(tmp_path, "rec16")
    (repo / INHERITED).write_text("the prior attempt at this issue\n")
    _stage_journal(
        repo, issue_id, phase="incomplete-candidate", paths=frozenset({INHERITED})
    )
    # Landed after the journal recorded its candidate, so nothing attributes it.
    (repo / OPERATOR).write_text("someone else's stranded edit\n")
    _install(
        monkeypatch,
        tmp_path,
        ScriptedRunner(
            implement=_shipped_and_declares(OPERATOR, INHERITED),
            verify=_pass_verdict,
        ),
    )

    result = _grind(repo, "--tasks", "1")
    assert result.exit_code == 0, result.stdout + result.stderr

    assert _issue(repo, issue_id)["status"] == "closed"
    committed = _committed_paths(repo)
    assert {CANDIDATE, INHERITED} <= committed
    assert OPERATOR not in committed
    assert (repo / OPERATOR).read_text() == "someone else's stranded edit\n"
    log = _log(repo)
    assert f"inherited work belonging to {issue_id}" in log
    assert "declared unrelated" in log


def test_disown_refusal_is_not_fatal_and_costs_no_correction_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-4: a worker that misjudges ownership must not be able to end the run.
    Grind overrides the declaration, says so, and the same attempt carries on to
    a verdict — no halt, no extra worker, no correction spent."""
    repo, issue_id = _seed(tmp_path, "rec17")
    (repo / INHERITED).write_text("the prior attempt at this issue\n")
    _stage_journal(
        repo, issue_id, phase="incomplete-candidate", paths=frozenset({INHERITED})
    )
    backend = ScriptedRunner(
        implement=_shipped_and_declares(INHERITED), verify=_pass_verdict
    )
    _install(monkeypatch, tmp_path, backend)

    result = _grind(repo, "--tasks", "1")
    assert result.exit_code == 0, result.stdout + result.stderr

    implementations = [
        name for name, _ in backend.prompts if name == Phase.IMPLEMENT.value
    ]
    assert len(implementations) == 1, "the refusal must not trigger a correction"
    assert _issue(repo, issue_id)["status"] == "closed"
    assert JournalStore(repo).load() is None, "the transaction reached finalization"
    log = _log(repo)
    assert f"inherited work belonging to {issue_id}" in log
    assert "candidate left uncommitted" not in log


# ---------------------------------------------------------------------------
# ortus-8nzp — a disowned path is never candidate drift, on any route
# ---------------------------------------------------------------------------


STRANDED = "stranded-notes.txt"


def _resume_with_a_stranded_inherited_path(repo: Path, issue_id: str) -> None:
    """Set up a resume whose inherited work includes one foreign leftover.

    The journal attributes `INHERITED` to the claimed issue, so only `STRANDED`
    — dirty since before the resume and attributed to nobody — is disownable.
    """
    (repo / STRANDED).write_text("left behind by somebody else\n")
    (repo / INHERITED).write_text("the prior attempt at this issue\n")
    _stage_journal(
        repo, issue_id, phase="corrections-exhausted", paths=frozenset({INHERITED})
    )


def test_resume_with_disowned_reaches_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resumed run that disowns inherited work is judged on its merits.

    Grind honors the declaration and leaves the path dirty, so every later
    ownership re-check must subtract the same disowned set. One that does not
    sees the untouched leftover as a path the read-only verifier added, and
    rejects a sound verdict as candidate drift.
    """
    repo, issue_id = _seed(tmp_path, "rec18")
    _resume_with_a_stranded_inherited_path(repo, issue_id)
    _install(
        monkeypatch,
        tmp_path,
        ScriptedRunner(
            implement=_shipped_and_declares(STRANDED), verify=_pass_verdict
        ),
    )

    result = _grind(repo, "--tasks", "1")
    assert result.exit_code == 0, result.stdout + result.stderr

    log = _log(repo)
    assert "mutated the candidate" not in log, (
        "the disowned leftover must not read as verifier tampering"
    )
    assert "candidate path set changed" not in log
    assert _issue(repo, issue_id)["status"] == "closed"
    committed = _committed_paths(repo)
    assert {CANDIDATE, INHERITED} <= committed
    assert STRANDED not in committed
    assert (repo / STRANDED).read_text() == "left behind by somebody else\n"
    dirty = GitClient(repo=repo).dirty_paths()
    assert dirty is not None and STRANDED in dirty


def test_resume_with_disowned_still_rejects_a_real_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other direction: subtracting the disowned set must not blunt the
    guard. A verifier that genuinely edits the candidate is still rejected, and
    nothing is closed or committed."""
    repo, issue_id = _seed(tmp_path, "rec19")
    _resume_with_a_stranded_inherited_path(repo, issue_id)

    def verify(repo: Path, log_path: Path) -> int:
        (repo / CANDIDATE).write_text("SHIPPED = True\n# the verifier edited this\n")
        return _pass_verdict(repo, log_path)

    _install(
        monkeypatch,
        tmp_path,
        ScriptedRunner(implement=_shipped_and_declares(STRANDED), verify=verify),
    )

    result = _grind(repo, "--tasks", "1")
    assert result.exit_code == 0, result.stdout + result.stderr

    assert "mutated the candidate" in _log(repo)
    assert _issue(repo, issue_id)["status"] == "in_progress"
    assert CANDIDATE not in _committed_paths(repo)
    assert (repo / STRANDED).read_text() == "left behind by somebody else\n"


def test_legacy_codex_commit_never_takes_disowned_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The legacy `--condition` Codex route commits whatever its capture calls
    owned, so that capture must subtract the disowned set like every other
    ownership check — otherwise the one route that skips the verifier is also
    the one that commits work a worker declared was not its own."""
    repo, issue_id = _seed(tmp_path, "rec20")
    _resume_with_a_stranded_inherited_path(repo, issue_id)

    class ClosingCodex:
        extra_env: dict[str, str] = {}

        def run(
            self, prompt: str, *, repo: Path, log_path: Path, **kwargs: object
        ) -> int:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.touch(exist_ok=True)
            (repo / CANDIDATE).write_text("SHIPPED = True\n")
            (repo / DECLARATION).parent.mkdir(parents=True, exist_ok=True)
            (repo / DECLARATION).write_text(f"{STRANDED}\n")
            subprocess.run(
                ["bd", "close", issue_id, "--reason", "legacy codex completed it"],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            return 0

    _install(monkeypatch, tmp_path, ClosingCodex())

    result = _grind(
        repo,
        "--backend",
        "codex",
        "--condition",
        "Finish the claimed issue.",
        "--iterations",
        "1",
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    assert _issue(repo, issue_id)["status"] == "closed"
    # The owned commit is not HEAD here: a tracker-export checkpoint follows it.
    committed = set(
        subprocess.run(
            ["git", "log", "--name-only", "--format=", "-3"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()
    )
    assert {CANDIDATE, INHERITED} <= committed
    assert STRANDED not in committed
    assert (repo / STRANDED).read_text() == "left behind by somebody else\n"
    dirty = GitClient(repo=repo).dirty_paths()
    assert dirty is not None and STRANDED in dirty


# ---------------------------------------------------------------------------
# AC-6 — each failure phase resumes, and only real ambiguity needs a human
# ---------------------------------------------------------------------------


def test_failure_phase_malformed_verdict_resumes_and_then_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-6: a malformed verdict commits and closes nothing, and the next grind
    picks the same issue up and carries it to a verified close."""
    repo, issue_id = _seed(tmp_path, "rec9")

    def implement(repo: Path, log_path: Path) -> int:
        (repo / CANDIDATE).write_text("SHIPPED = True\n")
        return 0

    rejected = ScriptedRunner(implement=implement, verify=_malformed_verdict)
    _install(monkeypatch, tmp_path, rejected)
    first = _grind(repo)
    assert first.exit_code == 0, first.stdout + first.stderr
    assert _issue(repo, issue_id)["status"] == "in_progress"
    assert JournalStore(repo).load() is not None
    assert not _finalization_commits(repo, issue_id)

    resumed = ScriptedRunner(implement=implement, verify=_pass_verdict)
    _install(monkeypatch, tmp_path, resumed)
    second = _grind(repo, "--tasks", "1")
    assert second.exit_code == 0, second.stdout + second.stderr

    assert "RECOVERY HANDOFF" in resumed.prompt_for(Phase.IMPLEMENT)
    assert _issue(repo, issue_id)["status"] == "closed"
    assert len(_finalization_commits(repo, issue_id)) == 1
    assert CANDIDATE in _committed_paths(repo)
    assert JournalStore(repo).load() is None


def test_failure_phase_closed_issue_is_never_reopened_by_a_stale_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-6: a worker that closed its own issue against the phase contract leaves
    a journal that owes work on finished work. Resuming it would reopen a closed
    issue, so the uncommitted work becomes context for the next selection."""
    repo, closed_id = _seed(tmp_path, "rec14")
    fresh_id = _create_issue(repo, "the issue that should run", priority="0")
    (repo / INHERITED).write_text("edits the closed issue left behind\n")
    _stage_journal(
        repo, closed_id, phase="incomplete-candidate", paths=frozenset({INHERITED})
    )
    subprocess.run(
        ["bd", "close", closed_id, "--reason", "worker closed it itself"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    backend = ScriptedRunner()
    _install(monkeypatch, tmp_path, backend)

    result = _grind(repo)
    assert result.exit_code == 0, result.stdout + result.stderr

    assert _issue(repo, closed_id)["status"] == "closed", "no reopen"
    assert "is already closed; not resuming it" in _log(repo)
    implementation = backend.prompt_for(Phase.IMPLEMENT)
    assert f"Work bd issue {fresh_id}." in implementation
    assert f"Work bd issue {closed_id}." not in implementation
    assert "RECOVERY HANDOFF" in implementation, "the stray edits are still context"
    assert f"{closed_id} is already closed" in implementation, "why, as context"
    assert (repo / INHERITED).read_text() == "edits the closed issue left behind\n"


def test_failure_phase_ambiguous_claims_request_human_routing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-6: two claims and no journal is the one case grind cannot decide. It
    preserves everything, names the candidates, and asks for routing."""
    repo, issue_id = _seed(tmp_path, "rec10")
    second_id = _create_issue(repo, "second claimed issue")
    _claim(repo, issue_id)
    _claim(repo, second_id)
    (repo / INHERITED).write_text("work that could belong to either issue\n")

    class NeverRuns:
        extra_env: dict[str, str] = {}

        def run(self, *args: object, **kwargs: object) -> int:
            raise AssertionError("ambiguous routing must not spawn a worker")

    _install(monkeypatch, tmp_path, NeverRuns())

    result = _grind(repo)
    assert result.exit_code == 1, result.stdout + result.stderr

    log = _log(repo)
    assert "cannot be routed" in log
    assert issue_id in log and second_id in log
    assert (repo / INHERITED).read_text() == "work that could belong to either issue\n"
    for candidate in (issue_id, second_id):
        assert _issue(repo, candidate)["status"] == "in_progress"


# ---------------------------------------------------------------------------
# AC-7 — repeated handoffs stay idempotent
# ---------------------------------------------------------------------------


def test_idempotent_repeated_handoffs_add_no_duplicate_finalization_or_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-7: resuming the same failed transaction three times preserves the work
    once — no duplicate close, commit, finalization record, or disown entry."""
    repo, issue_id = _seed(tmp_path, "rec11")
    (repo / INHERITED).write_text("the prior attempt at this issue\n")
    _stage_journal(
        repo, issue_id, phase="incomplete-candidate", paths=frozenset({INHERITED})
    )
    # Outside the recorded candidate, so it stays the worker's to disown — the
    # issue's own inherited work is not declarable (ortus-9ljd).
    (repo / OPERATOR).write_text("operator work nobody owns\n")

    def implement(repo: Path, log_path: Path) -> int:
        (repo / CANDIDATE).write_text("STILL_WIP = True\n")
        (repo / DECLARATION).parent.mkdir(parents=True, exist_ok=True)
        (repo / DECLARATION).write_text(f"{OPERATOR}\n")
        return 0

    backend = ScriptedRunner(implement=implement, verify=_malformed_verdict)
    _install(monkeypatch, tmp_path, backend)
    baseline_subjects = _subjects(repo)

    for _ in range(3):
        result = _grind(repo)
        assert result.exit_code == 0, result.stdout + result.stderr

    journal = JournalStore(repo).load()
    assert journal is not None and journal.issue_id == issue_id
    assert journal.unrelated_paths == (OPERATOR,), "a repeat disown is not a duplicate"
    assert OPERATOR not in journal.candidate_paths
    assert INHERITED in journal.candidate_paths
    assert len(journal.handoffs) == 3
    assert _subjects(repo) == baseline_subjects
    assert _issue(repo, issue_id)["status"] == "in_progress"
    assert not any(
        FINALIZATION_MARKER in body for body in _comment_bodies(repo, issue_id)
    )
    assert (repo / OPERATOR).read_text() == "operator work nobody owns\n"
    assert (repo / CANDIDATE).read_text() == "STILL_WIP = True\n"
