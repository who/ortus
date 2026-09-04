"""ortus check <repo> — verify prerequisites for the orchestrator (q075.6).

Strictly read-only (NFR-006). Each check returns a CheckResult; the verb
collects results, renders a rich table, and exits 0 if all pass else 1.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import typer
from rich.text import Text

from ortus.core import output, sandbox
from ortus.core.agent import (
    BACKEND_BINARIES,
    BACKENDS,
    OPENCODE_CONFIG_CONTENT_ENV,
    OPENCODE_PERMISSION_ENV,
    OPENCODE_READONLY_PERMISSION,
    OPENCODE_VERIFY_AGENT,
    BackendError,
    read_opencode_config_content,
    resolve_backend,
)
from ortus.core.agent_files import (
    BLOCK_SCHEMAS,
    MANAGED_FILES,
    AgentFileError,
    ManagedFile,
    duplicate_headings_message,
    duplicated_headings,
    gitignore_match,
    read_block,
    render_block,
)
from ortus.core.claude import ClaudeRunner, ReadOnlyExecutionBlocked
from ortus.core.codegraph import CodeGraphMode
from ortus.core.config import (
    DEFAULT_CODEGRAPH_MODE,
    DEFAULT_VERIFICATION_MODE,
    VERIFICATION_PROTOTYPE,
    load_config,
    read_recorded_local,
)
from ortus.core.hooks import HookConflictError, check_hooks_enabled
from ortus.core.init_render import BACKEND_TEMPLATES, MERGED_CONFIGS, read_opencode_config
from ortus.core.local_backend import (
    LOCAL_TABLE_BACKENDS,
    MIN_RECOMMENDED_CONTEXT,
    OPENCODE_BINARY,
    OPENCODE_CONFIG_FILE,
    OPENCODE_MCP_SERVER,
    OPENCODE_PROVIDER_ID,
    LocalConfig,
    LocalServerError,
    OpenCodeBinaryError,
    load_local_config,
    opencode_mcp_entry,
    opencode_provider_block,
    parse_local_table,
    probe_context_size,
    probe_models,
    resolve_opencode_binary,
)
from ortus.core.profiles import ProfileError
from ortus.core.prompts import (
    PROMPT_REGISTRY,
    READINESS_SPEC_PLACEHOLDER,
    PromptNotFound,
    bundled_prompt_text,
    bundled_sha256,
    parse_eject_stamp,
    resolve_prompt,
)
from ortus.core.readiness import READINESS_MEMORY_KEY, readiness_memory_command

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python 3.10
    import tomli as tomllib


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    message: str
    # "strict" rows drive the exit code; "info" rows render as WARN when not
    # ok and never fail the check (provisioned-but-not-run backends).
    level: str = "strict"


def _binary_check(
    name: str, *, version_flag: str = "--version", path: str | None = None
) -> CheckResult:
    """`name` answers `version_flag`: found on PATH, or at `path` when the
    caller already resolved it, in which case that path is what runs."""
    if path is None:
        path = shutil.which(name)
        if path is None:
            return CheckResult(name, False, f"{name} not on PATH")
        executable = name
    else:
        executable = path
    try:
        proc = subprocess.run(
            [executable, version_flag],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        version = (proc.stdout or proc.stderr).splitlines()[0:1]
        line = version[0] if version else "(version unknown)"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CheckResult(name, False, f"{name} on PATH but failed to run: {exc}")
    return CheckResult(name, True, f"{path} — {line}")


def check_bd() -> CheckResult:
    return _binary_check("bd")


def check_claude() -> CheckResult:
    return _binary_check("claude")


def check_codex() -> CheckResult:
    return _binary_check("codex")


def check_grok() -> CheckResult:
    return _binary_check("grok")


def check_opencode() -> CheckResult:
    """The `opencode` binary row, resolved the way the runner resolves it.

    PATH first, then the installer's `~/.opencode/bin`, which a non-login
    shell's PATH does not include: a row that stopped at PATH failed a
    standard install that grind would in fact launch. What this row reports
    is the path grind hands the worker, and a miss names both fixes.
    """
    try:
        path = resolve_opencode_binary()
    except OpenCodeBinaryError as exc:
        return CheckResult(OPENCODE_BINARY, False, f"{exc} — {exc.remediation}")
    return _binary_check(OPENCODE_BINARY, path=str(path))


def check_jq() -> CheckResult:
    return _binary_check("jq")


def check_sandbox() -> CheckResult:
    try:
        info = sandbox.smoke_test()
    except sandbox.SandboxUnavailable as exc:
        return CheckResult("sandbox", False, str(exc).splitlines()[0])
    return CheckResult("sandbox", True, f"{info.platform} → {info.binary}")


def check_verifier_execution(repo: Path) -> CheckResult:
    """Run the verification preflight so a blocked sandbox is visible up front.

    `check_sandbox` only proves the sandbox *binary* is installed. A posture
    that launches but cannot execute a command is the condition that made
    verifiers report every criterion blocked, and it is worth catching before
    a run rather than mid-run (ortus-dyio).
    """

    name = "verifier sandbox"
    try:
        ClaudeRunner().preflight_readonly(repo)
    except ReadOnlyExecutionBlocked as exc:
        return CheckResult(name, False, str(exc).splitlines()[0])
    return CheckResult(name, True, "read-only posture executed a command")


def check_beads_dir(repo: Path) -> CheckResult:
    beads = repo / ".beads"
    if not beads.is_dir():
        return CheckResult(".beads/", False, f"missing at {beads}")
    return CheckResult(".beads/", True, str(beads))


def check_readiness_memory(repo: Path) -> CheckResult:
    """Report whether the readiness-contract pointer reaches `bd prime`.

    Repos initialized before the pointer existed have no such memory, so the
    failure message carries the exact command that adds it — this check never
    writes it itself (NFR-006). `--readonly` and `--sandbox` keep the query
    from taking bd's write or auto-sync paths. A present key whose body no
    longer names `ortus spec` also fails: check, not init, is the enforcer
    that the pointer still points at the verb, since an operator edit that
    drops the verb leaves later sessions authoring headings from memory.
    """
    name = "bd readiness memory"
    if not (repo / ".beads").is_dir():
        return CheckResult(name, False, f"no bd workspace at {repo / '.beads'}")
    try:
        proc = subprocess.run(
            ["bd", "--readonly", "--sandbox", "memories", "--json"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CheckResult(name, False, f"bd memories failed to run: {exc}")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()[0:1]
        return CheckResult(
            name,
            False,
            f"bd memories exited {proc.returncode}: {detail[0] if detail else '(no output)'}",
        )
    try:
        memories = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return CheckResult(name, False, f"bd memories --json unparseable: {exc}")
    if READINESS_MEMORY_KEY not in memories:
        return CheckResult(
            name, False, f"missing — add it with: {readiness_memory_command()}"
        )
    body = memories[READINESS_MEMORY_KEY]
    if not isinstance(body, str) or "ortus spec" not in body:
        return CheckResult(
            name,
            False,
            "stale — the stored text no longer says `ortus spec`; "
            f"refresh it with: {readiness_memory_command()}",
        )
    return CheckResult(name, True, f"key={READINESS_MEMORY_KEY}")


def check_claude_settings(repo: Path) -> CheckResult:
    settings = repo / ".claude" / "settings.json"
    if not settings.is_file():
        return CheckResult(".claude/settings.json", False, f"missing at {settings}")
    try:
        data = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return CheckResult(".claude/settings.json", False, f"unparseable: {exc}")
    excluded = data.get("sandbox", {}).get("excludedCommands") or []
    missing = [c for c in ("bd", "bd *", "ortus", "ortus *") if c not in excluded]
    if missing:
        return CheckResult(
            ".claude/settings.json",
            False,
            f"sandbox.excludedCommands missing: {', '.join(missing)}",
        )
    return CheckResult(".claude/settings.json", True, str(settings))


def check_codex_settings(repo: Path) -> CheckResult:
    settings = repo / ".codex" / "config.toml"
    if not settings.is_file():
        return CheckResult(".codex/config.toml", False, f"missing at {settings}")
    try:
        with settings.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return CheckResult(".codex/config.toml", False, f"unparseable: {exc}")
    if data.get("sandbox_mode") != "workspace-write":
        return CheckResult(
            ".codex/config.toml", False, "sandbox_mode must be workspace-write"
        )
    return CheckResult(".codex/config.toml", True, str(settings))


def check_grok_settings(repo: Path) -> CheckResult:
    """Project `.grok/config.toml` exists and parses.

    Official project config contributes only ``[mcp_servers]``, ``[plugins]``,
    and ``[permission]``. Missing ``sandbox_mode`` is not a failure.
    """
    settings = repo / ".grok" / "config.toml"
    if not settings.is_file():
        return CheckResult(".grok/config.toml", False, f"missing at {settings}")
    try:
        with settings.open("rb") as fh:
            tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return CheckResult(".grok/config.toml", False, f"unparseable: {exc}")
    return CheckResult(".grok/config.toml", True, str(settings))


def check_opencode_settings(repo: Path) -> CheckResult:
    """Project `opencode.json` exists, parses, and carries the Ortus provider.

    The file is host-owned JSON that `ortus init` merges one key into, so a
    file that is present but has no `provider.<OPENCODE_PROVIDER_ID>` entry
    was never provisioned for this backend. Whether that entry still matches
    the `[local]` table is the provider row's question, not this one's.
    """
    name = OPENCODE_CONFIG_FILE
    settings = repo / name
    if not settings.is_file():
        return CheckResult(name, False, f"missing at {settings} — {OPENCODE_PROVISION_HINT}")
    data, error = _read_opencode_json(repo)
    if error is not None:
        return CheckResult(name, False, error)
    if _opencode_provider_entry(data) is None:
        return CheckResult(
            name,
            False,
            f"no provider.{OPENCODE_PROVIDER_ID} entry — {OPENCODE_PROVISION_HINT}",
        )
    return CheckResult(name, True, str(settings))


def _local_failure(exc: LocalServerError) -> str:
    """A probe verdict plus the first line of its remediation, for one cell."""
    return f"{exc} — {exc.remediation.splitlines()[0]}"


def _context_row(name: str, local: LocalConfig) -> CheckResult:
    """`n_ctx` from the server, as an informational row.

    A small window degrades a worker (a prompt plus CodeGraph tool output
    does not fit it) but does not stop the run, so the row warns and never
    fails. A server without `/props` (Ollama, vLLM) is a pass, not a guess.
    """
    n_ctx = probe_context_size(local)
    if n_ctx is None:
        return CheckResult(
            name, True, "context size not exposed by this server", level="info"
        )
    if n_ctx < MIN_RECOMMENDED_CONTEXT:
        return CheckResult(
            name,
            False,
            f"n_ctx={n_ctx} below the recommended {MIN_RECOMMENDED_CONTEXT} — "
            f"restart with --ctx-size {MIN_RECOMMENDED_CONTEXT}",
            level="info",
        )
    return CheckResult(name, True, f"n_ctx={n_ctx}", level="info")


def _local_config_row(name: str, local: LocalConfig) -> CheckResult:
    """A valid `[local]` table as one cell: the key variable by name, never by value."""
    if local.api_key_env is not None and not os.environ.get(local.api_key_env):
        return CheckResult(
            name,
            False,
            f"api_key_env={local.api_key_env} is not set in the environment — "
            "export it or drop the key",
        )
    return CheckResult(
        name,
        True,
        f"base_url={local.base_url} model={local.model} "
        f"key={local.api_key_env or 'none'}",
    )


#: The `opencode` rows in table order: the `[local]` table, the provider
#: entry `opencode.json` registers for it, the served model, the CodeGraph
#: MCP registration, the permission posture, and the context window.
OPENCODE_ROW_NAMES: tuple[str, ...] = (
    "[local]",
    "opencode provider",
    "opencode endpoint",
    "opencode mcp",
    "opencode posture",
    "opencode context",
)
OPENCODE_PROVISION_HINT = "run `ortus init --backend opencode --local-model <id>`"
OPENCODE_REPROVISION_HINT = "re-run `ortus init --force --backend opencode`"
#: The `mcp.codegraph` entry init writes and opencode 1.18.27 ran client-side,
#: as JSON the operator can paste; `opencode mcp add` prompts for the same
#: three facts.
OPENCODE_MCP_HINT = (
    f'add to {OPENCODE_CONFIG_FILE}: "mcp": '
    f"{json.dumps({OPENCODE_MCP_SERVER: opencode_mcp_entry()})} "
    f"(or `opencode mcp add {OPENCODE_MCP_SERVER}`)"
)
#: The tools an implement worker must hold and a verify worker must not.
OPENCODE_POSTURE_TOOLS: tuple[str, ...] = tuple(OPENCODE_READONLY_PERMISSION)


def _read_opencode_json(repo: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Project `opencode.json` parsed, or why it could not be: `(data, error)`."""
    try:
        return read_opencode_config(repo), None
    except ValueError as exc:
        return None, str(exc)


