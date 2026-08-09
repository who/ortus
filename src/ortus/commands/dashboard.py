"""ortus dashboard — one read-only live view of a grind run (ortus-0udo.2).

The dashboard watches a repository while a worker owns it. A grind computes its
candidate as the dirty set minus the baseline captured when it claimed the
issue, so anything written into the tree after that claim silently becomes the
worker's work and is judged by its verifier. An observer that writes therefore
corrupts the thing it observes — during one session an operator assistant
edited two files while a worker held the tree and had to unpick its own hunks
an hour later. This module is built so that it cannot: run state comes from
`ortus.core.runstate`, which is a pure function of two files, and every bd
invocation goes through `bd_argv`, which pins `--readonly --sandbox` (the same
flags `ortus check` uses for its own memory query). `tests/test_dashboard.py`
asserts both properties rather than trusting the convention, because a
convention is exactly what a later panel would forget.

This leaf is the shell: the verb, the layout, and the refresh loop. The five
regions it names — header, current action, candidate, verdict, warnings — are
filled by their own leaves and carry placeholders here. What the shell does own
is the pulse: one element that advances on every tick, so a healthy run looks
alive and a stalled one is obvious by its stillness. An operator once watched a
silent tail for hours unable to tell progress from death.

The verdict region is ortus-0udo.6. Verdicts are the most informative artifact a
run produces and the least visible: in one session a verifier passed every
criterion and then ended without emitting its envelope, so a sound candidate was
rejected and the operator learned only from a terse error after the run had
exited. The region therefore lists the criteria the run is being judged on
before any verdict exists, so four outstanding of seven is a visible fact, and
shows the decision as pending until an envelope appears, because a missing
envelope is itself the failure mode that cost that run. Expectation comes from
the authoritative issue packet the journal names — read off disk rather than
re-queried, which would answer for the issue as it is now rather than as it was
claimed — and is derived exactly as `ortus.commands.grind` derives the ids it
holds the verdict to, so the panel and the validator cannot drift apart.
Per-criterion status comes from the verifier report artifact, which the journal
attributes to an attempt by name (`<candidate>.verifier-<attempt>.md`), so the
latest report is the last reference rather than a merge of several. Nothing here
re-runs a criterion check or judges whether the verdict is right.

The candidate region is ortus-0udo.5. It answers what the run will commit if it
passes and what it is deliberately leaving alone, which was invisible through
several failures in one session: a worker inherited seven paths belonging to its
own issue and disowned them, which would have committed five and silently
abandoned seven, and separately a verifier was rejected for drift because
disowned paths were counted as unexplained. Both facts were already in the
journal and nobody was looking. Disowned paths are therefore shown beside owned
ones rather than hidden, because the question they answer is whether the worker
judged ownership correctly. Counts lead and paths follow, and every non-empty
group keeps a floor of rows before any group takes the remainder, so a large
candidate stays inside a fixed region without reducing a disowned set to a bare
count. The base commit is shown because a candidate is only meaningful against
the tree it was captured on, and a moved base is a failure the transaction
already checks for. Nothing here renders a diff, which is git's job, and nothing
here can disown a path, because the view is read-only.

The warnings region and replay are ortus-0udo.7. Three conditions ended runs
repeatedly in one session — a watchdog killing a worker mid-flight, correction
attempts exhausting into a human label, and a verifier unable to execute any
command — and each was diagnosed only afterwards by reading logs by hand. The
region therefore shows each warning with the ortus line that produced it rather
than a bare count: the stopgap script that reported seven phantom timeouts is
why a count alone is not something an operator can act on, and the evidence is
what lets them judge the claim. Which lines count is decided in
`ortus.core.runstate`, so live and replay cannot disagree about the vocabulary.

Replay (`--replay <log>`) points the same panels at a finished run and resolves
that run's journal from the log's own directory, so a run explains itself the
same way whether it is live or finished and there is one renderer to keep
correct. It is a flag on this verb rather than a verb of its own because only
the source differs.

Refresh is a timer over the incremental snapshot rather than a filesystem
watcher: a watcher on a repo under active test churn wakes constantly for paths
nothing here displays. Colour carries meaning rather than decoration — one
accent for healthy motion, one for attention, one for failure, dim for
everything static — against a dark ground, which is the default because the
palette is chosen for contrast and a light terminal would wash the accents out.
There is no emoji anywhere in the interface: glyph support is uneven across
terminal fonts, and a coloured rule or a filled bar reads faster than a
pictograph.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Optional

import typer

from ortus.core import output
from ortus.core.readiness import validate_issue
from ortus.core.runstate import TERMINAL_PHASES, LogEvent, RunSnapshot, read_snapshot
from ortus.core.transaction import JournalStore

#: Seconds between refreshes. A tick reads only the bytes the log grew by, so
#: this is cheap even against the megabyte logs a long session produces.
REFRESH_SECONDS = 1.0

#: Flags every bd invocation carries: `--readonly` keeps bd off its write paths
#: and `--sandbox` off its auto-sync ones. `check.py` already queries bd this
#: way, so this is the established read-only shape rather than a new one.
BD_READONLY_FLAGS: tuple[str, ...] = ("--readonly", "--sandbox")
BD_TIMEOUT_SECONDS = 15.0

#: Width of the pulse bar, in cells.
PULSE_WIDTH = 24
_PULSE_MARK = "█"  # full block
_PULSE_RULE = "─"  # box-drawing horizontal

#: What a region shows before its own leaf fills it.
PLACEHOLDER = "panel pending"

#: How many evidence lines the warnings region shows at once. Warnings
#: accumulate for the life of a run, and the newest are the ones being
#: diagnosed; anything older is counted in the summary and named as elided
#: rather than dropped silently.
WARNING_EVIDENCE_LINES = 6
#: Longest evidence line rendered. An ortus line is not clipped by the model,
#: and one long line must not push the rest of the region off screen.
WARNING_TEXT_CHARS = 120
#: The warnings region when the run has produced none. Stated as a finding
#: rather than left blank, so a quiet region reads as "nothing fired" instead
#: of "this panel is broken".
NO_WARNINGS = "none - no ortus warning line in this run"

#: How the candidate region names its three parts. Disowned work is labelled
#: rather than dropped: an operator has to be able to see that a path was
#: deliberately excluded, which is the judgement that failed once already.
OWNED = "owned"
INHERITED = "inherited"
DISOWNED = "disowned"
#: The candidate region with no journal at all. Nothing was ever captured, which
#: is a different fact from a candidate holding nothing, and the region says
#: which rather than rendering an empty one.
CANDIDATE_IDLE = "idle - no candidate captured"
#: A journal in flight whose candidate is still empty: nothing has gone dirty
#: since the claim. Stated rather than left blank, so the region reads as a
#: finding instead of a broken panel.
CANDIDATE_EMPTY = "no paths captured yet"
#: Path rows the region shows across all three groups. A candidate larger than
#: this is truncated with a count rather than scrolling the layout.
CANDIDATE_PATH_ROWS = 18
#: Rows each non-empty group keeps before any group takes the remainder, so a
#: large owned set cannot render the disowned one as a bare count.
CANDIDATE_GROUP_ROWS = 3
#: Longest path rendered on one row.
CANDIDATE_PATH_CHARS = 100

#: The structured log event grind appends once a verdict is decided, rejected
#: included. Its absence is what the verdict region reports as pending.
VERDICT_EVENT_KIND = "ortus.verdict"
#: Criterion identifiers, matched exactly as grind matches them when it builds
#: the expected set the verdict validator enforces.
CRITERION_ID = re.compile(r"\bAC-\d+\b")
#: Status vocabulary. The first three are what a verifier report records; the
#: fourth is this panel's own word for a criterion no report has covered yet,
#: which is the state an operator watching a live run mostly sees.
STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_NOT_ASSESSED = "not assessed"
NOT_REACHED = "not reached"

#: The verdict region when there is no run to judge at all.
VERDICT_IDLE = "idle - no run to judge"
#: The decision line before any envelope exists. A verifier that reviews a whole
#: candidate and then ends without emitting one is a real failure mode, and it
#: costs a run, so it is named while it is happening rather than afterwards.
VERDICT_PENDING = "decision pending - no verdict envelope emitted yet"
#: No verifier report has been written for this run yet, so every criterion is
#: outstanding rather than failing.
NO_REPORT = "no verifier report yet"
#: The packet the criteria would come from is not on disk.
NO_PACKET = "issue packet unavailable - criteria cannot be listed"

#: How many criterion rows the region shows. A criterion is never split across
#: the boundary: a report too large to display is summarised by dropping whole
#: rows and saying how many, never clipped mid-criterion.
CRITERION_ROWS = 24
#: Longest evidence excerpt per criterion row.
CRITERION_TEXT_CHARS = 88
#: Widths that keep the matrix aligned as a matrix.
_ID_WIDTH = 8
_STATUS_WIDTH = 12
#: Most of a verifier report this panel will read. Reports are bounded at
#: 24_000 characters before the CodeGraph block is appended, so this reads a
#: whole one and still cannot be made to swallow a runaway file.
REPORT_READ_CHARS = 64_000

_REPORT_CRITERIA_HEADING = "### Acceptance criteria"
_REPORT_SECTION = re.compile(r"^#{2,4}\s")
_REPORT_ROW = re.compile(
    r"^-\s+(?P<id>[A-Za-z][\w.-]*):\s+(?P<status>pass|fail|not assessed)\b"
    r"\s*[—–-]?\s*(?P<evidence>.*)$",
    re.IGNORECASE,
)
_REPORT_ELIDED = re.compile(r"^-\s+\[(?P<count>\d+) more entries truncated")
_REPORT_DECISION = re.compile(r"^Decision:\s+\*\*(?P<decision>[A-Za-z]+)\*\*")
_REPORT_CANDIDATE = re.compile(r"^Candidate:\s+`(?P<hash>[^`]*)`")
_REPORT_ATTEMPT = re.compile(r"^Verifier attempt:\s+(?P<attempt>\d+)")
#: The store names a report by the candidate it judged and the attempt that
#: produced it, so a report is attributable from its filename and the journal
#: alone and no verdict can be shown against the wrong attempt.
_REF_ATTEMPT = re.compile(r"\.verifier-(?P<attempt>\d+)\.md$")
_PACKET_GLOB = "{issue}-*.issue.json"

#: Terminal phases the model names, in the words an operator would use. A phase
#: absent from this map is rendered verbatim: a log from an older run whose
#: vocabulary has since changed must render what it can rather than raise.
OUTCOMES: dict[str, str] = {
    "corrections-exhausted": "correction attempts exhausted",
    "correction-rejected": "correction rejected",
    "plan-gap-escalated": "escalated as a plan gap",
    "orphaned-candidate": "candidate left orphaned",
    "incomplete-candidate": "candidate left incomplete",
}
#: A `finalized-*` phase is the run reaching a finalization boundary, which is
#: the only clean ending there is. Replay must say so rather than invent a
#: failure for a run that simply finished.
CLEAN_OUTCOME = "finished cleanly"
#: A replayed run whose journal is gone: the log alone is still worth reading.
NO_JOURNAL_OUTCOME = "no journal - replayed from the log alone"

KEY_HINT = "q  quit     read-only: never writes the repository, bd, or git"


@dataclass(frozen=True)
class RegionSpec:
    """One named region of the layout: its widget id and its border title."""

    key: str
    title: str


#: The agreed layout, in render order. Each region is filled by its own leaf.
REGIONS: tuple[RegionSpec, ...] = (
    RegionSpec("header", "run"),
    RegionSpec("current-action", "current action"),
    RegionSpec("candidate", "candidate"),
    RegionSpec("verdict", "verdict"),
    RegionSpec("warnings", "warnings"),
)

# --- palette ---------------------------------------------------------------
#
# Dark ground; colour used to carry meaning. Hex literals rather than theme
# variables so the contract the tests assert is the contract that renders.

GROUND = "#0b0f14"
PANEL = "#111820"
TEXT_STATIC = "#9aa7b4"
TEXT_DIM = "#5c6773"
ACCENT_MOTION = "#3ddc97"
ACCENT_ATTENTION = "#e2b53d"
ACCENT_FAILURE = "#ef5f5f"

#: Themes ship with Textual; this one is dark, which is the default posture.
DARK_THEME = "textual-dark"

#: State classes a region may carry. Exactly one is applied at a time.
STATE_CLASSES = ("state-idle", "state-live", "state-ended", "state-failed")

#: The message an operator sees when the terminal framework is not importable.
#: Their environment predates the dependency — a `uv tool` install, a container
#: image, a colleague's checkout — and what they need is the one command that
#: repairs it, not the stack that names a missing module and nothing else.
MISSING_TEXTUAL = (
    "ortus dashboard needs the textual package, which is not importable here. "
    "Install it with `pip install textual`, or reinstall ortus itself "
    "(`uv tool install --force ortus`), then run the command again. "
    "Every other ortus verb works without it."
)


class TextualUnavailable(RuntimeError):
    """textual is absent, or present but missing one of the submodules used."""


@dataclass(frozen=True)
class Frame:
    """Everything the shell renders at one tick."""

    header: str
    current_action: str
    candidate: str
    verdict: str
    warnings: str
    pulse: str
    #: Region key to state class, so colour tracks the run rather than layout.
    states: tuple[tuple[str, str], ...] = ()

    def bodies(self) -> dict[str, str]:
        """Region key to the text that region shows."""

        return {
            "header": self.header,
            "current-action": self.current_action,
            "candidate": self.candidate,
            "verdict": self.verdict,
            "warnings": self.warnings,
        }

    def texts(self) -> tuple[str, ...]:
        """Every string this frame puts on screen."""

        return (*self.bodies().values(), self.pulse)


def bd_argv(*args: str) -> list[str]:
    """The argument vector for a bd query, read-only flags pinned in front.

    Every bd call the dashboard makes is built here, so the read-only posture
    is one line a test can assert rather than a rule each panel remembers.
    """

    return ["bd", *BD_READONLY_FLAGS, *args]


def run_bd(repo: Path, *args: str, timeout: float = BD_TIMEOUT_SECONDS) -> str | None:
    """Run one read-only bd query in `repo`; None when it does not answer.

    A failed query degrades the panel that asked for it and never the view: bd
    being unavailable is not a reason to blank a screen whose other half is
    read straight off disk.
    """

    try:
        proc = subprocess.run(
            bd_argv(*args),
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def pulse(tick: int, *, width: int = PULSE_WIDTH) -> str:
    """A scanner that advances one cell per tick.

    Position comes from the tick rather than from the clock, so the bar moves
    on every refresh even when the run itself is producing nothing. Stillness
    on screen then means the dashboard has stopped, which is a fact worth being
    able to see.
    """

    span = max(1, width)
    cycle = max(1, 2 * span - 2)
    step = tick % cycle
    index = step if step < span else cycle - step
    return "".join(_PULSE_MARK if i == index else _PULSE_RULE for i in range(span))


def pulse_line(snapshot: RunSnapshot, tick: int, *, width: int = PULSE_WIDTH) -> str:
    """The moving strip: the scanner, the refresh count, the observation time."""

    observed = snapshot.observed_at
    stamp = observed.strftime("%H:%M:%S") if observed is not None else "--:--:--"
    return f"{pulse(tick, width=width)}  refresh {tick}  {stamp}"


@dataclass(frozen=True)
class ReplaySource:
    """A finished run to render: its log, and the repository holding its journal."""

    log_path: Path
    repo: Path


def resolve_replay(log: Path) -> ReplaySource:
    """Pair a grind log with the journal that sits alongside it.

    Grind writes both under one `logs/` tree, so the repository is the log's
    grandparent and the journal is found by the same store live mode uses. That
    is the whole resolution: it means replaying a log copied out of a run whose
    journal is gone still renders, from the log alone.
    """

    log = Path(log)
    parent = log.parent
    repo = parent.parent if parent.name == "logs" else parent
    return ReplaySource(log_path=log, repo=repo)


def clip(text: str, limit: int = WARNING_TEXT_CHARS) -> str:
    """One flattened line, bounded so a long entry cannot own the region."""

    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "..."


def stamp_of(moment: _dt.datetime | None) -> str:
    """Wall-clock time of an event, or a blank of the same width when unknown."""

    return moment.strftime("%H:%M:%S") if moment is not None else "--:--:--"


def warning_summary(snapshot: RunSnapshot) -> str:
    """The counts line: every warning kind seen, with how many times.

    Kinds are ordered by name so the line does not reshuffle between refreshes
    and an operator can read a changing number rather than a changing layout.
    """

    counts = snapshot.warning_counts
    if not counts:
        return ""
    return "   ".join(f"{kind} {count}" for kind, count in sorted(counts.items()))


def warning_evidence(
    snapshot: RunSnapshot, *, limit: int = WARNING_EVIDENCE_LINES
) -> tuple[str, ...]:
    """The ortus line behind each recent warning, oldest of the window first."""

    return tuple(
        f"{stamp_of(warning.at)}  {warning.kind:<10} {clip(warning.text)}"
        for warning in snapshot.warnings[-limit:]
    )


def warnings_panel(
    snapshot: RunSnapshot, *, limit: int = WARNING_EVIDENCE_LINES
) -> str:
    """The warnings region: what fired, how often, and the line that said so.

    A bare count is what made the stopgap script untrustworthy — it claimed
    seven timeouts that never happened — so every count here is backed by the
    ortus line an operator can judge it by. Which lines are warnings at all is
    decided by the model, so live and replay share one vocabulary.
    """

    if not snapshot.warnings:
        return NO_WARNINGS
    elided = len(snapshot.warnings) - limit
    lines = [warning_summary(snapshot)]
    if elided > 0:
        lines.append(f"({elided} earlier not shown)")
    lines.extend(warning_evidence(snapshot, limit=limit))
    return "\n".join(lines)


# --- verdict ---------------------------------------------------------------


@dataclass(frozen=True)
class Criterion:
    """One acceptance criterion and what is known about it."""

    id: str
    status: str
    evidence: str = ""
    #: False for an identifier a verdict reported that the packet never named.
    #: Such a row is still rendered: dropping either set would hide a mismatch
    #: the verdict validator already treats as a real condition.
    in_packet: bool = True


@dataclass(frozen=True)
class Envelope:
    """The verdict envelope grind logged once the decision was made."""

    decision: str
    candidate_hash: str = ""
    reason: str = ""
    at: _dt.datetime | None = None


@dataclass(frozen=True)
class VerifierReport:
    """One verifier report artifact, parsed down to its criterion matrix."""

    ref: str
    attempt: int = 0
    candidate_hash: str = ""
    decision: str = ""
    criteria: tuple[Criterion, ...] = ()
    #: Criteria the report itself said it had truncated. Counted rather than
    #: ignored, so a summarised report reads as summarised.
    elided: int = 0
    unreadable: bool = False


@dataclass(frozen=True)
class VerdictState:
    """What the verdict region knows at one tick.

    Carried between ticks because the log tail is incremental: an envelope is
    logged once, and a region that only ever saw the newest slice would forget
    the decision on the very next refresh.
    """

    criteria: tuple[str, ...] = ()
    #: Why the criteria could not be listed, in readiness's own words.
    readiness: str = ""
    report: VerifierReport | None = None
    envelope: Envelope | None = None
    log_path: Path | None = None
    candidate_hash: str = ""


def packet_path(repo: Path, snapshot: RunSnapshot) -> Path | None:
    """The authoritative issue packet artifact for this run, if it is on disk.

    The journal names it exactly. The glob is the fallback for a journal
    written before that reference was recorded: the store prefixes a packet
    with its issue, so the newest one for this issue is the run's own.
    """

    if snapshot.issue_packet_ref:
        named = repo / snapshot.issue_packet_ref
        if named.is_file():
            return named
    if not snapshot.issue_id:
        return None
    pattern = _PACKET_GLOB.format(issue=snapshot.issue_id)
    try:
        found = [
            path
            for path in JournalStore(repo).artifacts.glob(pattern)
            if path.is_file()
        ]
    except OSError:
        return None
    return max(found, key=_mtime) if found else None


def read_packet(path: Path) -> dict[str, Any] | None:
    """The packet artifact as a mapping; None when it cannot be read as one."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def packet_criteria(packet: dict[str, Any]) -> tuple[str, ...]:
    """The criterion identifiers this run is judged on, in packet order.

    Derived exactly as grind derives the expected set it hands the verdict
    validator, so what the panel says is outstanding and what the validator
    would call missing are the same question answered once.
    """

    field = str(packet.get("acceptance_criteria", ""))
    return tuple(dict.fromkeys(CRITERION_ID.findall(field)))


