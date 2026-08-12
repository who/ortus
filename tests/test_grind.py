"""Integration-ish tests for ortus grind (xvel.4 acceptance)."""

from __future__ import annotations

import json
import platform
import re
import shutil
import subprocess
import textwrap
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.commands import grind as grind_mod
from ortus.core import claude as claude_mod
from ortus.core import output as output_mod
from ortus.core import sandbox as sandbox_mod
from ortus.core.claude import ClaudeRunner
from ortus.core.git import CommitResult, GitClient
from ortus.core.readiness import _REQUIRED_SECTIONS
from ortus.core.profiles import Phase
from ortus.core.sandbox import SandboxInfo
from ortus.core.transaction import JournalStore
from tests._platform import skip_unless_bwrap_usable
from tests._shims import (
    install_machine_checks,
    machine_run,
    make_inline_python_shim,
    post_completion_comment,
    shim_path,
)
from tests.conftest import copy_bd_workspace
from tests.test_readiness import ready_issue

runner = CliRunner()

FAKE_CLAUDE = shim_path("fake-claude")
pytestmark = pytest.mark.integration


def _fake_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sandbox_mod, "smoke_test", lambda: SandboxInfo(platform="Linux", binary="bwrap")
    )


def _fixture_repo(tmp_path: Path) -> Path:
    """Repo with .beads/ + .claude/settings.json with hooks enabled."""
    repo = tmp_path / "fixture"
    (repo / ".beads").mkdir(parents=True)
    settings = repo / ".claude" / "settings.json"
    settings.parent.mkdir(exist_ok=True)
    settings.write_text(json.dumps({"sandbox": {"excludedCommands": ["bd", "bd *"]}}))
    return repo


def _create_ready_issue(
    repo: Path, title: str, *, priority: str = "2", extra_description: str = ""
) -> str:
    packet = ready_issue()
    description = packet["description"] + extra_description
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
            "--description",
            description,
            "--design",
            packet["design"],
            "--acceptance",
            packet["acceptance_criteria"],
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _create_unready_issue(repo: Path, title: str, *, priority: str = "2") -> str:
    """A hand-authored leaf: real work, but no readiness schema v1 packet."""
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
            "--description",
            "make it work",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _bd_repo(tmp_path: Path, name: str) -> Path:
    """bd-initialized repo with hooks enabled, ready for a grind invocation.

    A ~25ms copy of the session's bare template rather than a ~1.6s `bd init`
    (ortus-apmf). The template already carries the `main` branch, the fixture
    git identity, and the sandbox settings every grind fixture wrote by hand.
    """
    return copy_bd_workspace(tmp_path / name, "bare").path


