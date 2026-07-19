"""Top-level typer app + 8-verb registration (FR-001, FR-002, FR-004, FR-005)."""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

import typer

from ortus.commands.check import check
from ortus.commands.grind import grind
from ortus.commands.human import human
from ortus.commands.init import init
from ortus.commands.interview import interview
from ortus.commands.plan import plan
from ortus.commands.tail import tail
from ortus.commands.triage import triage

app = typer.Typer(
    name="ortus",
    help="Global CLI for bd-driven Claude Code workflows.",
    no_args_is_help=True,
    add_completion=False,
)


def _resolve_version() -> str:
    try:
        return _pkg_version("ortus")
    except PackageNotFoundError:
        from ortus import __version__

        return __version__


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"ortus {_resolve_version()}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show ortus version and exit.",
    ),
) -> None:
    """ortus: global CLI for bd-driven Claude Code workflows."""


# FR-002: exactly 8 verbs. Order here drives --help listing order.
app.command(name="init", help="Bootstrap a fresh repo for Claude or Codex.")(init)
app.command(name="plan", help="Decompose a PRD (or freeform idea) into bd issues.")(plan)
app.command(name="grind", help="Drive the bd queue via backend-neutral subprocess-per-task workers.")(grind)
app.command(name="interview", help="Interactive PRD-building interview.")(interview)
app.command(name="tail", help="Tail orchestrator log files (grind-*, goal-*, ralph-*).")(tail)
app.command(name="triage", help="Triage open bd issues interactively.")(triage)
app.command(name="human", help="Emit HUMAN-TODO.md for items needing a human decision.")(human)
app.command(name="check", help="Verify bd/agent/sandbox prerequisites.")(check)