def criteria_defect(packet: dict[str, Any]) -> str:
    """Why a packet names no criterion, in readiness's words.

    A packet with no identifiers is a readiness failure rather than a verdict
    with nothing in it, and the region says which so the operator knows the
    issue is the defect and not the run.
    """

    for failure in validate_issue(packet).failures:
        if failure.code == "observable_criteria":
            return f"readiness failure - {failure.section}: {failure.message}"
    return "readiness failure - the issue packet names no criterion identifier"


def parse_report(text: str, *, ref: str = "") -> VerifierReport:
    """Parse a verifier report down to its decision and criterion matrix.

    Only the matrix is read. A report runs to kilobytes of commands, findings
    and CodeGraph evidence that belong in the artifact rather than on a panel
    refreshing once a second.
    """

    attempt = 0
    match = _REF_ATTEMPT.search(ref)
    if match is not None:
        attempt = int(match.group("attempt"))
    decision = ""
    candidate_hash = ""
    criteria: list[Criterion] = []
    elided = 0
    in_matrix = False
    for line in text.splitlines():
        stripped = line.strip()
        if in_matrix:
            if _REPORT_SECTION.match(stripped):
                break
            row = _REPORT_ROW.match(stripped)
            if row is not None:
                criteria.append(
                    Criterion(
                        id=row.group("id"),
                        status=row.group("status").lower(),
                        evidence=row.group("evidence").strip(),
                    )
                )
                continue
            dropped = _REPORT_ELIDED.match(stripped)
            if dropped is not None:
                elided += int(dropped.group("count"))
            continue
        if stripped == _REPORT_CRITERIA_HEADING:
            in_matrix = True
            continue
        found = _REPORT_DECISION.match(stripped)
        if found is not None:
            decision = found.group("decision").lower()
            continue
        found = _REPORT_CANDIDATE.match(stripped)
        if found is not None:
            candidate_hash = found.group("hash")
            continue
        found = _REPORT_ATTEMPT.match(stripped)
        if found is not None and not attempt:
            attempt = int(found.group("attempt"))
    return VerifierReport(
        ref=ref,
        attempt=attempt,
        candidate_hash=candidate_hash,
        decision=decision,
        criteria=tuple(criteria),
        elided=elided,
    )


