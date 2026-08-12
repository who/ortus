"""Bounded correction retries after a failed verification (ortus-pzfd.5).

A failed verdict used to end the run: the candidate sat in the worktree and an
operator had to read the transcript to learn what to fix. These tests pin the
retry controller that replaced that dead end.

The controller's contract, in the order the loop applies it:

  1. The complete verifier report is persisted to the issue BEFORE a
     correction can spend an attempt, and the correction packet the fresh
     worker receives carries only the original issue, the current candidate
     hash, the failed criteria, and the precise findings.
  2. Every correction is followed by its OWN fresh verifier — a corrected
     candidate is never accepted on the strength of the attempt that produced
     it.
  3. Attempts are bounded. Exhaustion flags the issue for a human and leaves
     the candidate uncommitted and the issue unclosed.
  4. A finding that names an unresolved planning decision never reaches a
     correction worker at all: it routes once through the planning profile.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.commands import grind as grind_mod
from ortus.core import sandbox as sandbox_mod
from ortus.core.profiles import Phase
from ortus.core.sandbox import SandboxInfo
from ortus.core.transaction import JournalStore
from tests.conftest import copy_bd_workspace

pytestmark = [pytest.mark.integration, pytest.mark.slow]
runner = CliRunner()

CRITERIA = ("AC-1",)


def _fake_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sandbox_mod, "smoke_test", lambda: SandboxInfo(platform="Linux", binary="bwrap")
    )
    monkeypatch.setattr(
        sandbox_mod,
        "docker_precondition_check",
        lambda: SandboxInfo(platform="Linux", binary="docker"),
    )


def _seed(tmp_path: Path, name: str) -> tuple[Path, str]:
    """A bd workspace with one ready leaf, as a ~25ms template copy.

    The `leaf` template already carries the ready issue, the `main` branch, the
    fixture git identity, and the enabled .claude, so the whole setup costs a
    copy instead of a `bd init` plus a `bd create` (ortus-apmf).
    """
    workspace = copy_bd_workspace(tmp_path / name, "leaf")
    return workspace.path, workspace.issues[0]


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


def _comments(repo: Path, issue_id: str) -> str:
    return subprocess.run(
        ["bd", "comments", issue_id, "--json"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _grind_log(repo: Path) -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for path in (repo / "logs").glob("grind-*.log")
    )


class ScriptedRunner:
    """Fake backend that plays a fixed sequence of verifier verdicts.

    Implementation and correction phases both write a distinct candidate file
    so each attempt produces a genuinely different candidate hash — a
    correction that changed nothing would otherwise be indistinguishable from
    one the verifier never saw.
    """

    extra_env: dict[str, str] = {}

    def __init__(self, repo: Path, verdicts: list[dict]) -> None:
        self.repo = repo
        self.verdicts = verdicts
        self.prompts: list[tuple[str, str]] = []
        # bd comment count observed at the moment each phase started, so tests
        # can assert the failed report landed BEFORE the next correction.
        self.comments_at_start: list[tuple[str, int]] = []
        self._resolved_id: str | None = None

    def _issue_id(self) -> str:
        """The single fixture issue in this workspace, resolved from bd.

        Deliberately not parsed out of the prompt: bd prefixes contain digits
        and the prompts are full of hyphenated prose, so a token heuristic here
        silently resolved to "" and made `_comment_count` report 0 for every
        phase — which turned the AC-1 ordering assertion into a no-op.
        """
        if self._resolved_id is None:
            try:
                issues = json.loads(
                    subprocess.run(
                        ["bd", "list", "--all", "--json"],
                        cwd=self.repo,
                        check=True,
                        capture_output=True,
                        text=True,
                    ).stdout
                    or "[]"
                )
            except (subprocess.CalledProcessError, json.JSONDecodeError):
                issues = []
            self._resolved_id = next(
                (
                    str(issue["id"])
                    for issue in issues
                    if isinstance(issue, dict)
                    and issue.get("id")
                    and issue.get("issue_type") != "epic"
                ),
                "",
            )
        return self._resolved_id

    def _comment_count(self) -> int:
        issue_id = self._issue_id()
        assert issue_id, "fixture issue id must resolve; otherwise this check is vacuous"
        try:
            return len(json.loads(_comments(self.repo, issue_id) or "[]"))
        except (json.JSONDecodeError, subprocess.CalledProcessError):
            return 0

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
        if phase is Phase.IMPLEMENT:
            self.comments_at_start.append(("implement", self._comment_count()))
            index = sum(1 for name, _ in self.prompts if name == phase.value)
            (repo / f"candidate-{index}.py").write_text(f"ATTEMPT = {index}\n")
        elif phase is Phase.VERIFY:
            self._emit(repo, log_path)
        elif phase is Phase.PLAN:
            self.comments_at_start.append(("plan", self._comment_count()))
        return 0

    def _emit(self, repo: Path, log_path: Path) -> None:
        journal = JournalStore(repo).load()
        assert journal is not None
        spec = self.verdicts.pop(0) if self.verdicts else self.verdicts_default()
        payload = {
            "schema": 1,
            "candidate_hash": journal.candidate_hash,
            "decision": spec["decision"],
            "criteria": [
                {"id": name, "status": spec["decision"], "evidence": spec["evidence"]}
                for name in CRITERIA
            ],
            "commands": ["uv run pytest tests/test_grind_corrections.py -q"],
            "reviewed_files": ["candidate-1.py"],
            "reviewed_interfaces": ["ATTEMPT"],
            "risks": ["none"],
            "findings": list(spec["findings"]),
            "codegraph": ["fallback recorded"],
        }
        event = {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": "ORTUS_VERDICT: " + json.dumps(payload),
            },
        }
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")

    @staticmethod
    def verdicts_default() -> dict:
        return {
            "decision": "fail",
            "evidence": "still failing",
            "findings": ["src/demo.py:1 still returns the wrong value"],
        }


def _fail(evidence: str, *findings: str) -> dict:
    return {"decision": "fail", "evidence": evidence, "findings": list(findings)}


def _pass() -> dict:
    return {"decision": "pass", "evidence": "verified", "findings": ["none"]}


def _install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scripted: ScriptedRunner
) -> None:
    _fake_sandbox(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    monkeypatch.setattr(grind_mod, "_make_runner", lambda *a, **k: scripted)


# ---------------------------------------------------------------------------
# AC-1 — the failed report is durable before a correction is built
# ---------------------------------------------------------------------------


def test_failed_report_is_persisted_before_the_correction_packet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-1: the full verifier report reaches bd BEFORE the retry, and the
    correction packet carries the hash, failed criteria, and findings only."""
    repo, issue_id = _seed(tmp_path, "corr1")
    scripted = ScriptedRunner(
        repo,
        [
            _fail("AC-1 evidence: run() returned None", "src/demo.py:7 returns None"),
            _pass(),
        ],
    )
    _install(monkeypatch, tmp_path, scripted)

    result = runner.invoke(
        app,
        [
            "grind",
            str(repo),
            "--tasks",
            "1",
            "--idle-sleep",
            "0",
            "--max-corrections",
            "2",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    comments = _comments(repo, issue_id)
    assert "Ortus verifier report" in comments
    assert "Decision: **FAIL**" in comments
    assert "AC-1: fail" in comments

    correction = next(
        prompt
        for phase, prompt in scripted.prompts[1:]
        if phase == Phase.IMPLEMENT.value and "CORRECTION ATTEMPT" in prompt
    )
    assert "CORRECTION ATTEMPT 1" in correction
    assert issue_id in correction
    assert "AC-1: AC-1 evidence: run() returned None" in correction
    assert "src/demo.py:7 returns None" in correction
    assert "Current candidate SHA-256:" in correction
    # Minimal packet: the verifier transcript and the rendered report never
    # travel to the correction worker.
    assert "Ortus verifier report" not in correction
    assert "Files reviewed" not in correction

    # The report comment existed before the correction worker started.
    implement_counts = [n for name, n in scripted.comments_at_start if name == "implement"]
    assert implement_counts[0] == 0
    assert implement_counts[1] >= 1, (
        "the failed verifier report must be persisted before the correction runs"
    )


def test_failed_report_forbids_close_commit_and_push_in_the_correction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-1/AC-4: the correction packet grants no lifecycle authority."""
    repo, _issue_id = _seed(tmp_path, "corr2")
    scripted = ScriptedRunner(repo, [_fail("nope", "finding one"), _pass()])
    _install(monkeypatch, tmp_path, scripted)

    runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )

    correction = next(
        prompt for phase, prompt in scripted.prompts if "CORRECTION ATTEMPT" in prompt
    )
    lowered = correction.lower()
    assert "do not close the issue" in lowered
    assert "commit the correction on the issue branch" in lowered
    assert "git push" in lowered
    assert "ortus alone merges and finalizes" in lowered


# ---------------------------------------------------------------------------
# AC-2 — every correction gets a fresh verifier; retries are bounded
# ---------------------------------------------------------------------------


def test_retry_after_failure_reverifies_and_then_finalizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-2: fail → fresh correction → fresh verifier → pass → Ortus closes."""
    repo, issue_id = _seed(tmp_path, "corr3")
    scripted = ScriptedRunner(repo, [_fail("broken", "fix src/demo.py"), _pass()])
    _install(monkeypatch, tmp_path, scripted)

    result = runner.invoke(
        app,
        ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0", "--max-corrections", "2"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    phases = [phase for phase, _ in scripted.prompts]
    assert phases == [
        Phase.IMPLEMENT.value,
        Phase.VERIFY.value,
        Phase.IMPLEMENT.value,
        Phase.VERIFY.value,
        # No finalize-phase model run follows: the compose pass is retired
        # and finalization is deterministic.
    ], f"one fresh verifier per attempt; got {phases}"
    assert _issue(repo, issue_id)["status"] == "closed"
    assert JournalStore(repo).load() is None, "a finalized transaction clears itself"
    assert "correction attempt 1/2" in _grind_log(repo)


def test_retry_exhaustion_flags_a_human_and_commits_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-2: a candidate that never passes is escalated, never committed."""
    repo, issue_id = _seed(tmp_path, "corr4")
    scripted = ScriptedRunner(
        repo,
        [
            _fail("still broken", "finding A"),
            _fail("still broken", "finding A"),
        ],
    )
    _install(monkeypatch, tmp_path, scripted)

    result = runner.invoke(
        app,
        ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0", "--max-corrections", "1"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    issue = _issue(repo, issue_id)
    assert issue["status"] != "closed"
    assert "human" in issue.get("labels", [])
    combined = " ".join((result.stdout + result.stderr).split())
    assert "bounded correction attempts exhausted (1/1)" in combined
    assert "Ortus correction escalation" in _comments(repo, issue_id)
    journal = JournalStore(repo).load()
    assert journal is not None and journal.phase == "corrections-exhausted"
    assert journal.corrections == 1
    # Nothing was committed: the candidate is still an uncommitted worktree file.
    assert (repo / "candidate-2.py").is_file()
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=repo, capture_output=True, text=True
    ).stdout
    assert "candidate" not in log


def test_retry_exhaustion_prints_next_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ortus-ipyq AC-4: exhaustion names its issue, tells the truth about the
    candidate, and states the see-the-comment next action."""
    repo, issue_id = _seed(tmp_path, "corr8")
    scripted = ScriptedRunner(
        repo,
        [
            _fail("still broken", "finding A"),
            _fail("still broken", "finding A"),
        ],
    )
    _install(monkeypatch, tmp_path, scripted)

    result = runner.invoke(
        app,
        ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0", "--max-corrections", "1"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    console = " ".join((result.stdout + result.stderr).split())
    title = _issue(repo, issue_id)["title"]
    assert (
        f'work on "{title}" ({issue_id}) stopped: '
        "bounded correction attempts exhausted (1/1)." in console
    )
    assert "uncommitted edits preserved in the tree" in console
    assert (
        "Next: read the issue's newest comment, decide, and relabel it "
        "for the queue." in console
    )
    assert "1 awaiting retry" in console


def test_retry_disabled_escalates_on_the_first_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-2: --max-corrections 0 spends no attempt and never re-implements."""
    repo, issue_id = _seed(tmp_path, "corr5")
    scripted = ScriptedRunner(repo, [_fail("broken", "finding A")])
    _install(monkeypatch, tmp_path, scripted)

    result = runner.invoke(
        app,
        ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0", "--max-corrections", "0"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    phases = [phase for phase, _ in scripted.prompts]
    assert phases == [Phase.IMPLEMENT.value, Phase.VERIFY.value]
    assert _issue(repo, issue_id)["status"] != "closed"
    assert "bounded correction attempts exhausted (0/0)" in " ".join(
        (result.stdout + result.stderr).split()
    )


# ---------------------------------------------------------------------------
# AC-3 — plan gaps route once, and never to an implementation worker
# ---------------------------------------------------------------------------


def test_plan_gap_routes_once_to_the_planning_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-3: a planning gap consumes no correction attempt and reaches the
    planning profile exactly once before human escalation."""
    repo, issue_id = _seed(tmp_path, "corr6")
    scripted = ScriptedRunner(
        repo,
        [
            _fail(
                "AC-1 cannot be assessed",
                "PLAN-GAP: the packet never resolves whether retries are per-issue "
                "or per-attempt",
            )
        ],
    )
    _install(monkeypatch, tmp_path, scripted)

    result = runner.invoke(
        app,
        ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0", "--max-corrections", "3"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    phases = [phase for phase, _ in scripted.prompts]
    assert phases == [
        Phase.IMPLEMENT.value,
        Phase.VERIFY.value,
        Phase.PLAN.value,
    ], f"a plan gap must route to planning, not to another implementer; got {phases}"
    plan_prompt = next(
        prompt for phase, prompt in scripted.prompts if phase == Phase.PLAN.value
    )
    assert "PLAN-GAP ROUTING PASS" in plan_prompt
    assert issue_id in plan_prompt
    assert "do not edit source files" in plan_prompt.lower()

    issue = _issue(repo, issue_id)
    assert issue["status"] != "closed"
    assert "human" in issue.get("labels", [])
    journal = JournalStore(repo).load()
    assert journal is not None
    assert journal.plan_gap_routed is True
    assert journal.corrections == 0, "a plan gap must not spend a correction attempt"
    assert "routing once through the planning profile" in _grind_log(repo)


def test_plan_gap_routing_pass_failure_still_escalates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-3: a planning pass that cannot resolve the gap escalates to a human
    rather than falling back to implementation improvisation."""
    repo, issue_id = _seed(tmp_path, "corr7")

    class FailingPlan(ScriptedRunner):
        def run(self, prompt: str, *, profile: object, **kwargs: object) -> int:
            if profile.phase is Phase.PLAN:  # type: ignore[union-attr]
                self.prompts.append((Phase.PLAN.value, prompt))
                return 3
            return super().run(prompt, profile=profile, **kwargs)  # type: ignore[arg-type]

    scripted = FailingPlan(repo, [_fail("blocked", "plan gap: undecided schema")])
    _install(monkeypatch, tmp_path, scripted)

    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    assert "planning-gap routing pass failed (exit 3)" in " ".join(
        (result.stdout + result.stderr).split()
    )
    issue = _issue(repo, issue_id)
    assert issue["status"] != "closed"
    assert "human" in issue.get("labels", [])
    assert Phase.IMPLEMENT.value not in [
        phase for phase, prompt in scripted.prompts if "CORRECTION ATTEMPT" in prompt
    ]


# ---------------------------------------------------------------------------
# Machine-era corrections (ortus-l2u9.3): same-session return and the parked
# branch at the bound
# ---------------------------------------------------------------------------


def _machine_seed(tmp_path: Path, name: str) -> tuple[Path, str]:
    """A leaf workspace with a committed baseline, so the branch-scoped
    machine pipeline (not the legacy agent path) judges the candidate."""
    repo, issue_id = _seed(tmp_path, name)
    (repo / ".gitignore").write_text("logs/\n.cache/\n.beads/ortus.flock\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "fixture baseline"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo, issue_id


class _SessionWorker:
    """Worker that stamps a session id into its transcript and records every
    spawn's kwargs, so a test can assert what a correction was handed."""

    extra_env: dict[str, str] = {}

    def __init__(self, repo: Path, session_id: str = "sess-abc123") -> None:
        self.repo = repo
        self.session_id = session_id
        self.runs: list[dict[str, object]] = []

    def run(
        self,
        prompt: str,
        *,
        repo: Path,
        log_path: Path,
        readonly: bool = False,
        **kwargs: object,
    ) -> int:
        self.runs.append({"prompt": prompt, "readonly": readonly, **kwargs})
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps({"type": "system", "session_id": self.session_id}) + "\n"
            )
        attempt = sum(1 for run in self.runs if not run["readonly"])
        (repo / "candidate.py").write_text(f"ATTEMPT = {attempt}\n")
        journal = JournalStore(self.repo).load()
        assert journal is not None
        from tests._shims import post_completion_comment

        post_completion_comment(self.repo, journal.issue_id, {"AC-1": "pass"})
        return 0


def test_failure_returns_to_the_same_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-4 (ortus-l2u9.3): a failed round returns to the worker's own session
    — resumed by id, carrying the failing command and its output — instead of
    a fresh context that has never seen its previous attempt."""
    from tests._shims import install_machine_checks, machine_run

    repo, issue_id = _machine_seed(tmp_path, "same-session")
    worker = _SessionWorker(repo)
    _install(monkeypatch, tmp_path, worker)
    install_machine_checks(
        monkeypatch,
        sequence=[
            machine_run("fail", output="AssertionError: expected 2, got 1")
        ],
    )

    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    implement_runs = [run for run in worker.runs if not run["readonly"]]
    assert len(implement_runs) == 2, "one implementation, one correction"
    correction = implement_runs[1]
    assert correction.get("resume") == "sess-abc123"
    prompt = str(correction["prompt"])
    assert "CORRECTION ATTEMPT 1" in prompt
    assert "uv run pytest tests/test_grind.py -q" in prompt
    assert "AssertionError: expected 2, got 1" in prompt
    log = _grind_log(repo)
    assert "correction resumes worker session sess-abc123" in log
    assert _issue(repo, issue_id)["status"] == "closed"


def test_bound_parks_committed_with_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-5 (ortus-l2u9.3): at the retry bound the branch parks committed and
    the issue routes to a human with the pipeline record attached — a parked
    branch plus a diagnosis, never an abandoned dirty tree."""
    from tests._shims import install_machine_checks, machine_run

    repo, issue_id = _machine_seed(tmp_path, "parked-bound")
    worker = _SessionWorker(repo)
    _install(monkeypatch, tmp_path, worker)
    install_machine_checks(
        monkeypatch, default=machine_run("fail", output="still red")
    )

    result = runner.invoke(
        app,
        [
            "grind",
            str(repo),
            "--tasks",
            "1",
            "--idle-sleep",
            "0",
            "--max-corrections",
            "1",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    issue = _issue(repo, issue_id)
    assert issue["status"] == "in_progress"
    labels = issue.get("labels") or []
    assert "human" in [
        label if isinstance(label, str) else label.get("label") for label in labels
    ]
    branch = f"ortus/{issue_id}"
    committed = subprocess.run(
        ["git", "show", f"{branch}:candidate.py"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert committed.returncode == 0, "the candidate parks committed on its branch"
    comments = _comments(repo, issue_id)
    assert "Ortus correction escalation" in comments
    assert f"parked committed on `{branch}`" in comments
    assert "Verification records:" in comments
    assert ".md" in comments, "the pipeline record refs are attached"

