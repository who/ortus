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
    BeadCost,
    RunCost,
    SessionCost,
    UsageBuckets,
    find_grind_logs,
    parse_grind_log,
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


def _usage_fields(usage: UsageBuckets) -> list[tuple[str, str]]:
    fields = [
        ("output", _tokens(usage.output_tokens)),
        ("input", _input_line(usage)),
    ]
    if usage.reasoning_tokens is not None:
        fields.append(("reasoning", _tokens(usage.reasoning_tokens)))
    fields.append(
        (
            "cost",
            _UNSET if usage.cost_usd is None else f"{_usd(usage.cost_usd)} (provider-reported)",
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
        fields.extend(_usage_fields(bead.usage))
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
        fields.extend(_usage_fields(session.usage))
        fields.append(
            (
                "run",
                f"turns {_UNSET if session.turns is None else session.turns}"
                f"  errors {session.errors}  wall {_seconds(session.wall_seconds)}",
            )
        )
        _record(header, fields)


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
    json_out: bool = typer.Option(
        False, "--json", help="Emit the rollup as JSON on stdout."
    ),
) -> None:
    """Report price-weighted token cost per bead from grind worker streams.

    Token counts are split into the buckets providers bill separately — output,
    uncached input, cached input — and normalized so a Claude run and a Codex
    run report the same quantity under the same name. Dollars appear only when
    the provider reported them; nothing here applies a price table of its own.
    """

    target = resolve_repo(repo)
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
                },
                indent=2,
                sort_keys=True,
            )
        )
        return

    if sessions:
        _print_sessions(all_sessions)
        return
    _print_beads(beads)
