"""`ortus eval <root>` — the frozen before/after evaluation set.

Two surfaces over one fixture pack. `--matrix` prints the recipe: every
fixture under every arm, as commands to run in order, which is how a
treatment gets its A/B without anyone reconstructing the setup from memory.
Without it the verb reads the grind logs those runs left under `<root>` and
prints the comparison — primary metric first, guardrails beside it, and the
cells that reported nothing named rather than zeroed.

Read-only and offline in report mode: it re-reads logs, so it costs nothing
to re-run and can be run long after the last seat finished.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from ortus.core import output
from ortus.core.harness_eval import (
    build_report,
    matrix,
    render_matrix,
    render_report,
)


def evaluate(
    root: Path = typer.Argument(
        ...,
        help="Seat root: the directory holding one seat per fixture/arm cell.",
    ),
    show_matrix: bool = typer.Option(
        False,
        "--matrix",
        help="Print the run recipe for every cell instead of reading logs.",
    ),
    backend: str = typer.Option(
        "claude", "--backend", help="Backend the recipe's seats are built for."
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Emit JSON instead of the markdown rendering."
    ),
) -> None:
    """Run the frozen evaluation set's recipe, or report on what it produced."""

    if show_matrix:
        output.progress("eval", f"composing matrix for {root}")
        cells = matrix(root=root, backend=backend)
        if as_json:
            typer.echo(json.dumps([cell.as_dict() for cell in cells], indent=2))
        else:
            typer.echo(render_matrix(cells), nl=False)
        output.progress("eval", f"done ({len(cells)} cells)")
        return

    output.progress("eval", f"reading evaluation seats under {root}")
    report = build_report(root)
    if as_json:
        typer.echo(json.dumps(report.as_dict(), indent=2))
    else:
        typer.echo(render_report(report), nl=False)
    output.progress(
        "eval",
        f"done ({len(report.arms)} cells, {len(report.null_results)} with nulls)",
    )