def _opencode_provider_entry(data: dict[str, Any] | None) -> dict[str, Any] | None:
    """`provider.<OPENCODE_PROVIDER_ID>` of a parsed `opencode.json`, when an object."""
    if data is None:
        return None
    entry = (data.get("provider") or {}).get(OPENCODE_PROVIDER_ID)
    return entry if isinstance(entry, dict) else None


def _opencode_mcp_entry(data: dict[str, Any] | None) -> dict[str, Any] | None:
    """`mcp.codegraph` of a parsed `opencode.json`, when an object."""
    if data is None:
        return None
    servers = data.get("mcp")
    if not isinstance(servers, dict):
        return None
    entry = servers.get(OPENCODE_MCP_SERVER)
    return entry if isinstance(entry, dict) else None


def _opencode_mcp_registered(repo: Path) -> bool:
    """Report whether project `opencode.json` registers an enabled `codegraph`.

    opencode runs MCP servers itself from its own `mcp` table and presents
    each tool as a flat function, so that table is the whole registration:
    there is no launch-time injection as for codex and no Claude scope to
    fall back on. An entry with `enabled: false` is one no worker will see.
    """
    data, _ = _read_opencode_json(repo)
    entry = _opencode_mcp_entry(data)
    return entry is not None and entry.get("enabled", True) is not False


