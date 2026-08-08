"""Central wrapper for subprocess.run(['claude', '-p', ...]).

Standard flag set (ortus-6q8v non-regression):
  --dangerously-skip-permissions
  --output-format stream-json
  --verbose
  --fast                       (only when fast=True)

stdout/stderr are tee'd to log_path; the launching terminal sees NOTHING.
Signals to the parent (SIGINT/SIGTERM) kill the child process group so no
descendant claude PIDs leak.
"""

from __future__ import annotations

import os
import platform
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from ortus.core.profiles import AgentProfile

# Windows lacks setsid(), getpgid(), killpg(), and SIGKILL. The process-group
# reap path collapses to proc.terminate()/.kill() on the parent PID — Windows
# has no first-class process-group abstraction the way POSIX does, so we trust
# Popen.terminate() to issue TerminateProcess on the child.
_IS_WINDOWS = sys.platform == "win32"


STANDARD_FLAGS = (
    "--dangerously-skip-permissions",
    "--output-format",
    "stream-json",
    "--verbose",
)


@dataclass
class ClaudeRunner:
    """Runs `claude -p <prompt>` with the standard flag set, tee'd to log_path."""

    claude_binary: str = "claude"
    extra_env: dict[str, str] = field(default_factory=dict)

    def build_argv(
        self,
        prompt: str,
        *,
        fast: bool = False,
        profile: AgentProfile | None = None,
        readonly: bool = False,
    ) -> list[str]:
        argv: list[str] = [self.claude_binary, "-p", prompt]
        argv.extend(STANDARD_FLAGS)
        if profile is not None and profile.model is not None:
            argv.extend(["--model", profile.model])
        if profile is not None and profile.reasoning_effort is not None:
            argv.extend(["--effort", profile.reasoning_effort])
        if fast:
            argv.append("--fast")
        if readonly:
            # Defense in depth: the OS sandbox in ``run`` is authoritative;
            # these provider controls also keep write tools out of the model's
            # advertised surface.
            argv.extend(
                [
                    "--permission-mode",
                    "dontAsk",
                    "--disallowedTools",
                    "Write,Edit,NotebookEdit",
                ]
            )
        return argv

    def run(
        self,
        prompt: str,
        *,
        repo: Path,
        log_path: Path,
        fast: bool = False,
        profile: AgentProfile | None = None,
        timeout: float | None = None,
        readonly: bool = False,
    ) -> int:
        """Spawn claude, tee output to log_path (NOT stdout), return exit code.

        Raises subprocess.TimeoutExpired if timeout is exceeded; the child
        and its process group are SIGKILL'd before the exception propagates.
        """
        argv = self.build_argv(prompt, fast=fast, profile=profile, readonly=readonly)
        if readonly:
            argv = self._readonly_argv(argv, repo)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, **self.extra_env}
        if readonly:
            env.update(
                {
                    "XDG_CACHE_HOME": "/tmp/ortus-verifier-cache",
                    "UV_CACHE_DIR": "/tmp/ortus-verifier-cache/uv",
                    "PYTHONDONTWRITEBYTECODE": "1",
                }
            )

        # Open log_path in line-buffered append mode. Both stdout and stderr
        # go straight to the file; the parent's terminal sees nothing.
        with open(log_path, "ab", buffering=0) as log_fh:
            popen_kwargs: dict = dict(
                cwd=str(repo),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_fh,
                stderr=log_fh,
            )
            if not _IS_WINDOWS:
                # POSIX: detach into a new session so SIGINT propagates to the
                # process group, not just the parent. Windows has no setsid()
                # equivalent; we fall back to per-PID termination in _kill_group.
                popen_kwargs["start_new_session"] = True
            proc = subprocess.Popen(argv, **popen_kwargs)
            try:
                return proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_group(proc)
                raise
            except KeyboardInterrupt:
                _kill_group(proc)
                raise
            finally:
                # If the child somehow survived a normal-path exit (shouldn't,
                # since wait() blocks), reap its process group to mirror
                # goal.sh's cleanup_children trap.
                if proc.poll() is None:
                    _kill_group(proc)

    def _readonly_argv(self, argv: list[str], repo: Path) -> list[str]:
        """Apply the backend's OS-level read-only launch posture."""

        return _readonly_wrapper(argv, repo)