def read_report(repo: Path, snapshot: RunSnapshot) -> VerifierReport | None:
    """The latest verifier report artifact, or None when none exists yet.

    Latest rather than merged: a correction round writes a second report for
    the same issue, and the journal appends each reference as it is written, so
    the last one is the attempt now in force.
    """

    ref = snapshot.verifier_refs[-1] if snapshot.verifier_refs else ""
    if not ref:
        return None
    try:
        with (repo / ref).open(encoding="utf-8", errors="replace") as handle:
            text = handle.read(REPORT_READ_CHARS)
    except OSError:
        return VerifierReport(ref=ref, unreadable=True)
    return parse_report(text, ref=ref)


def scan_envelope(events: tuple[LogEvent, ...]) -> Envelope | None:
    """The newest verdict envelope in one slice of the log, if it holds one."""

    latest: Envelope | None = None
    for event in events:
        payload = event.payload
        if event.kind != VERDICT_EVENT_KIND or not isinstance(payload, dict):
            continue
        latest = Envelope(
            decision=str(payload.get("decision", "") or "unknown"),
            candidate_hash=str(payload.get("candidate_hash", "") or ""),
            reason=str(payload.get("reason", "") or ""),
            at=event.at,
        )
    return latest


def read_verdict(
    repo: Path, snapshot: RunSnapshot, previous: VerdictState | None = None
) -> VerdictState:
    """Everything the verdict region needs, read from disk. Writes nothing.

    Kept out of `frame`, which stays a pure function of what it is given, so
    the renderer is testable without a repository and the reads happen once per
    tick rather than once per region.
    """

    repo = Path(repo)
    carried = (
        previous.envelope
        if previous is not None and previous.log_path == snapshot.log_path
        else None
    )
    envelope = scan_envelope(snapshot.events) or carried

    path = packet_path(repo, snapshot)
    packet = read_packet(path) if path is not None else None
    criteria = packet_criteria(packet) if packet is not None else ()
    # Only a packet that is present and names nothing is a readiness failure. A
    # packet that is simply not on disk is an absence, and the region says which.
    readiness = criteria_defect(packet) if packet is not None and not criteria else ""

    return VerdictState(
        criteria=criteria,
        readiness=readiness,
        report=read_report(repo, snapshot),
        envelope=envelope,
        log_path=snapshot.log_path,
        candidate_hash=snapshot.candidate_hash,
    )


