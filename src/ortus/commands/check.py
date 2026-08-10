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
from ortus.core.agent import BackendError, resolve_backend
from ortus.core.claude import ClaudeRunner, ReadOnlyExecutionBlocked
from ortus.core.codegraph import CodeGraphMode
from ortus.core.config import DEFAULT_CODEGRAPH_MODE, load_config
from ortus.core.hooks import HookConflictError, check_hooks_enabled
from ortus.core.prompts import (
    READINESS_SPEC_PLACEHOLDER,
    PromptNotFound,
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
    from taking bd's write or auto-sync paths.
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
    if READINESS_MEMORY_KEY in memories:
        return CheckResult(name, True, f"key={READINESS_MEMORY_KEY}")
    return CheckResult(
        name, False, f"missing — add it with: {readiness_memory_command()}"
    )


def check_claude_settings(repo: Path) -> CheckResult:
    settings = repo / ".claude" / "settings.json"
    if not settings.is_file():
        return CheckResult(".claude/settings.json", False, f"missing at {settings}")
    try:
        data = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return CheckResult(".claude/settings.json", False, f"unparseable: {exc}")
    excluded = data.get("sandbox", {}).get("excludedCommands") or []
    missing = [c for c in ("bd", "bd *") if c not in excluded]
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


def check_prompt_overrides(repo: Path) -> CheckResult:
    """Optional informational check — flags any per-repo prompt overrides.

    A stale override is reported, not failed: it still runs, and failing here
    would break CI in repos whose overrides are otherwise deliberate.
    """
    override_dir = repo / ".ortus" / "prompts"
    if not override_dir.is_dir():
        message = "no overrides (using bundled)"
    elif overrides := sorted(p.name for p in override_dir.glob("*.md")):
        message = f"overrides: {', '.join(overrides)}"
    else:
        message = "directory empty"
    stale = _stale_plan_prompt(repo)
    if stale:
        message += (
            f"; stale {stale} predates {READINESS_SPEC_PLACEHOLDER} and teaches a "
            "frozen readiness contract — refresh or delete it"
        )
    return CheckResult(".ortus/prompts/", True, message)


def _run_all(repo: Path, backend: str = "claude") -> list[CheckResult]:
    results: list[CheckResult] = []
    checks: list[Callable[..., CheckResult]] = [
        check_bd,
        check_claude if backend == "claude" else check_codex,
        check_jq,
        check_sandbox,
    ]
    for c in checks:
        output.progress("check", f"{c.__name__.removeprefix('check_')} ...")
        results.append(c())
    repo_checks: list[tuple[Callable[[Path], CheckResult], str]] = [
        (check_beads_dir, ".beads/"),
        (check_readiness_memory, "bd readiness memory"),
        (
            check_claude_settings if backend == "claude" else check_codex_settings,
            ".claude/settings.json" if backend == "claude" else ".codex/config.toml",
        ),
    ]
    if backend == "claude":
        repo_checks.append((check_hooks, "hooks"))
        # Claude-only: the Codex verifier is not wrapped, so it has no
        # read-only posture to probe.
        repo_checks.append((check_verifier_execution, "verifier sandbox"))
    repo_checks.extend(
        [
            (check_ortusrc, ".ortusrc"),
            (lambda r: check_codegraph(r, backend), "codegraph"),
            (check_prompt_overrides, ".ortus/prompts/"),
        ]
    )
    for fn, label in repo_checks:
        output.progress("check", f"{label} ...")
        results.append(fn(repo))
    return results


def check(
    repo: Optional[Path] = typer.Argument(
        None, help="Target repo directory. Defaults to $PWD; no walk-up."
    ),
    backend: Optional[str] = typer.Option(
        None,
        "--backend",
        help="Agent backend to verify (claude|codex); defaults from .ortusrc.",
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
    output.table(
        ["", "Check", "Status", "Details"],
        [
            ("[green]✓[/green]" if r.ok else "[red]✗[/red]", r.name, "PASS" if r.ok else "FAIL", r.message)
            for r in results
        ],
    )
    failed = sum(1 for r in results if not r.ok)
    output.progress(
        "check",
        f"done ({len(results) - failed}/{len(results)} passed)",
    )
    if failed:
        raise typer.Exit(code=1)
