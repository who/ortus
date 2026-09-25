"""ortus cost <repo> — what each closed bead cost, from the logs already written.

The rollup is read-only and offline: it re-reads `logs/grind-*.log`, so it can
be run long after a grind finished and costs nothing to re-run. Default is the
newest run; `--runs 0` sweeps every log in the directory, which is how a
judge-gated stretch of runs gets compared against a plain one.

Rows carry `-` wherever a provider never reported the number. That is
deliberate: a null bucket and a zero bucket mean very different things when the
question is "what did this cost", and only Claude and OpenCode report dollars
at all.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from ortus.core import output
from ortus.core.cost import (
    COST_ESTIMATED,
    COST_PROVIDER,
    PRICE_TABLE_VERSION,
    BeadCost,
    FailureRate,
    RunCost,
    SessionCost,
    TreeCost,
    UsageBuckets,
    failure_rates,
    find_grind_logs,
    parse_grind_log,
    parse_tree,
    rollup_beads,
)
from ortus.core.repo import resolve_repo

#: What an unreported number renders as in the human table.
_UNSET = "-"


def _tokens(value: int | None) -> str:
    return _UNSET if value is None else f"{value:,}"


def _rate(value: float | None) -> str:
    return _UNSET if value is None else f"{value * 100:.1f}%"


def _usd(value: float | None) -> str:
    return _UNSET if value is None else f"${value:,.4f}"


def _seconds(value: float | None) -> str:
    if value is None:
        return _UNSET
    minutes, seconds = divmod(int(value), 60)
    return f"{minutes}m{seconds:02d}s"


def _joined(values: tuple[str, ...]) -> str:
    return ", ".join(values) if values else _UNSET


def _flags(closed: bool, incomplete: bool, partial_usage: bool) -> str:
    """The caveats a reader needs before trusting a row's numbers."""

    marks = []
    if closed:
        marks.append("closed")
    if incomplete:
        marks.append("incomplete")
    if partial_usage:
        marks.append("partial-usage")
    return " ".join(marks)


def _input_line(usage: UsageBuckets) -> str:
    if usage.input_tokens is None:
        return _UNSET
    parts = [f"{_tokens(usage.uncached_input_tokens)} new"]
    parts.append(f"{_tokens(usage.cached_input_tokens)} cached")
    if usage.cache_write_tokens is not None:
        parts.append(f"{_tokens(usage.cache_write_tokens)} cache-write")
    line = " + ".join(parts)
    if usage.cache_hit_rate is not None:
        line += f"  ({_rate(usage.cache_hit_rate)} served from cache)"
    return line


def _record(title: str, fields: list[tuple[str, str]]) -> None:
    """Print one report block. Plain stdout: a model id carrying `[1m]` is
    literal text here, not a Rich style tag that would be swallowed."""

    typer.echo(title)
    for label, value in fields:
        typer.echo(f"  {label:<9} {value}")
    typer.echo("")


def _cost_basis(cost_source: str | None) -> str:
    """How a row's dollars were arrived at, spelled out beside them.

    An estimate and a provider figure must never read alike: one is a bill and
    the other is this repository's price table applied to the tokens a reaped
    window managed to report.
    """

    if cost_source == COST_ESTIMATED:
        return f"estimated (price table {PRICE_TABLE_VERSION})"
    if cost_source == COST_PROVIDER:
        return "provider-reported"
    return "basis unknown"


def _usage_fields(
    usage: UsageBuckets, cost_source: str | None = None
) -> list[tuple[str, str]]:
    fields = [
        ("output", _tokens(usage.output_tokens)),
        ("input", _input_line(usage)),
    ]
    if usage.reasoning_tokens is not None:
        fields.append(("reasoning", _tokens(usage.reasoning_tokens)))
    fields.append(
        (
            "cost",
            _UNSET
            if usage.cost_usd is None
            else f"{_usd(usage.cost_usd)} ({_cost_basis(cost_source)})",
        )
    )
    return fields