def criterion_rows(state: VerdictState) -> tuple[Criterion, ...]:
    """Every criterion to render: the packet's, in its order, then any extras.

    A packet criterion no report has covered is not-yet-reached rather than
    failing — before any report exists that is every one of them — and an
    identifier only the verdict named is kept rather than dropped.
    """

    reported = state.report.criteria if state.report is not None else ()
    by_id = {item.id.upper(): item for item in reported}
    rows = [
        replace(by_id[name.upper()], id=name)
        if name.upper() in by_id
        else Criterion(id=name, status=NOT_REACHED)
        for name in state.criteria
    ]
    known = {name.upper() for name in state.criteria}
    rows.extend(
        replace(item, in_packet=False)
        for item in reported
        if item.id.upper() not in known
    )
    return tuple(rows)


def criteria_mismatch(state: VerdictState) -> str:
    """Both sides of a criterion-identifier mismatch, named rather than dropped.

    The verdict validator already treats a mismatch as a real condition — fatal
    on a pass, recorded on a fail — so the panel reports it as one instead of
    quietly rendering whichever set it happened to have.
    """

    if state.report is None or not state.criteria:
        return ""
    reported = {item.id.upper() for item in state.report.criteria}
    known = {name.upper() for name in state.criteria}
    unexpected = tuple(
        dict.fromkeys(
            item.id for item in state.report.criteria if item.id.upper() not in known
        )
    )
    missing = tuple(name for name in state.criteria if name.upper() not in reported)
    parts = []
    if unexpected:
        parts.append(f"not in the issue packet: {', '.join(unexpected)}")
    if missing:
        parts.append(f"missing from the verdict: {', '.join(missing)}")
    return f"id mismatch - {'; '.join(parts)}" if parts else ""


