"""ortus retro — propose lessons and issues from what recent runs recorded.

A bounded, advisory retrospective over the run records a grind leaves behind:
candidate journals, verification reports, and run logs. It proposes only —
everything it records lands in the same pending state a worker's lesson
proposal uses, and `ortus curate` remains the one review step that accepts,
edits, or rejects. It is deliberately an operator verb rather than part of a
grind iteration: it takes no grind lock, and a retrospective competing with a
worker for the model would be a cost during exactly the wrong minute.
"""

from __future__ import annotations

import datetime as _dt
import shutil
from pathlib import Path
from typing import Optional

import typer

from ortus.core import output
from ortus.core.agent import BackendError, resolve_backend
from ortus.core.bd import BdClient, BdError
from ortus.core.compose import with_default_model
from ortus.core.config import load_config
from ortus.core.profiles import AgentProfile, Phase, ProfileError
from ortus.core.repo import resolve_repo
from ortus.core.retro import MAX_RECORDS, RetroFailed, run_retrospective


def retro(
    repo: Optional[Path] = typer.Argument(
        None, help="Target repo directory. Defaults to $PWD; no walk-up."
    ),
    window: int = typer.Option(
        MAX_RECORDS,
        "--window",
        help="How many recent run records (journals, reports, logs) to read.",
    ),
    model: Optional[str] = typer.Option(
        None, "--model", help="Override the model for the pass."
    ),
    backend_option: Optional[str] = typer.Option(
        None,
        "--backend",
        help="Agent backend (claude|codex); overrides ORTUS_BACKEND and .ortusrc.",
    ),
    timeout: float = typer.Option(
        900.0, "--timeout", help="Hard cap (secs) on the single model pass."
    ),
) -> None:
    """Read recent run records and propose lessons and issues, pending curation."""
    target = resolve_repo(repo)
    try:
        backend = resolve_backend(backend_option, repo=target)
        # Summarization over supplied material, not correctness reasoning —
        # the same rationale as the commit-message pass, so it shares that
        # pass's profile phase and cheap default model.
        profile: AgentProfile | None = with_default_model(
            load_config(repo=target).resolve_profile(
                backend, Phase.FINALIZE, model=model
            )
        )
    except (BackendError, ProfileError) as exc:
        output.error(str(exc).splitlines()[0])
        raise typer.Exit(code=1)
    if shutil.which(backend) is None:
        # No model to run the pass with: report and exit cleanly rather than
        # fail — a retrospective is advisory and never worth an error state.
        profile = None

    log = target / "logs" / (
        "retro-" + _dt.datetime.now().strftime("%Y%m%d-%H%M%S") + ".log"
    )
    try:
        result = run_retrospective(
            target,
            bd=BdClient(target),
            today=_dt.date.today().isoformat(),
            log_path=log,
            backend=backend,
            profile=profile,
            timeout=timeout,
            limit=window,
        )
    except RetroFailed as exc:
        output.error(f"retrospective failed: {exc}")
        raise typer.Exit(code=1)
    except BdError as exc:
        output.error(str(exc).splitlines()[0])
        raise typer.Exit(code=1)

    for note in result.skipped:
        output.info(f"skipped record: {note}")
    if result.message:
        output.info(result.message)
        return
    output.info(f"read {len(result.records)} run record(s)")
    for note in result.notes:
        output.info(f"note: {note}")
    for proposal in result.recorded:
        output.success(
            f"proposed {proposal.kind} {proposal.pending_key!r}; "
            "pending until curated"
        )
    for proposal in result.duplicates:
        output.info(
            f"proposed {proposal.kind} {proposal.pending_key!r} is already "
            "covered by an accepted lesson"
        )
    if not result.recorded:
        if not result.duplicates:
            output.info("the records propose nothing; a clean window is a fine answer")
        return
    output.info(
        "review with `ortus curate`: accept (optionally edited) or reject; "
        "nothing proposed here is active until accepted"
    )
