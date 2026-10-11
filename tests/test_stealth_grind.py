"""Stealth bead 2/4: `ortus grind` in a repository that keeps Ortus out of git.

Covers the tracker never adopting origin (env and the session-close step),
commit messages following `commit_issue_ref`, the post-window `.gitignore`
refusal, the skipped housekeeping commit and filtered commit paths, and the
out-of-tree cache root. Outside stealth every surface is unchanged.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from ortus.cli import app
from ortus.commands import grind as grind_mod
from ortus.core import cache
from ortus.core import sandbox as sandbox_mod
from ortus.core.git import GitClient
from ortus.core.grind_loop import read_work_issue_condition
from ortus.core.prompts import (
    ResolvedPrompt,
    apply_session_close,
    bundled_prompt_text,
    condition_message_rules,
    message_rules,
    session_close_steps,
)
from ortus.core.sandbox import SandboxInfo
from tests.conftest import copy_bd_workspace, run_bd
from tests.test_readiness import ready_issue

cli = CliRunner()

DOLT_PUSH = "`bd dolt push`"
PRIVATE_PUSH = "`bd dolt push --remote ortus-private`"


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    (path / "README").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "README"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=path, check=True)
    return path


def _head_files(repo: Path) -> set[str]:
    out = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout
    return {line for line in out.splitlines() if line}


# --- AC-1: tracker never adopts origin --------------------------------------


def test_session_close_unchanged_outside_stealth() -> None:
    for audited in (False, True):
        text = bundled_prompt_text("goal-prompt", audited=audited)
        assert session_close_steps(False, None) in text
        assert DOLT_PUSH in session_close_steps(False, None)


def test_session_close_drops_dolt_push_in_stealth() -> None:
    steps = session_close_steps(True, None)
    assert "bd dolt push" not in steps
    assert steps == (
        "completion comment, commit, `bd close`, "
        "`git pull --rebase --autostash`, `git push`"
    )


def test_session_close_pushes_only_private_remote() -> None:
    steps = session_close_steps(True, "git@example.com:me/tracker.git")
    assert PRIVATE_PUSH in steps
    assert DOLT_PUSH not in steps
    assert steps.index(PRIVATE_PUSH) < steps.index("`git push`")


def test_session_close_override_untouched_with_warning() -> None:
    override = ResolvedPrompt(
        name="goal-prompt", source="repo", path=Path("/x/goal-prompt.md"),
        text="close: " + session_close_steps(False, None),
    )
    fitted, warning = apply_session_close(override, stealth=True, tracker_remote=None)
    assert fitted is override
    assert warning and "bd dolt push" in warning


@pytest.mark.parametrize("tracker_remote", [None, "git@example.com:me/t.git"])
def test_session_close_prompt_show_goal_in_stealth(
    tmp_path: Path, tracker_remote: str | None
) -> None:
    repo = _git_repo(tmp_path / "fork")
    rc = "stealth = true\n"
    if tracker_remote:
        rc += f'tracker_remote = "{tracker_remote}"\n'
    (repo / ".ortusrc").write_text(rc, encoding="utf-8")

    result = cli.invoke(app, ["prompt", "show", "goal", str(repo)])

    assert result.exit_code == 0, result.output
    assert DOLT_PUSH not in result.stdout
    assert (PRIVATE_PUSH in result.stdout) is bool(tracker_remote)
    assert "**Session-close**" in result.stdout


def test_session_close_prompt_show_goal_unchanged_outside_stealth(
    tmp_path: Path,
) -> None:
    repo = _git_repo(tmp_path / "plain")
    for audited in ("false", "true"):
        (repo / ".ortusrc").write_text(f"prompt_audit = {audited}\n", encoding="utf-8")
        result = cli.invoke(app, ["prompt", "show", "goal", str(repo)])
        assert result.exit_code == 0, result.output
        assert result.stdout == bundled_prompt_text(
            "goal-prompt", audited=audited == "true"
        )


# --- AC-2: commit-message rules follow commit_issue_ref ---------------------


def test_message_rules_subject_is_todays_text() -> None:
    assert message_rules("subject") == grind_mod._MESSAGE_RULES
    assert "counted with the `<issue-id>: ` prefix" in message_rules("subject")
    assert grind_mod._implementation_instruction("subject", False) is (
        grind_mod._IMPLEMENTATION_INSTRUCTION
    )


def test_message_rules_none_names_no_issue() -> None:
    rules = message_rules("none")
    assert "<issue-id>: " not in rules
    assert "no issue id anywhere in the message" in rules
    assert "Ortus-Issue" not in rules


def test_message_rules_trailer_asks_for_ortus_issue() -> None:
    rules = message_rules("trailer")
    assert "<issue-id>: " not in rules
    assert "`Ortus-Issue: <issue-id>`" in rules


@pytest.mark.parametrize("audited", [False, True])
def test_message_rules_reach_work_issue_condition(audited: bool) -> None:
    bundled = read_work_issue_condition(audited=audited)
    assert read_work_issue_condition(audited=audited, issue_ref="subject") == bundled
    none = read_work_issue_condition(audited=audited, issue_ref="none")
    assert "`<ISSUE_ID>: ` prefix" not in none
    assert "no issue id anywhere in the message" in none
    trailer = condition_message_rules(bundled, "trailer")
    assert "`Ortus-Issue: <ISSUE_ID>`" in trailer
    assert "`<ISSUE_ID>: ` prefix" not in trailer


def test_message_rules_stealth_instruction_carries_staging_rule() -> None:
    instruction = grind_mod._implementation_instruction("none", True)
    assert message_rules("none") in instruction
    assert "never `git add -f`" in instruction
    assert "`.git/info/exclude`" in instruction
    assert "never `git add -f`" not in grind_mod._IMPLEMENTATION_INSTRUCTION


# --- AC-3: the .gitignore refusal -------------------------------------------


class _FakeBd:
    def __init__(self) -> None:
        self.comments: list[tuple[str, str]] = []
        self.labels: list[tuple[str, str]] = []

    def add_comment(self, issue_id: str, body: str) -> None:
        self.comments.append((issue_id, body))

    def add_label(self, issue_id: str, label: str) -> None:
        self.labels.append((issue_id, label))


def _issue_with_scope(scope: str) -> dict:
    packet = ready_issue("st-1")
    packet["design"] = packet["design"].replace(
        "Add and thread the preview flag.", scope
    )
    return packet


@pytest.mark.parametrize("nested", [False, True])
def test_gitignore_commit_outside_scope_halts(tmp_path: Path, nested: bool) -> None:
    repo = _git_repo(tmp_path / "fork")
    git = GitClient(repo)
    pre = git.head_oid()
    target = repo / ("sub/.gitignore" if nested else ".gitignore")
    target.parent.mkdir(exist_ok=True)
    target.write_text("eval-out/\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "ignore eval-out"], cwd=repo, check=True)
    bd = _FakeBd()
    lines: list[str] = []

    with pytest.raises(typer.Exit) as exc:
        grind_mod._stealth_gitignore_guard(
            git, bd, _issue_with_scope("Edit `src/a.py`."), pre, lines.append
        )

    assert exc.value.exit_code == 1
    assert bd.labels == [("st-1", "human")]
    assert bd.comments and bd.comments[0][1].startswith("STEALTH-GITIGNORE")
    assert any("HALT" in line and ".gitignore" in line for line in lines)


def test_gitignore_dirty_edit_outside_scope_halts(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "fork")
    (repo / ".gitignore").write_text("x\n", encoding="utf-8")
    git = GitClient(repo)

    with pytest.raises(typer.Exit):
        grind_mod._stealth_gitignore_guard(
            git, _FakeBd(), None, git.head_oid(), lambda _line: None
        )


def test_gitignore_named_in_scope_proceeds(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "fork")
    (repo / ".gitignore").write_text("x\n", encoding="utf-8")
    git = GitClient(repo)
    bd = _FakeBd()

    grind_mod._stealth_gitignore_guard(
        git, bd, _issue_with_scope("Add `eval-out/` to `.gitignore`."),
        git.head_oid(), lambda _line: None,
    )

    assert not bd.labels and not bd.comments


def test_gitignore_scope_parse() -> None:
    assert grind_mod._scope_names_gitignore(_issue_with_scope("Edit `.gitignore`."))
    assert not grind_mod._scope_names_gitignore(_issue_with_scope("Edit `a.py`."))
    assert not grind_mod._scope_names_gitignore({"design": "mentions .gitignore"})
    assert not grind_mod._scope_names_gitignore(None)


class _EnvRecordingRunner:
    """Records the env each window ran with; optionally edits `.gitignore`."""

    def __init__(self, repo: Path, *, touch_gitignore: bool) -> None:
        self.repo = repo
        self.touch_gitignore = touch_gitignore
        self.extra_env: dict[str, str] = {}
        self.envs: list[dict[str, str]] = []

    def run(self, prompt: str, **kwargs: object) -> int:
        self.envs.append(dict(self.extra_env))
        if self.touch_gitignore:
            (self.repo / ".gitignore").write_text("eval-out/\n", encoding="utf-8")
            subprocess.run(["git", "add", ".gitignore"], cwd=self.repo, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "ignore eval-out"],
                cwd=self.repo, check=True,
            )
        return 0


def _stealth_grind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, scope: str, touch: bool
) -> tuple[Path, str, _EnvRecordingRunner, object]:
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    repo = copy_bd_workspace(tmp_path / "fork", "bare").path
    packet = _issue_with_scope(scope)
    issue_id = run_bd(
        repo, "create", "--silent", "--title", "stealth leaf", "--type", "task",
        "--priority", "1", "--description", packet["description"],
        "--design", packet["design"], "--acceptance", packet["acceptance_criteria"],
    )
    subprocess.run(["git", "config", "ortus.stealth", "true"], cwd=repo, check=True)
    worker = _EnvRecordingRunner(repo, touch_gitignore=touch)
    monkeypatch.setattr(
        sandbox_mod, "smoke_test", lambda: SandboxInfo(platform="Linux", binary="bwrap")
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setattr(grind_mod, "_make_runner", lambda *a, **k: worker)
    monkeypatch.setattr(
        grind_mod, "_compose_work_prompt", lambda *a, **k: "/goal work this issue"
    )
    result = cli.invoke(
        app, ["grind", str(repo), "--iterations", "1", "--idle-sleep", "0"]
    )
    return repo, issue_id, worker, result


def _labels(repo: Path, issue_id: str) -> list[str]:
    shown = run_bd(repo, "show", issue_id, "--json")
    return json.loads(shown)[0].get("labels") or []


@pytest.mark.integration
def test_stealth_grind_env_sets_bd_no_remote_adopt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _, worker, result = _stealth_grind(
        tmp_path, monkeypatch, scope="Edit `src/a.py`.", touch=False
    )

    assert worker.envs, result.output
    env = worker.envs[0]
    assert env.get("BD_NO_REMOTE_ADOPT") == "1"
    assert "ORTUS_STEALTH_ALLOW_GITIGNORE" not in env
    assert not Path(env["UV_CACHE_DIR"]).is_relative_to(repo)


@pytest.mark.integration
def test_gitignore_stealth_grind_halts_and_flags_human(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, issue_id, worker, result = _stealth_grind(
        tmp_path, monkeypatch, scope="Edit `src/a.py`.", touch=True
    )

    assert result.exit_code == 1, result.output
    assert "human" in _labels(repo, issue_id)
    assert "STEALTH-GITIGNORE" in run_bd(repo, "comments", issue_id, "--json")


@pytest.mark.integration
def test_gitignore_stealth_grind_scope_allows_with_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, issue_id, worker, result = _stealth_grind(
        tmp_path, monkeypatch, scope="Add `eval-out/` to `.gitignore`.", touch=True
    )

    assert worker.envs, result.output
    assert worker.envs[0].get("ORTUS_STEALTH_ALLOW_GITIGNORE") == "1"
    assert "STEALTH-GITIGNORE" not in run_bd(repo, "comments", issue_id, "--json")


# --- AC-4: no housekeeping commit, filtered commit paths --------------------


class _PreflightGit:
    def __init__(self) -> None:
        self.dirty = frozenset({".beads/issues.jsonl"})
        self.commits: list[str] = []

    def is_git_repo(self) -> bool:
        return True

    def dirty_paths(self) -> frozenset[str]:
        return self.dirty

    def commit_paths(self, paths: frozenset[str], message: str) -> bool:
        self.commits.append(message)
        self.dirty = frozenset()
        return True


def test_housekeeping_commit_skipped_in_stealth(monkeypatch: pytest.MonkeyPatch) -> None:
    git = _PreflightGit()
    lines: list[str] = []
    grind_mod._checkpoint_codex_preflight(
        git, "main", lines.append, accept_baseline=True, stealth=True  # type: ignore[arg-type]
    )
    assert git.commits == []
    assert any("skipping tracker housekeeping commit" in line for line in lines)


def test_housekeeping_commit_kept_outside_stealth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(grind_mod, "_enforce_branch_discipline", lambda *a, **k: None)
    git = _PreflightGit()
    grind_mod._checkpoint_codex_preflight(
        git, "main", lambda _line: None, accept_baseline=True  # type: ignore[arg-type]
    )
    assert git.commits == ["chore: sync beads state"]


def _stealth_tree(tmp_path: Path) -> Path:
    repo = _git_repo(tmp_path / "fork")
    exclude = repo / ".git" / "info" / "exclude"
    exclude.write_text("/.beads/\n", encoding="utf-8")
    (repo / ".beads").mkdir()
    (repo / ".beads" / "issues.jsonl").write_text("{}\n", encoding="utf-8")
    (repo / ".gitignore").write_text("x\n", encoding="utf-8")
    (repo / "a.py").write_text("a = 1\n", encoding="utf-8")
    return repo


def test_commit_paths_drops_excluded_and_gitignore(tmp_path: Path) -> None:
    repo = _stealth_tree(tmp_path)
    lines: list[str] = []
    git = GitClient(repo, stealth=True, log=lines.append)

    result = git.commit_paths(
        frozenset({"a.py", ".gitignore", ".beads/issues.jsonl"}), "add a"
    )

    assert result
    assert _head_files(repo) == {"a.py"}
    assert lines and ".gitignore" in lines[0] and ".beads/issues.jsonl" in lines[0]


def test_commit_paths_amend_drops_excluded(tmp_path: Path) -> None:
    repo = _stealth_tree(tmp_path)
    git = GitClient(repo, stealth=True)
    assert git.commit_paths(frozenset({"a.py"}), "add a")
    (repo / "a.py").write_text("a = 2\n", encoding="utf-8")

    assert git.amend_paths(frozenset({"a.py", ".gitignore", ".beads/issues.jsonl"}))
    assert _head_files(repo) == {"a.py"}


def test_commit_paths_unfiltered_outside_stealth(tmp_path: Path) -> None:
    repo = _stealth_tree(tmp_path)
    git = GitClient(repo)
    assert git.commit_paths(frozenset({"a.py", ".gitignore"}), "add both")
    assert _head_files(repo) == {"a.py", ".gitignore"}


# --- AC-5: cache root --------------------------------------------------------


def test_cache_root_outside_tree_in_stealth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "fork"
    repo.mkdir()
    monkeypatch.setenv("TMPDIR", str(tmp_path / "scratch"))
    root = cache.cache_root(repo, stealth=True)
    assert not root.is_relative_to(repo)
    assert root.parent == tmp_path / "scratch" / "ortus-cache"
    assert root == cache.cache_root(repo, stealth=True)
    assert cache.env_overrides(repo, stealth=True)["UV_CACHE_DIR"] == str(root / "uv")
    assert cache.ensure_cache_dirs(repo, stealth=True) == root
    assert (root / "uv").is_dir()


def test_cache_root_tmp_without_tmpdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TMPDIR", raising=False)
    root = cache.cache_root(tmp_path, stealth=True)
    assert root.parent == Path("/tmp/ortus-cache")


def test_cache_root_in_tree_outside_stealth(tmp_path: Path) -> None:
    assert cache.cache_root(tmp_path) == tmp_path / ".cache"
    assert cache.env_overrides(tmp_path)["XDG_CACHE_HOME"] == str(tmp_path / ".cache")