def short(digest: str, width: int = 12) -> str:
    """A candidate hash at the length the journal and reports print it."""

    return digest[:width] if digest else "unknown"


def stale_against(digest: str, current: str) -> bool:
    """Whether `digest` names a candidate other than the one in flight."""

    return bool(digest) and bool(current) and digest != current


def decision_line(state: VerdictState) -> str:
    """The overall decision, or pending until an envelope says otherwise."""

    envelope = state.envelope
    if envelope is None:
        return VERDICT_PENDING
    line = f"decision {envelope.decision}"
    if stale_against(envelope.candidate_hash, state.candidate_hash):
        line += (
            f"   stale - judged {short(envelope.candidate_hash)}, "
            f"current {short(state.candidate_hash)}"
        )
    elif envelope.candidate_hash:
        line += f"   candidate {short(envelope.candidate_hash)}"
    return line


def report_line(state: VerdictState) -> str:
    """Which report the statuses came from, and whether it is the current one."""

    report = state.report
    if report is None:
        return NO_REPORT
    if report.unreadable:
        return f"report {report.ref} could not be read"
    line = f"report {report.ref}"
    if report.attempt:
        line += f"   attempt {report.attempt}"
    if stale_against(report.candidate_hash, state.candidate_hash):
        line += f"   stale - judged {short(report.candidate_hash)}"
    if report.elided:
        line += f"   ({report.elided} criteria summarised by the report)"
    return line