def _opencode_provider_row(
    name: str, data: dict[str, Any] | None, error: str | None, local: LocalConfig
) -> CheckResult:
    """`opencode.json` registers the `[local]` table: same URL, model, and key reference.

    The entry is compared fact by fact rather than whole, so a model option
    the operator added survives; a drifted URL, a missing model, or a key
    reference that names the wrong variable each name the re-init that
    rewrites the entry. The key itself is never in the file, only `{env:NAME}`.
    """
    if error is not None:
        return CheckResult(name, False, error)
    entry = _opencode_provider_entry(data)
    if entry is None:
        return CheckResult(
            name,
            False,
            f"no provider.{OPENCODE_PROVIDER_ID} entry in {OPENCODE_CONFIG_FILE} — "
            f"{OPENCODE_PROVISION_HINT}",
        )
    expected = opencode_provider_block(local)
    options = entry.get("options")
    if not isinstance(options, dict):
        options = {}
    base_url = options.get("baseURL")
    if base_url != expected["options"]["baseURL"]:
        return CheckResult(
            name,
            False,
            f"baseURL={base_url!r} but local.base_url={local.base_url} — "
            f"{OPENCODE_REPROVISION_HINT}",
        )
    models = entry.get("models")
    if not isinstance(models, dict):
        models = {}
    if local.model not in models:
        registered = ", ".join(sorted(models)) or "none"
        return CheckResult(
            name,
            False,
            f"models lack {local.model!r} (registered: {registered}) — "
            f"{OPENCODE_REPROVISION_HINT}",
        )
    key_ref = expected["options"].get("apiKey")
    if options.get("apiKey") != key_ref:
        wanted = f"the {key_ref} reference" if key_ref else "absent"
        return CheckResult(
            name,
            False,
            f"apiKey is not {wanted} (local.api_key_env={local.api_key_env or 'none'}) — "
            f"{OPENCODE_REPROVISION_HINT}",
        )
    return CheckResult(
        name, True, f"{OPENCODE_PROVIDER_ID}/{local.model} at {local.base_url}"
    )


