"""ortus check <repo> — verify prerequisites for the orchestrator (q075.6).

Strictly read-only (NFR-006). Each check returns a CheckResult; the verb
collects results, renders a rich table, and exits 0 if all pass else 1.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import typer

from ortus.core import output, sandbox
from ortus.core.agent import BACKENDS, BackendError, resolve_backend
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
from ortus.core.config import DEFAULT_CODEGRAPH_MODE, load_config
from ortus.core.hooks import HookConflictError, check_hooks_enabled
from ortus.core.init_render import BACKEND_TEMPLATES
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


def _binary_check(name: str, *, version_flag: str = "--version") -> CheckResult:
    path = shutil.which(name)
    if path is None:
        return CheckResult(name, False, f"{name} not on PATH")
    try:
        proc = subprocess.run(
            [name, version_flag], capture_output=True, text=True, timeout=10, check=False
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
    agent — is what enforces it at run time.
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


def check_provisioned_backend(repo: Path, backend: str) -> CheckResult:
    """Informational row for a provisioned backend that is not the run backend.

    `ortus init --backend all` writes every backend's config dir, so a repo
    routinely carries config for backends it never runs. Discovery is the
    config dir on disk, not an `.ortusrc` key. Gaps here are WARN rows with a
    remediation, never failures: the exit code belongs to the run backend.
    """
    name = f"{backend} (provisioned)"
    config_rel = BACKEND_TEMPLATES[backend]
    gaps: list[str] = []
    if not (repo / config_rel).is_file():
        gaps.append(f"{config_rel} missing — run `ortus init --force`")
    if shutil.which(backend) is None:
        gaps.append(f"{backend} CLI not on PATH — install it")
    if backend == "claude" and not _claude_mcp_registered(repo):
        gaps.append(f"codegraph MCP not registered — {CODEGRAPH_MCP_HINT}")
    elif backend == "grok" and not _grok_mcp_registered(repo):
        gaps.append(f"codegraph MCP not registered — {CODEGRAPH_MCP_HINT}")
    # codex: CodeGraph is injected per child, so CLI + index (the strict
    # codegraph row) are its whole registration story.
    if not gaps:
        return CheckResult(name, True, "provisioned and runnable", level="info")
    return CheckResult(
        name,
        False,
        "provisioned but not runnable: " + "; ".join(gaps),
        level="info",
    )


def _run_all(repo: Path, backend: str = "claude") -> list[CheckResult]:
    results: list[CheckResult] = []
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
        if other == backend:
            continue
        if not (repo / BACKEND_TEMPLATES[other]).parent.is_dir():
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
        help="Agent backend to verify (claude|codex|grok); defaults from .ortusrc.",
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
    results = _run_all(target, resolved_backend)

    def _row(r: CheckResult) -> tuple[str, str, str, str]:
        if r.ok:
            return ("[green]✓[/green]", r.name, "PASS", r.message)
        if r.level == "info":
            return ("[yellow]![/yellow]", r.name, "WARN", r.message)
        return ("[red]✗[/red]", r.name, "FAIL", r.message)

    output.table(["", "Check", "Status", "Details"], [_row(r) for r in results])
    failed = sum(1 for r in results if not r.ok and r.level != "info")
    warned = sum(1 for r in results if not r.ok and r.level == "info")
    summary = f"done ({len(results) - failed - warned}/{len(results)} passed"
    if warned:
        summary += f", {warned} warning{'s' if warned != 1 else ''}"
    output.progress("check", summary + ")")
    if failed:
        raise typer.Exit(code=1)
