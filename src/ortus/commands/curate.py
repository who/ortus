"""ortus curate — review worker-proposed lessons before they reach the crew.

A worker may propose a lesson in its completion comment; grind records it in
the tracker in a pending state. This verb is the review step between the two:
it lists what is pending and lets the operator accept a proposal (verbatim or
edited) or reject it. Only accepted lessons are injected into later workers —
there is deliberately no automatic accept, because every accepted lesson
costs every future worker context.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from ortus.core import output
from ortus.core.bd import BdClient, BdError
from ortus.core.repo import resolve_repo


def curate(
    repo: Optional[Path] = typer.Argument(
        None, help="Target repo directory. Defaults to $PWD; no walk-up."
    ),
    accept: Optional[str] = typer.Option(
        None,
        "--accept",
        metavar="KEY",
        help="Accept the pending proposal KEY as a crew lesson.",
    ),
    reject: Optional[str] = typer.Option(
        None,
        "--reject",
        metavar="KEY",
        help="Reject the pending proposal KEY and delete it.",
    ),
    text: Optional[str] = typer.Option(
        None,
        "--text",
        help="Replacement lesson text when accepting — edit, then accept.",
    ),
) -> None:
    """Review pending lesson proposals: list, accept (optionally edited), reject."""
    if accept and reject:
        output.error("--accept and --reject are one decision each; pass only one")
        raise typer.Exit(code=2)
    if text is not None and not accept:
        output.error("--text edits an accepted proposal; it requires --accept KEY")
        raise typer.Exit(code=2)

    target = resolve_repo(repo)
    client = BdClient(target)

    try:
        if accept:
            if not client.accept_proposal(accept, text):
                output.error(
                    f"no pending proposal {accept!r}",
                    hint="run `ortus curate` to list what is pending",
                )
                raise typer.Exit(code=1)
            output.success(
                f"accepted {accept!r}"
                + (" with edited text" if text is not None else "")
                + "; later workers may now receive it"
            )
            return
        if reject:
            if not client.reject_proposal(reject):
                output.error(
                    f"no pending proposal {reject!r}",
                    hint="run `ortus curate` to list what is pending",
                )
                raise typer.Exit(code=1)
            output.success(f"rejected {reject!r}; the proposal is deleted")
            return

        pending = client.pending_proposals()
        if not pending:
            output.info("no pending lesson proposals")
            return
        output.info(f"{len(pending)} pending lesson proposal(s):")
        for key, body in pending.items():
            output.info(f"  {key}: {body}")
        output.info(
            "accept with `ortus curate --accept KEY` (add --text to edit), "
            "reject with `ortus curate --reject KEY`"
        )
    except BdError as exc:
        output.error(str(exc).splitlines()[0])
        raise typer.Exit(code=1)