def criterion_line(row: Criterion) -> str:
    """One matrix row: identifier, status, and the evidence behind it."""

    line = f"{row.id:<{_ID_WIDTH}}{row.status:<{_STATUS_WIDTH}}"
    if not row.in_packet:
        line += "not in packet   "
    return (line + clip(row.evidence, CRITERION_TEXT_CHARS)).rstrip()


def verdict_panel(
    snapshot: RunSnapshot, state: VerdictState, *, limit: int = CRITERION_ROWS
) -> str:
    """The verdict region: every criterion, its outcome, and the decision.

    Criteria are listed from the packet rather than from the verdict, so an
    operator watching a seven-criterion issue can see that four are still
    outstanding instead of only what has already been reported.
    """

    # No journal, no packet and no envelope is a repository with nothing to
    # judge. A replayed run whose journal is gone still has its log, and the
    # decision in it is the one thing worth reading, so that is not idle.
    if snapshot.idle and not state.criteria and state.envelope is None:
        return VERDICT_IDLE
    lines = [decision_line(state)]
    if state.envelope is not None and state.envelope.reason:
        lines.append(clip(state.envelope.reason))
    lines.append(report_line(state))
    if not state.criteria:
        lines.append(state.readiness or NO_PACKET)
        return "\n".join(lines)
    mismatch = criteria_mismatch(state)
    if mismatch:
        lines.append(mismatch)
    rows = criterion_rows(state)
    lines.extend(criterion_line(row) for row in rows[:limit])
    if len(rows) > limit:
        lines.append(f"({len(rows) - limit} more criteria not shown)")
    return "\n".join(lines)


def verdict_region_state(state: VerdictState | None) -> str:
    """Colour for the verdict region: failure, attention, or nothing yet."""

    if state is None:
        return "state-idle"
    if state.readiness or (state.report is not None and state.report.unreadable):
        return "state-failed"
    decision = state.envelope.decision.lower() if state.envelope is not None else ""
    if decision in ("fail", "rejected"):
        return "state-failed"
    if any(row.status == STATUS_FAIL for row in criterion_rows(state)):
        return "state-failed"
    if criteria_mismatch(state):
        return "state-ended"
    if decision == STATUS_PASS:
        stale = state.envelope is not None and stale_against(
            state.envelope.candidate_hash, state.candidate_hash
        )
        return "state-ended" if stale else "state-live"
    return "state-idle"


# --- candidate -------------------------------------------------------------


@dataclass(frozen=True)
class PathGroup:
    """One labelled part of the candidate: its paths, in render order."""

    label: str
    paths: tuple[str, ...] = ()


def candidate_groups(snapshot: RunSnapshot) -> tuple[PathGroup, ...]:
    """Owned, inherited and disowned paths, each path in exactly one group.

    Ownership is a difference over the journal's three sets rather than a field
    of its own. `candidate_paths` is what finalization commits, and a resumed
    worker is handed its predecessor's candidate plus whatever was already
    disowned, so the inherited part of a candidate is what the handoff also
    names and the rest is this worker's own. Disowned wins any overlap: a path a
    worker declared unrelated stays out of the commit whichever list also names
    it, and it must render once rather than twice under two labels.
    """

    disowned = frozenset(snapshot.unrelated_paths)
    inherited = frozenset(snapshot.handoff_paths) - disowned
    owned = frozenset(snapshot.candidate_paths) - inherited - disowned
    return (
        PathGroup(OWNED, tuple(sorted(owned))),
        PathGroup(INHERITED, tuple(sorted(inherited))),
        PathGroup(DISOWNED, tuple(sorted(disowned))),
    )


def path_rows(
    groups: tuple[PathGroup, ...],
    *,
    total: int = CANDIDATE_PATH_ROWS,
    floor: int = CANDIDATE_GROUP_ROWS,
) -> tuple[int, ...]:
    """How many paths each group lists, bounded so the region cannot grow.

    Every non-empty group keeps `floor` rows before any group takes the
    remainder, because a count with no paths under it is exactly what an
    operator could not act on when seven inherited paths were disowned in error.
    """

    shown = [min(len(group.paths), floor) for group in groups]
    spare = total - sum(shown)
    for index, group in enumerate(groups):
        if spare <= 0:
            break
        extra = min(len(group.paths) - shown[index], spare)
        shown[index] += extra
        spare -= extra
    return tuple(shown)


def path_line(path: str, *, limit: int = CANDIDATE_PATH_CHARS) -> str:
    """One path, flattened and bounded so an odd filename cannot break the view.

    Git reports whatever the filesystem holds, and a path may carry a newline, a
    tab or an escape sequence. The first two would split one row into several
    and the third would repaint the terminal, so anything unprintable is shown
    as a placeholder character rather than sent through.
    """

    printable = "".join(char if char.isprintable() else "?" for char in path)
    return clip(printable, limit)


def base_line(snapshot: RunSnapshot) -> str:
    """The commit the candidate is measured against, and how much it commits.

    A candidate only means anything against the tree it was captured on — grind
    refuses to finalize one whose base has moved — so the base is on the panel
    rather than in the journal alone.
    """

    return f"base {short(snapshot.base_head)}   commits {len(snapshot.candidate_paths)}"


def candidate_panel(snapshot: RunSnapshot, *, total: int = CANDIDATE_PATH_ROWS) -> str:
    """The candidate region: what the run will commit, and what it left alone.

    Groups with nothing in them are omitted rather than rendered empty, because
    a candidate with no handoff and nothing disowned is the common case and must
    read as a plain list rather than as three sections, two of them blank.
    """

    if snapshot.idle:
        return CANDIDATE_IDLE
    groups = candidate_groups(snapshot)
    budget = path_rows(groups, total=total)
    lines = [base_line(snapshot)]
    for group, rows in zip(groups, budget):
        if not group.paths:
            continue
        lines.append(f"{group.label} {len(group.paths)}")
        lines.extend(f"  {path_line(path)}" for path in group.paths[:rows])
        hidden = len(group.paths) - rows
        if hidden > 0:
            lines.append(f"  ({hidden} more not shown)")
    if len(lines) == 1:
        lines.append(CANDIDATE_EMPTY)
    return "\n".join(lines)


