"""`ortus prompt` — read-only access to the bundled runtime prompts.

`list` names every registered prompt and the layer that currently wins for a
repo; `show` prints the resolved text pipe-clean on stdout (header on stderr)
so `ortus prompt show goal` can feed another process directly. Neither verb
materializes or merges anything; overrides stay a plain three-layer file
lookup in ortus.core.prompts.
"""

from __future__ import annotations

from pathlib import Path

import typer

from ortus.core.prompts import (
    PROMPT_PACKAGE,
    PROMPT_REGISTRY,
    PromptNotFound,
    ResolvedPrompt,
    registry_entry,
    resolve_named_prompt,
)

prompt_app = typer.Typer(
    name="prompt",
    help="List bundled runtime prompts and print their resolved text.",
    no_args_is_help=True,
)


def _source_label(resolved: ResolvedPrompt) -> str:
    """The layer that won, phrased for an operator."""
    if resolved.source == "bundled":
        return "bundled (default)"
    return f"{resolved.source} ({resolved.path})"


@prompt_app.command("list")
def list_prompts(
    repo: Path = typer.Argument(
        Path("."),
        help="Repository whose .ortus/prompts/ overrides apply (default: cwd).",
    ),
) -> None:
    """Name, winning source, phase, and description for every prompt."""
    width = max(len(entry.name) for entry in PROMPT_REGISTRY)
    for entry in PROMPT_REGISTRY:
        resolved = resolve_named_prompt(entry.name, repo=repo)
        typer.echo(
            f"{entry.name:<{width}}  {_source_label(resolved):<18}  "
            f"{entry.phase:<14}  {entry.description}"
        )


@prompt_app.command("show")
def show_prompt(
    name: str = typer.Argument(..., help="Registered prompt name (see list)."),
    repo: Path = typer.Argument(
        Path("."),
        help="Repository whose .ortus/prompts/ overrides apply (default: cwd).",
    ),
    origin: bool = typer.Option(
        False,
        "--origin",
        help="Print only the winning source tier and path, not the text.",
    ),
) -> None:
    """Resolved prompt text on stdout; header and errors on stderr."""
    try:
        entry = registry_entry(name)
    except PromptNotFound as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2)
    resolved = resolve_named_prompt(entry.name, repo=repo)
    if origin:
        if resolved.path is None:
            # Zip-backed install: no filesystem path, name the package resource.
            typer.echo(f"bundled {PROMPT_PACKAGE}/{entry.filename}.md")
        else:
            typer.echo(f"{resolved.source} {resolved.path}")
        return
    typer.echo(f"{entry.name} <- {_source_label(resolved)}", err=True)
    typer.echo(resolved.text, nl=False)
