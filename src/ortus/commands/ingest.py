"""`ortus ingest <repo>` — file a bead that already passes readiness schema v1.

An agent that hand-assembles a `bd create` invocation has to get a multi-line
Markdown body through its own shell quoting, and the failures that produces are
silent: a truncated design section is a bead that reads fine and fails at claim
time. Ingest moves that assembly inside the CLI. The packet arrives as three
Markdown files (or one JSON object on stdin), the fields are assembled exactly
as the GitHub path assembles a drafted packet, and the readiness validator runs
on the candidate *before* anything is written.

Fail-closed is the whole point: an unready packet prints the same diagnostic
`ortus validate` prints and creates nothing, so there is never a half-formed
bead to find and delete afterwards. The operator repairs the packet and runs
ingest again. Packet files stay ephemeral transport; the bead is the record.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional

import typer

from ortus.core import output
from ortus.core.bd import BdClient, BdError
from ortus.core.github_bead import assemble_issue
from ortus.core.readiness import READINESS_SCHEMA_VERSION, validate_issue
from ortus.core.repo import resolve_repo

#: Issue field ← accepted packet file names, first match wins. The acceptance
#: field is spelled both ways in the wild, so both names are read rather than
#: making the author guess which one this build wants.
PACKET_FILES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("description", ("description.md",)),
    ("design", ("design.md",)),
    ("acceptance_criteria", ("acceptance.md", "acceptance_criteria.md")),
)

#: Stands in for the id the candidate does not have yet. The validator reports
#: failures against an id, and inventing a bd id — or creating a bead to get a
#: real one — would defeat the fail-closed contract.
CANDIDATE_ID = "<packet>"

STATUS_UNREADY = "UNREADY"


class PacketError(RuntimeError):
    """The packet could not be read. Raised before any bd write is attempted."""


def read_packet_dir(directory: Path) -> dict[str, Any]:
    """Read the three packet sections out of `directory`.

    Every required file must be present and carry text: a section missing here
    is a transport failure, and reporting it as one is more useful than letting
    the readiness validator report the same gap as a content failure.
    """
    if not directory.is_dir():
        raise PacketError(f"no packet directory at {directory}")
    draft: dict[str, Any] = {}
    for field, names in PACKET_FILES:
        for name in names:
            candidate = directory / name
            if candidate.is_file():
                text = candidate.read_text(encoding="utf-8").strip()
                if not text:
                    raise PacketError(f"{candidate} is empty")
                draft[field] = text
                break
        else:
            wanted = " or ".join(names)
            raise PacketError(f"packet {directory} has no {wanted}")
    return draft


def read_stdin_packet(text: str) -> dict[str, Any]:
    """Parse one JSON packet object from `text`."""
    stripped = text.strip()
    if not stripped:
        raise PacketError("--stdin was given but stdin was empty")
    try:
        value = json.loads(stripped)
    except ValueError as exc:
        raise PacketError(f"stdin is not JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise PacketError("stdin JSON must be an object with packet fields")
    return value


def load_packet(*, packet: Optional[Path], use_stdin: bool) -> dict[str, Any]:
    """Merge the named sources into one draft. Stdin wins on a shared field."""
    draft: dict[str, Any] = {}
    if packet is not None:
        draft.update(read_packet_dir(packet))
    if use_stdin:
        draft.update(read_stdin_packet(sys.stdin.read()))
    return draft


def _first_priority(*values: Any) -> int | None:
    """First value that reads as a bd priority, or None when none does.

    `0` is P0, the most urgent bead there is, so emptiness is tested rather
    than truthiness: a falsy-or-default reading here would quietly file the
    operator's P0 at P2.
    """
    for value in values:
        if value is None or value == "":
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None


def build_candidate(
    draft: dict[str, Any],
    *,
    title: Optional[str],
    issue_type: Optional[str],
    priority: Optional[int],
) -> dict[str, Any]:
    """Assemble the dict `validate_issue` reads, flags overriding the packet.

    A flag the operator typed is more current than a field baked into the
    packet, so it wins; an omitted flag leaves `assemble_issue` to apply the
    same `task`/`P2` defaults `BdClient.create` would have applied.
    """
    merged = dict(draft)
    if title:
        merged["title"] = title
    if issue_type:
        merged["issue_type"] = issue_type
    candidate = assemble_issue(merged, title_fallback="", draft_id=CANDIDATE_ID)
    resolved = _first_priority(priority, draft.get("priority"))
    if resolved is not None:
        candidate["priority"] = resolved
    return candidate


def _make_bd(repo: Path) -> BdClient:
    """Indirection so tests can substitute a client."""
    return BdClient(repo)


def _bd_reason(exc: BdError) -> str:
    """bd's own last stderr line, which names what it refused."""
    lines = [line for line in exc.stderr.splitlines() if line.strip()]
    return lines[-1].strip() if lines else f"bd exited {exc.returncode}"


