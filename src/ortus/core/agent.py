"""Agent backend selection and runner construction.

Claude remains Ortus's default.  The Codex backend deliberately uses a plain
``codex exec`` prompt: slash commands are an interactive Codex surface and a
literal ``/goal`` passed to ``codex exec`` does not activate Goal mode.
"""

from __future__ import annotations

import os
import json
from pathlib import Path
from typing import Literal, cast

from ortus.core.claude import ClaudeRunner
from ortus.core.config import load_config
from ortus.core.codegraph import CodeGraphCapability
from ortus.core.profiles import AgentProfile, Phase as Phase

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
        codegraph: CodeGraphCapability | None = None,
        sandbox_mode: str = "workspace-write",
    ) -> None:
        super().__init__(
            claude_binary=codex_binary,
            extra_env={} if extra_env is None else extra_env,
        )
        self.codegraph = codegraph
        self.sandbox_mode = sandbox_mode

    def configure_codegraph(self, capability: CodeGraphCapability | None) -> None:
        """Apply the capability produced by the outer probe to future launches."""
        self.codegraph = capability

    def run_codegraph_handshake(
        self,
        *,
        phase: str,
        repo: Path,
        log_path: Path,
        profile: AgentProfile | None = None,
        timeout: float | None = None,
    ) -> int:
        """Run a query-only child in a read-only sandbox before phase work."""
        prior_sandbox = self.sandbox_mode
        self.sandbox_mode = "read-only"
        try:
            prompt = (
                "CodeGraph capability handshake only. Do not call shell tools and do not "
                "edit files. Call codegraph_explore exactly once with the bounded query "
                f"'Orient to this repository for the {phase} phase', then stop."
            )
            return super().run(
                prompt,
                repo=repo,
                log_path=log_path,
                profile=profile,
                timeout=timeout,
            )
        finally:
            self.sandbox_mode = prior_sandbox

    @property
    def codex_binary(self) -> str:
        return self.claude_binary

    def build_argv(
        self,
        prompt: str,
        *,
        fast: bool = False,
        profile: AgentProfile | None = None,
    ) -> list[str]:
        # `fast` is intentionally ignored. Codex service-tier selection is a
        # Codex configuration concern and is not equivalent to Claude --fast.
        argv = [
            self.codex_binary,
            "exec",
            prompt,
            "--json",
            "--sandbox",
            self.sandbox_mode,
            "--color",
            "never",
        ]
        if self.codegraph is not None:
            # CLI overrides are trusted launch inputs and do not depend on a
            # repository's trust state. Values contain only an executable path,
            # fixed arguments, and an allowlist; no environment or credentials.
            argv.extend(
                [
                    "-c",
                    "mcp_servers.codegraph.command="
                    + json.dumps(self.codegraph.command),
                    "-c",
                    "mcp_servers.codegraph.args=" + json.dumps(self.codegraph.args),
                    "-c",
                    "mcp_servers.codegraph.enabled_tools="
                    + json.dumps(self.codegraph.tools),
                ]
            )
        if profile is not None and profile.model is not None:
            argv.extend(["-m", profile.model])
        if profile is not None and profile.reasoning_effort is not None:
            argv.extend(["-c", f"model_reasoning_effort={profile.reasoning_effort}"])
        return argv


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
        task + "\n\nCodex sandbox note: `.git` metadata is intentionally read-only in "
        "the workspace-write sandbox. Replace procedure step (3) with: do not "
        "run `git commit` or `git push`; after you close the one assigned issue, "
        "the outer Ortus process will commit and push the completed work."
    )
