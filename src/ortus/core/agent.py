"""Agent backend selection and runner construction.

Claude remains Ortus's default.  The Codex backend deliberately uses a plain
``codex exec`` prompt: slash commands are an interactive Codex surface and a
literal ``/goal`` passed to ``codex exec`` does not activate Goal mode.

Grok Build expands ``/goal`` on ``grok -p`` (memory ``grok-backend-q1`` =
EXPANDS), so that backend uses the same wrap as Claude.  GrokRunner is a
sibling of ClaudeRunner, not a subclass: Grok's sandbox and approval flags
are not Claude's.

The opencode backend is a model the operator serves themselves, driven by the
opencode CLI.  opencode speaks chat completions and runs MCP servers itself,
presenting each tool as a flat function, so neither failure the Codex
Responses path met at llama-server (a rejected ``developer`` role, namespace
tools silently dropped) can arise and nothing sits between the worker and the
server.  OpenCodeRunner is a sibling like GrokRunner because ``opencode run``
shares no flag with ``codex exec``.  The model is data in config: the
``[local]`` table of ``.ortusrc``.

``local`` names that same engine.  It stays a legal backend so a pinned
``backend = "local"`` and its ``[profiles.local.*]`` keep loading, and every
dispatch on the name (runner, prompt, decoder, check rows, CodeGraph
registration) takes opencode's path.  The Codex-driven engine it once named,
with the loopback shim that flattened namespace tools for it, is retired.
"""

from __future__ import annotations

import os
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from ortus.core.claude import ClaudeRunner, ReadOnlyExecutionBlocked, _spawn_logged
from ortus.core.config import INIT_ONLY_BACKEND_MESSAGE, load_config
from ortus.core.codegraph import CodeGraphCapability
from ortus.core.local_backend import (
    LOCAL_TABLE_BACKENDS,
    OPENCODE_PROVIDER_ID,
    LocalConfig,
    OpenCodeBinaryError,
    load_local_config,
    resolve_opencode_binary,
)
from ortus.core.profiles import AgentProfile, Phase as Phase, ProfileError