def _issue_ids(repo: Path) -> set[str]:
    listing = subprocess.run(
        ["bd", "list", "--all", "--json"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return {issue["id"] for issue in json.loads(listing or "[]")}


def _issue(repo: Path, issue_id: str) -> dict:
    shown = subprocess.run(
        ["bd", "show", issue_id, "--json"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return json.loads(shown)[0]


def _grind_log(repo: Path) -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for path in (repo / "logs").glob("grind-*.log")
    )


def _emit_verdict(
    repo: Path, log_path: Path, *, criteria: tuple[str, ...], decision: str = "pass"
) -> None:
    """Append the one passing verdict envelope a read-only verifier would emit."""
    journal = JournalStore(repo).load()
    assert journal is not None
    payload = {
        "schema": 1,
        "candidate_hash": journal.candidate_hash,
        "decision": decision,
        "criteria": [
            {"id": name, "status": decision, "evidence": "reviewed"}
            for name in criteria
        ],
        "commands": ["uv run pytest tests/test_grind.py -q"],
        "reviewed_files": ["src/demo.py"],
        "reviewed_interfaces": ["run"],
        "risks": ["none"],
        "findings": ["none"],
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


def _repair_packet_into(repo: Path, issue_id: str) -> None:
    """Stand in for what the repair subprocess does: update the packet in place."""
    packet = ready_issue()
    subprocess.run(
        [
            "bd",
            "update",
            issue_id,
            "--description",
            packet["description"],
            "--design",
            packet["design"],
            "--acceptance",
            packet["acceptance_criteria"],
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )


@pytest.mark.slow
def test_grind_repair_then_claim_repairs_an_unready_leaf_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-1 (ortus-xhrj.7): a queue whose only ready leaf fails readiness is
    repaired in place and then claimed, creating no new issue ids.

    Before this behavior grind logged a readiness skip, found nothing workable,
    and broke out of the outer loop — which reads to an operator as a grind
    failure rather than as an authoring defect in one packet.
    """
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    repo = _bd_repo(tmp_path, "rpair")
    issue_id = _create_unready_issue(repo, "hand authored leaf", priority="1")
    ids_before = _issue_ids(repo)
    phases: list[Phase] = []

    class RepairingClaude:
        extra_env: dict[str, str] = {}

        def run(
            self,
            prompt: str,
            *,
            repo: Path,
            log_path: Path,
            profile: object,
            **kwargs: object,
        ) -> int:
            phases.append(profile.phase)  # type: ignore[union-attr]
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.touch(exist_ok=True)
            if profile.phase is Phase.PLAN:  # type: ignore[union-attr]
                assert "READINESS REPAIR PASS" in prompt
                assert f"Repair ONLY these existing issue IDs: {issue_id}" in prompt
                # Grind has no PRD, so the pass is grounded in the packet itself.
                assert "which has no PRD" in prompt
                _repair_packet_into(repo, issue_id)
            elif profile.phase is Phase.VERIFY:  # type: ignore[union-attr]
                # The verifier is read-only: it emits a verdict rather than
                # closing. Ortus finalizes on the strength of that verdict.
                _emit_verdict(repo, log_path, criteria=("AC-1", "AC-2"))
            return 0

    _fake_sandbox(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    monkeypatch.setattr(grind_mod, "_make_runner", lambda: RepairingClaude())

    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    # The repair ran on the planning profile, ahead of the implement/verify
    # pair. That ordering is what this test is about.
    assert phases[:3] == [Phase.PLAN, Phase.IMPLEMENT, Phase.VERIFY]
    # Finalization's commit-message pass is allowed to decline: it raises
    # before spawning when the candidate diff is empty, and this queue's worker
    # writes no code, so whether a diff exists at all is incidental to the
    # repair behavior under test and has differed by platform. The pass has its
    # own coverage in tests/test_core_compose.py and test_grind_finalization.py.
    assert phases[3:] in ([], [Phase.FINALIZE])
    assert _issue_ids(repo) == ids_before, "repair must update in place, not create"
    assert JournalStore(repo).load() is None, "the passing candidate is finalized"
    assert _issue(repo, issue_id)["status"] == "closed"
    log_text = _grind_log(repo)
    assert "readiness repair pass 1/2" in log_text
    assert f"readiness repair: {issue_id} now passes readiness" in log_text
    assert f"harness selected+claimed {issue_id}" in log_text
    comments = subprocess.run(
        ["bd", "comments", issue_id, "--json"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "readiness repair pass" in comments


@pytest.mark.slow
def test_grind_repair_opt_out_restores_skip_and_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-2: --no-repair-unready spawns nothing and leaves the leaf open."""
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    repo = _bd_repo(tmp_path, "optout")
    issue_id = _create_unready_issue(repo, "hand authored leaf", priority="1")

    class NeverRuns:
        extra_env: dict[str, str] = {}

        def run(self, prompt: str, **kwargs: object) -> int:
            raise AssertionError("--no-repair-unready must not spawn any subprocess")

    _fake_sandbox(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    monkeypatch.setattr(grind_mod, "_make_runner", lambda: NeverRuns())

    result = runner.invoke(
        app,
        [
            "grind",
            str(repo),
            "--no-repair-unready",
            "--tasks",
            "1",
            "--idle-sleep",
            "0",
        ],
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    assert _issue(repo, issue_id)["status"] == "open"
    combined = result.stdout + result.stderr
    assert "readiness repair disabled by --no-repair-unready" in combined
    log_text = _grind_log(repo)
    # The pre-existing skip line keeps its format so log tailing is unaffected.
    assert "readiness skip (left open for planning/human repair)" in log_text
    assert "no ready issue to claim (queue blocked or human-only)" in log_text


@pytest.mark.slow
def test_grind_repair_budget_exhausted_prints_diagnostics_and_follow_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-3: once the per-run budget is spent, grind stops as it does today but
    names each per-issue failure and the exact follow-up command.

    A grind run now owns exactly one candidate transaction (the loop stops at a
    verdict, because finalization belongs to the dependent lifecycle issue), so
    exhaustion is exercised with a spent-on-arrival budget over two unready
    leaves rather than across two iterations.
    """
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    repo = _bd_repo(tmp_path, "budget")
    first = _create_unready_issue(repo, "first hand authored leaf", priority="1")
    second = _create_unready_issue(repo, "second hand authored leaf", priority="1")
    repairs: list[str] = []

    class NoPassClaude:
        extra_env: dict[str, str] = {}

        def run(self, prompt: str, **kwargs: object) -> int:
            repairs.append(prompt)
            raise AssertionError("a spent repair budget must not spawn a subprocess")

    _fake_sandbox(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    monkeypatch.setattr(grind_mod, "_make_runner", lambda: NoPassClaude())

    result = runner.invoke(
        app, ["grind", str(repo), "--repair-budget", "0", "--idle-sleep", "0"]
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    assert repairs == [], "an exhausted budget must not be spent anyway"
    assert _issue(repo, first)["status"] == "open"
    assert _issue(repo, second)["status"] == "open"
    combined = result.stdout + result.stderr
    assert "readiness repair budget exhausted (0/0 pass(es) used)" in combined
    # Rich hard-wraps long lines mid-token, so compare whitespace-free.
    squashed = re.sub(r"\s+", "", combined)
    assert (
        re.sub(r"\s+", "", f'skipped "first hand authored leaf" ({first})')
        in squashed
    )
    assert (
        re.sub(r"\s+", "", f'skipped "second hand authored leaf" ({second})')
        in squashed
    )
    assert re.sub(r"\s+", "", f"follow-up: bd update {second}") in squashed
    assert re.sub(r"\s+", "", f"then re-run: ortus grind {repo}") in squashed


@pytest.mark.slow
def test_grind_readiness_warning_dedupes_per_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-3: the console warns once per issue per run; the log keeps every
    occurrence. A repair pass that fixes nothing forces a second selection
    pass over the same unready leaf inside one run."""
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    repo = _bd_repo(tmp_path, "dedupe")
    issue_id = _create_unready_issue(repo, "hand authored leaf", priority="1")

    class NoFixClaude:
        extra_env: dict[str, str] = {}

        def run(self, prompt: str, *, log_path: Path, **kwargs: object) -> int:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.touch(exist_ok=True)
            return 0

    _fake_sandbox(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    monkeypatch.setattr(grind_mod, "_make_runner", lambda: NoFixClaude())

    result = runner.invoke(app, ["grind", str(repo), "--idle-sleep", "0"])

    assert result.exit_code == 0, result.stdout + result.stderr
    log_text = _grind_log(repo)
    assert (
        log_text.count("readiness skip (left open for planning/human repair)") == 2
    ), "the log must record every occurrence"
    squashed = re.sub(r"\s+", "", result.stdout + result.stderr)
    total = len(_REQUIRED_SECTIONS)
    skip_line = re.sub(
        r"\s+",
        "",
        f'skipped "hand authored leaf" ({issue_id}) — no readiness work spec '
        f"({total} of {total} sections missing)",
    )
    # Once as the warn, once in the exit listing — a second warn would make 3.
    assert squashed.count(skip_line) == 2
    assert squashed.count(re.sub(r"\s+", "", "It stays open and unclaimed.")) == 1


@pytest.mark.slow
def test_grind_repair_prompt_keeps_full_enumeration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-4: the console summary does not leak into the repair pass — its
    prompt still carries the section-by-section work order."""
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    repo = _bd_repo(tmp_path, "renum")
    issue_id = _create_unready_issue(repo, "hand authored leaf", priority="1")
    prompts: list[str] = []

    class NoFixClaude:
        extra_env: dict[str, str] = {}

        def run(self, prompt: str, *, log_path: Path, **kwargs: object) -> int:
            prompts.append(prompt)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.touch(exist_ok=True)
            return 0

    _fake_sandbox(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    monkeypatch.setattr(grind_mod, "_make_runner", lambda: NoFixClaude())

    result = runner.invoke(app, ["grind", str(repo), "--idle-sleep", "0"])

    assert result.exit_code == 0, result.stdout + result.stderr
    assert len(prompts) == 1, "exactly one repair pass runs for one unready leaf"
    prompt = prompts[0]
    assert "READINESS REPAIR PASS" in prompt
    assert (
        f"{issue_id}: description/objective: missing, empty, or placeholder section"
        in prompt
    )
    assert prompt.count("missing, empty, or placeholder section") == len(
        _REQUIRED_SECTIONS
    )


@pytest.mark.slow
def test_grind_queue_blocked_exit_uses_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-5: the run's exit explanation names the issue at summary altitude and
    keeps the follow-up command; the fifteen-clause wall stays in the log."""
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    repo = _bd_repo(tmp_path, "exitsum")
    issue_id = _create_unready_issue(repo, "hand authored leaf", priority="1")

    class NeverRuns:
        extra_env: dict[str, str] = {}

        def run(self, prompt: str, **kwargs: object) -> int:
            raise AssertionError("--no-repair-unready must not spawn any subprocess")

    _fake_sandbox(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    monkeypatch.setattr(grind_mod, "_make_runner", lambda: NeverRuns())

    result = runner.invoke(
        app, ["grind", str(repo), "--no-repair-unready", "--idle-sleep", "0"]
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    squashed = re.sub(r"\s+", "", result.stdout + result.stderr)
    total = len(_REQUIRED_SECTIONS)
    assert (
        re.sub(
            r"\s+",
            "",
            f'readiness: skipped "hand authored leaf" ({issue_id}) — '
            f"no readiness work spec ({total} of {total} sections missing)",
        )
        in squashed
    )
    assert re.sub(r"\s+", "", f"follow-up: bd update {issue_id}") in squashed
    # The enumeration's clauses stay off the console but in the log.
    assert re.sub(r"\s+", "", "description/behavioral context:") not in squashed
    assert "description/behavioral context: missing" in _grind_log(repo)


def test_grind_dry_run_prints_resolved_flags_and_exits(
    tmp_path: Path,
) -> None:
    """Dry-run path: no sandbox/hook/flock work; just emit the resolved state."""
    repo = _fixture_repo(tmp_path)
    result = runner.invoke(app, ["grind", str(repo), "--dry-run", "--tasks", "1"])
    assert result.exit_code == 0
    assert "repo:" in result.stdout
    assert "tasks:" in result.stdout
    assert "/goal" in result.stdout


def test_codex_dry_run_uses_plain_prompt(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path)
    (repo / ".ortusrc").write_text('backend = "codex"\n')
    result = runner.invoke(app, ["grind", str(repo), "--dry-run"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "backend:        codex" in result.stdout
    prompt = result.stdout.split("--- per-iteration prompt ---", 1)[1]
    assert "Work bd issue" in prompt
    assert "/goal" not in prompt


def test_dry_run_reports_independent_profiles(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path)
    (repo / ".ortusrc").write_text(
        '[profiles.claude.implement]\nmodel = "sonnet"\n'
        '[profiles.claude.verify]\nreasoning_effort = "high"\n'
    )
    result = runner.invoke(app, ["grind", str(repo), "--dry-run"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "claude/implement (model=sonnet, effort=provider-default)" in result.stdout
    assert "claude/verify (model=provider-default, effort=high)" in result.stdout


@pytest.mark.slow
def test_grind_routes_phase_profiles_and_fast_only_to_implementation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _bd_repo(tmp_path, "profile-routing")
    issue_id = _create_ready_issue(repo, "route profiles")
    (repo / ".ortusrc").write_text(
        '[profiles.claude.implement]\nmodel = "sonnet"\n'
        '[profiles.claude.verify]\nmodel = "opus"\n'
    )
    calls: list[dict[str, object]] = []

    class RoutingRunner:
        extra_env: dict[str, str] = {}

        def run(self, prompt: str, *, log_path: Path, **kwargs: object) -> int:
            calls.append(kwargs)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.touch(exist_ok=True)
            profile = kwargs["profile"]
            if profile.phase is Phase.VERIFY:  # type: ignore[union-attr]
                subprocess.run(
                    ["bd", "close", issue_id, "--reason", "verified"],
                    cwd=repo,
                    check=True,
                    capture_output=True,
                )
            return 0

    _fake_sandbox(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    monkeypatch.setattr(grind_mod, "_make_runner", lambda: RoutingRunner())
    result = runner.invoke(
        app, ["grind", str(repo), "--fast", "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert [call["profile"].phase for call in calls] == [  # type: ignore[union-attr]
        Phase.IMPLEMENT,
        Phase.VERIFY,
    ]
    assert [call["fast"] for call in calls] == [True, False]


# Marked slow rather than optimized (ortus-6ur4). Profiling the setup below in
# isolation puts it at 3.4s (bd init 1.3s, branch + bd create 1.1s, the git
# baseline commit 1.0s) against 17-26s for a whole case, so setup is under a
# fifth of the cost and the grind run itself is the rest. Only the bd init and
# create pair could be shared across cases — each case mutates its repo, so the
# baseline commit stays per-case — which on the CI numbers that failed the gate
# (5.14s to 8.07s) would still leave the slowest case around 7s. Cutting the
# remainder means cutting grind's own subprocess traffic, i.e. changing the
# finalization code this test exists to pin. A slow marker keeps every case in
# the CI gate — this module is already `integration`, so `-m "fast or
# integration"` still selects it — and only waives the 5s hermetic budget,
# which is precisely what pyproject documents the marker for.
@pytest.mark.slow
@pytest.mark.parametrize(
    "mutation,expected_phase,expected_text",
    [
        ("none", "verified-pass", "Decision: **PASS**"),
        ("content", "verification-rejected", "mutated the candidate"),
        ("new-path", "verification-rejected", "mutated the candidate path set"),
        ("timeout", "verification-timeout", "timed out"),
        ("nonzero", "verification-rejected", "exited with status 9"),
        (
            "implementation-branch-switch",
            "implementation-rejected",
            "left its issue branch",
        ),
        (
            "implementation-packet",
            "implementation-rejected",
            "work-spec artifact changed during implementation",
        ),
    ],
)
def test_verifier_report_and_mutation_isolation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    expected_phase: str,
    expected_text: str,
) -> None:
    repo = _bd_repo(tmp_path, mutation)
    issue_id = _create_ready_issue(repo, "verify candidate transaction")
    (repo / ".gitignore").write_text("logs/\n.cache/\n.beads/ortus.flock\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "fixture baseline"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    # The read-only agent verifier and its mutation guard are the reviewer
    # step now — on by flag, judging only after a green machine pipeline.
    _enable_reviewer(repo)
    primary = repo
    calls = 0

    class TransactionRunner:
        extra_env: dict[str, str] = {}

        def run(
            self,
            prompt: str,
            *,
            repo: Path,
            log_path: Path,
            readonly: bool = False,
            **kwargs: object,
        ) -> int:
            nonlocal calls
            calls += 1
            log_path.parent.mkdir(parents=True, exist_ok=True)
            if not readonly:
                (repo / "candidate.py").write_text("VALUE = 1\n")
                log_path.touch(exist_ok=True)
                _post_claims(primary)
                if mutation == "implementation-branch-switch":
                    # The forbidden move: carrying the work off the issue
                    # branch. Committing on the branch itself is the
                    # deliverable now; committing anywhere else strands it.
                    # Hooks are disabled for the simulated switch so the test
                    # pins the branch-switch rejection itself — beads ≥1.0.4
                    # ships a post-checkout hook whose tracker side effects
                    # would otherwise trip the lifecycle-state rejection first.
                    subprocess.run(
                        [
                            "git",
                            "-c",
                            "core.hooksPath=/dev/null",
                            "checkout",
                            "-b",
                            "rogue",
                        ],
                        cwd=repo,
                        check=True,
                        capture_output=True,
                    )
                    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
                    subprocess.run(
                        ["git", "commit", "-m", "worker commit"],
                        cwd=repo,
                        check=True,
                        capture_output=True,
                    )
                elif mutation == "implementation-packet":
                    journal = JournalStore(primary).load()
                    assert journal is not None
                    (primary / journal.issue_packet_ref).write_bytes(
                        b'{"id":"forged"}'
                    )
                return 0
            assert mutation not in {
                "implementation-branch-switch",
                "implementation-packet",
            }
            assert calls == 2
            journal = JournalStore(primary).load()
            assert journal is not None
            if mutation == "timeout":
                raise subprocess.TimeoutExpired("fake-verifier", 1)
            if mutation == "content":
                (repo / "candidate.py").write_text("VALUE = 2\n")
            elif mutation == "new-path":
                (repo / "verifier-created.py").write_text("MUTATED = True\n")
            payload = {
                "schema": 1,
                "candidate_hash": journal.candidate_hash,
                "decision": "pass",
                "criteria": [
                    {"id": "AC-1", "status": "pass", "evidence": "reviewed"},
                    {"id": "AC-2", "status": "pass", "evidence": "reviewed"},
                ],
                "commands": ["uv run pytest tests/test_grind.py -q"],
                "reviewed_files": ["candidate.py"],
                "reviewed_interfaces": ["VALUE"],
                "risks": ["candidate mutation"],
                "findings": ["none"],
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
            return 9 if mutation == "nonzero" else 0

    _fake_sandbox(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    monkeypatch.setattr(grind_mod, "_make_runner", lambda: TransactionRunner())
    install_machine_checks(
        monkeypatch, default=machine_run(criteria=("AC-1", "AC-2"))
    )

    result = runner.invoke(app, ["grind", str(repo), "--tasks", "1"])

    assert result.exit_code == 0, result.stdout + result.stderr
    journal = JournalStore(repo).load()
    comments = subprocess.run(
        ["bd", "comments", issue_id, "--json"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert expected_text in comments
    status = json.loads(
        subprocess.run(
            ["bd", "show", issue_id, "--json"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )[0]["status"]
    if expected_phase == "verified-pass":
        # The untampered candidate passes, so Ortus finalizes it in the same
        # iteration: the journal is consumed and the issue is closed by the
        # parent, never by either agent.
        assert journal is None, "a finalized transaction clears its journal"
        assert status == "closed"
    else:
        assert journal is not None and journal.phase == expected_phase
        assert status == "in_progress", "a rejected candidate keeps its claim"


@skip_unless_bwrap_usable
def test_verification_can_execute_a_trivial_command_on_this_host() -> None:
    """ortus-dyio AC-1: the read-only verifier posture still runs commands.

    Exercises the real wrapper, not a stand-in: the failure this covers was a
    posture that launched fine and then could not open a shell, which only a
    genuine sandbox launch reproduces.
    """
    system = platform.system()
    if system not in {"Linux", "Darwin"}:
        pytest.skip(f"no read-only verifier posture on {system}")
    if system == "Linux" and shutil.which("bwrap") is None:
        pytest.skip("bubblewrap not installed")
    ClaudeRunner().preflight_readonly(Path.cwd())


@skip_unless_bwrap_usable
def test_verification_preflight_catches_an_unwritable_agent_scratch_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe asserts on what the agent CLI needs, not just on `echo`.

    A bare `echo ok` succeeded throughout the incident; what failed was the
    CLI's own per-session directory write, so that is what the probe drives.
    """
    if platform.system() != "Linux" or shutil.which("bwrap") is None:
        pytest.skip("bubblewrap posture required")
    home = tmp_path / "home"
    (home / ".claude" / "session-env").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    # The pre-fix posture: a read-only root with no tmpfs over the scratch dirs.
    monkeypatch.setattr(claude_mod, "_agent_scratch_tmpfs", lambda _home: [])

    with pytest.raises(claude_mod.ReadOnlyExecutionBlocked) as caught:
        ClaudeRunner().preflight_readonly(tmp_path)

    message = str(caught.value)
    assert claude_mod.PREFLIGHT_PROBE in message
    assert "Read-only file system" in message
    assert str(home / ".claude" / "session-env") in message


@skip_unless_bwrap_usable
def test_verification_preflight_covers_the_repo_agent_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ortus-v8fn: the repo agent dir is writable whether or not it exists.

    Under the old posture a repo with no `.claude/` was unrecoverable: bwrap
    cannot mount a tmpfs on a missing directory under a read-only root, so the
    inner sandbox could never create its placeholders. Binding the repo writable
    removes the whole condition — the CLI makes the directory itself. The probe
    still covers the path, so a repo that genuinely cannot be written is caught.
    """
    if platform.system() != "Linux" or shutil.which("bwrap") is None:
        pytest.skip("bubblewrap posture required")
    # No agent scratch dirs under this $HOME, so the repo is the only target.
    (tmp_path / "home").mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))

    without = tmp_path / "no-agent-dir"
    (without / "src").mkdir(parents=True)
    ClaudeRunner().preflight_readonly(without)

    with_dir = tmp_path / "with-agent-dir"
    (with_dir / ".claude").mkdir(parents=True)
    ClaudeRunner().preflight_readonly(with_dir)

    # A probe has no business leaving anything behind in either case.
    for repo in (without, with_dir):
        assert not (repo / ".claude" / claude_mod._PREFLIGHT_SCRATCH).exists()


def test_candidate_paths_exclude_tool_state_written_during_review() -> None:
    """ortus-v8fn: the inverted posture lets the inner sandbox write for real.

    A repo that does not ignore `<repo>/.claude/hooks` reports the placeholder as
    untracked, which moved the candidate path set and had the mutation guard
    reject a sound verdict — observed on two repos. Tool state is carved out for
    the same reason the bd exports are: written by the machinery, never code
    under test.
    """
    dirty = frozenset(
        {
            "src/app.py",
            "tests/test_app.py",
            ".claude/hooks",
            ".git/config.lock",
            ".gitconfig",
            ".beads/issues.jsonl",
            "docs/pre-existing.md",
        }
    )
    baseline = frozenset({"docs/pre-existing.md"})

    assert grind_mod._candidate_paths(dirty, baseline) == frozenset(
        {"src/app.py", "tests/test_app.py"}
    )
    # A path that merely starts with a tool-state name is still candidate content.
    assert grind_mod._candidate_paths(frozenset({".gitignore-ish/x"}), frozenset()) == (
        frozenset({".gitignore-ish/x"})
    )


def _blocked_verifier_grind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> tuple[object, Path, str, list[str]]:
    """Run one grind whose reviewer preflight reports a blocked sandbox.

    The read-only preflight belongs to the agent reviewer, a default-off
    configured step these tests turn on; the machine pipeline judges first
    and is scripted green so the reviewer leg is actually reached.
    """
    repo = _bd_repo(tmp_path, name)
    issue_id = _create_ready_issue(repo, "candidate awaiting a working verifier")
    (repo / ".gitignore").write_text("logs/\n.cache/\n.beads/ortus.flock\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "fixture baseline"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    _enable_reviewer(repo)
    primary = repo
    prompts: list[str] = []

    class BlockedVerifierRunner:
        extra_env: dict[str, str] = {}

        def run(
            self,
            prompt: str,
            *,
            repo: Path,
            log_path: Path,
            readonly: bool = False,
            **kwargs: object,
        ) -> int:
            assert not readonly, "the verifier must not launch once the probe fails"
            prompts.append(prompt)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.touch(exist_ok=True)
            (repo / "candidate.py").write_text("VALUE = 1\n")
            _post_claims(primary)
            return 0

        def preflight_readonly(self, repo: Path, **kwargs: object) -> None:
            raise claude_mod.ReadOnlyExecutionBlocked(
                f"{claude_mod.PREFLIGHT_PROBE} failed: mkdir: cannot create "
                "directory: Read-only file system\n"
                "  agent session-env: /home/nobody/.claude/session-env"
            )

    _fake_sandbox(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    monkeypatch.setattr(grind_mod, "_make_runner", lambda: BlockedVerifierRunner())
    install_machine_checks(
        monkeypatch, default=machine_run(criteria=("AC-1", "AC-2"))
    )

    result = runner.invoke(app, ["grind", str(repo), "--tasks", "1"])
    return result, repo, issue_id, prompts


# Marked slow rather than optimized: the probe launches a real bwrap sandbox,
# so the cost is process setup this test exists to exercise, not work it could
# skip. Measured 5.58s on a CI runner and 15.23s on a loaded developer host,
# either side of the 5s hermetic budget. The module is already `integration`,
# so `-m "fast or integration"` still selects it; the marker waives only the
# budget, exactly as it does for test_verifier_report_and_mutation_isolation.
@pytest.mark.slow
def test_verification_preflight_aborts_the_run_naming_the_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ortus-dyio AC-2: a sandbox that cannot execute stops the run outright."""
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    result, repo, issue_id, _ = _blocked_verifier_grind(
        tmp_path, monkeypatch, "preflightabort"
    )

    assert result.exit_code == 1, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert claude_mod.PREFLIGHT_PROBE in combined
    assert "Read-only file system" in combined
    assert "session-env" in combined
    log = _grind_log(repo)
    assert f"HALT — {claude_mod.PREFLIGHT_PROBE} failed" in log
    assert _issue(repo, issue_id)["status"] == "open", "the issue stays claimable"


@pytest.mark.slow  # real bwrap launch; see the note above
def test_blocked_verification_spends_no_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ortus-dyio AC-3: no correction attempt, no plan-gap route, no commit."""
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    result, repo, issue_id, prompts = _blocked_verifier_grind(
        tmp_path, monkeypatch, "noblockedbudget"
    )

    assert result.exit_code == 1, result.stdout + result.stderr
    journal = JournalStore(repo).load()
    assert journal is not None, "the candidate transaction is preserved"
    assert journal.corrections == 0
    assert journal.plan_gap_routed is False
    # The machine pipeline's own green record is journaled; what the abort
    # must not have produced is an agent verdict.
    assert len(journal.verifier_refs) <= 1
    # One implementation worker ran; no correction or plan-gap worker followed.
    assert len(prompts) == 1, prompts
    comments = subprocess.run(
        ["bd", "comments", issue_id, "--json"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "Ortus correction escalation" not in comments
    assert "ORTUS_VERDICT" not in comments
    main_log = subprocess.run(
        ["git", "log", "--oneline", "main"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert (
        "fixture: enable the reviewer flag" in main_log.splitlines()[0]
    ), f"the abort must not land anything on main: {main_log}"


def test_large_issue_uses_bounded_claude_goal_and_full_codex_packet() -> None:
    issue = {
        "id": "demo-large",
        "title": "Thoroughly planned change",
        "description": "implementation detail " * 600,
        "design": "design detail " * 600,
        "acceptance_criteria": "acceptance detail " * 600,
    }
    template = grind_mod.read_work_issue_condition()

    claude_prompt = grind_mod._compose_work_prompt(template, issue, "claude")
    assert claude_prompt.startswith("/goal Work bd issue demo-large")
    assert "bd show demo-large --json" in claude_prompt
    assert len(claude_prompt.removeprefix("/goal ")) <= 4_000
    assert issue["description"] not in claude_prompt

    codex_prompt = grind_mod._compose_work_prompt(template, issue, "codex")
    assert not codex_prompt.startswith("/goal")
    assert issue["description"].strip() in codex_prompt
    assert issue["acceptance_criteria"].strip() in codex_prompt


def test_claude_goal_rejection_is_detected_only_in_requested_log_slice(
    tmp_path: Path,
) -> None:
    log = tmp_path / "grind.log"
    log.write_text('{"type":"result","num_turns":1,"result":"ok"}\n')
    offset = log.stat().st_size
    rejection = "Goal condition is limited to 4000 characters (got 7523)"
    with log.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps({"type": "result", "num_turns": 0, "result": rejection}) + "\n"
        )

    assert grind_mod._claude_goal_rejection(log, start_offset=offset) == rejection
    assert (
        grind_mod._claude_goal_rejection(log, start_offset=log.stat().st_size) is None
    )


@pytest.mark.slow
def test_codex_rejects_implementation_worker_that_closes_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _bd_repo(tmp_path, "codex-loop")
    for number in range(3):
        _create_ready_issue(repo, f"task {number}")
    (repo / ".ortusrc").write_text('backend = "codex"\n')
    (repo / ".codex").mkdir()
    (repo / ".codex" / "config.toml").write_text('sandbox_mode = "workspace-write"\n')
    with (repo / ".gitignore").open("a") as fh:
        fh.write("\nlogs/\n.cache/\n.beads/ortus.flock\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "test fixture baseline"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    prompts: list[str] = []
    primary = repo

    class ClosingCodex:
        extra_env: dict[str, str] = {}

        def run(
            self, prompt: str, *, repo: Path, log_path: Path, **kwargs: object
        ) -> int:
            prompts.append(prompt)
            assert not prompt.startswith("/goal")
            assert "Do NOT invoke `ortus grind`" in prompt
            match = re.search(r"Work bd issue ([^\.\s]+)\.", prompt)
            assert match
            subprocess.run(
                ["bd", "close", match.group(1), "--reason", "fake codex completed it"],
                cwd=primary,
                check=True,
                capture_output=True,
            )
            marker = repo / "codex-worker-output.txt"
            prior = marker.read_text() if marker.exists() else ""
            marker.write_text(prior + match.group(1) + "\n")
            return 0

    _fake_sandbox(monkeypatch)
    monkeypatch.setattr(
        grind_mod, "_make_runner", lambda backend="claude": ClosingCodex()
    )
    result = runner.invoke(
        app,
        ["grind", str(repo), "--backend", "codex", "--idle-sleep", "0"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert len(prompts) == 1
    journal = JournalStore(repo).load()
    assert journal is not None
    assert journal.phase == "implementation-rejected"
    commits = subprocess.run(
        ["git", "log", "--format=%s"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert sum("complete Codex grind task" in subject for subject in commits) == 0
    in_progress = subprocess.run(
        ["bd", "list", "--status", "in_progress", "--json"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    assert len(json.loads(in_progress.stdout)) == 1


def test_dry_run_startup_under_500ms(tmp_path: Path) -> None:
    """NFR-002: startup overhead ≤ 500ms (measured via --dry-run as a proxy)."""
    repo = _fixture_repo(tmp_path)
    t0 = time.monotonic()
    result = runner.invoke(app, ["grind", str(repo), "--dry-run"])
    elapsed = time.monotonic() - t0
    assert result.exit_code == 0
    assert elapsed < 0.5, (
        f"grind --dry-run took {elapsed * 1000:.0f}ms (NFR-002 budget: 500ms)"
    )


def test_grind_exits_one_on_missing_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _fixture_repo(tmp_path)

    def _boom() -> None:
        raise sandbox_mod.SandboxUnavailable(
            "Sandbox prerequisite missing: bubblewrap (bwrap)\n  hint"
        )

    monkeypatch.setattr(sandbox_mod, "smoke_test", _boom)
    result = runner.invoke(app, ["grind", str(repo)])
    assert result.exit_code == 1
    assert "bubblewrap" in (result.stdout + result.stderr)


def test_grind_exits_one_on_disabled_hooks_before_claude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance #3: disableAllHooks=true → exit 1 BEFORE any claude spawn."""
    repo = _fixture_repo(tmp_path)
    (repo / ".claude" / "settings.json").write_text(
        json.dumps(
            {"disableAllHooks": True, "sandbox": {"excludedCommands": ["bd", "bd *"]}}
        )
    )
    _fake_sandbox(monkeypatch)
    # Force home so the user's real ~/.claude isn't checked.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))

    # If claude DID spawn, our test would hang waiting on the fake-claude shim.
    # So make _make_runner raise to assert it's never called.
    def _should_not_be_called() -> ClaudeRunner:
        raise AssertionError("claude was spawned despite disableAllHooks=true")

    monkeypatch.setattr(grind_mod, "_make_runner", _should_not_be_called)

    result = runner.invoke(app, ["grind", str(repo)])
    assert result.exit_code == 1
    assert "disableAllHooks" in (result.stdout + result.stderr) or "hooks" in (
        result.stdout + result.stderr
    )


@pytest.mark.slow
def test_grind_runs_fake_claude_and_logs_locally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Smoke: with a fake claude that exits 0, grind runs one iteration and writes a log.

    Updated for ortus-3ico subprocess-per-task shape: the loop now spawns
    one claude per iteration, so we seed a single ready issue and cap with
    --iterations 1 --idle-sleep 0 so the fake-claude (which doesn't touch
    bd) doesn't trigger an infinite no-change retry.
    """
    repo = _bd_repo(tmp_path, "fixture")
    # Seed one ready issue so queue_drained() doesn't short-circuit before
    # claude is spawned.
    _create_ready_issue(repo, "smoke task")

    _fake_sandbox(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    monkeypatch.setattr(
        grind_mod, "_make_runner", lambda: ClaudeRunner(claude_binary=str(FAKE_CLAUDE))
    )

    result = runner.invoke(
        app,
        ["grind", str(repo), "--iterations", "1", "--idle-sleep", "0"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    log_dir = repo / "logs"
    assert log_dir.is_dir()
    logs = list(log_dir.glob("grind-*.log"))
    assert logs, "expected a grind-*.log under logs/"
    # The fake-claude shim writes "fake-claude done" to its stdout, which gets
    # tee'd to log_path by ClaudeRunner.run.
    assert any("fake-claude done" in p.read_text(encoding="utf-8") for p in logs)


@pytest.mark.slow
def test_grind_harness_selects_claims_and_injects_issue_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ortus-xo1u: the harness (not the worker) selects + claims the next ready
    issue and injects its EXACT id into the per-iteration /goal prompt.

    With a fake claude that echoes its argv but never touches bd, we can assert
    the worker was handed the specific id the harness claimed — proving the
    worker is TOLD which issue to work rather than choosing/transcribing it.
    """
    repo = _bd_repo(tmp_path, "fixture")
    issue_id = _create_ready_issue(repo, "inject me", priority="1")
    assert issue_id, "expected bd create to print the new id"

    _fake_sandbox(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    monkeypatch.setattr(
        grind_mod, "_make_runner", lambda: ClaudeRunner(claude_binary=str(FAKE_CLAUDE))
    )

    result = runner.invoke(
        app,
        ["grind", str(repo), "--iterations", "1", "--idle-sleep", "0"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    logs = list((repo / "logs").glob("grind-*.log"))
    assert logs
    log_text = "\n".join(p.read_text(encoding="utf-8") for p in logs)
    # The harness logged the in-harness select+claim of the EXACT id...
    assert f"harness selected+claimed {issue_id}" in log_text
    # ...and the worker's prompt (echoed by fake-claude's argv) carried that id.
    assert f"Work bd issue {issue_id}" in log_text


@pytest.mark.slow
def test_claude_goal_rejection_restores_claim_and_halts_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _bd_repo(tmp_path, "goal-rejection")
    issue_id = _create_ready_issue(
        repo,
        "oversized planned issue",
        priority="1",
        extra_description="\n" + "thorough implementation packet " * 300,
    )

    rejection = "Goal condition is limited to 4000 characters (got 7523)"
    shim = make_inline_python_shim(
        tmp_path,
        "claude-goal-rejection",
        textwrap.dedent(
            f"""\
            import json
            print(json.dumps({{
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "num_turns": 0,
                "result": {rejection!r},
            }}), flush=True)
            """
        ),
    )
    _fake_sandbox(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    monkeypatch.setattr(
        grind_mod, "_make_runner", lambda: ClaudeRunner(claude_binary=str(shim))
    )

    result = runner.invoke(
        app,
        ["grind", str(repo), "--iterations", "5", "--idle-sleep", "0"],
    )

    assert result.exit_code == 1, result.stdout + result.stderr
    assert "rejected the /goal condition" in result.stderr
    issue = json.loads(
        subprocess.run(
            ["bd", "show", issue_id, "--json"],
            cwd=str(repo),
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )[0]
    assert issue["status"] == "open"
    log = sorted((repo / "logs").glob("grind-*.log"))[-1].read_text(encoding="utf-8")
    assert log.count("spawning claude") == 1
    assert "HALT — Claude rejected /goal before running a worker turn" in log
    assert "WARN orphan claim" not in log


def test_grind_dry_run_default_shows_harness_select(tmp_path: Path) -> None:
    """Default (no --condition) dry-run advertises harness-side selection and
    the work-issue template with its placeholders intact."""
    repo = _fixture_repo(tmp_path)
    result = runner.invoke(app, ["grind", str(repo), "--dry-run"])
    assert result.exit_code == 0
    assert "select:" in result.stdout
    assert "harness" in result.stdout
    assert "<ISSUE_ID>" in result.stdout


def test_grind_fr003_no_beads(tmp_path: Path) -> None:
    bogus = tmp_path / "no-beads"
    bogus.mkdir()
    result = runner.invoke(app, ["grind", str(bogus)])
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# Claim-path branch safety (ortus-mfyu): the 2026-08-11 stranded-branch race
# ---------------------------------------------------------------------------


def _baseline_commit(repo: Path) -> None:
    (repo / ".gitignore").write_text("logs/\n.cache/\n.beads/ortus.flock\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "fixture baseline"],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _enable_reviewer(repo: Path) -> None:
    """Turn the agent reviewer on, committed so the flag never reads as work."""

    config = repo / ".ortusrc"
    existing = config.read_text(encoding="utf-8") if config.exists() else ""
    config.write_text(existing + "\nreviewer = true\n", encoding="utf-8")
    subprocess.run(["git", "add", ".ortusrc"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "fixture: enable the reviewer flag"],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _post_claims(repo: Path, criteria: tuple[str, ...] = ("AC-1", "AC-2")) -> None:
    """Post the claims-bearing completion comment a finished worker leaves."""

    journal = JournalStore(repo).load()
    if journal is not None and journal.issue_id:
        post_completion_comment(
            repo, journal.issue_id, {name: "pass" for name in criteria}
        )


class _PassingWorker:
    """Implementation writes one candidate file; the verifier passes it."""

    extra_env: dict[str, str] = {}

    def __init__(self, repo: Path) -> None:
        self.repo = repo

    def run(
        self,
        prompt: str,
        *,
        repo: Path,
        log_path: Path,
        readonly: bool = False,
        **kwargs: object,
    ) -> int:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.touch(exist_ok=True)
        if readonly:
            _emit_verdict(self.repo, log_path, criteria=("AC-1", "AC-2"))
        else:
            (self.repo / "candidate.py").write_text("VALUE = 1\n")
        return 0


def _run_claim_with_failing_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    *,
    preexisting_branch: bool,
) -> tuple[Path, str, object]:
    repo = _bd_repo(tmp_path, name)
    issue_id = _create_ready_issue(repo, "claim that cannot check out")
    _baseline_commit(repo)
    if preexisting_branch:
        subprocess.run(
            ["git", "branch", f"ortus/{issue_id}", "main"], cwd=repo, check=True
        )
    _fake_sandbox(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    monkeypatch.setattr(grind_mod, "_make_runner", lambda: _PassingWorker(repo))
    monkeypatch.setattr(
        "ortus.core.git.GitClient.checkout_reporting",
        lambda self, branch: (
            "simulated: local changes clash" if branch.startswith("ortus/") else ""
        ),
    )
    result = runner.invoke(app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"])
    return repo, issue_id, result


@pytest.mark.slow
def test_failed_claim_deletes_its_own_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-3: a claim that created its branch and then failed removes it and
    reverts the claim."""
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    repo, issue_id, result = _run_claim_with_failing_checkout(
        tmp_path, monkeypatch, "mfyu3", preexisting_branch=False
    )
    assert result.exit_code == 1
    gone = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/ortus/{issue_id}"],
        cwd=repo,
        capture_output=True,
    )
    assert gone.returncode != 0, "the claim-created branch must be removed"
    assert _issue(repo, issue_id)["status"] == "open"


@pytest.mark.slow
def test_failed_claim_keeps_a_reused_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-4: a pre-existing branch is never deleted by a failed claim."""
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    repo, issue_id, result = _run_claim_with_failing_checkout(
        tmp_path, monkeypatch, "mfyu4", preexisting_branch=True
    )
    assert result.exit_code == 1
    kept = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/ortus/{issue_id}"],
        cwd=repo,
        capture_output=True,
    )
    assert kept.returncode == 0, "a reused branch survives the failed claim"
    assert _issue(repo, issue_id)["status"] == "open"


@pytest.mark.slow
def test_checkout_blocker_carries_git_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-5: the halt names what git said, not just that a checkout failed."""
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    repo, _issue_id, result = _run_claim_with_failing_checkout(
        tmp_path, monkeypatch, "mfyu5", preexisting_branch=False
    )
    assert result.exit_code == 1
    combined = " ".join((result.stdout + result.stderr).split()) + _grind_log(repo)
    assert "simulated: local changes clash" in combined


def _stranded_claim_repo(tmp_path: Path) -> tuple[Path, object, bytes]:
    """A git repo manufactured into the observed strand: the tree on a prior
    issue's branch whose committed exports diverge from main, newer export
    bytes staged, and a journal owning that prior issue."""
    from ortus.core.git import GitClient
    from ortus.core.transaction import CandidateJournal

    repo = tmp_path / "strand"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "ortus-tests@example.invalid"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Ortus Tests"], cwd=repo, check=True)
    exports = repo / ".beads" / "issues.jsonl"
    exports.parent.mkdir()
    exports.write_text("baseline\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "baseline"], cwd=repo, check=True, capture_output=True
    )
    base = subprocess.run(
        ["git", "rev-parse", "main"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    subprocess.run(["git", "checkout", "-qb", "ortus/tmpl-strand"], cwd=repo, check=True)
    exports.write_text("baseline\nstranded-commit\n")
    subprocess.run(["git", "commit", "-aqm", "stranded exports"], cwd=repo, check=True)
    tip = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    newest = b"baseline\nstranded-commit\nnewest-staged\n"
    exports.write_bytes(newest)
    subprocess.run(["git", "add", ".beads/issues.jsonl"], cwd=repo, check=True)
    journal = CandidateJournal.start(
        repo=repo, issue_id="tmpl-strand", base_head=base, baseline_paths=()
    ).with_branch("ortus/tmpl-strand", tip)
    return repo, journal, newest


def test_claim_after_plan_gap_with_diverged_exports(tmp_path: Path) -> None:
    """ortus-mfyu AC-1/AC-2: claiming a different issue across the strand
    reasserts main, carries the newest export bytes, and cuts the new branch
    at the integration head."""
    from ortus.core.git import GitClient

    repo, journal, newest = _stranded_claim_repo(tmp_path)
    git = GitClient(repo)
    lines: list[str] = []

    branch, blocker, resumed = grind_mod._prepare_issue_branch(
        git,
        issue_id="tmpl-next",
        integration_branch="main",
        journal=journal,
        write_log=lines.append,
    )

    assert blocker == ""
    assert resumed is False
    assert branch == "ortus/tmpl-next"
    assert git.current_branch() == "ortus/tmpl-next"
    assert (repo / ".beads" / "issues.jsonl").read_bytes() == newest
    assert git.branch_tip("ortus/tmpl-next") == git.branch_tip("main")
    # The stranded branch keeps its commit untouched.
    assert git.branch_tip("ortus/tmpl-strand") == journal.branch_head


def test_tolerance_only_for_the_journals_own_issue(tmp_path: Path) -> None:
    """ortus-mfyu AC-6: a different issue's claim ends the stranded branch's
    hold via reassert; the journal's own issue resumes its branch by checkout,
    commits and all — the keystone's durable-home promise."""
    from ortus.core.git import GitClient

    repo, journal, newest = _stranded_claim_repo(tmp_path)
    git = GitClient(repo)
    lines: list[str] = []

    # Same-issue resume: checkout, never a reset, never a refusal.
    branch, blocker, resumed = grind_mod._prepare_issue_branch(
        git,
        issue_id="tmpl-strand",
        integration_branch="main",
        journal=journal,
        write_log=lines.append,
    )
    assert blocker == ""
    assert resumed is True
    assert branch == "ortus/tmpl-strand"
    assert git.current_branch() == "ortus/tmpl-strand"
    assert git.branch_tip("ortus/tmpl-strand") == journal.branch_head
    assert any("resumed existing ortus/tmpl-strand" in line for line in lines)

    # Different-issue claim from the same strand: the hold ends by reassert.
    lines.clear()
    branch, blocker, resumed = grind_mod._prepare_issue_branch(
        git,
        issue_id="tmpl-other",
        integration_branch="main",
        journal=journal,
        write_log=lines.append,
    )
    assert blocker == ""
    assert resumed is False
    assert git.current_branch() == "ortus/tmpl-other"
    assert any(
        "reasserted main (exports carried) before claiming" in line for line in lines
    )
    assert (repo / ".beads" / "issues.jsonl").read_bytes() == newest


# --- console milestones (ortus-kawu) -----------------------------------------
#
# The console narrates the run at executive altitude — claim with title,
# verdicts with the short candidate hash, corrections, landings with a running
# tally — while healthy CodeGraph plumbing narrates only to the log. Blockers
# and escalations always reach the console.


def _squashed_console(result: object) -> str:
    """stderr with rich's soft-wrapping collapsed, so assertions survive the
    80-column test console."""
    return " ".join((result.stderr or "").split())  # type: ignore[attr-defined]


def _narrated_grind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    name: str,
    title: str = "narrated",
    decisions: tuple[str, ...] = ("pass",),
    max_corrections: int | None = None,
) -> tuple[Path, str, object, list[str]]:
    """One harness-claimed run whose machine pipeline emits `decisions` in order.

    Returns the repo, the claimed issue id, the CliRunner result, and the
    candidate hash the journal held at each verification, so tests can pin the
    console's short-hash rendering to the real value.
    """
    repo = _bd_repo(tmp_path, name)
    issue_id = _create_ready_issue(repo, title)
    _baseline_commit(repo)

    hashes: list[str] = []
    decisions_left = list(decisions)
    impl_runs = [0]
    primary = repo

    class _NarratingRunner:
        extra_env: dict[str, str] = {}

        def run(
            self,
            prompt: str,
            *,
            repo: Path,
            log_path: Path,
            readonly: bool = False,
            **kwargs: object,
        ) -> int:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.touch(exist_ok=True)
            if not readonly:
                impl_runs[0] += 1
                (repo / "candidate.py").write_text(f"VALUE = {impl_runs[0]}\n")
                _post_claims(primary)
            return 0

    def scripted_checks(
        repo_path: Path, acceptance: object, ref: str, **kwargs: object
    ) -> object:
        journal = JournalStore(repo).load()
        assert journal is not None
        hashes.append(journal.candidate_hash)
        decision = decisions_left.pop(0) if decisions_left else "pass"
        return machine_run(decision, criteria=("AC-1", "AC-2"), ref=str(ref))

    _fake_sandbox(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    monkeypatch.setattr(grind_mod, "_make_runner", lambda: _NarratingRunner())
    monkeypatch.setattr(grind_mod, "_run_machine_checks", scripted_checks)
    args = ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    if max_corrections is not None:
        args += ["--max-corrections", str(max_corrections)]
    result = runner.invoke(app, args)
    return repo, issue_id, result, hashes


@pytest.mark.slow
def test_grind_console_prints_claim_with_title(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-1: the claim milestone leads with the issue title, id in parens."""
    _, issue_id, result, _ = _narrated_grind(
        tmp_path, monkeypatch, name="claim", title="narrate the run"
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    console = _squashed_console(result)
    assert f'claimed "narrate the run" ({issue_id}) — implementing' in console


@pytest.mark.slow
def test_grind_console_prints_verdict_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-2: the verdict line carries the decision and the 12-char hash,
    mirroring the log so the two channels correlate."""
    repo, _, result, hashes = _narrated_grind(tmp_path, monkeypatch, name="verdict")
    assert result.exit_code == 0, result.stdout + result.stderr
    assert hashes, "the machine pipeline never ran"
    console = _squashed_console(result)
    assert (
        "verdict: PASS — machine checks passed 2/2 criteria, claims agree "
        f"(candidate {hashes[-1][:12]}) after" in console
    )
    assert f"candidate={hashes[-1]}" in _grind_log(repo)


@pytest.mark.slow
def test_grind_console_prints_tally_and_finalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-3: correction attempts, the landing, and the running tally all
    reach the console."""
    _, issue_id, result, hashes = _narrated_grind(
        tmp_path, monkeypatch, name="tally", decisions=("fail", "pass")
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    console = _squashed_console(result)
    assert (
        "verdict: FAIL — machine checks passed 0/2 criteria, claims disagree "
        f"(candidate {hashes[0][:12]})" in console
    )
    assert f"correction attempt 1/2 for {issue_id}" in console
    assert f"landed {issue_id} on main — 1 done this run, 0 open" in console


@pytest.mark.slow
def test_grind_blockers_print_verbatim_on_console(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-6: quiet applies to health, never to trouble — an escalation's own
    words reach the console."""
    _, _, result, _ = _narrated_grind(
        tmp_path,
        monkeypatch,
        name="blocker",
        decisions=("fail",),
        max_corrections=0,
    )
    console = _squashed_console(result)
    assert "bounded correction attempts exhausted (0/0)" in console
    assert "candidate left uncommitted" not in console


def test_guard_backstop_push_announces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-3 (ortus-m1sj): the branch-guard's backstop push announces ref,
    remote, and commit range in the same register as finalization's push."""
    import io

    from rich.console import Console

    repo = tmp_path / "guard-push"
    repo.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    _git("init", "-b", "main")
    _git("config", "user.email", "ortus-tests@example.invalid")
    _git("config", "user.name", "Ortus Tests")
    (repo / "base.py").write_text("BASE = True\n")
    _git("add", "-A")
    _git("commit", "-m", "baseline")
    bare = tmp_path / "guard-push-origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare)],
        check=True,
        capture_output=True,
    )
    _git("remote", "add", "origin", str(bare))
    _git("push", "-u", "origin", "main")
    # The scenario the backstop exists for: a closed issue's commit sits on
    # local main while origin/main is still at the baseline.
    (repo / "closed.py").write_text("CLOSED = True\n")
    _git("add", "-A")
    _git("commit", "-m", "closed work")
    git = GitClient(repo)
    old, new = git.remote_tip("main"), git.branch_tip("main")

    err_buf = io.StringIO()
    monkeypatch.setattr(
        output_mod, "_err", Console(file=err_buf, force_terminal=False)
    )
    lines: list[str] = []
    grind_mod._enforce_branch_discipline(git, "main", lines.append, phase="post-close")

    console = " ".join(err_buf.getvalue().split())
    assert f"pushing main → origin ({old[:7]}..{new[:7]}, 1 commit)" in console
    assert "pushed main → origin" in console
    assert git.local_ahead_of_remote("main") == 0
    assert any("(pushed)" in line for line in lines), lines


def _verdictless_grind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, name: str, title: str
) -> tuple[Path, str, object]:
    """One harness-claimed run whose verification cannot produce a judgment.

    The machine-era analog of the silent verifier: the capture commit the
    pipeline needs before it can judge is refused, so verification fails with
    a named cause and the work stays uncommitted in the tree.
    """
    repo = _bd_repo(tmp_path, name)
    issue_id = _create_ready_issue(repo, title)
    _baseline_commit(repo)

    class _Worker:
        extra_env: dict[str, str] = {}

        def run(
            self,
            prompt: str,
            *,
            repo: Path,
            log_path: Path,
            readonly: bool = False,
            **kwargs: object,
        ) -> int:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.touch(exist_ok=True)
            if not readonly:
                (repo / "candidate.py").write_text("VALUE = 1\n")
            return 0

    _fake_sandbox(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    monkeypatch.setattr(grind_mod, "_make_runner", lambda: _Worker())
    monkeypatch.setattr(
        GitClient,
        "commit_paths",
        lambda self, paths, message: CommitResult(
            ok=False, command="commit", returncode=1, detail="hook refused: lint"
        ),
    )
    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    return repo, issue_id, result


@pytest.mark.slow
def test_verdictless_failure_names_issue_and_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ortus-ipyq AC-1: one message with the title and id, the truthful
    candidate state, and the re-run next action — never the old double print."""
    _, issue_id, result = _verdictless_grind(
        tmp_path, monkeypatch, name="verdictless", title="silent verifier"
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    console = _squashed_console(result)
    assert (
        f'verification of "silent verifier" ({issue_id}) failed: '
        "could not commit uncommitted candidate paths for judgment" in console
    )
    assert (
        "Its work is safe — uncommitted edits preserved in the tree — "
        "and the claim is kept." in console
    )
    assert (
        "Next: run `ortus grind` again; it resumes this issue at "
        "verification with a fresh verifier." in console
    )
    assert "verifier rejected candidate" not in console
    assert "candidate left uncommitted" not in console


def test_candidate_state_phrasing_is_computed() -> None:
    """ortus-ipyq AC-3: candidate-state phrasing derives from the journal and
    the repository — branch commits, dirty paths, both, or nothing."""
    from ortus.core.transaction import CandidateJournal

    class _FakeGit:
        def __init__(self, tip: str, dirty: frozenset[str] | None) -> None:
            self._tip = tip
            self._dirty = dirty

        def is_git_repo(self) -> bool:
            return True

        def branch_exists(self, name: str) -> bool:
            return True

        def branch_tip(self, name: str) -> str:
            return self._tip

        def dirty_paths(self) -> frozenset[str] | None:
            return self._dirty

    journal = CandidateJournal(
        issue_id="x-1",
        base_head="base",
        baseline_paths=(),
        baseline_fingerprints={},
        candidate_paths=("a.py",),
        issue_branch="ortus/x-1",
    )
    phrase = grind_mod._candidate_state_phrase
    assert phrase(_FakeGit("tip2", frozenset()), journal) == "committed on ortus/x-1"
    assert (
        phrase(_FakeGit("base", frozenset({"a.py"})), journal)
        == "uncommitted edits preserved in the tree"
    )
    assert phrase(_FakeGit("tip2", frozenset({"a.py"})), journal) == (
        "committed on ortus/x-1, with further uncommitted edits "
        "preserved in the tree"
    )
    assert phrase(_FakeGit("base", frozenset()), journal) == "no changes were made"
    assert phrase(_FakeGit("base", frozenset()), None) == "no changes were made"


@pytest.mark.slow
def test_exit_line_accounts_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ortus-ipyq AC-5: the exit line speaks operator — landed / awaiting
    retry / open in words, plus the next action while work awaits retry."""
    _, _, result = _verdictless_grind(
        tmp_path, monkeypatch, name="exitline", title="exit accounting"
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    console = _squashed_console(result)
    assert "done — 0 landed this session, 1 awaiting retry, 0 open" in console
    assert (
        "next: run `ortus grind` again; it resumes this issue at "
        "verification with a fresh verifier" in console
    )
    assert "done; closed" not in console


@pytest.mark.slow
@pytest.mark.parametrize("healthy", [True, False], ids=["healthy", "no-handshake"])
def test_grind_healthy_codegraph_lines_are_log_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, healthy: bool
) -> None:
    """AC-4: healthy handshake/refresh/summary lines narrate to the log only;
    a missing handshake still earns its console fallback line."""
    from ortus.core.codegraph import CodeGraphProbe

    repo = _bd_repo(tmp_path, f"cg-{'healthy' if healthy else 'fallback'}")
    _create_ready_issue(repo, "codegraph narration")
    _baseline_commit(repo)
    # The verification-phase agent narration only exists when the reviewer
    # step runs; the machine pipeline itself spawns no agent to narrate.
    _enable_reviewer(repo)
    primary = repo

    cg_event = {
        "type": "item.completed",
        "item": {
            "id": "cg1",
            "type": "mcp_tool_call",
            "server": "codegraph",
            "tool": "codegraph_explore",
            "arguments": {"query": "orientation"},
            "result": "symbols: run",
        },
    }

    class _NarratingRunner:
        extra_env: dict[str, str] = {}

        def run(
            self,
            prompt: str,
            *,
            repo: Path,
            log_path: Path,
            readonly: bool = False,
            **kwargs: object,
        ) -> int:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            if healthy:
                with log_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(cg_event) + "\n")
            else:
                log_path.touch(exist_ok=True)
            if readonly:
                _emit_verdict(primary, log_path, criteria=("AC-1", "AC-2"))
            else:
                (repo / "candidate.py").write_text("VALUE = 1\n")
                _post_claims(primary)
            return 0

    class _AvailableCodeGraph:
        def probe(self, repo: Path, mode: object, *, backend: str = "claude") -> object:
            return CodeGraphProbe(mode, True, True, True)

        def refresh(self, repo: Path, probe: object) -> tuple[str, int]:
            return ("fresh", 3)

    _fake_sandbox(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    monkeypatch.setattr(grind_mod, "_make_runner", lambda: _NarratingRunner())
    monkeypatch.setattr(grind_mod, "_make_codegraph", lambda: _AvailableCodeGraph())
    install_machine_checks(
        monkeypatch, default=machine_run(criteria=("AC-1", "AC-2"))
    )

    result = runner.invoke(app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"])
    assert result.exit_code == 0, result.stdout + result.stderr
    console = _squashed_console(result)
    log_text = _grind_log(repo)

    # Healthy plumbing never prints to the console...
    assert "CodeGraph handshake requested" not in console
    assert "CodeGraph handshake succeeded" not in console
    assert "refreshing CodeGraph index" not in console
    assert "CodeGraph phase summary" not in console
    # ...but stays in the log, the complete record (AC-7).
    assert "implementation CodeGraph handshake requested" in log_text
    assert "verification CodeGraph handshake requested" in log_text
    assert "refreshing CodeGraph index before verification" in log_text
    assert "CodeGraph phase summary" in log_text
    if healthy:
        assert "implementation CodeGraph handshake succeeded" in log_text
        assert "verification CodeGraph handshake succeeded" in log_text
        assert "CodeGraph fallback" not in console
    else:
        # Trouble still prints: the missing handshake earns its console line.
        assert (
            "implementation CodeGraph fallback: agent MCP capability handshake "
            "not observed" in console
        )


# ---------------------------------------------------------------------------
# prior lessons — a failed tracker read degrades to no lessons (ortus-s0tj)
# ---------------------------------------------------------------------------


def test_failed_lesson_read_degrades_to_no_lessons(tmp_path: Path) -> None:
    """AC-6: a tracker read that fails must not fail the run — the worker
    starts on today's contract, and the log says why."""
    from ortus.core.bd import BdClient

    # A bd binary that cannot be executed makes the read raise, without
    # mocking bd: the subprocess itself fails to launch.
    client = BdClient(tmp_path, binary=str(tmp_path / "missing-bd"))
    lines: list[str] = []
    section = grind_mod._lessons_contract(client, lines.append)
    assert section == ""
    assert any(
        "lessons" in line and "without stored lessons" in line for line in lines
    ), lines


def test_failed_lesson_read_degrades_on_bd_error(tmp_path: Path) -> None:
    """A bd exit failure (not just a missing binary) degrades the same way,
    and the log line stays single-line even though BdError carries stderr."""
    from ortus.core.bd import BdClient

    fake_bd = tmp_path / "failing-bd"
    fake_bd.write_text("#!/bin/sh\necho 'boom' >&2\nexit 3\n")
    fake_bd.chmod(0o755)
    client = BdClient(tmp_path, binary=str(fake_bd))
    lines: list[str] = []
    section = grind_mod._lessons_contract(client, lines.append)
    assert section == ""
    assert len(lines) == 1
    assert "\n" not in lines[0]


# ---------------------------------------------------------------------------
# lesson proposals — a worker may propose a lesson, held pending until curated
# ---------------------------------------------------------------------------

_PROPOSAL_COMMENT = (
    "**Changes**:\n"
    "- src/thing.py - hardened the sweep\n"
    "\n"
    "**Verification**: targeted tests pass\n"
    "\n"
    "**Lesson proposal v1**:\n"
    "key: sandbox-sweep\n"
    "lesson: the verification sandbox is read-only; copy a tree before sweeping\n"
    "date: 2026-08-12\n"
)

_PROPOSAL_BODY = (
    "the verification sandbox is read-only; copy a tree before sweeping "
    "(2026-08-12)"
)


def test_proposal_block_is_parsed() -> None:
    """AC-1: a completion comment's `**Lesson proposal v1**` block parses into
    a (key, dated body) pair, and text carrying the block delimiters cannot
    corrupt the surrounding comment's parsing."""
    proposals, malformed = grind_mod._lesson_proposals(_PROPOSAL_COMMENT)
    assert malformed == []
    assert proposals == [("sandbox-sweep", _PROPOSAL_BODY)]
    # The same comment still yields its **Changes** bullets untouched.
    assert grind_mod._changes_bullets(_PROPOSAL_COMMENT) == [
        "src/thing.py - hardened the sweep"
    ]

    # A block followed by another bolded header ends cleanly at the delimiter.
    hostile = (
        "**Lesson proposal v1**:\n"
        "key: delimiters\n"
        "lesson: a lesson may name **Changes** without breaking anything\n"
        "date: 2026-08-12\n"
        "**Verification**: written after the block\n"
    )
    proposals, malformed = grind_mod._lesson_proposals(hostile)
    assert malformed == []
    assert proposals == [
        ("delimiters", "a lesson may name **Changes** without breaking anything (2026-08-12)")
    ]


def test_proposal_is_recorded_pending(tmp_path: Path) -> None:
    """AC-2 + AC-3: a parsed proposal lands in the tracker under the pending
    prefix, where lesson selection never reads it."""
    from ortus.core.bd import BdClient

    repo = copy_bd_workspace(tmp_path / "repo", "bare").path
    issue_id = _create_ready_issue(repo, "Learn a hazard")
    subprocess.run(
        ["bd", "comments", "add", issue_id, _PROPOSAL_COMMENT],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    log: list[str] = []
    grind_mod._record_lesson_proposals(
        BdClient(repo), issue_id, write_log=log.append
    )

    client = BdClient(repo)
    assert client.pending_proposals() == {"sandbox-sweep": _PROPOSAL_BODY}
    assert client.lessons(limit=5, max_chars=400) == ()
    assert any("pending until curated" in line for line in log)
    # Recording again (a correction round rescans the comments) is idempotent.
    grind_mod._record_lesson_proposals(
        BdClient(repo), issue_id, write_log=log.append
    )
    assert client.pending_proposals() == {"sandbox-sweep": _PROPOSAL_BODY}


def test_no_proposal_is_unchanged(tmp_path: Path) -> None:
    """AC-6: a worker that proposes nothing writes nothing and logs nothing."""
    from ortus.core.bd import BdClient

    repo = copy_bd_workspace(tmp_path / "repo", "bare").path
    issue_id = _create_ready_issue(repo, "Ordinary completion")
    subprocess.run(
        [
            "bd",
            "comments",
            "add",
            issue_id,
            "**Changes**:\n- src/thing.py - did the work\n\n**Verification**: ok",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    log: list[str] = []
    grind_mod._record_lesson_proposals(
        BdClient(repo), issue_id, write_log=log.append
    )
    assert log == []
    assert BdClient(repo).memories() == {}


def test_malformed_proposal_is_ignored(tmp_path: Path) -> None:
    """AC-7: a malformed block earns a log line, records nothing, and never
    raises — including when the workspace itself cannot be read."""
    from ortus.core.bd import BdClient

    repo = copy_bd_workspace(tmp_path / "repo", "bare").path
    issue_id = _create_ready_issue(repo, "Learned it badly")
    subprocess.run(
        [
            "bd",
            "comments",
            "add",
            issue_id,
            # Undated, and the key is not a kebab-case slug.
            "**Lesson proposal v1**:\nkey: Not A Slug\nlesson: something\n",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    log: list[str] = []
    grind_mod._record_lesson_proposals(
        BdClient(repo), issue_id, write_log=log.append
    )
    assert any("ignored a malformed block" in line for line in log)
    assert BdClient(repo).memories() == {}

    # No bd workspace at all: the recorder degrades to a no-op, never a raise.
    grind_mod._record_lesson_proposals(
        BdClient(tmp_path / "nowhere"), issue_id, write_log=log.append
    )


def test_retro_does_not_run_in_an_iteration() -> None:
    """AC-5 (ortus-v8bj): the retrospective is an operator-invoked verb. The
    grind loop never references it, so no iteration can run one; and the verb
    never takes the grind repo lock or imports the grind command, so a running
    retrospective can never block an iteration either."""
    from ortus.commands import retro as retro_cmd
    from ortus.core import retro as retro_core

    grind_source = Path(grind_mod.__file__).read_text(encoding="utf-8")
    assert "retro" not in grind_source.lower()

    for module in (retro_cmd, retro_core):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "grind_flock" not in source
        assert "ortus.commands.grind" not in source
        assert "ortus.commands import grind" not in source


# ---------------------------------------------------------------------------
# Machine verification wiring (ortus-l2u9.3)
# ---------------------------------------------------------------------------


class _RecordingWorker:
    """Implementation-only worker that records every spawn's phase posture."""

    extra_env: dict[str, str] = {}

    def __init__(self, repo: Path, claims: dict[str, str] | None = None) -> None:
        self.repo = repo
        self.claims = claims if claims is not None else {"AC-1": "pass", "AC-2": "pass"}
        self.readonly_spawns = 0
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
        log_path.touch(exist_ok=True)
        if readonly:
            self.readonly_spawns += 1
            _emit_verdict(self.repo, log_path, criteria=("AC-1", "AC-2"))
            return 0
        (self.repo / "candidate.py").write_text("VALUE = 1\n")
        journal = JournalStore(self.repo).load()
        if journal is not None and journal.issue_id and self.claims:
            post_completion_comment(self.repo, journal.issue_id, self.claims)
        return 0


def _machine_grind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    name: str,
    claims: dict[str, str] | None = None,
    checks_default: object | None = None,
    reviewer: bool = False,
    max_corrections: int | None = 0,
) -> tuple[Path, str, object, _RecordingWorker]:
    """One branch-scoped run judged by the (scripted) machine pipeline."""
    repo = _bd_repo(tmp_path, name)
    issue_id = _create_ready_issue(repo, "machine judged leaf")
    _baseline_commit(repo)
    if reviewer:
        _enable_reviewer(repo)
    worker = _RecordingWorker(repo, claims)
    _fake_sandbox(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    monkeypatch.setattr(grind_mod, "_make_runner", lambda *a, **k: worker)
    install_machine_checks(
        monkeypatch,
        default=checks_default
        if checks_default is not None
        else machine_run(criteria=("AC-1", "AC-2")),
    )
    args = ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    if max_corrections is not None:
        args += ["--max-corrections", str(max_corrections)]
    result = runner.invoke(app, args)
    return repo, issue_id, result, worker


def _comments_text(repo: Path, issue_id: str) -> str:
    return subprocess.run(
        ["bd", "comments", issue_id, "--json"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.mark.slow
def test_flag_off_spawns_no_verifier_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-1: with the reviewer flag off, verification is the machine pipeline
    and no agent is spawned to judge."""
    repo, issue_id, result, worker = _machine_grind(
        tmp_path, monkeypatch, name="mach1"
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert _issue(repo, issue_id)["status"] == "closed"
    assert worker.readonly_spawns == 0, "no read-only verifier agent may launch"
    log = _grind_log(repo)
    assert "verification CodeGraph handshake requested" not in log
    assert "machine checks running against" in log


@pytest.mark.slow
def test_verification_comment_is_the_runner_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-2: the durable comment carries the runner's commands, verdicts, and
    exit codes — the record minus the agent that used to type it."""
    repo, issue_id, result, _worker = _machine_grind(
        tmp_path, monkeypatch, name="mach2"
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    comments = _comments_text(repo, issue_id)
    assert "Ortus machine verification record" in comments
    assert "Deterministic AC run @" in comments
    assert "uv run pytest tests/test_grind.py -q" in comments
    assert "exit 0" in comments
    assert "claims agree with the measured results" in comments


@pytest.mark.slow
def test_claim_disagreement_fails_per_criterion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-3: a claims/results disagreement fails the run, stated per criterion
    — in either direction, so a claim can never stand in for a result."""
    repo, issue_id, result, _worker = _machine_grind(
        tmp_path,
        monkeypatch,
        name="mach3",
        claims={"AC-1": "pass", "AC-2": "fail"},
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert _issue(repo, issue_id)["status"] == "in_progress"
    comments = _comments_text(repo, issue_id)
    assert "AC-2: claimed fail, measured pass" in comments
    log = _grind_log(repo)
    assert "verifier verdict=fail" in log


@pytest.mark.slow
def test_missing_claims_block_fails_with_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-8: a worker emitting no claims block fails the claim diff with a
    message naming the block, never a crash."""
    repo, issue_id, result, _worker = _machine_grind(
        tmp_path, monkeypatch, name="mach8", claims={}
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert _issue(repo, issue_id)["status"] == "in_progress"
    comments = _comments_text(repo, issue_id)
    assert "carries no **Claims v1** block" in comments


@pytest.mark.slow
def test_reviewer_flag_runs_after_green_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-7: with the flag on, the agent reviewer runs after a green machine
    pipeline and is skipped — no tokens spent — on a red one."""
    repo, issue_id, result, worker = _machine_grind(
        tmp_path, monkeypatch, name="mach7g", reviewer=True
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert worker.readonly_spawns == 1, "green machine run dispatches the reviewer"
    assert _issue(repo, issue_id)["status"] == "closed"

    repo, issue_id, result, worker = _machine_grind(
        tmp_path,
        monkeypatch,
        name="mach7r",
        reviewer=True,
        checks_default=machine_run("fail", criteria=("AC-1", "AC-2")),
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert worker.readonly_spawns == 0, "a red machine run spends no reviewer tokens"
    assert "reviewer skipped — the machine pipeline is red" in _grind_log(repo)
    assert _issue(repo, issue_id)["status"] == "in_progress"


@pytest.mark.slow
def test_acceptance_hash_rechecked_before_judgment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-6: the acceptance_criteria hash taken at claim is rechecked before
    judgment; an edit landing mid-run blocks rather than being re-read."""
    from ortus.core.codegraph import CodeGraphMode, CodeGraphProbe

    repo = _bd_repo(tmp_path, "mach6")
    issue_id = _create_ready_issue(repo, "hash guarded leaf")
    _baseline_commit(repo)
    packet = _issue(repo, issue_id)
    git = GitClient(repo)
    store = grind_mod.JournalStore(repo)
    packet_digest, packet_ref = store.save_packet(issue_id, packet)
    branch = f"ortus/{issue_id}"
    subprocess.run(["git", "branch", branch, "main"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "checkout", branch],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    journal = (
        grind_mod.CandidateJournal.start(
            repo=repo,
            issue_id=issue_id,
            base_head=git.head_oid(),
            baseline_paths=(),
            packet_hash=packet_digest,
            packet_ref=packet_ref,
        )
        .with_branch(branch, git.head_oid())
        .with_candidate((), phase="candidate-captured", candidate_hash="0" * 64)
    )
    store.save(journal)

    comments: list[str] = []
    statuses: list[str] = []
    edited = dict(packet)
    edited["acceptance_criteria"] = str(packet["acceptance_criteria"]) + "\n- AC-9: invented later."

    class _EditedBd:
        def show(self, _issue_id: str) -> dict:
            return edited

        def add_comment(self, _issue_id: str, body: str) -> None:
            comments.append(body)

        def update_status(self, _issue_id: str, status: str) -> None:
            statuses.append(status)

    log = repo / "logs" / "grind-hash.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.touch()
    outcome = grind_mod._machine_verify_candidate(
        bd=_EditedBd(),
        git=git,
        store=store,
        journal=journal,
        repo=repo,
        log=log,
        write_log=lambda _line: None,
        issue_id=issue_id,
        probe=CodeGraphProbe(CodeGraphMode.OFF, False, False, False),
        baseline=frozenset(),
        freshness="not-refreshed",
        sync_ms=0,
        iteration=1,
        integration_branch="main",
    )
    assert outcome.failure is not None
    assert "acceptance criteria changed after claim" in outcome.failure
    assert outcome.journal.phase == "verification-rejected"
    assert any("acceptance criteria changed after claim" in body for body in comments)
    assert statuses == ["in_progress"], "the claim is restored before the report"


# ---------------------------------------------------------------------------
# Worker workspaces (ortus-u4zv.2)
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_primary_checkout_never_leaves_integration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-1: through claim, implementation, verification and finalization the
    primary repository's checkout stays on the integration branch."""
    repo = _bd_repo(tmp_path, "ws1")
    issue_id = _create_ready_issue(repo, "workspace isolated leaf")
    _baseline_commit(repo)
    primary = repo
    observed: list[str] = []

    class _ObservingWorker:
        extra_env: dict[str, str] = {}

        def run(
            self,
            prompt: str,
            *,
            repo: Path,
            log_path: Path,
            readonly: bool = False,
            **kwargs: object,
        ) -> int:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.touch(exist_ok=True)
            observed.append(
                subprocess.run(
                    ["git", "branch", "--show-current"],
                    cwd=primary,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
            )
            assert repo != primary, "the worker must run in its own workspace"
            (repo / "candidate.py").write_text("VALUE = 1\n")
            _post_claims(primary)
            return 0

    _fake_sandbox(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    monkeypatch.setattr(grind_mod, "_make_runner", lambda *a, **k: _ObservingWorker())
    install_machine_checks(
        monkeypatch, default=machine_run(criteria=("AC-1", "AC-2"))
    )
    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert observed == ["main"], "the primary moved during the worker's run"
    final = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=repo,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert final == "main"
    assert _issue(repo, issue_id)["status"] == "closed"


@pytest.mark.slow
def test_primary_side_commit_stays_out_of_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-3: operator intake in the primary tree during implementation — a
    file edit, tracker writes — never enters the candidate."""
    repo = _bd_repo(tmp_path, "ws3")
    issue_id = _create_ready_issue(repo, "isolated from intake")
    _baseline_commit(repo)
    primary = repo

    class _IntakeCollidingWorker:
        extra_env: dict[str, str] = {}

        def run(
            self,
            prompt: str,
            *,
            repo: Path,
            log_path: Path,
            readonly: bool = False,
            **kwargs: object,
        ) -> int:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.touch(exist_ok=True)
            # The operator's intake session, mid-run: a scratch note in the
            # primary tree that a shared-tree worker would have absorbed.
            (primary / "intake-note.md").write_text("operator scratch\n")
            (repo / "candidate.py").write_text("VALUE = 1\n")
            _post_claims(primary)
            return 0

    _fake_sandbox(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    monkeypatch.setattr(
        grind_mod, "_make_runner", lambda *a, **k: _IntakeCollidingWorker()
    )
    install_machine_checks(
        monkeypatch, default=machine_run(criteria=("AC-1", "AC-2"))
    )
    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert _issue(repo, issue_id)["status"] == "closed"
    landed = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
    ).stdout
    assert "intake-note.md" not in landed
    assert (repo / "intake-note.md").read_text() == "operator scratch\n"