def _opencode_endpoint_row(name: str, local: LocalConfig) -> CheckResult:
    """`GET {base_url}/models` lists the configured model; wire-agnostic, so shared."""
    try:
        probe_models(local)
    except LocalServerError as exc:
        return CheckResult(name, False, _local_failure(exc))
    return CheckResult(name, True, f"reachable; model {local.model} served")


def _opencode_mcp_row(
    name: str, data: dict[str, Any] | None, error: str | None
) -> CheckResult:
    if error is not None:
        return CheckResult(name, False, error)
    entry = _opencode_mcp_entry(data)
    if entry is None:
        return CheckResult(
            name,
            False,
            f"codegraph is not in the {OPENCODE_CONFIG_FILE} mcp table — {OPENCODE_MCP_HINT}",
        )
    if entry.get("enabled", True) is False:
        return CheckResult(name, False, "mcp.codegraph has enabled=false — set it to true")
    command = entry.get("command")
    if isinstance(command, list):
        shown = " ".join(str(part) for part in command)
    else:
        shown = str(entry.get("url") or entry.get("type") or "unspecified")
    return CheckResult(name, True, f"codegraph server registered ({shown})")


def _permission_verdict(table: dict[str, Any], tool: str) -> str | None:
    """`allow`, `ask`, or `deny` for `tool` under `table`, or None when unclear.

    A pattern table resolves by its catch-all `*` entry; without one the
    posture depends on each command's text, which a check may not guess.
    """
    value = table.get(tool)
    if isinstance(value, dict):
        value = value.get("*")
    return value if isinstance(value, str) else None


def _opencode_posture_row(
    name: str, data: dict[str, Any] | None, error: str | None
) -> CheckResult:
    """The permission posture an implement worker and a verify worker will get.

    Resolved the way opencode resolves it at startup — the project
    `permission` table with `OPENCODE_PERMISSION` from the environment merged
    over it — because every worker inherits the operator's shell, so a
    denial exported there would quietly cripple implement runs. A key the
    tables do not mention is opencode's headless default, allow. The verify
    posture is the denial `OpenCodeRunner` exports for that phase, in the
    global table and again at agent scope for the agent a headless run
    resolves, reported rather than exercised: no worker launches from check.
    The agent-scope copy is merged over the operator's
    `OPENCODE_CONFIG_CONTENT`, so a value there that is not a JSON object is
    the one thing that makes the verify launch refuse, and the row says so.
    """
    if error is not None:
        return CheckResult(name, False, error)
    table = (data or {}).get("permission")
    if table is None:
        table = {}
    if not isinstance(table, dict):
        return CheckResult(
            name, False, f"posture unknown: {OPENCODE_CONFIG_FILE} permission is not an object"
        )
    env_source = f"${OPENCODE_PERMISSION_ENV}"
    env_table: Any = {}
    override = os.environ.get(OPENCODE_PERMISSION_ENV)
    if override:
        try:
            env_table = json.loads(override)
        except json.JSONDecodeError as exc:
            return CheckResult(name, False, f"posture unknown: {env_source} is not JSON ({exc})")
        if not isinstance(env_table, dict):
            return CheckResult(
                name, False, f"posture unknown: {env_source} is not a JSON object"
            )
    try:
        read_opencode_config_content(os.environ)
    except ReadOnlyExecutionBlocked as exc:
        return CheckResult(name, False, f"posture unknown: {exc}")
    resolved: list[str] = []
    for tool in OPENCODE_POSTURE_TOOLS:
        if tool in env_table:
            verdict, source = _permission_verdict(env_table, tool), env_source
        elif tool in table:
            verdict, source = _permission_verdict(table, tool), OPENCODE_CONFIG_FILE
        else:
            verdict, source = "allow", None
        if verdict is None:
            return CheckResult(
                name,
                False,
                f"posture unknown: {source} permission.{tool} is a pattern table "
                "with no '*' entry — state the catch-all",
            )
        if verdict != "allow":
            return CheckResult(
                name,
                False,
                f"{source} sets permission.{tool}={verdict}; a headless implement "
                "worker needs allow — set it to allow (verify denies it on its own)",
            )
        resolved.append(f"{tool}={verdict}" if source is None else f"{tool}={verdict} ({source})")
    verify = ", ".join(OPENCODE_READONLY_PERMISSION)
    return CheckResult(
        name,
        True,
        f"implement: {' '.join(resolved)}; verify: {OPENCODE_PERMISSION_ENV} denies "
        f"{verify} globally and {OPENCODE_CONFIG_CONTENT_ENV} denies them for "
        f"agent {OPENCODE_VERIFY_AGENT}",
    )


