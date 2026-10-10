"""Export private event records and replay operator labels without provider calls."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Iterable, List, Optional

import typer

from ortus.core.judge_calibration import compare_candidates, read_candidates
from ortus.core.judge_log import LOG_NAME, ROUTE_LOG_NAME
from ortus.core.judge_outcomes import dumps, read_outcomes, referenced_issues, snapshot_outcomes
from ortus.core.judge_progress import PROGRESS_LOG_NAME
from ortus.core.judge_replay import (
    SITE_KINDS, ReplayError, join_events, read_events, read_labels, read_records, summarize,
)
from ortus.core.judge_scorers import read_tool_labels, summarize_sites
from ortus.core.output import progress

judge_app = typer.Typer(help="Export judge events and calculate offline replay metrics.",
                       no_args_is_help=True)


def _write_atomic(path: Path, chunks: Iterable[str], force: bool) -> None:
    """Publish a complete private file; link provides atomic no-clobber semantics."""
    temporary = None
    try:
        if path.is_symlink() or (path.exists() and not force):
            raise ReplayError("output exists; use --force to replace a regular file")
        if path.exists() and not path.is_file():
            raise ReplayError("output must be a regular file")
        fd, temporary = tempfile.mkstemp(prefix=".judge-", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            for chunk in chunks:
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        if force:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
    except FileExistsError:
        raise ReplayError("output exists; use --force to replace a regular file") from None
    except OSError:
        raise ReplayError("cannot write output") from None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def _read(path: Path):
    return join_events(read_events(path, warn=lambda message: progress("judge", f"warning: {message}")))


def _records(path: Path) -> list[dict]:
    return list(read_records(path, warn=lambda message: progress("judge", f"warning: {message}")))


def _fail(exc: ReplayError) -> None:
    progress("judge", str(exc))
    raise typer.Exit(1)


@judge_app.command()
def export(
    input: Path = typer.Argument(..., help="Version-1 judge event JSONL."),
    output: Path = typer.Option(..., "--output", help="Destination JSONL file."),
    force: bool = typer.Option(False, "--force", help="Replace an existing output file."),
) -> None:
    """Export validated, deduplicated events using only the logger's allowed fields."""
    progress("judge", "reading and validating events (large logs may take 1-3 min)")
    try:
        if input.resolve() == output.resolve():
            raise ReplayError("output must differ from input")
        data = _read(input)
        progress("judge", "writing event export")
        _write_atomic(output, (json.dumps(e, sort_keys=True, allow_nan=False) + "\n"
                               for e in data.events), force)
    except ReplayError as exc:
        _fail(exc)
    progress("judge", f"done ({len(data.events)} events exported)")


@judge_app.command()
def outcomes(
    repo: Path = typer.Argument(..., help="Repository whose bd tracker to read."),
    output: Path = typer.Option(..., "--output", help="Destination outcomes JSON file."),
    log: Optional[List[Path]] = typer.Option(
        None, "--log", help="Jev log naming the issues to snapshot (repeatable). "
        "Defaults to the three logs under <repo>/logs.",
    ),
    force: bool = typer.Option(False, "--force", help="Replace an existing output file."),
) -> None:
    """Snapshot tracker outcomes read-only: status, labels, reopens, human flag, closed-at."""
    progress("judge", "reading Jev logs for referenced issues")
    try:
        logs = list(log or ())
        if not logs:
            logs = [path for name in (LOG_NAME, ROUTE_LOG_NAME, PROGRESS_LOG_NAME)
                    if (path := repo / "logs" / name).is_file()]
        if output.resolve() in {path.resolve() for path in logs}:
            raise ReplayError("output must differ from inputs")
        issue_ids = (referenced_issues(record for path in logs for record in _records(path))
                     if logs else None)
        progress("judge", "reading tracker history with bd --readonly "
                 "(one bd call per issue; large logs may take 1-3 min)")
        snapshot = snapshot_outcomes(repo, issue_ids)
        progress("judge", "writing outcomes snapshot")
        _write_atomic(output, [dumps(snapshot)], force)
    except ReplayError as exc:
        _fail(exc)
    progress("judge", f"done ({len(snapshot['issues'])} issues)")


@judge_app.command()
def replay(
    input: List[Path] = typer.Argument(
        ..., help="Jev logs: decisions, model routes and progress (one or more)."),
    labels: Optional[Path] = typer.Option(
        None, "--labels", help="JSON object keyed by decision_id."),
    output: Path = typer.Option(..., "--output", help="Destination metrics JSON file."),
    outcomes: Optional[List[Path]] = typer.Option(
        None, "--outcomes", help="Snapshot from `ortus judge outcomes` (repeatable)."),
    tool_labels: Optional[Path] = typer.Option(
        None, "--tool-labels", help="pre_tool rubric: decision_id to expected_action."),
    thresholds: Optional[Path] = typer.Option(
        None, "--thresholds",
        help="TOML with at most 20 soft candidate tables (log, flag or rewrite).",
    ),
    force: bool = typer.Option(False, "--force", help="Replace an existing output file."),
) -> None:
    """Calculate accuracy, latency, coverage and measured costs without rerunning the judge.

    Labels contain expected_action (proceed, human or skip), needs_human (boolean),
    and optional worker_cost_usd. Shadow accuracy uses the intended action only
    when the observed issue matches the actual worker. Percentiles use nearest
    rank. Costs remain null when no measurement is available.

    With --outcomes, labels become optional: every Jev site is scored against
    the tracker outcomes and reported under `sites`, with Jev's accuracy
    against the code baseline and the preregistered verdict from
    docs/jev-prereg.md.

    With --thresholds, each candidate band is also scored against the recorded
    answers and reported under `calibration`. Candidates are compared, never
    applied: no configuration is read for enforcement or written back, and no
    candidate can withhold a claim.
    """
    progress("judge", "reading events and labels (large logs may take 1-3 min)")
    try:
        if labels is None and not outcomes:
            raise ReplayError("pass --labels, --outcomes, or both")
        sources = [*input, *(outcomes or ())]
        sources += [path for path in (labels, tool_labels, thresholds) if path is not None]
        if output.resolve() in {path.resolve() for path in sources}:
            raise ReplayError("output must differ from inputs")
        inputs = [_records(path) for path in input]
        data = join_events(e for records in inputs for e in records if e["event"] not in SITE_KINDS)
        label_map = read_labels(labels) if labels is not None else {}
        metrics = summarize(data, label_map)
        if outcomes:
            progress("judge", "scoring every Jev site against tracker outcomes")
            tracker: dict[str, dict] = {}
            for path in outcomes:
                for issue_id, entry in read_outcomes(path).items():
                    if tracker.setdefault(issue_id, entry) != entry:
                        raise ReplayError("outcomes disagree about one issue")
            rubric = read_tool_labels(tool_labels) if tool_labels is not None else {}
            metrics["sites"] = summarize_sites(inputs, tracker, rubric)
        if thresholds is not None:
            progress("judge", "comparing threshold candidates (no settings are applied)")
            metrics["calibration"] = compare_candidates(
                data, label_map, read_candidates(thresholds)
            )
        progress("judge", "writing replay metrics")
        _write_atomic(output, [json.dumps(metrics, indent=2, sort_keys=True, allow_nan=False) + "\n"], force)
    except ReplayError as exc:
        _fail(exc)
    progress("judge", f"done ({metrics['decisions']} decisions, {metrics['labeled']} labeled)")
