"""ortus check <repo> — verify prerequisites for the orchestrator (q075.6).

Strictly read-only (NFR-006). Each check returns a CheckResult; the verb
collects results, renders a rich table, and exits 0 if all pass else 1.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import typer

from ortus.core import output, sandbox
from ortus.core.config import load_config
from ortus.core.hooks import HookConflictError, check_hooks_enabled


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


def check_jq() -> CheckResult:
    return _binary_check("jq")


def check_sandbox() -> CheckResult:
    try:
        info = sandbox.smoke_test()
    except sandbox.SandboxUnavailable as exc:
        return CheckResult("sandbox", False, str(exc).splitlines()[0])
    return CheckResult("sandbox", True, f"{info.platform} → {info.binary}")


def check_beads_dir(repo: Path) -> CheckResult:
    beads = repo / ".beads"
    if not beads.is_dir():
        return CheckResult(".beads/", False, f"missing at {beads}")
    return CheckResult(".beads/", True, str(beads))


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


def check_prompt_overrides(repo: Path) -> CheckResult:
    """Optional informational check — flags any per-repo prompt overrides."""
    override_dir = repo / ".ortus" / "prompts"
    if not override_dir.is_dir():
        return CheckResult(".ortus/prompts/", True, "no overrides (using bundled)")
    overrides = sorted(p.name for p in override_dir.glob("*.md"))
    if not overrides:
        return CheckResult(".ortus/prompts/", True, "directory empty")
    return CheckResult(
        ".ortus/prompts/", True, f"overrides: {', '.join(overrides)}"
    )


CHECKS: list[Callable[..., CheckResult]] = [
    check_bd,
    check_claude,
    check_jq,
    check_sandbox,
]


def _run_all(repo: Path) -> list[CheckResult]:
    results: list[CheckResult] = []
    for c in CHECKS:
        output.progress("check", f"{c.__name__.removeprefix('check_')} ...")
        results.append(c())
    for fn, label in (
        (check_beads_dir, ".beads/"),
        (check_claude_settings, ".claude/settings.json"),
        (check_hooks, "hooks"),
        (check_ortusrc, ".ortusrc"),
        (check_prompt_overrides, ".ortus/prompts/"),
    ):
        output.progress("check", f"{label} ...")
        results.append(fn(repo))
    return results


def check(
    repo: Optional[Path] = typer.Argument(
        None, help="Target repo directory. Defaults to $PWD; no walk-up."
    ),
) -> None:
    """Verify bd/claude/sandbox prereqs and hook-disable state."""
    target = (repo if repo is not None else Path.cwd()).resolve()
    output.progress("check", f"target: {target}")
    results = _run_all(target)
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