def check_opencode_rows(repo: Path) -> list[CheckResult]:
    """The six `opencode` rows: table, provider, endpoint, MCP, posture, context.

    No row launches opencode: the MCP and posture rows read the file the
    worker will read, and the endpoint row is one wire-agnostic `GET /models`.
    Whether the model calls tools is the worker's own CodeGraph handshake to
    prove, on the wire opencode actually uses. An invalid table skips the
    provider, endpoint, and context rows but never MCP and posture, which do
    not depend on it; a failed endpoint skips the context probe rather than
    reporting one outage twice. The context row is informational either way.
    """
    (
        config_name,
        provider_name,
        endpoint_name,
        mcp_name,
        posture_name,
        context_name,
    ) = OPENCODE_ROW_NAMES
    data, error = _read_opencode_json(repo)
    mcp_row = _opencode_mcp_row(mcp_name, data, error)
    posture_row = _opencode_posture_row(posture_name, data, error)
    try:
        local = load_local_config(load_config(repo=repo))
    except ProfileError as exc:
        skipped = "skipped: [local] config invalid"
        return [
            CheckResult(config_name, False, str(exc)),
            CheckResult(provider_name, False, skipped),
            CheckResult(endpoint_name, False, skipped),
            mcp_row,
            posture_row,
            CheckResult(context_name, False, skipped, level="info"),
        ]
    endpoint_row = _opencode_endpoint_row(endpoint_name, local)
    if endpoint_row.ok:
        context_row = _context_row(context_name, local)
    else:
        context_row = CheckResult(
            context_name, False, "skipped: endpoint failed", level="info"
        )
    return [
        _local_config_row(config_name, local),
        _opencode_provider_row(provider_name, data, error, local),
        endpoint_row,
        mcp_row,
        posture_row,
        context_row,
    ]


def check_hooks(repo: Path) -> CheckResult:
    try:
        check_hooks_enabled(repo)
    except HookConflictError as exc:
        return CheckResult("hooks", False, str(exc).splitlines()[0])
    return CheckResult("hooks", True, "disableAllHooks not set in any layer")


def check_ortusrc(repo: Path) -> CheckResult:
    try:
        cfg = load_config(repo=repo)
    except Exception as exc:
        return CheckResult(".ortusrc", False, f"parse error: {exc}")
    sources = ", ".join(layer.source for layer in cfg.layers)
    return CheckResult(".ortusrc", True, f"layers loaded: {sources}")


def check_verification(repo: Path) -> CheckResult:
    """Report the bar `ortus grind` will hold each issue to.

    A prototype pin lowers what a worker must prove before it closes an
    issue, so it is stated here, before any run, rather than discovered in a
    grind log. The mode is validated by `load_config`; an unknown value is
    the same parse failure the `.ortusrc` row reports, named for this key.
    """
    name = "verification"
    try:
        mode = load_config(repo=repo).get("verification", DEFAULT_VERIFICATION_MODE)
    except Exception as exc:
        return CheckResult(name, False, f".ortusrc parse error: {exc}")
    if mode == VERIFICATION_PROTOTYPE:
        return CheckResult(
            name,
            True,
            "mode=prototype — lint + syntax gate only; the issue's behavioral "
            "test commands are not run",
        )
    return CheckResult(
        name, True, "mode=full — the issue's criterion-check commands"
    )


CODEGRAPH_INSTALL_HINT = (
    "install the CodeGraph CLI (https://github.com/colbymchenry/codegraph)"
)
CODEGRAPH_INDEX_HINT = "run `codegraph init` in this repo"
CODEGRAPH_MCP_HINT = "register the MCP server with `codegraph install`"


def _grok_mcp_registered(repo: Path) -> bool:
    """Report whether project ``.grok/config.toml`` registers ``codegraph``.

    Official project config is the only file-backed Grok scope Ortus emits
    and validates. Claude's ``.mcp.json`` / ``~/.claude.json`` are not Grok
    registration, even though the binary may merge those compat sources.
    """
    path = repo / ".grok" / "config.toml"
    if not path.is_file():
        return False
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    servers = data.get("mcp_servers") or {}
    return isinstance(servers, dict) and "codegraph" in servers


def _claude_mcp_registered(repo: Path) -> bool:
    """Report whether Claude can see a `codegraph` MCP server for this repo.

    Only the file-backed registration layers are observable from the outer
    process — project `.mcp.json`, and the user/local scopes bd and Claude
    both keep in `~/.claude.json`. Launching an agent to ask it directly
    would make a strictly read-only prerequisite check spawn a process, so
    this reports what those layers say and nothing more.
    """
    candidates: list[Path] = [repo / ".mcp.json", Path.home() / ".claude.json"]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        if "codegraph" in (data.get("mcpServers") or {}):
            return True
        projects = data.get("projects")
        if isinstance(projects, dict):
            entry = projects.get(str(repo)) or {}
            if isinstance(entry, dict) and "codegraph" in (entry.get("mcpServers") or {}):
                return True
    return False


