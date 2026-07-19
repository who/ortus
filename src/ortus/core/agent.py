"""Agent backend selection and runner construction.

Claude remains Ortus's default.  The Codex backend deliberately uses a plain
``codex exec`` prompt: slash commands are an interactive Codex surface and a
literal ``/goal`` passed to ``codex exec`` does not activate Goal mode.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, cast

from ortus.core.claude import ClaudeRunner
from ortus.core.config import load_config

Backend = Literal["claude", "codex"]
BACKENDS: tuple[Backend, ...] = ("claude", "codex")


class BackendError(ValueError):
    """Raised when an unsupported backend name is configured."""


class CodexRunner(ClaudeRunner):
    """Run one plain, non-interactive Codex task and log its JSONL stream."""

    def __init__(
        self,
        codex_binary: str = "codex",
        *,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            claude_binary=codex_binary,
            extra_env={} if extra_env is None else extra_env,
        )

    @property
    def codex_binary(self) -> str:
        return self.claude_binary

    def build_argv(self, prompt: str, *, fast: bool = False) -> list[str]:
        # `fast` is intentionally ignored. Codex service-tier selection is a
        # Codex configuration concern and is not equivalent to Claude --fast.
        return [
            self.codex_binary,
            "exec",
            prompt,
            "--json",
            "--sandbox",
            "workspace-write",
            "--color",
            "never",
        ]


def resolve_backend(
    requested: str | None = None,
    *,
    repo: Path | None = None,
    home: Path | None = None,
) -> Backend:
    """Resolve flag > environment > project/user config > Claude default."""
    configured = load_config(repo=repo, home=home).get("backend", "claude")
    name = requested or os.environ.get("ORTUS_BACKEND") or configured
    if name not in BACKENDS:
        raise BackendError(
            f"unknown backend {name!r}; expected one of: {', '.join(BACKENDS)}"
        )
    return cast(Backend, name)


def make_runner(backend: Backend) -> ClaudeRunner:
    if backend == "codex":
        return CodexRunner()
    return ClaudeRunner()


def compose_worker_prompt(backend: Backend, task: str) -> str:
    """Wrap a logical worker task for the selected execution surface."""
    if backend == "claude":
        return f"/goal {task}"
    return (
        task
        + "\n\nCodex sandbox note: `.git` metadata is intentionally read-only in "
        "the workspace-write sandbox. Replace procedure step (3) with: do not "
        "run `git commit` or `git push`; after you close the one assigned issue, "
        "the outer Ortus process will commit and push the completed work."
    )
