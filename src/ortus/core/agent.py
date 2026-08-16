"""Agent backend selection and runner construction.

Claude remains Ortus's default.  The Codex backend deliberately uses a plain
``codex exec`` prompt: slash commands are an interactive Codex surface and a
literal ``/goal`` passed to ``codex exec`` does not activate Goal mode.

Grok Build expands ``/goal`` on ``grok -p`` (memory ``grok-backend-q1`` =
EXPANDS), so that backend uses the same wrap as Claude.  GrokRunner is a
sibling of ClaudeRunner, not a subclass: Grok's sandbox and approval flags
are not Claude's.
"""

from __future__ import annotations

import os
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast

from ortus.core.claude import ClaudeRunner, _spawn_logged
from ortus.core.config import INIT_ONLY_BACKEND_MESSAGE, load_config
from ortus.core.codegraph import CodeGraphCapability
from ortus.core.profiles import AgentProfile, Phase as Phase

Backend = Literal["claude", "codex", "grok"]
BACKENDS: tuple[Backend, ...] = ("claude", "codex", "grok")

# Isolated probe 2026-08-13: grok -p '/goal …' is consumed by the host goal
# driver. wrap_grok_prompt still accepts q1= so a VERBATIM re-probe can flip
# the wrap without inventing a third termination shape.
GROK_GOAL_MODE = "EXPANDS"


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

    @property
    def codex_binary(self) -> str:
        return self.claude_binary

    def build_argv(
        self,
        prompt: str,
        *,
        fast: bool = False,
        profile: AgentProfile | None = None,
        readonly: bool = False,
        resume: str | None = None,
    ) -> list[str]:
        # `fast` is intentionally ignored. Codex service-tier selection is a
        # Codex configuration concern and is not equivalent to Claude --fast.
        # `resume` is accepted for signature parity and ignored: grind never
        # captures a Codex session id, so corrections on this backend run in
        # a fresh context carrying the pipeline record (logged as degraded).
        argv = [
            self.codex_binary,
            "exec",
            prompt,
            "--json",
            "--sandbox",
            "read-only" if readonly else self.sandbox_mode,
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

    def _readonly_argv(self, argv: list[str], repo: Path) -> list[str]:
        """Trust Codex's native sandbox while leaving its runtime state writable.

        Wrapping the whole process in the Claude-oriented read-only filesystem
        sandbox also makes Codex's app-server and session directories read-only,
        preventing the verifier from starting at all. ``--sandbox read-only``
        already protects the repository for this backend.
        """

        return argv

    def preflight_readonly(self, repo: Path, *, timeout: float = 60.0) -> None:
        """No read-only posture to probe: nothing wraps the Codex process.

        Mirrors `_readonly_argv`. Codex verifies under its own ``--sandbox
        read-only``, which leaves its runtime directories writable, so the
        blocked-execution failure the Claude preflight guards cannot arise
        here — and probing would only write to the host on its behalf.
        """

        return None


@dataclass
class GrokRunner:
    """Run one non-interactive Grok Build task and log its streaming-json stream.

    Sibling of ClaudeRunner, not a subclass: Grok's ``--sandbox`` /
    ``--always-approve`` surface is not Claude's permission-mode set, and
    stuffing the grok binary into ``claude_binary`` would hide that.
    """

    grok_binary: str = "grok"
    extra_env: dict[str, str] = field(default_factory=dict)
    codegraph: CodeGraphCapability | None = None
    sandbox_mode: str = "workspace"

    def configure_codegraph(self, capability: CodeGraphCapability | None) -> None:
        """Store the outer probe result. MCP registration is a later leaf."""
        self.codegraph = capability

    def build_argv(
        self,
        prompt: str,
        *,
        fast: bool = False,
        profile: AgentProfile | None = None,
        readonly: bool = False,
        resume: str | None = None,
    ) -> list[str]:
        # `fast` is intentionally ignored. Grok has no Claude-equivalent --fast
        # tier flag. `codegraph` is stored for later MCP wiring; this leaf does
        # not emit grok -c / grok mcp overrides.
        argv = [
            self.grok_binary,
            "-p",
            prompt,
            "--output-format",
            "streaming-json",
            "--sandbox",
            "read-only" if readonly else self.sandbox_mode,
            "--always-approve",
        ]
        if resume:
            argv.extend(["--resume", resume])
        if profile is not None and profile.model is not None:
            argv.extend(["-m", profile.model])
        if profile is not None and profile.reasoning_effort is not None:
            argv.extend(["--effort", profile.reasoning_effort])
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
        resume: str | None = None,
        reap_when: Callable[[], bool] | None = None,
        reap_poll: float = 2.0,
        on_poll: Callable[[], None] | None = None,
    ) -> int:
        """Spawn grok, tee output to log_path (NOT stdout), return exit code."""
        argv = self.build_argv(
            prompt, fast=fast, profile=profile, readonly=readonly, resume=resume
        )
        if readonly:
            argv = self._readonly_argv(argv, repo)
        return _spawn_logged(
            argv,
            repo=repo,
            log_path=log_path,
            extra_env=self.extra_env,
            timeout=timeout,
            readonly=readonly,
            reap_when=reap_when,
            reap_poll=reap_poll,
            on_poll=on_poll,
        )

    def _readonly_argv(self, argv: list[str], repo: Path) -> list[str]:
        """Trust Grok's native sandbox; do not wrap the process in bwrap.

        ``--sandbox read-only`` already protects the repository and leaves
        ``~/.grok/`` writable for session state. An outer read-only root would
        prevent the verifier from starting, same failure Codex already
        documented.
        """

        return argv

    def preflight_readonly(self, repo: Path, *, timeout: float = 60.0) -> None:
        """No Ortus-owned read-only wrapper to probe."""

        return None


