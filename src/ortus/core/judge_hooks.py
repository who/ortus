"""Parent-owned registration and escalation for one Claude worker.

Settings add a hook through Claude's settings merge; they never copy or
rewrite operator settings. The private files share the worker's OS identity,
so this supplements policy inside that trust boundary, not outside it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from ortus.core import hooks
from ortus.core.agent import BackendError
from ortus.core.bd import BdClient
from ortus.core.claude import ClaudeRunner
from ortus.core.judge import JudgeConfig
from ortus.core.judge_hook import CONTEXT_ENV, _private, read_object


def check_pre_tool(repo: Path, backend: str, *, docker: bool = False) -> None:
    """Refuse a launch whose hook registration cannot be honored."""
    if backend != "claude":
        raise BackendError("judge.pre_tool supports only Claude; selected backend is unsupported")
    if docker:
        raise BackendError("judge.pre_tool: Docker hook context access is unavailable")
    try:
        hooks.check_hooks_enabled(repo)
    except hooks.HookConflictError:
        raise BackendError("judge.pre_tool: Claude settings disable hooks") from None
    # The goal precheck predates settings.local.json and managed-only hooks.
    # Inspect all hook layers strictly for this explicitly enabled feature.
    layers = hooks._candidate_layers(repo, Path.home())
    layers.append(repo / ".claude" / "settings.local.json")
    for path in layers:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError
        except (OSError, ValueError):
            raise BackendError("judge.pre_tool: cannot read Claude hook settings") from None
        if data.get("disableAllHooks") is True or data.get("allowManagedHooksOnly") is True:
            raise BackendError("judge.pre_tool: Claude settings disable run-scoped hooks")


class HookRun:
    """Register before spawn, poll without tracker writes, clean up after reap."""

    def __init__(self, repo: Path, issue_id: str, config: JudgeConfig, run_id: str,
                 runner: ClaudeRunner):
        self.issue_id = issue_id
        self.run_id = run_id
        self.session_id = str(uuid4())
        self.session_hash = hashlib.sha256(self.session_id.encode()).hexdigest()
        self.requested = False
        self.seen: set[str] = set()
        self.failure = False
        self.escalated = False
        self.runner = runner
        self.previous_env = runner.extra_env
        self.previous_settings = runner.hook_settings
        self.previous_session = runner.hook_session_id
        # tempfile resolves the platform temporary root, so a sandbox that
        # exports TMPDIR over a read-only /tmp still gets a writable inbox.
        self.temporary = TemporaryDirectory(prefix="ortus-judge-hook-")
        self.directory = Path(self.temporary.name).resolve()
        # The worker owns the repository tree. A temporary root inside it would
        # hand the worker its own context and signal inbox, so refuse the launch
        # rather than register a hook the worker can rewrite.
        if self.directory.is_relative_to(repo.resolve()):
            self.temporary.cleanup()
            raise BackendError(
                "judge.pre_tool: temporary directory is inside the repository; "
                "point TMPDIR outside the working tree"
            )
        self.context_path = self.directory / "context.json"
        self.settings_path = self.directory / "settings.json"
        try:
            self._write(self.context_path, {
                "version": 1, "repo": str(repo.resolve()), "issue_id": issue_id,
                "session_id": self.session_id, "run_id": run_id,
                "judge": asdict(config),
            })
            # Do not resolve the interpreter symlink: that would lose its venv.
            python = os.path.abspath(sys.executable)
            command = shlex.join([python, "-m", "ortus.core.judge_hook"])
            self._write(self.settings_path, {"hooks": {"PreToolUse": [{
                "matcher": "*", "hooks": [{"type": "command", "command": command,
                                           "timeout": 6}],
            }]}})
            env = {**os.environ, **self.previous_env, CONTEXT_ENV: str(self.context_path)}
            # Native command hooks run beside the CLI, outside its Bash tool
            # sandbox. Prove import and private-context access there before spawn.
            probe = subprocess.run(
                [python, "-c", "import os; from ortus.core.judge_hook import load_context; "
                 "load_context(os.environ)"], cwd=repo, env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=5, check=False,
            )
            if probe.returncode:
                raise BackendError("judge.pre_tool: hook context preflight failed")
            runner.extra_env = {**self.previous_env, CONTEXT_ENV: str(self.context_path)}
            runner.hook_settings = self.settings_path
            runner.hook_session_id = self.session_id
        except (OSError, subprocess.TimeoutExpired):
            self.close()
            raise BackendError("judge.pre_tool: hook context access is unavailable") from None
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _write(path: Path, body: dict) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(body, stream, sort_keys=True)

    def poll(self) -> bool:
        """Accept only complete signals bound to this issue, run and session."""
        try:
            paths = list(self.directory.iterdir())
        except OSError:
            self.failure = True
            return True
        for path in paths:
            match = re.fullmatch(r"human-([0-9a-f]{32})\.json", path.name)
            if not match or match[1] in self.seen:
                continue
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, "rb") as stream:
                    _private(os.fstat(stream.fileno()))
                    body = read_object(stream)
            except (OSError, ValueError):
                continue
            if body != {
                "version": 1, "call_id": match[1], "issue_id": self.issue_id,
                "run_id": self.run_id, "session_hash": self.session_hash,
                "reason": "needs_human",
            } or type(body.get("version")) is not int:
                continue
            self.seen.add(match[1])
            self.requested = True
        return self.requested or self.failure

    def escalate(self, bd: BdClient) -> None:
        """Call only after the worker's process group has been reaped."""
        if self.failure:
            raise BackendError("judge.pre_tool: signal inbox unavailable; worker stopped")
        if self.requested and not self.escalated:
            bd.add_label(self.issue_id, "human")
            bd.add_comment(self.issue_id, "judge pre_tool: human reason=needs_human")
            self.escalated = True

    def close(self) -> None:
        self.runner.extra_env = self.previous_env
        self.runner.hook_settings = self.previous_settings
        self.runner.hook_session_id = self.previous_session
        self.temporary.cleanup()