def _kill_group(proc: subprocess.Popen) -> None:
    """Terminate the child (and on POSIX, its process group)."""
    if proc.poll() is not None:
        return
    if _IS_WINDOWS:
        # Windows has no killpg; rely on TerminateProcess via Popen helpers.
        # If the child spawned its own children (e.g. cmd.exe wrapper),
        # those are orphaned — there is no portable Job Object plumbing here.
        try:
            proc.terminate()
            try:
                proc.wait(timeout=2)
                return
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        except (OSError, ProcessLookupError):
            pass
        return
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
        try:
            proc.wait(timeout=2)
            return
        except subprocess.TimeoutExpired:
            os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


# Directories the agent CLI writes under $HOME on every run: a per-session env
# dir, shell snapshots, session transcripts, per-project state. `--ro-bind / /`
# makes all of them read-only, and the CLI then fails to start a shell at all
# ("EROFS: read-only file system, mkdir .../session-env/<uuid>"), so every
# verification returns each criterion BLOCKED instead of judged (ortus-dyio).
# Each gets an empty tmpfs: writable, discarded with the sandbox, and carrying
# nothing from the host session into the verifier.
_AGENT_SCRATCH_DIRS: tuple[str, ...] = (
    ".claude/session-env",
    ".claude/shell-snapshots",
    ".claude/sessions",
    ".claude/projects",
    ".claude/file-history",
    ".claude/paste-cache",
)


def _agent_scratch_tmpfs(home: Path) -> list[str]:
    """`--tmpfs` args for agent scratch dirs that exist on this host.

    A tmpfs needs its mountpoint to already exist: the root is bound read-only,
    so bwrap cannot create a missing one. Skipping absent paths keeps the
    wrapper working across CLI versions that add or drop a directory.
    """

    args: list[str] = []
    for relative in _AGENT_SCRATCH_DIRS:
        candidate = home / relative
        if candidate.is_dir():
            args.extend(["--tmpfs", str(candidate)])
    return args


def _readonly_wrapper(argv: list[str], repo: Path) -> list[str]:
    """Apply a source-read-only OS posture while leaving /tmp writable."""

    system = platform.system()
    if system == "Linux":
        wrapper = [
            "bwrap",
            "--ro-bind",
            "/",
            "/",
            "--dev-bind",
            "/dev",
            "/dev",
            "--proc",
            "/proc",
            "--tmpfs",
            "/tmp",
            *_agent_scratch_tmpfs(Path.home()),
        ]
        resolved_repo = repo.resolve()
        try:
            relative_to_tmp = resolved_repo.relative_to("/tmp")
        except ValueError:
            pass
        else:
            current = Path("/tmp")
            for part in relative_to_tmp.parts[:-1]:
                current /= part
                wrapper.extend(["--dir", str(current)])
            wrapper.extend(["--ro-bind", str(resolved_repo), str(resolved_repo)])
        return [*wrapper, "--chdir", str(resolved_repo), "--", *argv]
    if system == "Darwin":
        repo_literal = str(repo.resolve()).replace("\\", "\\\\").replace('"', '\\"')
        profile = (
            "(version 1) (allow default) (deny file-write*) "
            '(allow file-write* (subpath "/tmp") (subpath "/private/tmp") '
            '(subpath "/dev")) '
            f'(deny file-write* (subpath "{repo_literal}"))'
        )
        return ["sandbox-exec", "-p", profile, *argv]
    raise RuntimeError(f"read-only verifier unsupported on {system}")
