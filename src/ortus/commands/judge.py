"""Export private event records and replay operator labels without provider calls."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Iterable

import typer

from ortus.core.judge_replay import ReplayError, join_events, read_events, read_labels, summarize
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
def replay(
    input: Path = typer.Argument(..., help="Version-1 judge event JSONL."),
    labels: Path = typer.Option(..., "--labels", help="JSON object keyed by decision_id."),
    output: Path = typer.Option(..., "--output", help="Destination metrics JSON file."),
    force: bool = typer.Option(False, "--force", help="Replace an existing output file."),
) -> None:
    """Calculate accuracy, latency, coverage and measured costs without rerunning the judge.

    Labels contain expected_action (proceed, human or skip), needs_human (boolean),
    and optional worker_cost_usd. Shadow accuracy uses the intended action only
    when the observed issue matches the actual worker. Percentiles use nearest
    rank. Costs remain null when no measurement is available.
    """
    progress("judge", "reading events and labels (large logs may take 1-3 min)")
    try:
        if output.resolve() in (input.resolve(), labels.resolve()):
            raise ReplayError("output must differ from inputs")
        data = _read(input)
        metrics = summarize(data, read_labels(labels))
        progress("judge", "writing replay metrics")
        _write_atomic(output, [json.dumps(metrics, indent=2, sort_keys=True, allow_nan=False) + "\n"], force)
    except ReplayError as exc:
        _fail(exc)
    progress("judge", f"done ({metrics['decisions']} decisions, {metrics['labeled']} labeled)")