def _print_beads(beads: tuple[BeadCost, ...]) -> None:
    for bead in beads:
        marks = _flags(bead.closed, bead.incomplete, bead.partial_usage)
        header = f"{bead.issue_id or '(unattributed)'}  [{bead.sessions} session(s)]"
        if marks:
            header += f"  {marks}"
        fields = [
            ("backend", _joined(bead.backends)),
            ("model", _joined(bead.models)),
            ("effort", _joined(bead.efforts)),
        ]
        fields.extend(_usage_fields(bead.usage, bead.cost_source))
        fields.append(
            (
                "run",
                f"turns {_UNSET if bead.turns is None else bead.turns}"
                f"  errors {bead.errors}  wall {_seconds(bead.wall_seconds)}",
            )
        )
        _record(header, fields)


def _print_sessions(sessions: tuple[SessionCost, ...]) -> None:
    for session in sessions:
        marks = _flags(session.closed, session.incomplete, session.partial_usage)
        iteration = _UNSET if session.iteration is None else session.iteration
        header = (
            f"{session.issue_id or '(unattributed)'}  "
            f"[{session.run_id} iter {iteration}]"
        )
        if marks:
            header += f"  {marks}"
        fields = [
            ("backend", session.backend),
            ("model", session.model or _UNSET),
            ("effort", session.effort or _UNSET),
            ("session", session.session_id or _UNSET),
        ]
        fields.extend(_usage_fields(session.usage, session.cost_source))
        fields.append(
            (
                "run",
                f"turns {_UNSET if session.turns is None else session.turns}"
                f"  errors {session.errors}  wall {_seconds(session.wall_seconds)}",
            )
        )
        _record(header, fields)


def _print_failures(rates: tuple[FailureRate, ...]) -> None:
    """Per backend and model, how its worker windows failed. Silent when none did.

    Only the classes that actually fired are listed. The zero-filled buckets
    are still in the JSON, where a reader is comparing runs; a console block
    that printed seven zeroes per row would bury the one number that moved.
    """

    failing = [rate for rate in rates if rate.failures]
    if not failing:
        return
    typer.echo("worker failures")
    for rate in failing:
        fired = ", ".join(
            f"{name} {count}" for name, count in rate.counts.items() if count
        )
        typer.echo(
            f"  {rate.backend}/{rate.model or _UNSET}  "
            f"{rate.failures}/{rate.sessions} window(s) "
            f"({_rate(rate.failure_rate)})  {fired}"
        )
    typer.echo("")


def _print_tree(tree: TreeCost) -> None:
    """The whole tree: what planning cost, what the workers cost, per closed bead.

    Cost per closed bead is the figure the harness A/Bs compare, and it is
    printed as `n/a` rather than as a number when nothing closed — an arm that
    finished no bead has no price per bead, and a zero there would read as the
    cheapest arm in the comparison.
    """

    counts = tree.source_counts()
    _record(
        f"tree  [{len(tree.planner)} planner + {len(tree.workers)} worker window(s)]",
        [
            ("planner", _usd(tree.planner_usd)),
            ("workers", _usd(tree.worker_usd)),
            ("total", _usd(tree.total_usd)),
            ("closed", f"{tree.closed_beads} bead(s) of {len(tree.beads)}"),
            (
                "per bead",
                "n/a"
                if tree.usd_per_closed_bead is None
                else _usd(tree.usd_per_closed_bead),
            ),
            (
                "basis",
                f"{counts[COST_PROVIDER]} provider-reported, "
                f"{counts[COST_ESTIMATED]} estimated (price table "
                f"{PRICE_TABLE_VERSION}), {counts['unpriced']} unpriced",
            ),
        ],
    )


