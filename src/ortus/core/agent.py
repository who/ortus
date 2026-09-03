"""Agent backend selection and runner construction.

Claude remains Ortus's default.  The Codex backend deliberately uses a plain
``codex exec`` prompt: slash commands are an interactive Codex surface and a
literal ``/goal`` passed to ``codex exec`` does not activate Goal mode.

Grok Build expands ``/goal`` on ``grok -p`` (memory ``grok-backend-q1`` =
EXPANDS), so that backend uses the same wrap as Claude.  GrokRunner is a
sibling of ClaudeRunner, not a subclass: Grok's sandbox and approval flags
are not Claude's.

The local backend is the Codex CLI aimed at a model the operator serves
themselves.  LocalRunner therefore *is* a CodexRunner: the same argv, followed
by trusted ``-c`` overrides that register an ``ortus_local`` model provider
from the ``[local]`` table of ``.ortusrc``.  The model is data in config.

The opencode backend is that same operator-served model driven by the opencode
CLI instead of Codex.  opencode speaks chat completions and runs MCP servers
itself, presenting each tool as a flat function, so neither failure the Codex
Responses path met at llama-server (a rejected ``developer`` role, namespace
tools silently dropped) can arise and no shim is involved.  OpenCodeRunner is
a sibling like GrokRunner because ``opencode run`` shares no flag with
``codex exec``.  ``local`` keeps its Codex engine, byte for byte, until the
retirement leaf removes it.
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
from ortus.core.local_backend import (
    LOCAL_PROVIDER_ID,
    LOCAL_WIRE_API,
    OPENCODE_PROVIDER_ID,
    LocalConfig,
    load_local_config,
)
from ortus.core.mcp_shim import McpShim, start_shim
from ortus.core.profiles import AgentProfile, Phase as Phase, ProfileError

Backend = Literal["claude", "codex", "grok", "local", "opencode"]
BACKENDS: tuple[Backend, ...] = ("claude", "codex", "grok", "local", "opencode")
#: The executable each backend launches. ``local`` drives the Codex CLI at an
#: operator-served model, so readiness probes look for ``codex``, not for a
#: binary called ``local``; ``opencode`` drives the same model through its
#: own CLI.
BACKEND_BINARIES: dict[Backend, str] = {
    "claude": "claude",
    "codex": "codex",
    "grok": "grok",
    "local": "codex",
    "opencode": "opencode",
}

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


class LocalRunner(CodexRunner):
    """Codex argv plus an ``ortus_local`` provider aimed at an operator-served model.

    A subclass, unlike GrokRunner: the argv *is* the codex argv followed by
    trusted ``-c`` provider overrides, so there is no foreign flag surface to
    keep apart. The overrides carry a URL, a provider id, a wire API, and at
    most the *name* of an environment variable. Codex reads that variable at
    launch, so no key material ever enters argv, ``extra_env``, or a log.

    The worker always talks to a loopback shim rather than the server. The
    shim flattens codex's namespace-shaped MCP tools, which the servers this
    backend targets drop on the floor, and it demotes the ``developer`` role
    codex opens every turn with, which llama-server's chat template rejects
    before the model runs, so the shim is needed whether or not CodeGraph is
    configured. It lives exactly as long as the child, and the provider
    ``base_url`` override names its port.
    """

    def __init__(
        self,
        local: LocalConfig,
        codex_binary: str = "codex",
        *,
        extra_env: dict[str, str] | None = None,
        codegraph: CodeGraphCapability | None = None,
        sandbox_mode: str = "workspace-write",
    ) -> None:
        super().__init__(
            codex_binary,
            extra_env=extra_env,
            codegraph=codegraph,
            sandbox_mode=sandbox_mode,
        )
        self.local = local
        #: The shim serving the child that ``run`` is currently supervising.
        self.shim: McpShim | None = None

    @property
    def provider_base_url(self) -> str:
        """Where the provider override points: the shim while one is running.

        Outside ``run`` that is the configured server itself, which is what
        ``ortus check`` and the probes read.
        """
        return self.local.base_url if self.shim is None else self.shim.base_url

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
        """Spawn codex through the loopback shim, for every local worker.

        The shim starts before the argv is built so the provider override can
        name its port, and stops in ``finally`` however the child ends (exit,
        reap, timeout, interrupt), so no listener or thread outlives a worker.
        A shim that cannot bind raises here, before codex is spawned.
        """
        launch = dict(
            repo=repo,
            log_path=log_path,
            fast=fast,
            profile=profile,
            timeout=timeout,
            readonly=readonly,
            resume=resume,
            reap_when=reap_when,
            reap_poll=reap_poll,
            on_poll=on_poll,
        )
        self.shim = start_shim(self.local.base_url, api_key_env=self.local.api_key_env)
        try:
            return super().run(prompt, **launch)
        finally:
            shim, self.shim = self.shim, None
            if shim is not None:
                shim.close()

    def build_argv(
        self,
        prompt: str,
        *,
        fast: bool = False,
        profile: AgentProfile | None = None,
        readonly: bool = False,
        resume: str | None = None,
    ) -> list[str]:
        argv = super().build_argv(
            prompt, fast=fast, profile=profile, readonly=readonly, resume=resume
        )
        provider = f"model_providers.{LOCAL_PROVIDER_ID}"
        # Values render through json.dumps exactly like the CodeGraph block, so
        # each is a TOML string. Codex 0.147.0 connects to a provider with no
        # credential source as-is, so no requires_openai_auth pair is needed.
        argv.extend(
            [
                "-c",
                f"{provider}.name=" + json.dumps(LOCAL_PROVIDER_ID),
                "-c",
                f"{provider}.base_url=" + json.dumps(self.provider_base_url),
                "-c",
                f"{provider}.wire_api=" + json.dumps(LOCAL_WIRE_API),
            ]
        )
        if self.local.api_key_env is not None and self.shim is None:
            # With the shim in the path the key rides the upstream leg only:
            # the shim reads the named variable itself, and codex stays
            # unauthenticated on loopback.
            argv.extend(
                ["-c", f"{provider}.env_key=" + json.dumps(self.local.api_key_env)]
            )
        argv.extend(["-c", "model_provider=" + json.dumps(LOCAL_PROVIDER_ID)])
        if profile is None or profile.model is None:
            # A profile model already rode in on the superclass argv and wins;
            # otherwise the configured model is the only `-m`.
            argv.extend(["-m", self.local.model])
        return argv


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


#: The environment variable opencode parses as a JSON object and merges over
#: the ``permission`` table of its configuration at startup.
OPENCODE_PERMISSION_ENV = "OPENCODE_PERMISSION"
#: The read-only verify posture proven against opencode 1.18.27: with these
#: three denied, opencode drops the write, edit, and bash tools from the
#: model's surface entirely, so a verifier has nothing that can touch the
#: tree. ``bash`` is the vector the permission table cannot otherwise contain
#: (an allowed bash tool writes through a redirect), so it is denied too.
OPENCODE_READONLY_PERMISSION: dict[str, str] = {
    "edit": "deny",
    "write": "deny",
    "bash": "deny",
}


@dataclass
class OpenCodeRunner:
    """Run one headless ``opencode run`` task at an operator-served model.

    Sibling of ClaudeRunner and GrokRunner, not a CodexRunner subclass: the
    ``run --format json -m provider/model`` surface shares nothing with
    ``codex exec``. ``-m`` is always ``OPENCODE_PROVIDER_ID`` followed by a
    slash and the model, so a served id that itself carries slashes or colons
    still parses (opencode splits on the first slash). ``local`` supplies the
    model; its ``api_key_env`` is only ever the *name* of a variable that the
    provider entry in ``opencode.json`` resolves, so no key material enters
    argv, the launch environment, or a log.
    """

    local: LocalConfig
    opencode_binary: str = "opencode"
    extra_env: dict[str, str] = field(default_factory=dict)
    codegraph: CodeGraphCapability | None = None

    def configure_codegraph(self, capability: CodeGraphCapability | None) -> None:
        """Store the outer probe result.

        opencode registers MCP servers in ``opencode.json`` and runs them
        itself, so there is no launch-time override to emit; that file is
        the init and check leaves' to write and to verify.
        """
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
        # `fast` is intentionally ignored: opencode has no tier flag. The
        # read-only posture rides in the environment (`launch_env`), not in
        # argv, so `readonly` leaves the argv unchanged.
        model = self.local.model
        if profile is not None and profile.model is not None:
            model = profile.model
        argv = [
            self.opencode_binary,
            "run",
            "--format",
            "json",
            "-m",
            f"{OPENCODE_PROVIDER_ID}/{model}",
        ]
        if profile is not None and profile.reasoning_effort is not None:
            # A named variant of the model. opencode 1.18.27 applies nothing
            # for a name the model does not define, so the profile validation
            # in `profiles` is the only typo check.
            argv.extend(["--variant", profile.reasoning_effort])
        if resume:
            argv.extend(["--session", resume])
        argv.append(prompt)
        return argv

    def launch_env(self, *, readonly: bool = False) -> dict[str, str]:
        """``extra_env``, plus the verify posture when ``readonly``.

        The denial travels as JSON in ``OPENCODE_PERMISSION_ENV``, which
        opencode merges over its configured ``permission`` table when it
        starts, so the posture is per launch and no project file changes
        for a verify run.
        """
        env = dict(self.extra_env)
        if readonly:
            env[OPENCODE_PERMISSION_ENV] = json.dumps(
                OPENCODE_READONLY_PERMISSION, sort_keys=True
            )
        return env

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
        """Spawn opencode, tee output to log_path (NOT stdout), return exit code."""
        argv = self.build_argv(
            prompt, fast=fast, profile=profile, readonly=readonly, resume=resume
        )
        if readonly:
            argv = self._readonly_argv(argv, repo)
        return _spawn_logged(
            argv,
            repo=repo,
            log_path=log_path,
            extra_env=self.launch_env(readonly=readonly),
            timeout=timeout,
            readonly=readonly,
            reap_when=reap_when,
            reap_poll=reap_poll,
            on_poll=on_poll,
        )

    def _readonly_argv(self, argv: list[str], repo: Path) -> list[str]:
        """The posture is opencode's own permission table; nothing wraps the process.

        With write, edit, and bash denied the model has no tool that can
        touch the tree. An outer read-only root would also make opencode's
        own state directories read-only, the failure Codex documented, so
        whether grind adds one on top is the preflight leaf's decision.
        """

        return argv

    def preflight_readonly(self, repo: Path, *, timeout: float = 60.0) -> None:
        """No Ortus-owned read-only wrapper to probe; mirrors ``_readonly_argv``."""

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


def make_runner(
    backend: Backend, *, repo: Path | None = None
) -> ClaudeRunner | GrokRunner | OpenCodeRunner:
    """Construct the runner for ``backend``.

    ``repo`` matters only to ``local`` and ``opencode``, whose served model
    comes from the ``[local]`` table of that repository's layered config; the
    other backends ignore it. A missing or malformed table surfaces as
    ``BackendError`` with the config error's own text, so every existing
    ``except BackendError`` site reports it unchanged.
    """
    if backend == "codex":
        return CodexRunner()
    if backend == "grok":
        return GrokRunner()
    if backend in ("local", "opencode"):
        try:
            local = load_local_config(load_config(repo=repo))
        except ProfileError as exc:
            raise BackendError(str(exc)) from exc
        if backend == "opencode":
            return OpenCodeRunner(local)
        return LocalRunner(local)
    return ClaudeRunner()


def compose_worker_prompt(backend: Backend, task: str) -> str:
    """Wrap a logical worker task for the selected execution surface.

    ``local`` is ``codex exec`` under another provider, so it takes the plain
    Codex prompt: a literal ``/goal`` would reach the model verbatim.
    ``opencode`` has no slash commands at all and needs none: the objective
    is the prompt, and its own turn loop runs to completion.
    """
    if backend == "claude":
        return f"/goal {task}"
    if backend == "grok":
        return wrap_grok_prompt(task)
    if backend == "opencode":
        return (
            task
            + "\n\nopencode lifecycle note: session-close per AGENTS.md. If "
            "`git commit` or `bd close` cannot run non-interactively, that is "
            "PLAN-GAP — do not invent a substitute."
        )
    return (
        task
        + "\n\nCodex sandbox note: `.git` metadata is read-only in the "
        "workspace-write sandbox. Session-close per AGENTS.md. If `git commit` "
        "cannot run, that is PLAN-GAP — do not invent a substitute."
    )