def candidate_region_state(snapshot: RunSnapshot) -> str:
    """Colour for the candidate region: attention once work has been disowned.

    Disowning is the one thing in this region an operator may need to overrule,
    and it is the judgement that went wrong, so it is the only condition here
    that claims a colour.
    """

    if snapshot.idle:
        return "state-idle"
    return "state-ended" if snapshot.unrelated_paths else "state-idle"


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def outcome_line(snapshot: RunSnapshot) -> str:
    """How the run ended and at which phase, in the operator's words.

    A run that ended cleanly has no terminal failure, and a run still in flight
    has no ending at all; both say so rather than being given one.
    """

    if snapshot.idle:
        return NO_JOURNAL_OUTCOME
    if not snapshot.terminal:
        return f"no terminal state recorded - last phase {snapshot.phase}"
    if snapshot.phase in TERMINAL_PHASES:
        ended = OUTCOMES.get(snapshot.phase, snapshot.phase)
    else:
        ended = CLEAN_OUTCOME
    return f"{ended}   phase {snapshot.phase}"


def header_line(snapshot: RunSnapshot, replay: ReplaySource | None = None) -> str:
    """The shell's own run state, until the header leaf fills it.

    An absent journal is a valid state, not an error, so a repository with no
    run in flight opens idle. A run that ended while the view was open reports
    its terminal phase rather than freezing on the last live frame.

    Replay names the log it is reading, because two runs share a log directory
    and the filename is what tells them apart, and adds how the run ended.
    """

    if replay is not None:
        issue = snapshot.issue_id or "unknown issue"
        return f"replay {issue}   {replay.log_path.name}\n{outcome_line(snapshot)}"
    if snapshot.idle:
        return "idle - no transaction in flight"
    issue = snapshot.issue_id or "unknown issue"
    line = f"{issue}   phase {snapshot.phase}"
    if snapshot.terminal:
        line += "   ended"
    return line


def region_state(
    key: str, snapshot: RunSnapshot, verdict: VerdictState | None = None
) -> str:
    """The state class for one region; colour carries meaning, not decoration."""

    if key == "header":
        if snapshot.idle:
            return "state-idle"
        return "state-ended" if snapshot.terminal else "state-live"
    if key == "warnings" and snapshot.warnings:
        return "state-failed"
    if key == "candidate":
        return candidate_region_state(snapshot)
    if key == "verdict":
        return verdict_region_state(verdict)
    return "state-idle"


def frame(
    snapshot: RunSnapshot,
    tick: int,
    *,
    width: int = PULSE_WIDTH,
    replay: ReplaySource | None = None,
    verdict: VerdictState | None = None,
) -> Frame:
    """Build the frame for `snapshot` at `tick`. Pure: reads nothing, writes nothing.

    Replay builds the same frame from the same snapshot; only the header names
    its source. One renderer means a finished run is explained exactly as the
    live run was, and there is a single path to keep correct.

    The verdict state is passed in rather than read here, because it comes from
    artifacts on disk and this stays a pure function of what it is handed.
    """

    state = verdict if verdict is not None else VerdictState()
    return Frame(
        header=header_line(snapshot, replay),
        current_action=PLACEHOLDER,
        candidate=candidate_panel(snapshot),
        verdict=verdict_panel(snapshot, state),
        warnings=warnings_panel(snapshot),
        pulse=pulse_line(snapshot, tick, width=width),
        states=tuple(
            (spec.key, region_state(spec.key, snapshot, state)) for spec in REGIONS
        ),
    )


_CSS = f"""
Screen {{
    background: {GROUND};
    color: {TEXT_STATIC};
}}

Region {{
    background: {PANEL};
    color: {TEXT_STATIC};
    border: round {TEXT_DIM};
    border-title-color: {TEXT_DIM};
    padding: 0 1;
    height: auto;
    min-height: 3;
}}

Region.state-idle {{ border: round {TEXT_DIM}; }}
Region.state-live {{ border: round {ACCENT_MOTION}; border-title-color: {ACCENT_MOTION}; }}
Region.state-ended {{ border: round {ACCENT_ATTENTION}; border-title-color: {ACCENT_ATTENTION}; }}
Region.state-failed {{ border: round {ACCENT_FAILURE}; border-title-color: {ACCENT_FAILURE}; }}

/* The body scrolls, so a terminal too small for the layout degrades to a
   shorter view rather than crashing. */
#body {{
    height: 1fr;
    background: {GROUND};
}}

#pulse {{
    height: 1;
    padding: 0 1;
    color: {ACCENT_MOTION};
    background: {GROUND};
}}

#hint {{
    height: 1;
    padding: 0 1;
    color: {TEXT_DIM};
    background: {GROUND};
}}
"""


#: The shell classes, built on first use and reused after. They subclass
#: textual's, so they cannot exist at module scope without pulling a terminal
#: UI framework into every invocation of every verb — registration needs only
#: the callback below, and a stale environment must lose this one command
#: rather than the whole CLI.
_SHELL: dict[str, type] | None = None


