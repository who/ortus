"""Central wrapper for subprocess.run(['claude', '-p', ...]).

Standard flag set per PRD (FR-013, ortus-6q8v non-regression):
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
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO


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

    def build_argv(self, prompt: str, *, fast: bool = False) -> list[str]:
        argv: list[str] = [self.claude_binary, "-p", prompt]
        argv.extend(STANDARD_FLAGS)
        if fast:
            argv.append("--fast")
        return argv

    def run(
        self,
        prompt: str,
        *,
        repo: Path,
        log_path: Path,
        fast: bool = False,
        timeout: float | None = None,
    ) -> int:
        """Spawn claude, tee output to log_path (NOT stdout), return exit code.

        Raises subprocess.TimeoutExpired if timeout is exceeded; the child
        and its process group are SIGKILL'd before the exception propagates.
        """
        argv = self.build_argv(prompt, fast=fast)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, **self.extra_env}

        # Open log_path in line-buffered append mode. Both stdout and stderr
        # go straight to the file; the parent's terminal sees nothing.
        with open(log_path, "ab", buffering=0) as log_fh:
            proc = subprocess.Popen(
                argv,
                cwd=str(repo),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_fh,
                stderr=log_fh,
                start_new_session=True,
            )
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


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGKILL the child's process group (no orphan claude PIDs)."""
    if proc.poll() is not None:
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