Backend = Literal["claude", "codex", "grok", "local", "opencode"]
BACKENDS: tuple[Backend, ...] = ("claude", "codex", "grok", "local", "opencode")
#: The executable each backend launches. ``local`` is ``opencode`` under its
#: older name, so readiness probes look for ``opencode``, not for a binary
#: called ``local``.
BACKEND_BINARIES: dict[Backend, str] = {
    "claude": "claude",
    "codex": "codex",
    "grok": "grok",
    "local": "opencode",
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
#: The environment variable opencode parses as a JSON configuration document
#: and merges over its resolved configuration at startup. Rows it places
#: under ``agent.<name>.permission`` follow the global rows in opencode's
#: permission ruleset, and the last matching row wins.
OPENCODE_CONFIG_CONTENT_ENV = "OPENCODE_CONFIG_CONTENT"
#: The agent ``opencode run`` uses when no ``--agent`` is passed.
#: ``OpenCodeRunner.build_argv`` never passes one, so this is the agent a
#: verify run resolves its permissions for.
OPENCODE_VERIFY_AGENT = "build"


def opencode_agent_scope_denial() -> dict[str, Any]:
    """The verify denial as an agent-scoped configuration document.

    ``OPENCODE_PERMISSION`` lands in the global permission table. An
    ``agent.build.permission`` allow in the project ``opencode.json``, in the
    user-level config, in ``OPENCODE_CONFIG_CONTENT``, or in an agent file
    appends its rows after the global ones, so that allow is the last match
    and hands the verifier the write tools back: proven live against
    opencode 1.18.27, where a project allow had the model rewrite a guard
    file under the global denial. The same denial placed at agent scope was
    the last match under every scope tested, so it is the guard.
    """
    return {
        "agent": {
            OPENCODE_VERIFY_AGENT: {"permission": dict(OPENCODE_READONLY_PERMISSION)}
        }
    }


def read_opencode_config_content(env: Mapping[str, str]) -> dict[str, Any]:
    """The operator's ``OPENCODE_CONFIG_CONTENT`` under ``env`` as an object.

    Absent or empty means no operator document: ``{}``. A value that is
    present but not a JSON object raises ``ReadOnlyExecutionBlocked``: the
    verify denial is merged over the operator's document so their other keys
    survive, and there is nothing to merge into a string or a list. Replacing
    the value would silently drop configuration the operator meant to apply,
    and passing it through would launch a verifier whose agent-scope posture
    is whatever that value resolves to, so neither is a verify posture.
    """
    raw = env.get(OPENCODE_CONFIG_CONTENT_ENV)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReadOnlyExecutionBlocked(
            f"${OPENCODE_CONFIG_CONTENT_ENV} is not JSON ({exc}); the verify "
            "denial cannot be merged into it — fix or unset the variable"
        ) from exc
    if not isinstance(parsed, dict):
        raise ReadOnlyExecutionBlocked(
            f"${OPENCODE_CONFIG_CONTENT_ENV} is not a JSON object; the verify "
            "denial cannot be merged into it — fix or unset the variable"
        )
    return parsed


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """``base`` with ``overlay`` merged in, nested objects key by key.

    A key both sides hold as objects merges recursively; otherwise the
    overlay's value replaces the base's, so a pattern table the operator put
    under a denied tool gives way to the plain ``deny``.
    """
    merged = dict(base)
    for key, value in overlay.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged


@dataclass
class OpenCodeRunner:
    """Run one headless ``opencode run`` task at an operator-served model.

    Sibling of ClaudeRunner and GrokRunner, not a subclass of either: the
    ``run --format json -m provider/model`` surface shares nothing with
    ``codex exec``. ``-m`` is always ``OPENCODE_PROVIDER_ID`` followed by a
    slash and the model, so a served id that itself carries slashes or colons
    still parses (opencode splits on the first slash). ``local`` supplies the
    model; its ``api_key_env`` is only ever the *name* of a variable that the
    provider entry in ``opencode.json`` resolves, so no key material enters
    argv, the launch environment, or a log.
    """

    local: LocalConfig
    #: What ``argv[0]`` is. ``make_runner`` supplies the absolute path
    #: ``resolve_opencode_binary`` found (PATH, then ``~/.opencode/bin``); the
    #: bare default is for callers that construct the runner themselves and
    #: launches by PATH like any other name.
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

        The denial travels twice, as JSON in the environment, so the posture
        is per launch and no project file changes for a verify run.
        ``OPENCODE_PERMISSION_ENV`` carries it as the global table opencode
        merges over its configured ``permission`` at startup.
        ``OPENCODE_CONFIG_CONTENT_ENV`` carries it again at agent scope for
        ``OPENCODE_VERIFY_AGENT``, merged over the document the operator
        already exports there (``extra_env`` first, the process environment
        second, the order the launch applies) so their unrelated keys
        survive. Without that second copy an agent-scoped allow in any
        configuration scope is the last match and re-advertises the write
        tools. Raises ``ReadOnlyExecutionBlocked`` when the operator's value
        is not a JSON object, the same refusal ``preflight_readonly`` gives.
        """
        env = dict(self.extra_env)
        if readonly:
            env[OPENCODE_PERMISSION_ENV] = json.dumps(
                OPENCODE_READONLY_PERMISSION, sort_keys=True
            )
            operator = read_opencode_config_content(self._operator_env())
            env[OPENCODE_CONFIG_CONTENT_ENV] = json.dumps(
                _deep_merge(operator, opencode_agent_scope_denial()), sort_keys=True
            )
        return env

    def _operator_env(self) -> dict[str, str]:
        """The environment a launch resolves: ``extra_env`` over the process's."""
        return {**os.environ, **self.extra_env}

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

        Settled, not deferred: the denial ``launch_env`` exports is the whole
        verify posture. opencode's permission is tool-level, and with write,
        edit, and bash denied it drops those tools from the model's surface,
        so the verifier holds nothing that can touch the tree — bash
        included, the one tool a permission table cannot otherwise contain.
        An outer read-only root would add nothing the denial does not
        already guarantee, and would make opencode's own state directories
        read-only, the failure Codex documented; grind adds none on top.
        """

        return argv

    def preflight_readonly(self, repo: Path, *, timeout: float = 60.0) -> None:
        """Refuse a verify launch whose agent-scope denial has nowhere to land.

        No command is probed. The Claude preflight exists because its
        verifier runs commands through a wrapper that can turn out unable to
        execute anything; an opencode verifier runs no commands at all — bash
        is among the denied tools — and no Ortus-owned wrapper sits in its
        path, so that failure cannot arise here. What can is an operator
        ``OPENCODE_CONFIG_CONTENT_ENV`` that is not a JSON object: the denial
        ``launch_env`` exports is merged over that document rather than
        replacing it, so the ``ReadOnlyExecutionBlocked`` the launch would
        raise is raised here instead, naming the variable, before a worker
        is dispatched.
        """

        read_opencode_config_content(self._operator_env())


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
    ``except BackendError`` site reports it unchanged. Those two backends
    also resolve the opencode executable here, once: the runner is handed
    an absolute path, so the worker runs whatever PATH it inherits, and a
    binary that is nowhere is the same ``BackendError`` naming the fix
    rather than a ``FileNotFoundError`` at spawn time.
    """
    if backend == "codex":
        return CodexRunner()
    if backend == "grok":
        return GrokRunner()
    if backend in LOCAL_TABLE_BACKENDS:
        try:
            local = load_local_config(load_config(repo=repo))
        except ProfileError as exc:
            raise BackendError(str(exc)) from exc
        try:
            binary = resolve_opencode_binary()
        except OpenCodeBinaryError as exc:
            raise BackendError(f"{exc}; {exc.remediation}") from exc
        return OpenCodeRunner(local, opencode_binary=str(binary))
    return ClaudeRunner()


def compose_worker_prompt(backend: Backend, task: str) -> str:
    """Wrap a logical worker task for the selected execution surface.

    ``opencode``, and ``local`` as its older name, has no slash commands at
    all and needs none: the objective is the prompt, and its own turn loop
    runs to completion. Codex takes the plain prompt because a literal
    ``/goal`` would reach the model verbatim.
    """
    if backend == "claude":
        return f"/goal {task}"
    if backend == "grok":
        return wrap_grok_prompt(task)
    if backend in LOCAL_TABLE_BACKENDS:
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
