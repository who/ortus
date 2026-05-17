"""ortus init <repo> — bootstrap a fresh repo with bd + .claude + AGENTS.md (q075.5)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

import typer

from ortus.core import output
from ortus.core.init_render import (
    FRAMEWORK_CHOICES,
    FRAMEWORK_DEFAULTS,
    LINTER_CHOICES,
    LINTER_DEFAULTS,
    PACKAGE_MANAGER_CHOICES,
    PACKAGE_MANAGER_DEFAULTS,
    PROJECT_TYPES,
    RenderContext,
    render_all,
)


def _bd_init(repo: Path, prefix: str | None) -> None:
    """Run `bd init --prefix <prefix>` inside `repo`.

    Streams bd's output straight to the operator's stdout/stderr instead of
    capturing it. Capturing can deadlock on a pipe-buffer boundary if bd writes
    more than ~64 KB before exiting, and bd's non-TTY code path can be much
    slower than its TTY one — both manifested as a multi-minute hang on a
    fresh dir. Streaming sidesteps both, and the operator gets to see bd's
    own progress lines during the init.
    """
    args = ["bd", "init"]
    if prefix:
        args.extend(["--prefix", prefix])
    subprocess.run(args, cwd=str(repo), check=True)


def _resolve_choice(
    flag_name: str,
    cli_value: Optional[str],
    project_type: str,
    choices: dict[str, tuple[str, ...]],
    defaults: dict[str, str],
) -> str:
    """Resolve one of --package-manager / --framework / --linter.

    Order: explicit CLI value (validated) → per-language default. Raises
    typer.Exit(1) with a helpful message on an invalid combination.
    """
    valid = choices[project_type]
    if cli_value is None:
        return defaults[project_type]
    if cli_value not in valid:
        output.error(
            f"{flag_name}={cli_value!r} is not valid for --project-type={project_type}",
            hint=f"choices for {project_type}: {', '.join(valid)}",
        )
        raise typer.Exit(code=1)
    return cli_value


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
    package_manager: Optional[str] = typer.Option(
        None,
        "--package-manager",
        help="Package manager (choices depend on --project-type; per-language default applies if omitted).",
    ),
    framework: Optional[str] = typer.Option(
        None,
        "--framework",
        help="Web/app framework (choices depend on --project-type; defaults to 'none').",
    ),
    linter: Optional[str] = typer.Option(
        None,
        "--linter",
        help="Linter (choices depend on --project-type; per-language default applies if omitted).",
    ),
) -> None:
    """Bootstrap a new repo: bd workspace, .claude/settings.json, .ortusrc, AGENTS.md."""
    if project_type not in PROJECT_TYPES:
        output.error(
            f"--project-type={project_type!r} is not recognized",
            hint=f"choices: {', '.join(PROJECT_TYPES)}",
        )
        raise typer.Exit(code=1)

    resolved_pm = _resolve_choice(
        "--package-manager", package_manager, project_type,
        PACKAGE_MANAGER_CHOICES, PACKAGE_MANAGER_DEFAULTS,
    )
    resolved_fw = _resolve_choice(
        "--framework", framework, project_type,
        FRAMEWORK_CHOICES, FRAMEWORK_DEFAULTS,
    )
    resolved_lint = _resolve_choice(
        "--linter", linter, project_type,
        LINTER_CHOICES, LINTER_DEFAULTS,
    )

    target = (repo if repo is not None else Path.cwd()).resolve()
    output.progress("init", f"target: {target}")
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
        output.progress("init", f"creating .beads/ workspace (prefix={resolved_prefix})")
        try:
            _bd_init(target, resolved_prefix)
        except subprocess.CalledProcessError as exc:
            # bd's output streamed directly to the operator's terminal, so the
            # error message is already on screen above this line. Just signal
            # the failure and exit.
            output.error(f"bd init failed (exit {exc.returncode})")
            raise typer.Exit(code=1)
        output.success(f"bd workspace initialized (prefix={resolved_prefix})")
    elif force:
        output.warn(f".beads/ exists; skipping bd init (--force re-renders templates only)")

    output.progress(
        "init",
        f"rendering ortus-owned files (project_type={project_type}, "
        f"package_manager={resolved_pm}, framework={resolved_fw}, linter={resolved_lint})",
    )
    ctx = RenderContext(
        prefix=resolved_prefix,
        project_type=project_type,
        package_manager=resolved_pm,
        framework=resolved_fw,
        linter=resolved_lint,
    )
    written = render_all(target, ctx)
    for p in written:
        output.success(f"wrote {p.relative_to(target)}")

    output.progress("init", f"done ({len(written)} files, prefix={resolved_prefix})")
    output.success(f"ortus init complete: {target}")
