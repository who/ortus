"""Top-level typer app + 8-verb registration (FR-001, FR-002, FR-004, FR-005)."""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

import typer

from ortus.commands.check import check
from ortus.commands.dashboard import dashboard
from ortus.commands.grind import grind
from ortus.commands.human import human
from ortus.commands.ingest import ingest
from ortus.commands.init import init
from ortus.commands.interview import interview
from ortus.commands.plan import plan
from ortus.commands.prompt import prompt_app
from ortus.commands.spec import spec
from ortus.commands.tail import tail
from ortus.commands.unlock import unlock
from ortus.commands.validate import validate

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


# FR-002 verb set (+unlock, added for stuck-flock recovery). Order here
# drives --help listing order.
app.command(name="init", help="Bootstrap a fresh repo for Claude, Codex, Grok, or a local model.")(init)
app.command(name="plan", help="Decompose a PRD (or freeform idea) into bd issues.")(plan)
app.command(name="grind", help="Drive the bd queue via backend-neutral subprocess-per-task workers.")(grind)
app.command(name="interview", help="Interactive PRD-building interview.")(interview)
app.command(name="tail", help="Tail the newest orchestrator log (use --all for every matching file).")(tail)
app.command(name="human", help="Emit HUMAN-TODO.md for items needing a human decision.")(human)
app.command(name="check", help="Verify bd/agent/sandbox prerequisites.")(check)
app.command(
    name="unlock",
    help="Clear a stuck grind flock; optionally revert in-progress claims.",
)(unlock)
app.command(
    name="spec",
    help="Print the readiness schema issue-authoring contract.",
)(spec)
app.command(
    name="validate",
    help="Report whether bd issues satisfy readiness schema v1 before grinding.",
)(validate)
app.command(
    name="dashboard",
    help="Watch one grind run in a read-only live view.",
)(dashboard)
app.command(
    name="ingest",
    # `short_help` rather than `help`: the commands table needs one line, while
    # `ortus ingest --help` is the discovery surface for a sidecar agent and
    # carries the whole packet-and-exit-code contract from the docstring.
    short_help=(
        "File a readiness schema v1 bead from a packet or stdin JSON — the "
        "filing path for agents, in place of a multiline bd create."
    ),
)(ingest)
app.add_typer(prompt_app, name="prompt")
