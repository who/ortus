"""`ortus eval <root>` — the frozen before/after evaluation set.

Three surfaces over one fixture pack. `--matrix` prints the recipe: every
fixture under every arm, as commands to run in order, which is how a
treatment gets its A/B without anyone reconstructing the setup from memory.
`--run` issues those same commands itself, cell by cell, and ends with the
report, so a comparison is one command rather than eight copy-pasted blocks
and a mistyped seat name cannot quietly produce two arms that were never
comparable. Without either flag the verb reads the grind logs those runs left
under `<root>` and prints the comparison — primary metric first, guardrails
beside it, and the cells that reported nothing named rather than zeroed.

Read-only and offline in report mode: it re-reads logs, so it costs nothing
to re-run and can be run long after the last seat finished. `--run` is the
opposite: it spends real model budget, which is why it is a flag the operator
types rather than the default.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from ortus.core import output
from ortus.core.harness_eval import (
    CELL_FAILED,
    CELL_RAN,
    CELL_SKIPPED,
    DEFAULT_CELL_TIMEOUT_SECONDS,
    CommandExecutor,
    EvalReport,
    build_report,
    matrix,
    render_matrix,
    render_report,
    run_matrix,
    shell_executor,
)


def _make_executor() -> CommandExecutor:
    """Indirection so tests drive a sweep without spawning a backend."""

    return shell_executor


def _emit(report: EvalReport, as_json: bool) -> None:
    """Print the report in whichever shape the caller asked for."""

    if as_json:
        typer.echo(json.dumps(report.as_dict(), indent=2))
    else:
        typer.echo(render_report(report), nl=False)


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
    run: bool = typer.Option(
        False,
        "--run",
        help="Execute every cell of the recipe, then print the report. "
        "Spends real model budget.",
    ),
    backend: str = typer.Option(
        "claude", "--backend", help="Backend the recipe's seats are built for."
    ),
    cell_timeout: float = typer.Option(
        DEFAULT_CELL_TIMEOUT_SECONDS,
        "--cell-timeout",
        help="Wall-clock budget in seconds for one cell's commands; "
        "0 waits indefinitely.",
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Emit JSON instead of the markdown rendering."
    ),
) -> None:
    """Run the frozen evaluation set's recipe, or report on what it produced."""

    if show_matrix and run:
        raise typer.BadParameter(
            "--matrix prints the recipe and --run executes it; pick one."
        )

    if show_matrix:
        output.progress("eval", f"composing matrix for {root}")
        cells = matrix(root=root, backend=backend)
        if as_json:
            typer.echo(json.dumps([cell.as_dict() for cell in cells], indent=2))
        else:
            typer.echo(render_matrix(cells), nl=False)
        output.progress("eval", f"done ({len(cells)} cells)")
        return

    if run:
        output.progress("eval", f"running the evaluation matrix under {root}")
        executions = run_matrix(
            root,
            backend=backend,
            executor=_make_executor(),
            timeout=cell_timeout if cell_timeout > 0 else None,
        )
        report = build_report(root, executions=executions)
        _emit(report, as_json)
        tally = {
            status: sum(1 for item in executions if item.status == status)
            for status in (CELL_RAN, CELL_SKIPPED, CELL_FAILED)
        }
        output.progress(
            "eval",
            f"done ({tally[CELL_RAN]} ran, {tally[CELL_SKIPPED]} skipped, "
            f"{tally[CELL_FAILED]} failed)",
        )
        return

    output.progress("eval", f"reading evaluation seats under {root}")
    report = build_report(root)
    _emit(report, as_json)
    output.progress(
        "eval",
        f"done ({len(report.arms)} cells, {len(report.null_results)} with nulls)",
    )