def ingest(
    repo: Optional[Path] = typer.Argument(
        None, help="Target repo directory. Defaults to $PWD; no walk-up."
    ),
    packet: Optional[Path] = typer.Option(
        None,
        "--packet",
        help="Packet directory holding description.md, design.md, and acceptance.md.",
    ),
    use_stdin: bool = typer.Option(
        False,
        "--stdin",
        help="Read one JSON packet object from stdin; overrides --packet fields.",
    ),
    title: Optional[str] = typer.Option(
        None, "--title", help="Issue title. Required unless the packet carries one."
    ),
    issue_type: Optional[str] = typer.Option(
        None, "--type", help="bd issue type. Default: task. Epics are refused."
    ),
    priority: Optional[int] = typer.Option(
        None, "--priority", help="bd priority 0-4. Default: 2."
    ),
) -> None:
    """File one readiness schema v1 bead from a packet; unready creates nothing."""
    target = resolve_repo(repo)
    if packet is None and not use_stdin:
        output.error(
            "ingest: name a packet source",
            hint="pass --packet <dir>, or --stdin with a JSON packet object",
        )
        raise typer.Exit(code=2)

    sources = ", ".join(
        source
        for source in (str(packet) if packet is not None else "", "stdin" if use_stdin else "")
        if source
    )
    output.progress("ingest", f"reading packet from {sources}")
    try:
        draft = load_packet(packet=packet, use_stdin=use_stdin)
    except PacketError as exc:
        output.error(f"ingest: {exc}")
        raise typer.Exit(code=1)

    candidate = build_candidate(
        draft, title=title, issue_type=issue_type, priority=priority
    )
    if not candidate["title"]:
        output.error(
            "ingest: the packet carries no title",
            hint="pass --title, or add a title field to the JSON packet",
        )
        raise typer.Exit(code=1)
    if candidate["issue_type"].strip().lower() == "epic":
        # An epic is exempt from readiness because it is a container for leaves
        # that carry their own work specs. Ingest files work specs, so honouring
        # the exemption here would file exactly the unready bead it exists to
        # prevent.
        output.error(
            "ingest: epics are containers and carry no work spec",
            hint="file the leaf with --type task, bug, feature, or chore",
        )
        raise typer.Exit(code=1)

    output.progress(
        "ingest", f"validating {candidate['title']!r} against readiness schema {READINESS_SCHEMA_VERSION}"
    )
    report = validate_issue(candidate)
    if not report.ready:
        # Same row shape `ortus validate` prints, on stdout, so the repair loop
        # reads one diagnostic vocabulary whichever verb reported the gap.
        typer.echo(f"{STATUS_UNREADY} {report.diagnostic()}")
        output.progress("ingest", f"done (not created: {report.summary()})")
        raise typer.Exit(code=1)

    try:
        issue_id = _make_bd(target).create(
            title=candidate["title"],
            issue_type=candidate["issue_type"],
            priority=int(candidate["priority"]),
            description=candidate["description"],
            design=candidate["design"],
            acceptance=candidate["acceptance_criteria"],
        )
    except BdError as exc:
        output.error(f"ingest: bd refused the create: {_bd_reason(exc)}")
        raise typer.Exit(code=1)

    # The id is the verb's result: bare on stdout so `id=$(ortus ingest ...)`
    # captures it without parsing.
    typer.echo(issue_id)
    output.progress("ingest", f"done (created {issue_id})")
