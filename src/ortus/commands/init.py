"""ortus init <repo> — bootstrap a fresh repo with bd + .claude + AGENTS.md (q075.5)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

import typer

from ortus.core import output
from ortus.core.init_render import RenderContext, render_all


def _bd_init(repo: Path, prefix: str | None) -> None:
    """Run `bd init --prefix <prefix>` inside `repo`."""
    args = ["bd", "init"]
    if prefix:
        args.extend(["--prefix", prefix])
    subprocess.run(args, cwd=str(repo), check=True, capture_output=True)


def init(
    repo: Optional[Path] = typer.Argument(
        None,
        help="Target repo directory. Defaults to $PWD. Created if missing.",
    ),
    force: bool = typer.Option(
        False, "--force", help="Re-render ortus-owned files even if .beads/ already exists."
    ),
    prefix: Optional[str] = typer.Option(
        None,
        "--prefix",
        help="bd issue-id prefix (default: target directory basename).",
    ),
    project_type: str = typer.Option(
        "polyglot",
        "--project-type",
        help="Project type for templating (python|typescript|go|rust|polyglot).",
    ),
) -> None:
    """Bootstrap a new repo: bd workspace, .claude/settings.json, .ortusrc, AGENTS.md."""
    target = (repo if repo is not None else Path.cwd()).resolve()
    target.mkdir(parents=True, exist_ok=True)

    already_initialized = (target / ".beads").is_dir()
    if already_initialized and not force:
        output.error(
            f"{target} already has a .beads/ workspace",
            hint="pass --force to re-render ortus-owned files (.claude/settings.json, .ortusrc, AGENTS.md, .gitignore)",
        )
        raise typer.Exit(code=1)

    resolved_prefix = prefix or target.name

    if not already_initialized:
        try:
            _bd_init(target, resolved_prefix)
        except subprocess.CalledProcessError as exc:
            # bd may write its actionable error to stdout (success messages)
            # or stderr (failures) depending on subcommand and version, and
            # on Windows the default capture encoding can mangle non-ASCII.
            # Surface both streams so the operator can see the real cause.
            def _decode(stream: object) -> str:
                if isinstance(stream, (bytes, bytearray)):
                    return stream.decode("utf-8", errors="replace")
                return stream or ""
            stderr = _decode(exc.stderr).strip()
            stdout = _decode(exc.stdout).strip()
            message = stderr or stdout or str(exc)
            output.error(f"bd init failed (exit {exc.returncode}): {message}")
            if stderr and stdout:
                # Both streams have content — append stdout so it isn't lost.
                output.error(f"bd init stdout: {stdout}")
            raise typer.Exit(code=1)
        output.success(f"bd workspace initialized (prefix={resolved_prefix})")
    elif force:
        output.warn(f".beads/ exists; skipping bd init (--force re-renders templates only)")

    ctx = RenderContext(prefix=resolved_prefix, project_type=project_type)
    written = render_all(target, ctx)
    for p in written:
        output.success(f"wrote {p.relative_to(target)}")

    output.success(f"ortus init complete: {target}")
