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
_F2HE2_NO_VERIFY = pytest.mark.skip(
    reason="f2he.2: grind judges bd status only; no Claims, verifier, or Ortus close"
)
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    """Console capture without Rich highlight codes."""

    return _ANSI.sub("", text or "")


def _fake_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sandbox_mod, "smoke_test", lambda: SandboxInfo(platform="Linux", binary="bwrap")
    )


def _fixture_repo(tmp_path: Path) -> Path:
    """Beads + hooks-enabled settings, and a git work tree.

    f2he.4 made a git repo a grind start precondition (work stays on
    main). Sandbox/hook tests still need to reach those later checks.
    """
    repo = tmp_path / "fixture"
    (repo / ".beads").mkdir(parents=True)
    settings = repo / ".claude" / "settings.json"
    settings.parent.mkdir(exist_ok=True)
    settings.write_text(json.dumps({"sandbox": {"excludedCommands": ["bd", "bd *"]}}))
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
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


def _comments_blob(repo: Path, issue_id: str) -> str:
    return subprocess.run(
        ["bd", "comments", issue_id, "--json"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _packet_fields(issue: dict) -> tuple[str, str, str]:
    return (
        str(issue.get("description") or ""),
        str(issue.get("design") or ""),
        str(issue.get("acceptance_criteria") or issue.get("acceptance") or ""),
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


@pytest.mark.slow
def test_grind_unready_does_not_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-1: default grind never spawns a planning/repair worker for unready-only."""
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    repo = _bd_repo(tmp_path, "norepair")
    issue_id = _create_unready_issue(repo, "hand authored leaf", priority="1")
    before = _packet_fields(_issue(repo, issue_id))

    class NeverRuns:
        extra_env: dict[str, str] = {}

        def run(self, prompt: str, **kwargs: object) -> int:
            raise AssertionError("default grind must not spawn any subprocess")

    _fake_sandbox(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    monkeypatch.setattr(grind_mod, "_make_runner", lambda: NeverRuns())

    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    after = _issue(repo, issue_id)
    assert after["status"] == "open"
    assert _packet_fields(after) == before, "grind must not rewrite the work spec"
    combined = result.stdout + result.stderr
    assert "automatically" not in combined
    assert "READINESS REPAIR PASS" not in combined
    log_text = _grind_log(repo)
    assert "readiness repair pass" not in log_text
    assert "no ready issue to claim (queue blocked or human-only)" in log_text


@pytest.mark.slow
def test_grind_unready_flags_human(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-2: each unready leaf is labeled human, stays open, and is commented."""
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    repo = _bd_repo(tmp_path, "flaghuman")
    issue_id = _create_unready_issue(repo, "hand authored leaf", priority="1")

    class NeverRuns:
        extra_env: dict[str, str] = {}

        def run(self, prompt: str, **kwargs: object) -> int:
            raise AssertionError("default grind must not spawn any subprocess")

    _fake_sandbox(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    monkeypatch.setattr(grind_mod, "_make_runner", lambda: NeverRuns())

    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    shown = _issue(repo, issue_id)
    assert shown["status"] == "open"
    assert "human" in (shown.get("labels") or [])
    comments = _comments_blob(repo, issue_id)
    assert issue_id in comments
    assert "missing, empty, or placeholder section" in comments
    assert "grind will not repair this packet" in comments


@pytest.mark.slow
def test_grind_unready_flags_all_then_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two unready leaves are both flagged human, then grind stops."""
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    repo = _bd_repo(tmp_path, "flagall")
    first = _create_unready_issue(repo, "first hand authored leaf", priority="1")
    second = _create_unready_issue(repo, "second hand authored leaf", priority="1")

    class NeverRuns:
        extra_env: dict[str, str] = {}

        def run(self, prompt: str, **kwargs: object) -> int:
            raise AssertionError("default grind must not spawn any subprocess")

    _fake_sandbox(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    monkeypatch.setattr(grind_mod, "_make_runner", lambda: NeverRuns())

    result = runner.invoke(app, ["grind", str(repo), "--idle-sleep", "0"])

    assert result.exit_code == 0, result.stdout + result.stderr
    for issue_id in (first, second):
        shown = _issue(repo, issue_id)
        assert shown["status"] == "open"
        assert "human" in (shown.get("labels") or [])
        comments = _comments_blob(repo, issue_id)
        assert "missing, empty, or placeholder section" in comments
    assert "no ready issue to claim (queue blocked or human-only)" in _grind_log(repo)


@pytest.mark.slow
def test_grind_unready_label_failure_warns_and_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed human label still warns and stops; repair is not spawned."""
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    repo = _bd_repo(tmp_path, "labelfail")
    issue_id = _create_unready_issue(repo, "hand authored leaf", priority="1")

    class NeverRuns:
        extra_env: dict[str, str] = {}

        def run(self, prompt: str, **kwargs: object) -> int:
            raise AssertionError("label failure must not spawn a repair worker")

    def boom(self: object, target_id: str, label: str) -> None:
        raise grind_mod.BdError(
            ["bd", "label", "add", target_id, label], 1, "denied"
        )

    _fake_sandbox(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    monkeypatch.setattr(grind_mod, "_make_runner", lambda: NeverRuns())
    monkeypatch.setattr(grind_mod.BdClient, "add_label", boom)

    result = runner.invoke(app, ["grind", str(repo), "--idle-sleep", "0"])

    assert result.exit_code == 0, result.stdout + result.stderr
    shown = _issue(repo, issue_id)
    assert shown["status"] == "open"
    assert "human" not in (shown.get("labels") or [])
    combined = result.stdout + result.stderr
    assert "could not label" in combined
    assert "automatically" not in combined
    assert "readiness repair pass" not in _grind_log(repo)


@pytest.mark.slow
def test_grind_ready_leaf_does_not_flag_unready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ready leaf is claimed; sibling unready leaves are not flagged this iteration."""
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    repo = _bd_repo(tmp_path, "readyplus")
    unready_id = _create_unready_issue(repo, "unready sibling", priority="1")
    ready_id = _create_ready_issue(repo, "ready leaf", priority="2")
    recorded = _RecordingRunner()

    _fake_sandbox(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    monkeypatch.setattr(grind_mod, "_make_runner", lambda *a, **k: recorded)
    monkeypatch.setattr(
        grind_mod,
        "_compose_work_prompt",
        lambda *a, **k: "/goal work this issue",
    )

    result = runner.invoke(
        app, ["grind", str(repo), "--iterations", "1", "--idle-sleep", "0"]
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    assert recorded.calls, "the ready leaf must be handed to a worker"
    unready = _issue(repo, unready_id)
    assert unready["status"] == "open"
    assert "human" not in (unready.get("labels") or [])
    assert _issue(repo, ready_id)["id"] == ready_id


@pytest.mark.slow
def test_grind_leftover_in_progress_is_not_unready_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leftover in_progress claim is continued, not repaired or human-flagged."""
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    repo = _bd_repo(tmp_path, "leftover-unready")
    leftover_id = _create_unready_issue(repo, "leftover unready", priority="1")
    subprocess.run(
        ["bd", "update", leftover_id, "--status=in_progress"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    (repo / "leftover-notes.txt").write_text("inherited work\n", encoding="utf-8")
    recorded = _RecordingRunner()

    _fake_sandbox(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    monkeypatch.setattr(grind_mod, "_make_runner", lambda *a, **k: recorded)
    monkeypatch.setattr(
        grind_mod,
        "_compose_work_prompt",
        lambda *a, **k: "/goal work this issue",
    )

    result = runner.invoke(
        app, ["grind", str(repo), "--iterations", "1", "--idle-sleep", "0"]
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    shown = _issue(repo, leftover_id)
    assert shown["status"] == "in_progress"
    assert "human" not in (shown.get("labels") or [])
    assert recorded.calls, "leftover in_progress must continue, not take the unready exit"
    profile = recorded.calls[0].get("profile")
    assert getattr(profile, "phase", None) is not Phase.PLAN
    assert "readiness repair pass" not in _grind_log(repo)


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
            raise AssertionError("unready-only grind must not spawn any subprocess")

    _fake_sandbox(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    monkeypatch.setattr(grind_mod, "_make_runner", lambda: NeverRuns())

    result = runner.invoke(app, ["grind", str(repo), "--idle-sleep", "0"])

    assert result.exit_code == 0, result.stdout + result.stderr
    squashed = re.sub(r"\s+", "", _plain(result.stdout + result.stderr))
    total = sum(1 for section in _REQUIRED_SECTIONS if section.required)
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
    assert "corrections:" not in result.stdout
    assert "--max-corrections" not in result.stdout


def test_grind_help_lists_grok() -> None:
    result = runner.invoke(app, ["grind", "--help"])
    assert result.exit_code == 0
    assert "grok" in result.stdout


def test_grind_help_omits_max_corrections() -> None:
    """AC-1: the corrections option and spawn symbols are gone."""
    result = runner.invoke(app, ["grind", "--help"])
    assert result.exit_code == 0
    assert "--max-corrections" not in result.stdout
    unknown = runner.invoke(app, ["grind", "--max-corrections", "1", "--dry-run"])
    assert unknown.exit_code != 0
    assert not hasattr(grind_mod, "_correction_task")
    assert not hasattr(grind_mod, "_compose_correction_prompt")


def test_grind_help_omits_repair_flags() -> None:
    """Retired readiness-repair flags are unknown options, not hidden no-ops."""
    result = runner.invoke(app, ["grind", "--help"])
    assert result.exit_code == 0
    assert "--repair-unready" not in result.stdout
    assert "--repair-budget" not in result.stdout
    unknown = runner.invoke(app, ["grind", "--repair-unready", "--dry-run"])
    assert unknown.exit_code != 0
    unknown_budget = runner.invoke(
        app, ["grind", "--repair-budget", "1", "--dry-run"]
    )
    assert unknown_budget.exit_code != 0
    assert not hasattr(grind_mod, "_run_readiness_repair")
    assert not hasattr(grind_mod, "_repair_context")


def test_grok_dry_run_resolves_backend_and_goal_wrap(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path)
    result = runner.invoke(app, ["grind", str(repo), "--backend", "grok", "--dry-run"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "backend:        grok" in result.stdout
    prompt = result.stdout.split("--- per-iteration prompt ---", 1)[1]
    assert prompt.lstrip().startswith("/goal ")


def test_backend_all_is_rejected_as_init_only(tmp_path: Path) -> None:
    """`--backend all` is an init breadth, never a grind run backend."""
    repo = _fixture_repo(tmp_path)
    result = runner.invoke(app, ["grind", str(repo), "--backend", "all", "--dry-run"])
    assert result.exit_code == 1
    # collapse the console's line wrapping before matching the phrase
    combined = " ".join((result.stdout + result.stderr).split())
    assert "init provisioning option" in combined


def test_codex_dry_run_uses_plain_prompt(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path)
    (repo / ".ortusrc").write_text('backend = "codex"\n')
    result = runner.invoke(app, ["grind", str(repo), "--dry-run"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "backend:        codex" in result.stdout
    prompt = result.stdout.split("--- per-iteration prompt ---", 1)[1]
    assert "bd ready" in prompt
    assert "AGENTS.md" in prompt
    # Pointer names goal-prompt.md; Codex must not wrap with the slash command.
    assert not prompt.lstrip().startswith("/goal")


def test_dry_run_reports_independent_profiles(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path)
    (repo / ".ortusrc").write_text(
        '[profiles.claude.implement]\nmodel = "sonnet"\n'
        '[profiles.claude.verify]\nreasoning_effort = "high"\n'
    )
    result = runner.invoke(app, ["grind", str(repo), "--dry-run"])
    assert result.exit_code == 0, result.stdout + result.stderr
    plain = _plain(result.stdout)
    assert "claude/implement (model=sonnet, effort=provider-default)" in plain
    assert "claude/verify (model=provider-default, effort=high)" in plain


@pytest.mark.slow
@_F2HE2_NO_VERIFY
def test_grind_routes_phase_profiles_and_fast_only_to_implementation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _bd_repo(tmp_path, "profile-routing")
    issue_id = _create_ready_issue(repo, "route profiles")
    (repo / ".ortusrc").write_text(
        "reviewer = true\n"
        '[profiles.claude.implement]\nmodel = "sonnet"\n'
        '[profiles.claude.verify]\nmodel = "opus"\n'
    )
    _baseline_commit(repo)
    primary = repo
    calls: list[dict[str, object]] = []

    class RoutingRunner:
        extra_env: dict[str, str] = {}

        def run(self, prompt: str, *, log_path: Path, **kwargs: object) -> int:
            calls.append(kwargs)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.touch(exist_ok=True)
            profile = kwargs["profile"]
            if profile.phase is Phase.IMPLEMENT:  # type: ignore[union-attr]
                _post_claims(primary)
            if profile.phase is Phase.VERIFY:  # type: ignore[union-attr]
                subprocess.run(
                    ["bd", "close", issue_id, "--reason", "verified"],
                    cwd=primary,
                    check=True,
                    capture_output=True,
                )
            return 0

    _fake_sandbox(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    monkeypatch.setattr(grind_mod, "_make_runner", lambda: RoutingRunner())
    install_machine_checks(
        monkeypatch, default=machine_run(criteria=("AC-1", "AC-2"))
    )
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
@_F2HE2_NO_VERIFY
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
@_F2HE2_NO_VERIFY
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
@_F2HE2_NO_VERIFY
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
    assert claude_prompt.startswith("/goal ")
    assert "bd ready" in claude_prompt
    assert "AGENTS.md" in claude_prompt
    assert len(claude_prompt.removeprefix("/goal ")) <= 4_000
    assert issue["description"] not in claude_prompt

    grok_prompt = grind_mod._compose_work_prompt(template, issue, "grok")
    assert grok_prompt.startswith("/goal ")
    assert "Achieved when" in grok_prompt
    assert issue["description"] not in grok_prompt

    codex_prompt = grind_mod._compose_work_prompt(template, issue, "codex")
    assert not codex_prompt.startswith("/goal")
    assert "bd ready" in codex_prompt
    assert issue["description"] not in codex_prompt


def test_claude_goal_stays_under_cap_with_recovery_handoff() -> None:
    """A 1,500-character recovery handoff must stay inside Claude's /goal cap."""
    from ortus.core.codegraph import (
        CodeGraphMode,
        CodeGraphPhase,
        CodeGraphProbe,
        phase_contract,
    )

    handoff = " RECOVERY: " + "x" * 1500
    bare = grind_mod._compose_work_prompt(
        "",
        {"id": "x", "title": "t"},
        "claude",
        phase_instruction=handoff,
    )
    assert grind_mod._GOAL_POINTER in bare
    assert "**Orient.**" not in bare
    assert len(bare.removeprefix("/goal ")) <= 4_000

    contract = phase_contract(
        CodeGraphPhase.IMPLEMENTATION,
        CodeGraphProbe(
            mode=CodeGraphMode.REQUIRED,
            index_present=True,
            cli_present=True,
            available=True,
        ),
    )
    stacked = grind_mod._compose_work_prompt(
        "",
        {"id": "x", "title": "t"},
        "claude",
        phase_instruction=grind_mod._IMPLEMENTATION_INSTRUCTION + handoff,
        phase_contract_text=contract,
    )
    assert grind_mod._GOAL_POINTER in stacked
    assert "**Orient.**" not in stacked
    assert len(stacked.removeprefix("/goal ")) <= 4_000


def test_goal_condition_limit_is_claude_only() -> None:
    """Grok host /goal is not Claude's 4,000-character slash-command cap."""
    issue = {"id": "demo-wide", "title": "wide instruction"}
    wide = "x" * 5_000
    grok_prompt = grind_mod._compose_work_prompt(
        "", issue, "grok", phase_instruction=wide
    )
    assert grok_prompt.startswith("/goal ")
    assert len(grok_prompt.removeprefix("/goal ")) > 4_000
    with pytest.raises(grind_mod.BackendError, match="4,000-character"):
        grind_mod._compose_work_prompt("", issue, "claude", phase_instruction=wide)


class _DoneBarBd:
    def __init__(self, closed: int) -> None:
        self.closed = closed

    def count_by_status(self, status: str, *, exclude_labels: tuple[str, ...] = ()) -> int:
        del status, exclude_labels
        return self.closed


class _DoneBarGit:
    def __init__(
        self,
        *,
        ahead: int,
        tip: str = "abc",
        dirty: frozenset[str] | None = frozenset(),
    ) -> None:
        self.ahead = ahead
        self.tip = tip
        self.dirty = dirty

    def remote_tip(self, branch: str) -> str:
        del branch
        return self.tip

    def local_ahead_of_remote(self, branch: str) -> int:
        del branch
        return self.ahead

    def dirty_paths(self) -> frozenset[str] | None:
        return self.dirty


def test_done_bar_met_requires_new_close_and_in_sync() -> None:
    """Reap when closed count grew, origin is not behind local, and the tree is clean."""
    assert (
        grind_mod._done_bar_met(_DoneBarBd(658), _DoneBarGit(ahead=0), 657, "main")
        == "closed 657->658"
    )
    assert (
        grind_mod._done_bar_met(_DoneBarBd(657), _DoneBarGit(ahead=0), 657, "main")
        is None
    )
    assert (
        grind_mod._done_bar_met(_DoneBarBd(658), _DoneBarGit(ahead=1), 657, "main")
        is None
    )
    assert (
        grind_mod._done_bar_met(
            _DoneBarBd(658), _DoneBarGit(ahead=0, tip=""), 657, "main"
        )
        is None
    )


def test_done_bar_met_is_false_when_worktree_is_dirty() -> None:
    """A close with leftover edits is not done; the worker still has to commit."""
    assert (
        grind_mod._done_bar_met(
            _DoneBarBd(658),
            _DoneBarGit(ahead=0, dirty=frozenset({"src/ortus/core/github_bead.py"})),
            657,
            "main",
        )
        is None
    )


def test_done_bar_met_is_false_when_dirty_paths_unknown() -> None:
    """A failed git status is not an empty tree; do not reap."""
    assert (
        grind_mod._done_bar_met(
            _DoneBarBd(658),
            _DoneBarGit(ahead=0, dirty=None),
            657,
            "main",
        )
        is None
    )


def test_done_bar_met_is_false_on_tracker_error() -> None:
    class _BoomBd:
        def count_by_status(self, status: str, **kwargs: object) -> int:
            raise RuntimeError("tracker down")

    assert (
        grind_mod._done_bar_met(_BoomBd(), _DoneBarGit(ahead=0), 657, "main")
        is None
    )


@pytest.mark.slow
def test_grok_implement_reaps_on_done_bar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-3: a grok implement spawn gets a reap_when bound to the claimed id."""
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    repo = _bd_repo(tmp_path, "grok-reap")
    _create_ready_issue(repo, "reap after close")
    recorded = _RecordingRunner()
    _fake_sandbox(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    monkeypatch.setattr(grind_mod, "_make_runner", lambda *a, **k: recorded)
    result = runner.invoke(
        app,
        [
            "grind",
            str(repo),
            "--backend",
            "grok",
            "--iterations",
            "1",
            "--idle-sleep",
            "0",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert recorded.calls
    reap_when = recorded.calls[0].get("reap_when")
    assert callable(reap_when)


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
@_F2HE2_NO_VERIFY
def test_codex_rejects_implementation_worker_that_closes_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _bd_repo(tmp_path, "codex-loop")
    for number in range(3):
        _create_ready_issue(repo, f"task {number}")
    (repo / ".ortusrc").write_text('backend = "codex"\n')
    # bd >=1.1 `init` scaffolds agent config (.agents/, .claude/, .codex/,
    # .cursor/) into the workspace, so the template may already carry this
    # directory; older bds leave it to the fixture.
    (repo / ".codex").mkdir(exist_ok=True)
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
            assert "bd ready" in prompt
            claimed_rows = json.loads(
                subprocess.run(
                    ["bd", "list", "--status=in_progress", "--json"],
                    cwd=primary,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout
            )
            claimed = next(
                (item["id"] for item in claimed_rows if item.get("id")), None
            )
            if claimed is None:
                ready = json.loads(
                    subprocess.run(
                        ["bd", "ready", "--json"],
                        cwd=primary,
                        check=True,
                        capture_output=True,
                        text=True,
                    ).stdout
                )
                claimed = next(
                    item["id"]
                    for item in ready
                    if item.get("issue_type") != "epic"
                )
                subprocess.run(
                    ["bd", "update", claimed, "--status=in_progress"],
                    cwd=primary,
                    check=True,
                    capture_output=True,
                )
            subprocess.run(
                ["bd", "close", claimed, "--reason", "fake codex completed it"],
                cwd=primary,
                check=True,
                capture_output=True,
            )
            marker = repo / "codex-worker-output.txt"
            prior = marker.read_text() if marker.exists() else ""
            marker.write_text(prior + claimed + "\n")
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
    assert prompts, "a Codex worker must spawn"
    assert not (repo / "logs" / "grind-transaction.json").exists()
    assert JournalStore(repo).load() is None
    log = _grind_log(repo)
    assert "transaction handoff" not in log
    assert "journal owned" not in log
    commits = subprocess.run(
        ["git", "log", "--format=%s"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert sum("complete Codex grind task" in subject for subject in commits) == 0


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


def test_claude_hook_precheck_still_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude grind still refuses a repo that disabled hooks (AC-2)."""
    test_grind_exits_one_on_disabled_hooks_before_claude(tmp_path, monkeypatch)


def test_grok_grind_skips_claude_hook_precheck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _fixture_repo(tmp_path)
    (repo / ".claude" / "settings.json").write_text(
        json.dumps(
            {"disableAllHooks": True, "sandbox": {"excludedCommands": ["bd", "bd *"]}}
        )
    )
    _fake_sandbox(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    seen: list[object] = []

    def _capture(backend: str = "claude") -> ClaudeRunner:
        seen.append(backend)
        raise AssertionError(f"stop after runner pick: {backend}")

    monkeypatch.setattr(grind_mod, "_make_runner", _capture)
    result = runner.invoke(app, ["grind", str(repo), "--backend", "grok"])
    combined = result.stdout + result.stderr
    assert "disableAllHooks" not in combined
    if seen:
        assert seen == ["grok"]


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
    assert f"goal-prompt ready for {issue_id}" in log_text
    assert "bd ready" in log_text


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
    """Default (no --condition) dry-run shows the goal-prompt worker loop."""
    repo = _fixture_repo(tmp_path)
    result = runner.invoke(app, ["grind", str(repo), "--dry-run"])
    assert result.exit_code == 0
    assert "select:" in result.stdout
    assert "goal-prompt" in result.stdout
    assert "bd ready" in result.stdout


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
    result = runner.invoke(app, args)
    return repo, issue_id, result, hashes


@pytest.mark.slow
@_F2HE2_NO_VERIFY
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
@_F2HE2_NO_VERIFY
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
        "acceptance checks: PASS — machine checks passed 2/2 criteria, claims agree "
        f"(owned {hashes[-1][:12]}) after" in console
    )
    assert f"owned={hashes[-1]}" in _grind_log(repo)


@pytest.mark.slow
@_F2HE2_NO_VERIFY
def test_grind_console_prints_tally_and_finalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-3: correction attempts, the landing, and the running tally all
    reach the console."""
    _, issue_id, result, hashes = _narrated_grind(
        tmp_path,
        monkeypatch,
        name="tally",
        decisions=("fail", "pass"),
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    console = _squashed_console(result)
    assert (
        "acceptance checks: FAIL — machine checks passed 0/2 criteria, claims disagree "
        f"(owned {hashes[0][:12]})" in console
    )
    assert f"correction attempt 1/2 for {issue_id}" in console
    assert f"landed {issue_id} on main — 1 done this run, 0 open" in console


@pytest.mark.slow
@_F2HE2_NO_VERIFY
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
@_F2HE2_NO_VERIFY
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


@pytest.mark.slow
@_F2HE2_NO_VERIFY
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
@_F2HE2_NO_VERIFY
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
        # f2he.2 skips verification, so only the implementation fallback fires.
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
@_F2HE2_NO_VERIFY
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
@_F2HE2_NO_VERIFY
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
@_F2HE2_NO_VERIFY
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
@_F2HE2_NO_VERIFY
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
@_F2HE2_NO_VERIFY
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


# ---------------------------------------------------------------------------
# Worker workspaces (ortus-u4zv.2)
# ---------------------------------------------------------------------------


@pytest.mark.slow
@_F2HE2_NO_VERIFY
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
@_F2HE2_NO_VERIFY
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


class _RecordingRunner:
    """Captures run() kwargs so tests can see whether grind passed resume=."""

    extra_env: dict[str, str] = {}

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def run(self, prompt: str, **kwargs: object) -> int:
        self.calls.append({"prompt": prompt, **kwargs})
        return 0


class _CloseWithoutClaimsRunner:
    extra_env: dict[str, str] = {}

    def __init__(self, host: Path) -> None:
        self.host = host

    def run(self, prompt: str, **kwargs: object) -> int:
        listing = json.loads(
            subprocess.run(
                ["bd", "list", "--status=in_progress", "--json"],
                cwd=self.host,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )
        issue_id = next((item["id"] for item in listing if item.get("id")), None)
        if issue_id is None:
            ready = json.loads(
                subprocess.run(
                    ["bd", "ready", "--json"],
                    cwd=self.host,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout
            )
            issue_id = next(
                item["id"]
                for item in ready
                if item.get("issue_type") != "epic"
            )
            subprocess.run(
                ["bd", "update", issue_id, "--status=in_progress"],
                cwd=self.host,
                check=True,
                capture_output=True,
            )
        subprocess.run(
            ["bd", "close", issue_id, "--reason", "worker closed without Claims"],
            cwd=self.host,
            check=True,
            capture_output=True,
        )
        return 0


class _ClaimAndBailRunner:
    extra_env: dict[str, str] = {}

    def __init__(self, host: Path) -> None:
        self.host = host

    def run(self, prompt: str, **kwargs: object) -> int:
        listing = json.loads(
            subprocess.run(
                ["bd", "list", "--status=in_progress", "--json"],
                cwd=self.host,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )
        if listing:
            return 0
        ready = json.loads(
            subprocess.run(
                ["bd", "ready", "--json"],
                cwd=self.host,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )
        issue_id = next(
            item["id"] for item in ready if item.get("issue_type") != "epic"
        )
        subprocess.run(
            ["bd", "update", issue_id, "--status=in_progress"],
            cwd=self.host,
            check=True,
            capture_output=True,
        )
        return 0


@pytest.mark.slow
def test_grind_counts_worker_close_without_claims_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-1: a worker close is a win; grind does not require Claims v1."""
    repo = _bd_repo(tmp_path, "close-no-claims")
    issue_id = _create_ready_issue(repo, "close me")
    _fake_sandbox(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    monkeypatch.setattr(
        grind_mod, "_make_runner", lambda *a, **k: _CloseWithoutClaimsRunner(repo)
    )
    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert _issue(repo, issue_id)["status"] == "closed"
    log = _grind_log(repo)
    assert f"worker closed {issue_id}" in log
    assert "Claims v1" not in log
    assert "claims disagree" not in log.lower()


@pytest.mark.slow
@pytest.mark.codegraph_default
def test_silent_fresh_worker_under_required_fails_handshake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A silent required worker fails the live handshake even if it closed."""
    from ortus.core.codegraph import CodeGraphProbe

    repo = _bd_repo(tmp_path, "silent-required")
    issue_id = _create_ready_issue(repo, "close silently")
    _fake_sandbox(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    monkeypatch.setattr(
        grind_mod, "_make_runner", lambda *a, **k: _CloseWithoutClaimsRunner(repo)
    )

    class _AvailableCodeGraph:
        def probe(self, repo: Path, mode: object, *, backend: str = "claude") -> object:
            return CodeGraphProbe(mode, True, True, True)

        def refresh(self, repo: Path, probe: object) -> tuple[str, int]:
            return ("fresh", 1)

    monkeypatch.setattr(grind_mod, "_make_codegraph", lambda: _AvailableCodeGraph())
    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    combined = result.stdout + result.stderr
    assert result.exit_code == 1, combined
    assert "no CodeGraph MCP" in combined
    assert _issue(repo, issue_id)["status"] == "closed"


@pytest.mark.slow
def test_grind_leaves_unfinished_claim_in_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-2: unfinished work stays in_progress; grind does not revert it."""
    repo = _bd_repo(tmp_path, "leave-claimed")
    issue_id = _create_ready_issue(repo, "leave me")
    _fake_sandbox(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    monkeypatch.setattr(
        grind_mod, "_make_runner", lambda *a, **k: _ClaimAndBailRunner(repo)
    )
    result = runner.invoke(
        app, ["grind", str(repo), "--iterations", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert _issue(repo, issue_id)["status"] == "in_progress"
    log = _grind_log(repo)
    assert f"left {issue_id} in_progress" in log
    assert "orphan-policy: revert" not in log
    assert "human" not in (_issue(repo, issue_id).get("labels") or [])


@pytest.mark.slow
def test_grind_implement_argv_has_no_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """f2he.5 AC-1: implement spawn does not pass resume=."""
    repo = _bd_repo(tmp_path, "no-resume")
    _create_ready_issue(repo, "fresh")
    _baseline_commit(repo)
    recorded = _RecordingRunner()
    _fake_sandbox(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    monkeypatch.setattr(grind_mod, "_make_runner", lambda *a, **k: recorded)
    result = runner.invoke(
        app, ["grind", str(repo), "--iterations", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert recorded.calls
    assert "resume" not in recorded.calls[0]


@pytest.mark.slow
def test_leftover_claim_spawn_has_no_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """f2he.5 AC-2: journal/leftover resume is a new process, no --resume."""
    repo = _bd_repo(tmp_path, "leftover-no-resume")
    issue_id = _create_ready_issue(repo, "leftover")
    subprocess.run(
        ["bd", "update", issue_id, "--status=in_progress"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    _baseline_commit(repo)
    recorded = _RecordingRunner()
    _fake_sandbox(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    monkeypatch.setattr(grind_mod, "_make_runner", lambda *a, **k: recorded)
    result = runner.invoke(
        app, ["grind", str(repo), "--iterations", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert recorded.calls
    assert "resume" not in recorded.calls[0]
    assert _issue(repo, issue_id)["status"] == "in_progress"


@pytest.mark.slow
def test_grind_does_not_cut_issue_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """f2he.4 AC-1: a one-task grind does not create ortus/<id>."""
    repo = _bd_repo(tmp_path, "no-issue-branch")
    issue_id = _create_ready_issue(repo, "on main")
    _fake_sandbox(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    monkeypatch.setattr(
        grind_mod, "_make_runner", lambda *a, **k: _CloseWithoutClaimsRunner(repo)
    )
    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    branches = subprocess.run(
        ["git", "branch", "--list", f"ortus/{issue_id}"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert branches.strip() == ""


@pytest.mark.slow
def test_grind_does_not_create_workspace_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """f2he.4 AC-2: a one-task grind does not create logs/grind-workspaces/<id>."""
    repo = _bd_repo(tmp_path, "no-workspace")
    issue_id = _create_ready_issue(repo, "no clone")
    _fake_sandbox(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    monkeypatch.setattr(
        grind_mod, "_make_runner", lambda *a, **k: _CloseWithoutClaimsRunner(repo)
    )
    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert not (repo / "logs" / "grind-workspaces" / issue_id).exists()


def test_grind_requires_git_repo(tmp_path: Path) -> None:
    """f2he.4 AC-3: grind refuses to start when the tree is not a git repo."""
    repo = tmp_path / "not-git"
    (repo / ".beads").mkdir(parents=True)
    result = runner.invoke(app, ["grind", str(repo)])
    assert result.exit_code == 1
    combined = result.stdout + result.stderr
    assert "not a git repository" in combined.lower() or "git" in combined.lower()


@pytest.mark.slow
def test_grind_does_not_write_journal_after_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-1: a two-issue grind does not write a journal or handoff line."""
    repo = _bd_repo(tmp_path, "no-handoff")
    first = _create_ready_issue(repo, "first close")
    second = _create_ready_issue(repo, "second close")
    _baseline_commit(repo)
    _fake_sandbox(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    monkeypatch.setattr(
        grind_mod, "_make_runner", lambda *a, **k: _CloseWithoutClaimsRunner(repo)
    )
    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "2", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert _issue(repo, first)["status"] == "closed"
    assert _issue(repo, second)["status"] == "closed"
    assert not (repo / "logs" / "grind-transaction.json").exists()
    log = _grind_log(repo)
    assert "transaction handoff" not in log
    assert "journal owned" not in log
    assert "starting a new transaction" not in log
    assert f"worker closed {first}" in log or f"worker closed {second}" in log


@pytest.mark.slow
def test_startup_ignores_leftover_finalized_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-2: leftover finalized-* journal is discarded, never finalized, no HALT."""
    repo = _bd_repo(tmp_path, "stale-finalized")
    leftover_id = _create_ready_issue(repo, "already shipped")
    ready_id = _create_ready_issue(repo, "next ready")
    _baseline_commit(repo)
    subprocess.run(
        ["bd", "close", leftover_id, "--reason", "already shipped"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    journal_path = repo / "logs" / "grind-transaction.json"
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal_path.write_text(
        json.dumps(
            {
                "schema": 4,
                "issue_id": leftover_id,
                "base_head": "0" * 40,
                "baseline_paths": [],
                "baseline_fingerprints": {},
                "candidate_paths": [],
                "phase": "finalized-commit",
                "finalization": {"report": True, "close": True, "compose": True, "commit": True},
            }
        ),
        encoding="utf-8",
    )
    assert not hasattr(grind_mod, "_finalize_candidate")
    _fake_sandbox(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    monkeypatch.setattr(
        grind_mod, "_make_runner", lambda *a, **k: _CloseWithoutClaimsRunner(repo)
    )
    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert not journal_path.exists()
    assert _issue(repo, leftover_id)["status"] == "closed"
    assert _issue(repo, ready_id)["status"] == "closed"
    log = _grind_log(repo)
    assert "session-close resume" not in log
    assert "HALT" not in log
    assert "discarded leftover candidate journal" in log
