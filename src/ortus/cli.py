"""Top-level typer app. Verb registration lives in q075.2."""

import typer

from ortus import __version__

app = typer.Typer(
    name="ortus",
    help="Global CLI for bd-driven Claude Code workflows.",
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"ortus {__version__}")
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
