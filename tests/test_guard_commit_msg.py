"""The 72-character commit-subject limit, enforced before the commit exists."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.core.worker_guard import (
    ALLOW_FORCE_PUSH_ENV,
    ALLOW_LONG_SUBJECT_ENV,
    COMMIT_MSG_BEGIN,
    GRIND_WORKER_ENV,
    commit_subject_violation,
    install_commit_msg_hook,
    subject_violation,
)

runner = CliRunner()

SUBJECT_72 = "ortus-abcd: " + "x" * 60
SUBJECT_73 = SUBJECT_72 + "y"
LIMIT_TEXT = "commit subject is 73 characters; the limit is 72"


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=False, env=env
    )


def _commit_msg(tmp_path: Path, message: str, env: dict[str, str] | None = None):
    path = tmp_path / "COMMIT_EDITMSG"
    path.write_text(message, encoding="utf-8")
    return runner.invoke(app, ["guard", "commit-msg", str(path)], env=env)


# AC-1


def test_limit_refuses_73_and_allows_72(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ALLOW_LONG_SUBJECT_ENV, raising=False)
    assert len(SUBJECT_72) == 72 and len(SUBJECT_73) == 73
    refused = _commit_msg(tmp_path, f"{SUBJECT_73}\n\nbody\n")
    assert refused.exit_code == 1
    assert (
        "ortus guard: commit subject is 73 characters; the limit is 72 "
        "(issue-id prefix included). Shorten it and commit again."
    ) in refused.stderr
    assert _commit_msg(tmp_path, f"{SUBJECT_72}\n\nbody\n").exit_code == 0


def test_limit_reads_past_comment_lines(tmp_path: Path) -> None:
    message = f"# Please enter the commit message\n{SUBJECT_73}\n# trailing comment\n"
    assert _commit_msg(tmp_path, message).exit_code == 1
    long_comment = "# " + "c" * 100 + f"\n{SUBJECT_72}\n"
    assert _commit_msg(tmp_path, long_comment).exit_code == 0


@pytest.mark.parametrize(
    "subject",
    [
        "Merge branch 'feature/" + "z" * 80 + "'",
        "fixup! " + "z" * 80,
        "squash! " + "z" * 80,
        'Revert "' + "z" * 80 + '"',
    ],
)
def test_limit_exempts_git_generated_subjects(subject: str) -> None:
    assert subject_violation(subject + "\n") is None


def test_limit_escape_hatch(tmp_path: Path) -> None:
    result = _commit_msg(tmp_path, SUBJECT_73, env={ALLOW_LONG_SUBJECT_ENV: "1"})
    assert result.exit_code == 0


# AC-2


def _stub_init(monkeypatch: pytest.MonkeyPatch) -> None:
    import ortus.commands.init as init_mod

    def bd_init(repo: Path, prefix: str | None) -> None:
        (repo / ".beads").mkdir(exist_ok=True)

    monkeypatch.setattr(init_mod, "_bd_init", bd_init)
    monkeypatch.setattr(init_mod, "_bd_no_git_ops", lambda repo: None)
    monkeypatch.setattr(init_mod, "_bd_remember", lambda repo: None)
    monkeypatch.setattr(init_mod, "_bd_hooks_install", lambda repo: None)
    monkeypatch.setattr(init_mod, "_codegraph_cli", lambda: "/usr/bin/codegraph")
    monkeypatch.setattr(
        init_mod,
        "_codegraph_index",
        lambda repo, **kw: (repo / ".codegraph").mkdir(parents=True, exist_ok=True),
    )
    monkeypatch.setattr(init_mod, "_backend_cli", lambda name: f"/usr/bin/{name}")


def _hook_env() -> dict[str, str]:
    """The test's environment with this interpreter's `ortus` first on PATH."""
    env = {k: v for k, v in os.environ.items() if k != ALLOW_LONG_SUBJECT_ENV}
    env["PATH"] = f"{Path(sys.executable).parent}{os.pathsep}{env.get('PATH', '')}"
    return env


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    return repo


def _commit_count(repo: Path) -> int:
    proc = _git(repo, "rev-list", "--count", "--all")
    return int(proc.stdout.strip() or 0) if proc.returncode == 0 else 0


def test_init_hook_refuses_a_long_subject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_init(monkeypatch)
    repo = _repo(tmp_path)
    result = runner.invoke(app, ["init", str(repo), "--project-type", "python"])
    assert result.exit_code == 0, (result.stdout or "") + (result.stderr or "")
    hook = repo / ".git" / "hooks" / "commit-msg"
    assert COMMIT_MSG_BEGIN in hook.read_text(encoding="utf-8")
    assert os.access(hook, os.X_OK)

    env = _hook_env()
    refused = _git(repo, "commit", "--allow-empty", "-m", SUBJECT_73, env=env)
    assert refused.returncode != 0
    assert LIMIT_TEXT in refused.stderr
    assert _commit_count(repo) == 0

    allowed = _git(repo, "commit", "--allow-empty", "-m", SUBJECT_72, env=env)
    assert allowed.returncode == 0, allowed.stderr
    assert _commit_count(repo) == 1


def test_init_hook_honours_hooks_path_and_keeps_foreign_lines(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    hooks = repo / ".beads" / "hooks"
    hooks.mkdir(parents=True)
    _git(repo, "config", "core.hooksPath", ".beads/hooks")
    foreign = "#!/bin/sh\necho project commit-msg hook\n"
    (hooks / "commit-msg").write_text(foreign, encoding="utf-8")

    import ortus.commands.init as init_mod

    hook = init_mod._install_commit_msg_hook(repo)
    assert hook == hooks / "commit-msg"
    once = hook.read_text(encoding="utf-8")
    assert once.startswith(foreign) and COMMIT_MSG_BEGIN in once
    # A re-install replaces its own section rather than appending a second.
    install_commit_msg_hook(hooks)
    assert hook.read_text(encoding="utf-8") == once

    refused = _git(repo, "commit", "--allow-empty", "-m", SUBJECT_73, env=_hook_env())
    assert refused.returncode != 0
    assert _commit_count(repo) == 0


# AC-3


def _worker_hook(command: str, env: dict[str, str]):
    payload = {"tool_name": "Bash", "tool_input": {"command": command}}
    return runner.invoke(app, ["guard", "worker-bash"], input=json.dumps(payload), env=env)


def test_worker_bash_refuses_a_long_commit_subject(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ALLOW_LONG_SUBJECT_ENV, raising=False)
    monkeypatch.delenv(ALLOW_FORCE_PUSH_ENV, raising=False)
    env = {GRIND_WORKER_ENV: "1"}
    refused = _worker_hook(f'git commit -m "{SUBJECT_73}" -m "body"', env)
    assert refused.exit_code == 2
    assert LIMIT_TEXT in refused.stderr
    assert _worker_hook(f'git commit -m "{SUBJECT_72}"', env).exit_code == 0
    # The force-push escape hatch does not waive the subject limit.
    assert _worker_hook(f'git commit -m "{SUBJECT_73}"', {**env, ALLOW_FORCE_PUSH_ENV: "1"}).exit_code == 2
    assert _worker_hook(f'git commit -m "{SUBJECT_73}"', {**env, ALLOW_LONG_SUBJECT_ENV: "1"}).exit_code == 0


def test_worker_bash_inactive_outside_a_grind_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(GRIND_WORKER_ENV, raising=False)
    assert _worker_hook(f'git commit -m "{SUBJECT_73}"', {}).exit_code == 0


@pytest.mark.parametrize(
    "command",
    [
        f'git add -A && git commit -am "{SUBJECT_73}"',
        f"git commit --message={SUBJECT_73.replace(' ', '_')}",
        f'git -C sub commit --message "{SUBJECT_73}"',
        f'git commit -m"{SUBJECT_73}"',
        f"git commit -m \"$(cat <<'EOF'\n{SUBJECT_73}\n\nBody paragraph.\nEOF\n)\"",
    ],
)
def test_worker_bash_subject_spellings(command: str) -> None:
    assert commit_subject_violation(command) is not None


@pytest.mark.parametrize(
    "command",
    [
        f'git commit -m "{SUBJECT_72}" -m "{SUBJECT_73}"',
        f'git commit -F msg.txt -m "{SUBJECT_72}"',
        'git commit -m "$(cat msg.txt)"',
        f'echo "git commit -m {SUBJECT_73}" > notes.txt',
        "git commit",
        f'git log --grep "{SUBJECT_73}"',
    ],
)
def test_worker_bash_subject_passes(command: str) -> None:
    assert commit_subject_violation(command) is None