def _shell() -> dict[str, type]:
    """Import textual and define the shell classes, once, on demand.

    Raises :class:`TextualUnavailable` when the framework cannot be imported.
    A partial install — the package present but a submodule missing — raises
    ``ImportError`` rather than ``ModuleNotFoundError`` and is reported the
    same way, because to the operator it is the same broken environment.
    """

    global _SHELL
    if _SHELL is not None:
        return _SHELL

    try:
        from textual.app import App, ComposeResult
        from textual.binding import Binding
        from textual.containers import VerticalScroll
        from textual.widgets import Static
    except ImportError as exc:
        raise TextualUnavailable(MISSING_TEXTUAL) from exc

    class Region(Static):
        """One bordered region of the layout, filled by its own leaf."""

        def __init__(self, spec: RegionSpec) -> None:
            # Markup off: a region renders data read off disk, and a path or an
            # evidence line holding square brackets would otherwise be parsed as
            # a style tag and disappear from the screen.
            super().__init__("", id=spec.key, markup=False)
            self.spec = spec
            #: The plain text currently displayed, kept so the shell (and its
            #: tests) can read back what was rendered.
            self.body = ""

        def on_mount(self) -> None:
            self.border_title = self.spec.title

        def set_body(self, text: str) -> None:
            self.body = text
            self.update(text)

        def set_state(self, state: str) -> None:
            self.remove_class(*STATE_CLASSES)
            self.add_class(state)

    class DashboardApp(App[None]):
        """The shell: the region layout, the dark palette, and the refresh timer."""

        TITLE = "ortus dashboard"
        CSS = _CSS
        BINDINGS = [
            Binding("q", "quit", "quit"),
            Binding("ctrl+c", "quit", "quit", show=False),
        ]
        #: The dashboard takes no actions, so it offers none: the command palette
        #: is the one surface that would put a mutating verb on screen.
        ENABLE_COMMAND_PALETTE = False

        def __init__(
            self,
            repo: Path,
            *,
            refresh_seconds: float = REFRESH_SECONDS,
            replay: ReplaySource | None = None,
        ) -> None:
            super().__init__()
            self.repo = Path(repo)
            self.refresh_seconds = refresh_seconds
            #: The finished run being rendered, or None for live mode. Live mode is
            #: the default and reads the newest log, exactly as before replay
            #: existed; replay only pins which log the same reader follows.
            self.replay = replay
            self.snapshot = RunSnapshot()
            #: Carried between ticks: the log tail is incremental, and an envelope
            #: is logged once, so the decision has to outlive the slice it arrived in.
            self.verdict = VerdictState()
            self.tick = 0
            self.last_frame = frame(
                self.snapshot, self.tick, replay=replay, verdict=self.verdict
            )

        def compose(self) -> ComposeResult:
            yield Region(REGIONS[0])
            with VerticalScroll(id="body"):
                for spec in REGIONS[1:]:
                    yield Region(spec)
            yield Static("", id="pulse")
            yield Static(KEY_HINT, id="hint")

        def on_mount(self) -> None:
            self.theme = DARK_THEME
            self.refresh_run()
            self.set_interval(self.refresh_seconds, self.refresh_run)

        def advance(self) -> Frame:
            """Read the next snapshot and build its frame. Renders nothing.

            Split from the paint so the refresh loop is testable headless, and so a
            repository that becomes unreadable mid-run keeps the last frame instead
            of taking the view down — the pulse still advances, which is how an
            operator can tell the dashboard is alive.
            """

            log_path = self.replay.log_path if self.replay is not None else None
            try:
                self.snapshot = read_snapshot(
                    self.repo, previous=self.snapshot, log_path=log_path
                )
            except OSError:
                pass
            self.verdict = read_verdict(self.repo, self.snapshot, previous=self.verdict)
            self.tick += 1
            self.last_frame = frame(
                self.snapshot, self.tick, replay=self.replay, verdict=self.verdict
            )
            return self.last_frame

        def refresh_run(self) -> None:
            """One tick: re-read the run, then repaint."""

            self.advance()
            self.paint()

        def paint(self) -> None:
            """Push the current frame onto the widgets."""

            bodies = self.last_frame.bodies()
            states = dict(self.last_frame.states)
            for region in self.query(Region):
                text = bodies.get(region.spec.key)
                if text is not None:
                    region.set_body(text)
                state = states.get(region.spec.key)
                if state is not None:
                    region.set_state(state)
            for bar in self.query("#pulse"):
                bar.update(self.last_frame.pulse)

    _SHELL = {"Region": Region, "DashboardApp": DashboardApp}
    return _SHELL


def __getattr__(name: str) -> Any:
    """Serve `Region` and `DashboardApp` as module attributes, built on access.

    The classes read as module-level to every caller and every test, which is
    what they were before the import moved; only the moment they come into
    existence changed. PEP 562 is what keeps that true without a shim class.
    """

    if name in ("Region", "DashboardApp"):
        return _shell()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def dashboard(
    repo: Optional[Path] = typer.Argument(
        None, help="Target repo directory. Defaults to $PWD; no walk-up."
    ),
    replay: Optional[Path] = typer.Option(
        None,
        "--replay",
        help="Replay a finished run from its grind log instead of watching live.",
    ),
) -> None:
    """Watch one grind run live: phase, current action, candidate, verdict, warnings.

    Strictly read-only: never writes the repository, bd, or git, because a
    write into a worktree a worker owns becomes that worker's candidate. Needs
    no bd workspace and no run in flight — a quiet repository opens idle. Press
    q to exit.

    `--replay <log>` renders a finished run through the same panels, including
    how it ended, and takes its journal from the log's own directory rather
    than from the repo argument, so replaying a log names one run without
    ambiguity. A log still being written replays too, up to its current end.
    """

    try:
        app_type = _shell()["DashboardApp"]
    except TextualUnavailable as exc:
        output.error(str(exc))
        raise typer.Exit(code=1) from exc

    if replay is not None:
        source = resolve_replay(replay.resolve())
        if not source.log_path.is_file():
            output.error(f"no such run log: {source.log_path}")
            raise typer.Exit(code=1)
        app_type(source.repo, replay=source).run()
        return

    target = (repo if repo is not None else Path.cwd()).resolve()
    if not target.is_dir():
        output.error(f"no such directory: {target}")
        raise typer.Exit(code=1)
    app_type(target).run()
