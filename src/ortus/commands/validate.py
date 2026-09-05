"""`ortus validate <repo> [<id>...]` — the readiness verdict before a run.

Grind rejects an unready issue at claim time and prints why. An operator who
has just authored a bead should not have to launch a run to learn that. This
verb fetches the named issues (or every open issue when none is named), runs
the exact validator grind applies, and prints one row per issue: READY,
EXEMPT for an epic, or UNREADY followed by the same diagnostic grind logs.

It reports and never repairs: readiness rules, the schema, and grind's
claim-time enforcement are untouched, so the preview cannot drift from the
verdict at claim. Exit 0 means every row is ready or exempt, so
`ortus validate . && ortus grind .` gates a run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import typer

from ortus.core import output
from ortus.core.bd import BdClient, BdError
from ortus.core.readiness import READINESS_SCHEMA_VERSION, validate_issue
from ortus.core.repo import resolve_repo

STATUS_READY = "READY"
STATUS_EXEMPT = "EXEMPT"
STATUS_UNREADY = "UNREADY"
STATUS_ERROR = "ERROR"


@dataclass(frozen=True)
class IssueVerdict:
    """One issue's readiness outcome, rendered one line per issue."""

    issue_id: str
    status: str
    detail: str

    @property
    def ok(self) -> bool:
        return self.status in (STATUS_READY, STATUS_EXEMPT)

    def line(self) -> str:
        return f"{self.status} {self.detail}"

    def payload(self) -> dict[str, Any]:
        return {
            "id": self.issue_id,
            "status": self.status.lower(),
            "ok": self.ok,
            "detail": self.detail,
        }


def _make_bd(repo: Path) -> BdClient:
    """Indirection so tests can substitute a client."""
    return BdClient(repo)


def _bd_reason(exc: BdError) -> str:
    """bd's own last stderr line, which names the id it could not find."""
    lines = [line for line in exc.stderr.splitlines() if line.strip()]
    return lines[-1].strip() if lines else f"bd exited {exc.returncode}"


def _verdict_for(issue: dict[str, Any]) -> IssueVerdict:
    """Run the claim-time validator on one fetched issue.

    A payload malformed enough to make the validator raise is reported as
    unready with the exception text: the operator asked for a verdict, and a
    traceback is not one.
    """
    issue_id = str(issue.get("id") or "<missing-id>").strip()
    try:
        report = validate_issue(issue)
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        return IssueVerdict(issue_id, STATUS_UNREADY, f"{issue_id}: {exc}")
    if report.exempt:
        return IssueVerdict(report.issue_id, STATUS_EXEMPT, f"{report.issue_id} (epic)")
    if report.ready:
        return IssueVerdict(report.issue_id, STATUS_READY, report.issue_id)
    return IssueVerdict(report.issue_id, STATUS_UNREADY, report.diagnostic())


def _named_verdicts(bd: BdClient, issue_ids: list[str]) -> list[IssueVerdict]:
    """Verdicts for explicit ids, in the order given, duplicates collapsed.

    A missing id is one named error row; the rest are still validated so a
    typo in one argument does not hide the verdict on the others.
    """
    verdicts: list[IssueVerdict] = []
    for issue_id in dict.fromkeys(issue_ids):
        try:
            issue = bd.show(issue_id)
        except BdError as exc:
            verdicts.append(
                IssueVerdict(issue_id, STATUS_ERROR, f"{issue_id}: {_bd_reason(exc)}")
            )
            continue
        verdicts.append(_verdict_for(issue))
    return verdicts


def _queue_verdicts(bd: BdClient) -> list[IssueVerdict]:
    """Verdicts for every open issue: the pre-grind sweep.

    The listing is compact, so each issue's authoritative fields are loaded
    with `show`, exactly as plan does before it judges new issues.
    """
    ids = [
        str(item["id"])
        for item in bd.list_open()
        if isinstance(item, dict) and item.get("id")
    ]
    return _named_verdicts(bd, ids)


def _counts(verdicts: list[IssueVerdict]) -> str:
    tally = {
        status: sum(verdict.status == status for verdict in verdicts)
        for status in (STATUS_READY, STATUS_EXEMPT, STATUS_UNREADY, STATUS_ERROR)
    }
    return ", ".join(f"{count} {status.lower()}" for status, count in tally.items())


def validate(
    repo: Optional[Path] = typer.Argument(
        None, help="Target repo directory. Defaults to $PWD; no walk-up."
    ),
    issue_ids: Optional[list[str]] = typer.Argument(
        None,
        help="Issue ids to check. None: every open issue in the workspace.",
    ),
    as_json: bool = typer.Option(
        False, "--json", help="One JSON object on stdout instead of text rows."
    ),
) -> None:
    """Report whether issues satisfy readiness schema v1 before grinding."""
    target = resolve_repo(repo)
    bd = _make_bd(target)
    ids = list(issue_ids or [])
    output.progress(
        "validate",
        f"readiness schema {READINESS_SCHEMA_VERSION}: "
        + (f"{len(ids)} named issue(s)" if ids else "every open issue"),
    )
    try:
        verdicts = _named_verdicts(bd, ids) if ids else _queue_verdicts(bd)
    except BdError as exc:
        output.error(f"validate: could not list open issues: {_bd_reason(exc)}")
        raise typer.Exit(code=1)

    ok = all(verdict.ok for verdict in verdicts)
    # Rows go straight to stdout, unwrapped and unstyled: the verdict is the
    # verb's result and a long diagnostic must survive a pipe on one line.
    if as_json:
        typer.echo(
            json.dumps(
                {"ok": ok, "issues": [verdict.payload() for verdict in verdicts]},
                indent=2,
            )
        )
    elif not verdicts:
        typer.echo(f"nothing to validate: no open issues in {target}")
    else:
        for verdict in verdicts:
            typer.echo(verdict.line())

    if not verdicts:
        output.progress("validate", "done (nothing to validate)")
        return
    output.progress("validate", f"done ({_counts(verdicts)})")
    if not ok:
        raise typer.Exit(code=1)
