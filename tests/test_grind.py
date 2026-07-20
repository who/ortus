"""Integration-ish tests for ortus grind (xvel.4 acceptance)."""

from __future__ import annotations

import json
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
from ortus.core import sandbox as sandbox_mod
from ortus.core.claude import ClaudeRunner
from ortus.core.profiles import Phase
from ortus.core.sandbox import SandboxInfo
from tests._shims import make_inline_python_shim, normalize_git_branch, shim_path

runner = CliRunner()

FAKE_CLAUDE = shim_path("fake-claude")


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


def test_grind_routes_profiles_and_fast_only_to_implementation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    repo = tmp_path / "profile-routing"
    repo.mkdir()
    subprocess.run(
        ["bd", "init", "--non-interactive", "--prefix", "route"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    normalize_git_branch(repo)
    issue_id = subprocess.run(
        ["bd", "create", "--silent", "--title", "route profiles", "--type", "task"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    settings = repo / ".claude" / "settings.json"
    settings.parent.mkdir(exist_ok=True)
    settings.write_text(json.dumps({"sandbox": {"excludedCommands": ["bd", "bd *"]}}))
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


def test_codex_outer_loop_drives_three_issues_to_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    repo = tmp_path / "codex-loop"
    repo.mkdir()
    subprocess.run(
        ["bd", "init", "--non-interactive", "--prefix", "cdx"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    normalize_git_branch(repo)
    for number in range(3):
        subprocess.run(
            [
                "bd",
                "create",
                "--silent",
                "--title",
                f"task {number}",
                "--type",
                "task",
                "--priority",
                "2",
            ],
            cwd=repo,
            check=True,
            capture_output=True,
        )
    (repo / ".ortusrc").write_text('backend = "codex"\n')
    (repo / ".codex").mkdir()
    (repo / ".codex" / "config.toml").write_text('sandbox_mode = "workspace-write"\n')
    with (repo / ".gitignore").open("a") as fh:
        fh.write("\nlogs/\n.cache/\n.beads/ortus.flock\n")
    subprocess.run(
        ["git", "config", "user.email", "ortus-tests@example.invalid"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Ortus Tests"], cwd=repo, check=True)
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
    assert len(prompts) == 3
    commits = subprocess.run(
        ["git", "log", "--format=%s"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert sum("complete Codex grind task" in subject for subject in commits) == 3
    assert (
        subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        == ""
    )
    ready = subprocess.run(
        ["bd", "ready", "--json"], cwd=repo, check=True, capture_output=True, text=True
    )
    assert json.loads(ready.stdout) == []


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


def test_grind_runs_fake_claude_and_logs_locally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Smoke: with a fake claude that exits 0, grind runs one iteration and writes a log.

    Updated for ortus-3ico subprocess-per-task shape: the loop now spawns
    one claude per iteration, so we seed a single ready issue and cap with
    --iterations 1 --idle-sleep 0 so the fake-claude (which doesn't touch
    bd) doesn't trigger an infinite no-change retry.
    """
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    repo = tmp_path / "fixture"
    repo.mkdir()
    subprocess.run(
        ["bd", "init", "--prefix", "fixtg"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    normalize_git_branch(repo)
    # Seed one ready issue so queue_drained() doesn't short-circuit before
    # claude is spawned.
    subprocess.run(
        [
            "bd",
            "create",
            "--silent",
            "--title",
            "smoke task",
            "--type",
            "task",
            "--priority",
            "2",
        ],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    settings = repo / ".claude" / "settings.json"
    settings.parent.mkdir(exist_ok=True)
    settings.write_text(json.dumps({"sandbox": {"excludedCommands": ["bd", "bd *"]}}))

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


def test_grind_harness_selects_claims_and_injects_issue_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ortus-xo1u: the harness (not the worker) selects + claims the next ready
    issue and injects its EXACT id into the per-iteration /goal prompt.

    With a fake claude that echoes its argv but never touches bd, we can assert
    the worker was handed the specific id the harness claimed — proving the
    worker is TOLD which issue to work rather than choosing/transcribing it.
    """
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    repo = tmp_path / "fixture"
    repo.mkdir()
    subprocess.run(
        ["bd", "init", "--prefix", "fixth"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    normalize_git_branch(repo)
    create = subprocess.run(
        [
            "bd",
            "create",
            "--silent",
            "--title",
            "inject me",
            "--type",
            "task",
            "--priority",
            "1",
        ],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )
    issue_id = create.stdout.strip()
    assert issue_id, "expected bd create to print the new id"
    settings = repo / ".claude" / "settings.json"
    settings.parent.mkdir(exist_ok=True)
    settings.write_text(json.dumps({"sandbox": {"excludedCommands": ["bd", "bd *"]}}))

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


def test_claude_goal_rejection_restores_claim_and_halts_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    repo = tmp_path / "goal-rejection"
    repo.mkdir()
    subprocess.run(
        ["bd", "init", "--prefix", "goalrej"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    normalize_git_branch(repo)
    issue_id = subprocess.run(
        [
            "bd",
            "create",
            "--silent",
            "--title",
            "oversized planned issue",
            "--description",
            "thorough implementation packet " * 300,
            "--type",
            "task",
            "--priority",
            "1",
        ],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    settings = repo / ".claude" / "settings.json"
    settings.parent.mkdir(exist_ok=True)
    settings.write_text(json.dumps({"sandbox": {"excludedCommands": ["bd", "bd *"]}}))

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