def check_codegraph(repo: Path, backend: str = "claude") -> CheckResult:
    """Report CodeGraph as a first-class prerequisite.

    Fails under `required` when the CLI or the index is missing, and passes
    informationally under `off`. Under `auto` the same gaps are reported as
    the fallback the run will take, not as a failure. Never raises: a repo
    with no CodeGraph at all must still get a full table.

    Claude's MCP registration is reported but never fails the check. Only the
    file-backed scopes are observable here, `ortus init` does not register the
    server, and the phase handshake — which does prove it, from inside the
    agent — is what enforces it at run time. opencode's is reported the same
    way here; its own `opencode mcp` row is the strict one.
    """
    name = "codegraph"
    try:
        cfg = load_config(repo=repo)
    except Exception as exc:
        return CheckResult(name, False, f".ortusrc parse error: {exc}")
    raw = cfg.get("codegraph", DEFAULT_CODEGRAPH_MODE)
    try:
        mode = CodeGraphMode(raw)
    except ValueError:
        return CheckResult(
            name, False, f"invalid codegraph mode {raw!r}; expected off, auto, or required"
        )
    if mode is CodeGraphMode.OFF:
        return CheckResult(name, True, "mode=off — disabled by policy, no index required")

    cli = _binary_check("codegraph")
    index = (repo / ".codegraph").is_dir()
    # Codex never reads a user MCP config: `CodeGraphAdapter.probe()` builds a
    # CodeGraphCapability and Ortus injects it into every fresh child, so its
    # registration is satisfied exactly when the CLI and index are.
    if backend == "codex":
        registered = cli.ok and index
        registration = "injected per child by ortus" if registered else "needs CLI + index"
    elif backend == "grok":
        registered = _grok_mcp_registered(repo)
        registration = (
            "codegraph server registered"
            if registered
            else f"not registered in a readable scope — {CODEGRAPH_MCP_HINT}"
        )
    elif backend in LOCAL_TABLE_BACKENDS:
        registered = _opencode_mcp_registered(repo)
        registration = (
            "codegraph server registered"
            if registered
            else f"not registered in {OPENCODE_CONFIG_FILE} — {OPENCODE_MCP_HINT}"
        )
    else:
        registered = _claude_mcp_registered(repo)
        registration = (
            "codegraph server registered"
            if registered
            else f"not registered in a readable scope — {CODEGRAPH_MCP_HINT}"
        )

    missing: list[str] = []
    if not cli.ok:
        missing.append(f"CLI: {cli.message} — {CODEGRAPH_INSTALL_HINT}")
    if not index:
        missing.append(f"index .codegraph/ missing — {CODEGRAPH_INDEX_HINT}")

    detail = (
        f"mode={mode.value}; CLI={'ok' if cli.ok else 'missing'}; "
        f"index={'present' if index else 'missing'}; MCP={registration}"
    )
    if not missing:
        return CheckResult(name, True, detail)
    joined = "; ".join(missing)
    if mode is CodeGraphMode.REQUIRED:
        return CheckResult(name, False, f"{detail} — required but unavailable: {joined}")
    return CheckResult(name, True, f"{detail} — auto fallback: {joined}")


def _repo_codegraph_mode(repo: Path) -> str:
    """The pinned CodeGraph policy, or the default when `.ortusrc` cannot say.

    The managed blocks render a CodeGraph paragraph from this value, so the
    comparison below has to read it the same way `ortus init` wrote it.
    """
    try:
        return str(load_config(repo=repo).get("codegraph", DEFAULT_CODEGRAPH_MODE))
    except Exception:
        return DEFAULT_CODEGRAPH_MODE


def check_agent_file(repo: Path, managed: ManagedFile) -> CheckResult:
    """One managed instruction file: present, well-formed, and current.

    Strict for every backend. A missing or drifted block is a failure the
    operator fixes with one command, and saying so is the whole point of the
    check — the alternative is an agent session silently running against a
    contract nobody refreshed.
    """
    name = managed.filename
    ignored = gitignore_match(repo, name)
    if ignored is not None:
        return CheckResult(
            name, False, f"gitignored by {ignored!r} — ortus manages it as tracked source"
        )
    path = repo / name
    if not path.is_file():
        return CheckResult(name, False, f"missing at {path} — run `ortus init --force`")
    try:
        block = read_block(path, managed.block)
    except AgentFileError as exc:
        return CheckResult(name, False, f"malformed markers — {exc}")
    if block is None:
        return CheckResult(
            name,
            False,
            f"no `ortus block={managed.block}` markers — run `ortus init --force`",
        )
    bundled = BLOCK_SCHEMAS[managed.block]
    if block.schema > bundled:
        return CheckResult(
            name,
            True,
            f"warning: block schema={block.schema} is newer than this ortus "
            f"(schema={bundled}); left untouched — upgrade ortus",
        )
    rendered = render_block(managed.block, codegraph=_repo_codegraph_mode(repo))
    if block.text != rendered:
        drift = "schema" if block.schema < bundled else "content"
        return CheckResult(
            name,
            False,
            f"{drift} drift from bundled block={managed.block} schema={bundled} — "
            "run `ortus init --force` to refresh",
        )
    return CheckResult(name, True, f"block={managed.block} schema={bundled} current")


def check_agent_file_duplicates(repo: Path, managed: ManagedFile) -> Optional[CheckResult]:
    """Info-level row when host prose repeats headings the managed block owns.

    The dangerous state is check-green-with-duplicates: init preserves every
    byte outside the markers, so a stale pre-marker copy of the block's
    sections sits above the current one until an operator deletes it, and
    agents reading top-down hit the stale copy first. Never a failure — the
    row points at the manual cleanup, and a clean file adds no row at all.
    """
    path = repo / managed.filename
    if not path.is_file():
        return None
    try:
        duplicates = duplicated_headings(path.read_text(encoding="utf-8"), path=path)
    except (OSError, AgentFileError):
        # The strict row already reports unreadable or malformed files.
        return None
    if not duplicates:
        return None
    return CheckResult(
        managed.filename,
        False,
        duplicate_headings_message(managed.filename, duplicates),
        level="info",
    )


def _agent_file_rows(repo: Path, managed: ManagedFile) -> list[CheckResult]:
    """The strict block row, plus the duplicate-headings WARN row when earned."""
    rows = [check_agent_file(repo, managed)]
    duplicate = check_agent_file_duplicates(repo, managed)
    if duplicate is not None:
        rows.append(duplicate)
    return rows