def wrap_grok_prompt(task: str, *, q1: str = GROK_GOAL_MODE) -> str:
    """Wrap a logical worker task for ``grok -p`` given the Q1 finding.

    EXPANDS (the recorded finding) uses the same ``/goal`` wrap as Claude.
    VERBATIM must never be handed a literal ``/goal`` string: the host would
    forward it to the model instead of driving Goal mode.
    """
    if q1 == "EXPANDS":
        return f"/goal {task}"
    if q1 == "VERBATIM":
        return (
            task
            + "\n\nGrok lifecycle note: session-close per AGENTS.md. If this "
            "surface cannot `git commit` or `bd close` non-interactively, "
            "that is PLAN-GAP — do not invent a substitute."
        )
    raise BackendError(
        f"PLAN-GAP: grok-backend-q1 must be EXPANDS or VERBATIM, got {q1!r}"
    )


def resolve_backend(
    requested: str | None = None,
    *,
    repo: Path | None = None,
    home: Path | None = None,
) -> Backend:
    """Resolve flag > environment > project/user config > Claude default."""
    configured = load_config(repo=repo, home=home).get("backend", "claude")
    name = requested or os.environ.get("ORTUS_BACKEND") or configured
    if name == "all":
        raise BackendError(INIT_ONLY_BACKEND_MESSAGE)
    if name not in BACKENDS:
        raise BackendError(
            f"unknown backend {name!r}; expected one of: {', '.join(BACKENDS)}"
        )
    return cast(Backend, name)


def make_runner(backend: Backend) -> ClaudeRunner | GrokRunner:
    if backend == "codex":
        return CodexRunner()
    if backend == "grok":
        return GrokRunner()
    return ClaudeRunner()


def compose_worker_prompt(backend: Backend, task: str) -> str:
    """Wrap a logical worker task for the selected execution surface."""
    if backend == "claude":
        return f"/goal {task}"
    if backend == "grok":
        return wrap_grok_prompt(task)
    return (
        task
        + "\n\nCodex sandbox note: `.git` metadata is read-only in the "
        "workspace-write sandbox. Session-close per AGENTS.md. If `git commit` "
        "cannot run, that is PLAN-GAP — do not invent a substitute."
    )
