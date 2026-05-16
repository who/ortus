"""ortus grind <repo> — long-lived claude -p '/goal CONDITION' loop (xvel.4).

Ports ortus/goal.sh. Preserves every invariant from FR-010..FR-013:
  - flock at .beads/ortus.flock
  - sandbox smoke test (or docker_precondition_check with --docker)
  - hook precheck (disableAllHooks)
  - cache env-var exports
  - cleanup_children trap (handled by core/claude._kill_group)
  - tee to logs/grind-<ts>.log; terminal output is empty
"""

from __future__ import annotations

import datetime as _dt
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

import typer

from ortus.core import cache, hooks, output, sandbox
from ortus.core.claude import ClaudeRunner
from ortus.core.grind_logic import FlockBusy, build_condition, grind_flock
from ortus.core.repo import resolve_repo


def _make_runner() -> ClaudeRunner:
    """Indirection so tests can swap in a fake claude binary."""
    return ClaudeRunner()


def _log_path(repo: Path) -> Path:
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    log = repo / "logs" / f"grind-{ts}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    return log


def _bd_ready_count(repo: Path) -> int:
    """`bd ready --json | length`, returns 0 on any error (mirrors goal.sh)."""
    try:
        proc = subprocess.run(
            ["bd", "ready", "--json"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        import json

        data = json.loads(proc.stdout or "[]")
        return len(data) if isinstance(data, list) else 0
    except (subprocess.SubprocessError, ValueError, OSError):
        return 0


def grind(
    repo: Optional[Path] = typer.Argument(
        None, help="Target repo directory. Defaults to $PWD; no walk-up."
    ),
    tasks: int = typer.Option(
        0, "--tasks", help="Stop after N tasks complete (0 = drain queue)."
    ),
    iterations: int = typer.Option(
        0, "--iterations", help="Stop after N claude turns (0 = unlimited)."
    ),
    condition: Optional[str] = typer.Option(
        None, "-c", "--condition", help="Custom /goal condition (overrides default)."
    ),
    fast: bool = typer.Option(False, "--fast", help="Use claude --fast (premium output)."),
    docker: bool = typer.Option(
        False, "--docker", help="Run claude inside docker sandbox instead of bwrap."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print resolved flags + composed condition; do not spawn claude."
    ),
) -> None:
    """Drive the bd queue via a long-lived claude -p /goal session."""
    target = resolve_repo(repo)

    # Compose the /goal directive first so dry-run can short-circuit.
    built = build_condition(condition, max_tasks=tasks, max_iters=iterations)
    prompt = f"/goal {built.text}"

    if dry_run:
        output.info(f"repo:       {target}")
        output.info(f"tasks:      {tasks}")
        output.info(f"iterations: {iterations}")
        output.info(f"fast:       {fast}")
        output.info(f"docker:     {docker}")
        output.info("--- prompt ---")
        output.info(prompt)
        return

    # Phase 0 — sandbox precondition (Tier 1 native vs Tier 2 docker).
    try:
        if docker:
            sandbox.docker_precondition_check()
        else:
            sandbox.smoke_test()
    except sandbox.SandboxUnavailable as exc:
        output.error(str(exc).splitlines()[0])
        raise typer.Exit(code=1)

    # Phase 1 — hook precheck (must run BEFORE any claude spawn).
    try:
        hooks.check_hooks_enabled(target)
    except hooks.HookConflictError as exc:
        output.error(str(exc).splitlines()[0])
        raise typer.Exit(code=1)

    # Phase 2 — acquire the flock so two grinds can't run concurrently.
    try:
        with grind_flock(target) as lockfile:
            log = _log_path(target)
            output.info(f"grind starting; log → {log.relative_to(target)}")
            initial = _bd_ready_count(target)
            output.info(f"initial ready backlog: {initial}")

            # Phase 3 — cache env vars (relocate ~/.cache into project-local).
            cache.ensure_cache_dirs(target)
            cache_env = cache.env_overrides(target)

            runner = _make_runner()
            runner.extra_env.update(cache_env)
            rc = runner.run(prompt, repo=target, log_path=log, fast=fast)

            final = _bd_ready_count(target)
            if initial > 0:
                drained = initial - final
                pct = drained * 100 // initial
                output.info(f"session ended; drained {drained}/{initial} ({pct}%); {final} remaining")
            else:
                output.info(f"session ended; {final} ready remaining")
            if rc != 0:
                raise typer.Exit(code=rc)
    except FlockBusy as exc:
        output.error(str(exc), hint="another `ortus grind` is already running here")
        raise typer.Exit(code=1)