def _stale_plan_prompt(repo: Path) -> Optional[str]:
    """Name the winning plan-prompt override if it predates the placeholder.

    The bundled prompt carries the readiness contract as `$readiness_spec`; an
    override copied before that still assembles (substitution is tolerant), it
    just teaches whatever contract was frozen into the copy.
    """
    try:
        resolved = resolve_prompt("plan-prompt", repo=repo)
    except PromptNotFound:
        return None
    if resolved.source == "bundled" or READINESS_SPEC_PLACEHOLDER in resolved.text:
        return None
    return f"{resolved.source} plan-prompt.md ({resolved.path})"


def _override_warning(override_dir: Path, filename: str) -> Optional[str]:
    """The warning one repo override earns, or None for a clean ejected copy.

    Three cases, checked in order: a filename the resolver never loads, a
    copy with no provenance stamp, and a stamp whose recorded hash no longer
    matches the current bundled text (the default moved since the eject).
    User edits below a current stamp are expected and never reported.
    """
    known = {f"{entry.filename}.md" for entry in PROMPT_REGISTRY}
    if filename not in known:
        return f"{filename} is not a bundled prompt filename and is never loaded"
    try:
        text = (override_dir / filename).read_text(encoding="utf-8")
    except OSError as exc:
        return f"{filename} unreadable: {exc}"
    stamp = parse_eject_stamp(text)
    if stamp is None:
        return (
            f"{filename} has no ejected-from stamp — provenance unknown; "
            "re-create it with `ortus prompt eject`"
        )
    version, digest = stamp
    if digest != bundled_sha256(bundled_prompt_text(filename[: -len(".md")])):
        return (
            f"{filename} was ejected from ortus/{version} and the bundled "
            "default has moved since — review, then re-eject with --force"
        )
    return None


def check_prompt_overrides(repo: Path) -> CheckResult:
    """Informational check — flags per-repo prompt overrides and their health.

    Findings are warnings, never failures: an override still runs, and
    failing here would break CI in repos whose overrides are deliberate.
    """
    override_dir = repo / ".ortus" / "prompts"
    overrides: list[str] = []
    if not override_dir.is_dir():
        message = "no overrides (using bundled)"
    elif overrides := sorted(p.name for p in override_dir.glob("*.md")):
        message = f"overrides: {', '.join(overrides)}"
    else:
        message = "directory empty"
    warnings = [
        warning
        for filename in overrides
        if (warning := _override_warning(override_dir, filename))
    ]
    stale = _stale_plan_prompt(repo)
    if stale:
        warnings.append(
            f"stale {stale} predates {READINESS_SPEC_PLACEHOLDER} and teaches a "
            "frozen readiness contract — refresh or delete it"
        )
    if warnings:
        return CheckResult(
            ".ortus/prompts/", False, message + "; " + "; ".join(warnings), level="info"
        )
    return CheckResult(".ortus/prompts/", True, message)


def backend_provisioned(repo: Path, backend: str) -> bool:
    """Whether `repo` carries provisioning for `backend`.

    Discovery is the config dir on disk, not an `.ortusrc` key: `ortus init
    --backend all` writes every backend's directory and pins only one run
    backend. A merged config such as opencode's sits at the repo root, so the
    file itself is the proof. `local` is opencode under its older name and
    never earns a row of its own: the `opencode` row reports that
    provisioning once.
    """
    if backend == "local":
        return False
    if backend not in BACKEND_TEMPLATES:
        # No template means nothing could have been provisioned.
        return False
    config = repo / BACKEND_TEMPLATES[backend]
    if BACKEND_TEMPLATES[backend] in MERGED_CONFIGS:
        return config.is_file()
    return config.parent.is_dir()


def check_provisioned_backend(repo: Path, backend: str) -> CheckResult:
    """Informational row for a provisioned backend that is not the run backend.

    Gaps here are WARN rows with a remediation, never failures: the exit code
    belongs to the run backend. For `opencode` the row is deliberately
    offline — it validates the `[local]` table and the file-backed
    registration and says where the endpoint probes live, because checking
    one backend must never wait on another backend's server.
    """
    name = f"{backend} (provisioned)"
    config_rel = BACKEND_TEMPLATES[backend]
    binary = BACKEND_BINARIES[backend]
    gaps: list[str] = []
    if not (repo / config_rel).is_file():
        gaps.append(f"{config_rel} missing — run `ortus init --force`")
    if backend in LOCAL_TABLE_BACKENDS:
        # The installer's `~/.opencode/bin` counts, as it does for the run
        # backend's own row and for the launch.
        try:
            resolve_opencode_binary()
        except OpenCodeBinaryError as exc:
            gaps.append(f"{exc} — {exc.remediation}")
    elif shutil.which(binary) is None:
        gaps.append(f"{binary} CLI not on PATH — install it")
    if backend == "claude" and not _claude_mcp_registered(repo):
        gaps.append(f"codegraph MCP not registered — {CODEGRAPH_MCP_HINT}")
    elif backend == "grok" and not _grok_mcp_registered(repo):
        gaps.append(f"codegraph MCP not registered — {CODEGRAPH_MCP_HINT}")
    elif backend in LOCAL_TABLE_BACKENDS:
        if not _opencode_mcp_registered(repo):
            gaps.append(f"codegraph MCP not registered — {OPENCODE_MCP_HINT}")
        gaps.extend(_local_table_gaps(repo, OPENCODE_PROVISION_HINT))
    # codex: CodeGraph is injected per child, so CLI + index (the strict
    # codegraph row) are its whole registration story.
    if gaps:
        return CheckResult(
            name,
            False,
            "provisioned but not runnable: " + "; ".join(gaps),
            level="info",
        )
    if backend in LOCAL_TABLE_BACKENDS:
        return CheckResult(
            name,
            True,
            f"provisioned; endpoint not probed — run `ortus check --backend {backend}`",
            level="info",
        )
    return CheckResult(name, True, "provisioned and runnable", level="info")


