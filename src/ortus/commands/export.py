"""`ortus export` — refresh the tracked beads export, scrubbed and atomic.

The tracker is the source of truth and `.beads/issues.jsonl` is a passive
export of it, so a clone that wants to record tracker state in git needs one
repeatable way to regenerate that file. This verb is it: the refresh goes
through the same boundary Ortus already owns, which removes the strings this
clone refuses to publish before the new bytes become the tracked file.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from ortus.core import output
from ortus.core.bd import PROTECTED_TERMS_FILE, BdClient


def _record_count(path: Path) -> int:
    """Records the refreshed export holds, or -1 when it cannot be read.

    Parsing each line is the cheapest honest count: a line the tracker wrote
    half-way through a record is not a record, and the caller would rather see
    the failure than a line tally that flatters a truncated file.
    """

    try:
        with path.open(encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip() and json.loads(line))
    except (OSError, ValueError):
        return -1


def export(
    repo: Path = typer.Argument(
        Path("."), help="Repository whose beads export should be refreshed."
    ),
) -> None:
    """Refresh `.beads/issues.jsonl` from the tracker, scrubbed of local terms."""
    target = repo.expanduser().resolve()
    if not (target / ".beads").is_dir():
        output.error(
            f"{target} has no .beads/ workspace",
            hint="run this inside a bd-tracked repository, or bootstrap one with ortus init",
        )
        raise typer.Exit(code=1)

    client = BdClient(repo=target)
    output.progress("export", "refreshing .beads/issues.jsonl from the tracker")
    if not client.supports_export():
        output.error(
            "this bd cannot regenerate its export on demand",
            hint="upgrade bd, or let its own hooks maintain .beads/issues.jsonl",
        )
        raise typer.Exit(code=1)
    reason = client.export_issues()
    if reason:
        output.error(f"could not refresh the export: {reason}")
        raise typer.Exit(code=1)

    export_path = target / ".beads" / "issues.jsonl"
    records = _record_count(export_path)
    if records < 0:
        output.error(f"the refreshed export at {export_path} is not readable JSONL")
        raise typer.Exit(code=1)
    # The term list itself is never printed: this verb exists to keep those
    # strings out of files, and a console line is one more place they would sit.
    output.success(f"{records} records exported to {export_path}")
    output.progress("export", f"done ({records} records, terms from {PROTECTED_TERMS_FILE})")
