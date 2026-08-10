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
from ortus.core import sandbox as sandbox_mod
from ortus.core.claude import ClaudeRunner
from ortus.core.profiles import Phase
from ortus.core.sandbox import SandboxInfo
from ortus.core.transaction import JournalStore
from tests._platform import skip_unless_bwrap_usable
from tests._shims import make_inline_python_shim, shim_path
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
    # pair, and finalization spent one last pass writing the commit message.
    assert phases == [Phase.PLAN, Phase.IMPLEMENT, Phase.VERIFY, Phase.FINALIZE]
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
    assert f"readiness: {first}" in combined
    assert f"readiness: {second}" in combined
    # Rich hard-wraps long lines mid-token, so compare whitespace-free.
    squashed = re.sub(r"\s+", "", combined)
    assert re.sub(r"\s+", "", f"follow-up: bd update {second}") in squashed
    assert re.sub(r"\s+", "", f"then re-run: ortus grind {repo}") in squashed


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
            "implementation-commit",
            "implementation-rejected",
            "committed to the repository",
        ),
        (
            "implementation-packet",
            "implementation-rejected",
            "issue packet artifact changed during implementation",
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
                if mutation == "implementation-commit":
                    # The forbidden move: committing advances HEAD, so the
                    # captured candidate diff would come back empty.
                    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
                    subprocess.run(
                        ["git", "commit", "-m", "worker commit"],
                        cwd=repo,
                        check=True,
                        capture_output=True,
                    )
                elif mutation == "implementation-packet":
                    journal = JournalStore(repo).load()
                    assert journal is not None
                    (repo / journal.issue_packet_ref).write_bytes(b'{"id":"forged"}')
                return 0
            assert mutation not in {"implementation-commit", "implementation-packet"}
            assert calls == 2
            journal = JournalStore(repo).load()
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
    """Run one grind whose verification preflight reports a blocked sandbox."""
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
    assert journal.verifier_refs == ()
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
    head = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert head.splitlines()[0].endswith(
        "fixture baseline"
    ), f"the abort must not commit the candidate: {head}"


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
                cwd=repo,
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