def _report_tree(target: Path, *, runs: int, json_out: bool) -> None:
    """The whole-tree rollup, as data or as a block. Empty logs are an error.

    A repository with no log at all cannot be answered with zeroes: the
    question is what the work cost, and "nothing was recorded" is a different
    answer from "it was free".
    """

    rollup = parse_tree(target, runs=runs)
    if not rollup.planner and not rollup.workers:
        output.error(
            f"no plan or grind logs under {target / 'logs'}",
            hint="run ortus plan or ortus grind first",
        )
        raise typer.Exit(code=1)
    output.progress(
        "cost",
        f"rolling up {len(rollup.planner)} planner and "
        f"{len(rollup.workers)} worker window(s)",
    )
    if json_out:
        typer.echo(json.dumps(rollup.as_dict(), indent=2, sort_keys=True))
        return
    _print_tree(rollup)


def cost(
    repo: Optional[Path] = typer.Argument(
        None, help="Target repo directory. Defaults to $PWD; no walk-up."
    ),
    log: Optional[Path] = typer.Option(
        None,
        "--log",
        help="Roll up this log file instead of the repo's newest grind log.",
    ),
    runs: int = typer.Option(
        1,
        "--runs",
        min=0,
        help="How many of the newest grind logs to include (0 = every log).",
    ),
    issue: Optional[str] = typer.Option(
        None, "--issue", help="Report only this bead id."
    ),
    sessions: bool = typer.Option(
        False,
        "--sessions",
        help="One row per worker window instead of one per bead.",
    ),
    tree: bool = typer.Option(
        False,
        "--tree",
        help=(
            "Roll up planning and worker windows together and report cost per "
            "closed bead for the whole tree."
        ),
    ),
    json_out: bool = typer.Option(
        False, "--json", help="Emit the rollup as JSON on stdout."
    ),
) -> None:
    """Report price-weighted token cost per bead from grind worker streams.

    Token counts are split into the buckets providers bill separately — output,
    uncached input, cached input — and normalized so a Claude run and a Codex
    run report the same quantity under the same name. Dollars are the provider's
    own figure wherever it reported one; a Claude window that was reaped before
    it could report is weighted by this repository's price table and labelled
    estimated.
    """

    target = resolve_repo(repo)
    if tree:
        if log is not None:
            output.error(
                "--tree reads the repository's logs, so it cannot take --log",
                hint="drop --log, or drop --tree to roll up one file",
            )
            raise typer.Exit(code=1)
        _report_tree(target, runs=runs, json_out=json_out)
        return
    if log is not None:
        paths: tuple[Path, ...] = (log,)
        if not log.is_file():
            output.error(
                f"no such log file: {log}",
                hint="pass a path under logs/, or drop --log to use the newest",
            )
            raise typer.Exit(code=1)
    else:
        paths = find_grind_logs(target, newest=runs)
        if not paths:
            output.error(
                f"no grind logs under {target / 'logs'}",
                hint="run ortus grind first, or pass --log <path>",
            )
            raise typer.Exit(code=1)

    output.progress("cost", f"rolling up {len(paths)} grind log(s)")
    parsed: list[RunCost] = [parse_grind_log(path) for path in paths]

    all_sessions = tuple(
        session for run in parsed for session in run.sessions
    )
    if issue is not None:
        all_sessions = tuple(s for s in all_sessions if s.issue_id == issue)
        if not all_sessions:
            output.error(
                f"no worker window in the selected logs worked {issue}",
                hint="widen the search with --runs 0",
            )
            raise typer.Exit(code=1)

    beads = rollup_beads(all_sessions)
    output.progress("cost", f"done ({len(beads)} bead(s), {len(all_sessions)} session(s))")

    if json_out:
        typer.echo(
            json.dumps(
                {
                    "runs": [
                        {
                            "run_id": run.run_id,
                            "path": str(run.path),
                            "backend": run.backend,
                        }
                        for run in parsed
                    ],
                    "sessions": [s.as_dict() for s in all_sessions],
                    "beads": [b.as_dict() for b in beads],
                    "failures": [r.as_dict() for r in failure_rates(all_sessions)],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return

    if sessions:
        _print_sessions(all_sessions)
    else:
        _print_beads(beads)
    _print_failures(failure_rates(all_sessions))