def _local_table_gaps(repo: Path, hint: str) -> list[str]:
    """The offline `[local]` verdict for a provisioned row: missing or invalid."""
    table = read_recorded_local(repo)
    if not table:
        return [f"[local] table missing — {hint}"]
    try:
        parse_local_table(table)
    except ProfileError as exc:
        return [f"[local] table invalid: {exc} — {hint}"]
    return []


def _run_all(repo: Path, backend: str = "claude") -> list[CheckResult]:
    results: list[CheckResult] = []
    if backend == "local":
        # opencode under its older name: the same binary, file, and rows, and
        # the same exclusion from the provisioned rows below.
        backend = "opencode"
    if backend == "claude":
        backend_binary = check_claude
        settings_check: Callable[[Path], CheckResult] = check_claude_settings
        settings_label = ".claude/settings.json"
    elif backend == "codex":
        backend_binary = check_codex
        settings_check = check_codex_settings
        settings_label = ".codex/config.toml"
    elif backend == "grok":
        backend_binary = check_grok
        settings_check = check_grok_settings
        settings_label = ".grok/config.toml"
    elif backend == "opencode":
        # opencode at the same served model: its own binary and project
        # file, plus the rows that follow it. Nothing of codex's appears.
        backend_binary = check_opencode
        settings_check = check_opencode_settings
        settings_label = OPENCODE_CONFIG_FILE
    else:
        raise ValueError(f"unsupported check backend {backend!r}")
    checks: list[Callable[..., CheckResult]] = [
        check_bd,
        backend_binary,
        check_jq,
        check_sandbox,
    ]
    for c in checks:
        output.progress("check", f"{c.__name__.removeprefix('check_')} ...")
        results.append(c())
    repo_checks: list[tuple[Callable[[Path], CheckResult | list[CheckResult]], str]] = [
        (check_beads_dir, ".beads/"),
        (check_readiness_memory, "bd readiness memory"),
        (settings_check, settings_label),
    ]
    if backend == "opencode":
        repo_checks.append((check_opencode_rows, "opencode"))
    if backend == "claude":
        repo_checks.append((check_hooks, "hooks"))
        # Claude-only: the Codex verifier is not wrapped, so it has no
        # read-only posture to probe.
        repo_checks.append((check_verifier_execution, "verifier sandbox"))
    repo_checks.extend(
        [
            # Bound early so each lambda keeps its own managed file rather than
            # the last one the loop saw.
            (lambda r, m=managed: _agent_file_rows(r, m), managed.filename)
            for managed in MANAGED_FILES
        ]
    )
    repo_checks.extend(
        [
            (check_ortusrc, ".ortusrc"),
            (check_verification, "verification"),
            (lambda r: check_codegraph(r, backend), "codegraph"),
            (check_prompt_overrides, ".ortus/prompts/"),
        ]
    )
    for fn, label in repo_checks:
        output.progress("check", f"{label} ...")
        outcome = fn(repo)
        if isinstance(outcome, CheckResult):
            results.append(outcome)
        else:
            results.extend(outcome)
    for other in BACKENDS:
        if other == backend or not backend_provisioned(repo, other):
            continue
        output.progress("check", f"{other} (provisioned) ...")
        results.append(check_provisioned_backend(repo, other))
    return results


def check(
    repo: Optional[Path] = typer.Argument(
        None, help="Target repo directory. Defaults to $PWD; no walk-up."
    ),
    backend: Optional[str] = typer.Option(
        None,
        "--backend",
        help=(
            "Agent backend to verify (claude|codex|grok|local|opencode); "
            "defaults from .ortusrc."
        ),
    ),
) -> None:
    """Verify bd/claude/sandbox prereqs and hook-disable state."""
    target = (repo if repo is not None else Path.cwd()).resolve()
    try:
        resolved_backend = resolve_backend(backend, repo=target)
    except BackendError as exc:
        output.error(str(exc))
        raise typer.Exit(code=1)
    output.progress("check", f"target: {target}")
    output.progress("check", f"backend: {resolved_backend}")
    try:
        results = _run_all(target, resolved_backend)
    except ValueError as exc:
        # A run backend the registry knows but check has no rows for yet.
        output.error(str(exc), hint="check rows for this backend are a later leaf")
        raise typer.Exit(code=1)

    def _row(r: CheckResult) -> tuple[Text, str, str, str]:
        # The glyph is a styled renderable rather than markup: `output.table`
        # escapes string cells so a `[local]` row name or detail prints
        # literally, and a markup string would be escaped with the rest.
        if r.ok:
            return (Text("✓", style="green"), r.name, "PASS", r.message)
        if r.level == "info":
            return (Text("!", style="yellow"), r.name, "WARN", r.message)
        return (Text("✗", style="red"), r.name, "FAIL", r.message)

    output.table(["", "Check", "Status", "Details"], [_row(r) for r in results])
    failed = sum(1 for r in results if not r.ok and r.level != "info")
    warned = sum(1 for r in results if not r.ok and r.level == "info")
    summary = f"done ({len(results) - failed - warned}/{len(results)} passed"
    if warned:
        summary += f", {warned} warning{'s' if warned != 1 else ''}"
    output.progress("check", summary + ")")
    if failed:
        raise typer.Exit(code=1)
