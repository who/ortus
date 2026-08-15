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

This task is the shell: the verb, the layout, and the refresh loop. The five
regions it names — header, current action, candidate, verdict, warnings — are
filled by their own tasks. What the shell does own is the pulse: one element
that advances on every tick, so a healthy run looks alive and a stalled one is
obvious by its stillness. An operator once watched a silent tail for hours
unable to tell progress from death.

The header region is ortus-0udo.3. Two failures in one session were invisible
until they were fatal: a worker was killed at the watchdog cap while holding
finished work, stranding a candidate a human then recovered by hand, and
correction attempts were consumed silently until escalation to a human label
arrived as a surprise. Both facts were already on disk. The header therefore
names the claimed issue with its priority, the phase, the iteration, the
elapsed run time, the corrections spent against their cap, and how much
watchdog headroom is left — headroom rather than elapsed, because the decision
it informs is whether to intervene before a kill. Both caps are read from
grind's own option declarations rather than copied here, since `--worker-timeout`
is a flag whose default has changed once already and a stale copy would count
down against a number no run uses. Title and priority come from one read-only
bd query per issue rather than from the journal, which stores only the id, and
a query that does not answer degrades the header to the id rather than blanking
it.

The current-action region is ortus-0udo.4, and it answers the one question
`ortus tail` cannot: whether a worker is thinking, blocked on a subprocess, or
stalled. While a worker waits on a test suite it issues one blocking tool call
and then nothing, so tail prints nothing for ten or twenty minutes and a healthy
suite run is indistinguishable from a hang — measured on one turn, 61 percent of
the elapsed time was spent blocked on tests. The region therefore names the
latest action, says how long it has been the latest, and marks it as blocked
rather than thinking once that age passes a threshold, because the age is
exactly the quantity an operator uses to judge a stall and the log is the only
signal a read-only observer has. Under it goes the pytest workspace count, which
is the signal an operator was reduced to watching by hand: it moves while a
suite runs, so it corroborates that work is happening underneath a silent
action. It is labelled as evidence rather than drawn as progress, because the
total is unknown and a temp root shared by two grinds counts both. Nothing here
parses pytest output or predicts completion.

The verdict region is ortus-0udo.6. Verdicts are the most informative artifact a
run produces and the least visible: in one session a verifier passed every
criterion and then ended without emitting its envelope, so a sound candidate was
rejected and the operator learned only from a terse error after the run had
exited. The region therefore lists the criteria the run is being judged on
before any verdict exists, so four outstanding of seven is a visible fact, and
shows the decision as pending until an envelope appears, because a missing
envelope is itself the failure mode that cost that run. Expectation comes from
the authoritative work spec the journal names — read off disk rather than
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
import getpass
import inspect
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Optional

import typer

from ortus.core import output
from ortus.core.readiness import validate_issue
from ortus.core.runstate import (
    TERMINAL_PHASES,
    GROK_CRUMB_TYPES,
    LogEvent,
    RunSnapshot,
    is_grok_event,
    read_snapshot,
    summarize_grok_tool,
)
from ortus.core.transaction import JOURNAL_RELATIVE_PATH, JournalStore

#: Seconds between refreshes. A tick reads only the bytes the log grew by, so
#: this is cheap even against the megabyte logs a long session produces.
REFRESH_SECONDS = 1.0
#: Grok crumbs land faster than one a second; 250 ms is enough to paint them
#: as they arrive without a busy-loop. Claude and Codex stay on 1 Hz.
GROK_REFRESH_SECONDS = 0.25

#: Flags every bd invocation carries: `--readonly` keeps bd off its write paths
#: and `--sandbox` off its auto-sync ones. `check.py` already queries bd this
#: way, so this is the established read-only shape rather than a new one.
BD_READONLY_FLAGS: tuple[str, ...] = ("--readonly", "--sandbox")
BD_TIMEOUT_SECONDS = 15.0

#: Width of the pulse bar, in cells.
PULSE_WIDTH = 24
#: Second independent mover in Grok mode: crumb-count driven, so a hang
#: freezes this strip while the tick scanner still walks.
GROK_ACTIVITY_WIDTH = 8
_PULSE_MARK = "█"  # full block
_PULSE_RULE = "─"  # box-drawing horizontal

#: Newest crumbs the Grok feed keeps. A 10k burst must drop the oldest rather
#: than grow the region without bound.
CRUMB_FEED_LINES = 10
CRUMB_TEXT_CHARS = 88
CRUMB_THINK = "think"
CRUMB_TEXT = "text"
CRUMB_TOOL = "tool"
TOOL_IN_FLIGHT = "in-flight"
TOOL_DONE = "done"
TOOL_FAIL = "fail"
NO_CRUMBS = ""

#: How long an action may be the latest before it is called blocked rather than
#: thinking. A worker issues one tool call and then goes quiet for as long as
#: the subprocess behind it takes, and heartbeat-only stretches of ten to twenty
#: minutes are normal, so the boundary sits well above an ordinary turn and well
#: below the much longer bound at which the warnings region escalates a block.
BLOCKED_SECONDS = 90.0

#: The two states an action can be in while a run is live, and the third it
#: takes once the run has ended. Distinct words as well as distinct colour: a
#: terminal that cannot render the palette must still say which state it is in.
ACTION_THINKING = "thinking"
ACTION_BLOCKED = "blocked"
ACTION_ENDED = "ended"
#: No transaction is in flight, so there is no action. Said rather than left
#: showing the last action of a run that has already finished, which is the
#: stale reading an operator would act on.
ACTION_IDLE = "idle - no action"
#: A run in flight whose log has carried no action yet: the worker has started
#: and not yet done anything the log names.
NO_ACTION = "no action logged yet"
#: Longest action text rendered. An action is a tool call with its argument,
#: which is unbounded, and one long line must not own the region.
ACTION_TEXT_CHARS = 100

#: Where pytest keeps its temporary workspaces, by default and per user. The
#: count of what is under it climbs while a suite runs, which is why an operator
#: with no other signal was reduced to listing it by hand.
PYTEST_ROOT_PREFIX = "pytest-of-"
#: How the workspace count is labelled. Evidence, explicitly: the total is
#: unknown so this is not progress, and a temp root shared by two concurrent
#: grinds counts both runs rather than this one.
WORKSPACE_EVIDENCE = (
    "evidence  pytest workspaces {count}   shared temp root, not this run's progress"
)

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
DISOWNED = "unrelated"
#: The owned-paths region with no run record at all. Nothing was ever captured,
#: which is a different fact from a region holding nothing, and the region says
#: which rather than rendering an empty one.
CANDIDATE_IDLE = "idle - no owned paths captured"
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
#: The decision line before any envelope exists. A worker that reviews a whole
#: edit set and then ends without emitting criterion results is a real failure
#: mode, and it is named while it is happening rather than afterwards.
VERDICT_PENDING = "decision pending - no criterion results emitted yet"
#: No verifier report has been written for this run yet, so every criterion is
#: outstanding rather than failing.
NO_REPORT = "no verifier report yet"
#: The work spec the criteria would come from is not on disk.
NO_PACKET = "work spec unavailable - criteria cannot be listed"

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
    "plan-gap-escalated": "escalated as a planning gap",
    "orphaned-candidate": "work left orphaned",
    "incomplete-candidate": "work left incomplete",
}
#: A `finalized-*` phase is the run reaching a finalization phase transition, which is
#: the only clean ending there is. Replay must say so rather than invent a
#: failure for a run that simply finished.
CLEAN_OUTCOME = "finished cleanly"
#: A replayed run whose journal is gone: the log alone is still worth reading.
NO_JOURNAL_OUTCOME = "no run record - replayed from the log alone"

#: The header with no transaction in flight. An absent journal is a valid state
#: rather than an error, so an idle repository says so instead of being given a
#: fabricated phase.
HEADER_IDLE = "idle - no transaction in flight"
#: A journal in flight that names no issue, which an older journal can be.
UNKNOWN_ISSUE = "unknown issue"
#: Longest issue title the header renders. A title is free text and the header
#: is one region among five, so the title is the field that gives way — never
#: the phase or the budget, which are what an operator scans for.
HEADER_TITLE_CHARS = 72
#: Said once a run has reached a terminal phase, so a view left open on a
#: finished run does not read as a live one.
HEADER_ENDED = "ended"

#: grind's own option names. Both caps are read from the declaration rather than
#: copied here: `--worker-timeout`'s default has changed once already, and a
#: stale copy would count down against a cap no run actually uses.
WORKER_TIMEOUT_OPTION = "worker_timeout"
MAX_CORRECTIONS_OPTION = "max_corrections"

#: A cap of zero disables the watchdog. That is no limit, which is a different
#: fact from a limit that has expired, and the header says which.
WATCHDOG_OFF = "watchdog off - no cap on this worker"
#: grind's declaration could not be read, so there is no cap to count against.
#: Stated rather than guessed: a countdown against an invented number is worse
#: than no countdown.
WATCHDOG_UNKNOWN = "watchdog - cap not readable from the run"
#: The journal records no start for the worker now running, so headroom cannot
#: be computed. A resumed run whose journal predates attempt records is the
#: case this covers.
WATCHDOG_NO_START = "watchdog - no worker start recorded"

KEY_HINT = "q  quit     read-only: never writes the repository, bd, or git"


@dataclass(frozen=True)
class RegionSpec:
    """One named region of the layout: its widget id and its border title."""

    key: str
    title: str


#: The agreed layout, in render order. Each region is filled by its own task.
REGIONS: tuple[RegionSpec, ...] = (
    RegionSpec("header", "run"),
    RegionSpec("current-action", "current action"),
    RegionSpec("candidate", "owned paths"),
    RegionSpec("verdict", "acceptance checks"),
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
    #: Grok-only crumb feed. Empty on Claude/Codex so those frames stay the
    #: pre-change sparse panel.
    crumbs: str = ""

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

        extra = (self.crumbs,) if self.crumbs else ()
        return (*self.bodies().values(), self.pulse, *extra)


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


def pulse(tick: int, *, width: int = PULSE_WIDTH, density: int = 1) -> str:
    """A scanner that advances one cell per tick.

    Position comes from the tick rather than from the clock, so the bar moves
    on every refresh even when the run itself is producing nothing. Stillness
    on screen then means the dashboard has stopped, which is a fact worth being
    able to see. Density greater than one (Grok crumb rate) adds extra marks
    so a busy stream is visibly denser than a stall; Claude stays at 1.
    """

    span = max(1, width)
    cycle = max(1, 2 * span - 2)
    step = tick % cycle
    index = step if step < span else cycle - step
    marks = {index}
    extra = max(1, density)
    if extra > 1:
        stride = max(1, span // extra)
        for offset in range(1, extra):
            marks.add((index + offset * stride) % span)
    return "".join(_PULSE_MARK if i in marks else _PULSE_RULE for i in range(span))


def pulse_density(crumb_rate: int) -> int:
    """How many pulse marks a recent crumb count deserves. 1 is the idle scan."""

    if crumb_rate <= 0:
        return 1
    if crumb_rate <= 2:
        return 2
    if crumb_rate <= 5:
        return 3
    return 4


def pulse_line(
    snapshot: RunSnapshot,
    tick: int,
    *,
    width: int = PULSE_WIDTH,
    grok: bool = False,
    crumb_rate: int = 0,
    crumb_count: int = 0,
) -> str:
    """The moving strip: the scanner, the refresh count, the observation time.

    Grok mode adds a second, crumb-count-driven strip so a hung worker (tick
    still walks, activity frozen) is distinct from a dead dashboard (both
    frozen). Claude and Codex keep the single 1 Hz scanner.
    """

    observed = snapshot.observed_at
    stamp = observed.strftime("%H:%M:%S") if observed is not None else "--:--:--"
    scanner = pulse(tick, width=width, density=pulse_density(crumb_rate) if grok else 1)
    if not grok:
        return f"{scanner}  refresh {tick}  {stamp}"
    activity = pulse(crumb_count, width=GROK_ACTIVITY_WIDTH)
    return f"{scanner}  {activity}  refresh {tick}  {stamp}"


def refresh_interval(*, grok: bool = False) -> float:
    """Seconds between ticks. Only Grok mode drops below 1 Hz."""

    return GROK_REFRESH_SECONDS if grok else REFRESH_SECONDS


@dataclass(frozen=True)
class ReplaySource:
    """A finished run to render: its log, and the repository holding its journal."""

    log_path: Path
    repo: Path


@dataclass(frozen=True)
class Crumb:
    """One Grok thought, text, or tool crumb for the rolling feed."""

    kind: str
    text: str
    at: _dt.datetime | None = None
    tool_id: str = ""
    status: str = ""


@dataclass(frozen=True)
class ToolLight:
    """One in-flight, done, or failed Grok tool, keyed by toolCallId."""

    name: str
    state: str
    call_id: str = ""
    detail: str = ""


def journal_backend(repo: Path) -> str:
    """The backend the journal names, if it names one.

    CandidateJournal does not yet declare a backend field, so this reads the
    raw JSON. A missing key is not a signal. `.ortusrc` is never consulted:
    an idle repo must not flip into an empty NOC just because the project
    default is grok.
    """

    path = Path(repo) / JOURNAL_RELATIVE_PATH
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(payload, dict):
        return ""
    named = payload.get("backend")
    if isinstance(named, str) and named.strip():
        return named.strip().lower()
    return ""


def log_backend(events: tuple[LogEvent, ...], crumbs: tuple[Crumb, ...] = ()) -> str:
    """Backend implied by parsed event types, or empty when none have spoken."""

    if crumbs or any(event.payload and is_grok_event(event.payload) for event in events):
        return "grok"
    kinds = {event.kind for event in events}
    if kinds & {"item.started", "item.completed", "turn.completed"}:
        return "codex"
    if kinds & {"assistant", "user", "system", "result"}:
        return "claude"
    return ""


def backend_conflict(journal: str, log: str) -> str:
    """PLAN-GAP line when journal and log name different backends; else empty."""

    if journal and log and journal != log:
        return (
            f"PLAN-GAP: journal backend={journal} disagrees with log backend={log}"
        )
    return ""


def is_grok_mode(
    snapshot: RunSnapshot,
    *,
    crumbs: tuple[Crumb, ...] = (),
    journal: str = "",
) -> bool:
    """True when this run is a Grok run the dashboard should oversee.

    Detected from parsed Grok event types or a journal that names backend
    grok. A Claude log whose text contains the substring `thought` does not
    trip this: detection never looks at unparsed text.
    """

    if crumbs:
        return True
    if any(event.payload and is_grok_event(event.payload) for event in snapshot.events):
        return True
    return journal == "grok" and snapshot.journal_present


def classify_crumb(event: LogEvent) -> Crumb | None:
    """Turn one incremental log event into a feed crumb, or None to drop it."""

    payload = event.payload
    if payload is None or not is_grok_event(payload):
        return None
    kind = event.kind or str(payload.get("type") or "")
    if kind not in GROK_CRUMB_TYPES:
        return None
    if kind in ("thought", "text"):
        data = payload.get("data")
        body = " ".join(str(data).split()) if data is not None else ""
        if not body:
            return None
        label = CRUMB_THINK if kind == "thought" else CRUMB_TEXT
        return Crumb(
            kind=kind,
            text=f"{label}  {clip(body, CRUMB_TEXT_CHARS)}",
            at=event.at,
        )
    if kind == "tool_call":
        summary = summarize_grok_tool(payload)
        call_id = str(payload.get("toolCallId") or payload.get("id") or summary)
        return Crumb(
            kind=kind,
            text=f"{CRUMB_TOOL}  {clip(summary, CRUMB_TEXT_CHARS)}",
            at=event.at,
            tool_id=call_id,
            status=TOOL_IN_FLIGHT,
        )
    status = str(payload.get("status") or "")
    if status == "completed":
        state = TOOL_DONE
    elif status in ("failed", "error"):
        state = TOOL_FAIL
    else:
        return None
    call_id = str(payload.get("toolCallId") or payload.get("id") or "")
    return Crumb(
        kind=kind,
        text=f"{state}  {CRUMB_TOOL}",
        at=event.at,
        tool_id=call_id,
        status=state,
    )


def ingest_crumbs(
    events: tuple[LogEvent, ...],
    previous: tuple[Crumb, ...] = (),
    tools: tuple[ToolLight, ...] = (),
    *,
    limit: int = CRUMB_FEED_LINES,
) -> tuple[tuple[Crumb, ...], tuple[ToolLight, ...], int]:
    """Fold incremental events into a bounded feed and a tool-row state.

    Only `events` (this tick's tail) are classified, so a 10k burst never
    re-parses the whole log. The feed keeps the newest `limit` crumbs.
    """

    arrived: list[Crumb] = []
    lights = {light.call_id: light for light in tools if light.call_id}
    unnamed = [light for light in tools if not light.call_id]
    for event in events:
        crumb = classify_crumb(event)
        if crumb is None:
            continue
        arrived.append(crumb)
        if crumb.kind == "tool_call":
            name = crumb.text[len(CRUMB_TOOL) + 2 :] if crumb.text.startswith(f"{CRUMB_TOOL}  ") else crumb.text
            lights[crumb.tool_id] = ToolLight(
                name=name.split("  ", 1)[0] or "tool",
                state=TOOL_IN_FLIGHT,
                call_id=crumb.tool_id,
                detail=name,
            )
        elif crumb.kind == "tool_call_update" and crumb.tool_id:
            prior = lights.get(crumb.tool_id)
            lights[crumb.tool_id] = ToolLight(
                name=prior.name if prior is not None else CRUMB_TOOL,
                state=crumb.status,
                call_id=crumb.tool_id,
                detail=prior.detail if prior is not None else "",
            )
    feed = (*previous, *arrived)[-max(1, limit) :]
    ordered = (*unnamed, *lights.values())
    return feed, ordered, len(arrived)


def tool_row(tools: tuple[ToolLight, ...]) -> str:
    """In-flight / done / fail lights for tools seen this run."""

    if not tools:
        return ""
    parts = [f"{light.state} {light.name}" for light in tools]
    return "tools  " + "  |  ".join(parts)


def crumb_panel(
    crumbs: tuple[Crumb, ...],
    tools: tuple[ToolLight, ...] = (),
    *,
    limit: int = CRUMB_FEED_LINES,
) -> str:
    """Bounded newest-N feed plus the tool row. Empty when there are no crumbs."""

    window = crumbs[-max(1, limit) :]
    lines = [crumb.text for crumb in window]
    row = tool_row(tools)
    if row:
        lines.append(row)
    return "\n".join(lines)


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


def printable(text: str) -> str:
    """Every unprintable character replaced by a placeholder.

    The strings this view renders come off disk unmodified — a filename git
    reported, a shell command the agent ran — and either may carry a newline, a
    tab or an escape sequence. The first two would split one row into several
    and the third would repaint the terminal, so nothing unprintable is sent
    through.
    """

    return "".join(char if char.isprintable() else "?" for char in text)


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
    #: False for an identifier a verdict reported that the work spec never named.
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
    """The authoritative work-spec artifact for this run, if it is on disk.

    The journal names it exactly. The glob is the fallback for a journal
    written before that reference was recorded: the store prefixes a work spec
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
    """The work-spec artifact as a mapping; None when it cannot be read as one."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def packet_criteria(packet: dict[str, Any]) -> tuple[str, ...]:
    """The criterion identifiers this run is judged on, in work-spec order.

    Derived exactly as grind derives the expected set it hands the verdict
    validator, so what the panel says is outstanding and what the validator
    would call missing are the same question answered once.
    """

    field = str(packet.get("acceptance_criteria", ""))
    return tuple(dict.fromkeys(CRITERION_ID.findall(field)))


def criteria_defect(packet: dict[str, Any]) -> str:
    """Why a work spec names no criterion, in readiness's words.

    A work spec with no identifiers is a readiness failure rather than a verdict
    with nothing in it, and the region says which so the operator knows the
    issue is the defect and not the run.
    """

    for failure in validate_issue(packet).failures:
        if failure.code == "observable_criteria":
            return f"readiness failure - {failure.section}: {failure.message}"
    return "readiness failure - the work spec names no criterion identifier"


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
    # Only a work spec that is present and names nothing is a readiness failure.
    # One that is simply not on disk is an absence, and the region says which.
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
    """Every criterion to render: the work spec's, in its order, then any extras.

    A work-spec criterion no report has covered is not-yet-reached rather than
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
        parts.append(f"not in the work spec: {', '.join(unexpected)}")
    if missing:
        parts.append(f"missing from the criterion results: {', '.join(missing)}")
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
        line += f"   owned paths {short(envelope.candidate_hash)}"
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
        line += "not in work spec   "
    return (line + clip(row.evidence, CRITERION_TEXT_CHARS)).rstrip()


def verdict_panel(
    snapshot: RunSnapshot, state: VerdictState, *, limit: int = CRITERION_ROWS
) -> str:
    """The verdict region: every criterion, its outcome, and the decision.

    Criteria are listed from the work spec rather than from the verdict, so an
    operator watching a seven-criterion issue can see that four are still
    outstanding instead of only what has already been reported.
    """

    # No journal, no work spec and no envelope is a repository with nothing to
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
    """One path, flattened and bounded so an odd filename cannot break the view."""

    return clip(printable(path), limit)


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


# --- current action --------------------------------------------------------


def duration(seconds: float) -> str:
    """An elapsed time in the coarsest unit that still reads as a number.

    Seconds below a minute, then minutes, then hours. The question this answers
    is whether an action has been current for a moment or for a quarter of an
    hour; resolution finer than that only makes a line that changes under the
    operator harder to read. Negative input is impossible from the model, which
    clamps, and is clamped again here so a caller cannot render one.
    """

    total = int(max(0.0, seconds))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def pytest_root(base: Path | None = None) -> Path:
    """The pytest temporary root for this user: `<tempdir>/pytest-of-<user>`.

    Derived rather than configured, because it is pytest's own default and the
    dashboard is reading the same directory the operator does. A host with no
    passwd entry — a container, mostly — has no user name to key on, and pytest
    itself falls back to the same word.
    """

    root = Path(base) if base is not None else Path(tempfile.gettempdir())
    try:
        user = getpass.getuser()
    except (KeyError, OSError):
        user = "unknown"
    return root / f"{PYTEST_ROOT_PREFIX}{user}"


def _scan(path: Path) -> list[os.DirEntry[str]] | None:
    """One directory listing, or None when there is nothing to list.

    `scandir` rather than `iterdir` plus `stat`: the entry type comes back with
    the listing on every filesystem the grind runs on, so classifying an entry
    costs no extra syscall. This runs on every refresh against a host that is
    already spending most of its time compiling and testing.
    """

    try:
        with os.scandir(path) as entries:
            return list(entries)
    except OSError:
        return None


def workspace_count(root: Path | None = None) -> int | None:
    """How many pytest workspaces exist under `root`, or None when it is absent.

    An absent root is the normal case — nothing has run a suite on this host
    since it was last cleaned — and is reported as absent rather than as zero,
    which would read as a suite that has produced nothing. When the root holds
    session directories the count is taken from the newest of them, because that
    is the one a running suite is filling and therefore the number that moves;
    the root's own entries are counted only when there is no session below it.
    """

    entries = _scan(pytest_root() if root is None else Path(root))
    if entries is None:
        return None
    sessions = [entry for entry in entries if entry.is_dir(follow_symlinks=False)]
    if not sessions:
        return len(entries)
    newest = max(sessions, key=lambda entry: (_mtime(Path(entry.path)), entry.name))
    inner = _scan(Path(newest.path))
    return len(entries) if inner is None else len(inner)


def action_state(
    snapshot: RunSnapshot,
    *,
    replay: ReplaySource | None = None,
    threshold: float = BLOCKED_SECONDS,
) -> str:
    """Whether the latest action is being thought about, blocked on, or over.

    Blocked is decided by the age of the action rather than by any subprocess
    state, because the log is the only signal a read-only observer has and the
    age is the quantity an operator judges a stall by. A finished run has no
    current action at all, so its last one is neither: replaying it, or watching
    a run reach a terminal phase, must not show an age that grows forever.
    """

    if snapshot.terminal or replay is not None:
        return ACTION_ENDED
    return ACTION_BLOCKED if snapshot.blocked_seconds >= threshold else ACTION_THINKING


def action_panel(
    snapshot: RunSnapshot,
    *,
    replay: ReplaySource | None = None,
    workspaces: int | None = None,
    threshold: float = BLOCKED_SECONDS,
) -> str:
    """The current-action region: what the worker is doing, and for how long.

    The action survives a heartbeat because the model only displaces it with
    another action, so a stretch of content-free events keeps showing the suite
    the worker is blocked on rather than blanking. A live view with no journal
    shows no action even when an old log is lying around, since that action
    belongs to a run that has already ended; replay is the one reader that wants
    a finished run's last action, and it says so by pinning the log.

    The workspace count corroborates an action that is still current, so a run
    that has ended does not carry one: the directory would still be there, and
    counting it against a finished run would be evidence for nothing.
    """

    if snapshot.idle and replay is None:
        return ACTION_IDLE
    if not snapshot.latest_action:
        return NO_ACTION

    state = action_state(snapshot, replay=replay, threshold=threshold)
    stamp = stamp_of(snapshot.latest_action_at)
    if state == ACTION_ENDED:
        age = f"{state}   last action at {stamp}"
    else:
        age = f"{state} {duration(snapshot.blocked_seconds)}   since {stamp}"

    lines = [clip(printable(snapshot.latest_action), ACTION_TEXT_CHARS), age]
    if workspaces is not None and state != ACTION_ENDED:
        lines.append(WORKSPACE_EVIDENCE.format(count=workspaces))
    return "\n".join(lines)


def action_region_state(
    snapshot: RunSnapshot,
    *,
    replay: ReplaySource | None = None,
    threshold: float = BLOCKED_SECONDS,
) -> str:
    """Colour for the current-action region: motion while thinking, attention
    once blocked. The two states are what the region exists to tell apart, so
    they never share a colour."""

    if (snapshot.idle and replay is None) or not snapshot.latest_action:
        return "state-idle"
    state = action_state(snapshot, replay=replay, threshold=threshold)
    return "state-live" if state == ACTION_THINKING else "state-ended"


def outcome_line(snapshot: RunSnapshot) -> str:
    """How the run ended and at which phase, in the operator's words.

    A run that ended cleanly has no terminal failure, and a run still in flight
    has no ending at all; both say so rather than being given one.
    """

    if snapshot.idle:
        return NO_JOURNAL_OUTCOME
    if not snapshot.terminal:
        return f"no terminal state recorded - last step {snapshot.phase}"
    if snapshot.phase in TERMINAL_PHASES:
        ended = OUTCOMES.get(snapshot.phase, snapshot.phase)
    else:
        ended = CLEAN_OUTCOME
    return f"{ended}   step {snapshot.phase}"


# --- header ----------------------------------------------------------------


@dataclass(frozen=True)
class IssueIdentity:
    """What bd says the run's issue is, beyond the id the journal records."""

    issue_id: str = ""
    title: str = ""
    priority: int | None = None
    #: True once a query has been made for `issue_id`, whether or not it
    #: answered. The refresh loop ticks once a second and a bd invocation costs
    #: a subprocess, while an issue's title does not change under a run, so the
    #: question is asked once per issue and the answer — including "bd did not
    #: answer" — is carried between ticks.
    queried: bool = False


def parse_identity(payload: str, issue_id: str) -> IssueIdentity:
    """Title and priority out of one `show --json` answer.

    bd answers with a list of issues, so the entry for the id asked about is
    taken by name rather than by position. Anything unparseable degrades to the
    id, which is the same outcome as bd not answering at all.
    """

    try:
        decoded = json.loads(payload)
    except ValueError:
        return IssueIdentity(issue_id=issue_id, queried=True)
    entries = decoded if isinstance(decoded, list) else [decoded]
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("id") != issue_id:
            continue
        priority = entry.get("priority")
        return IssueIdentity(
            issue_id=issue_id,
            title=str(entry.get("title", "")),
            priority=priority if isinstance(priority, int) else None,
            queried=True,
        )
    return IssueIdentity(issue_id=issue_id, queried=True)


def read_identity(
    repo: Path, snapshot: RunSnapshot, *, previous: IssueIdentity | None = None
) -> IssueIdentity:
    """Ask bd what the run's issue is, once per issue.

    A failed query is remembered as a failed query rather than retried on the
    next tick: bd being unavailable is a property of the environment, not of
    this second, and re-spawning a subprocess once a second against it would
    make the view stutter for an answer that is not coming.
    """

    issue = snapshot.issue_id
    if not issue:
        return IssueIdentity()
    if previous is not None and previous.issue_id == issue and previous.queried:
        return previous
    payload = run_bd(repo, "show", issue, "--json")
    if payload is None:
        return IssueIdentity(issue_id=issue, queried=True)
    return parse_identity(payload, issue)


def declared_cap(option: str) -> int | None:
    """grind's declared default for one of its cap options, or None.

    Read from the declaration rather than copied, because the failure this
    guards against is a copy going stale: `--worker-timeout` already moved once
    (1800 to 5400) after real workers were killed mid-verification. A
    declaration this cannot read yields None, and the header says the cap is
    unknown rather than counting down against a number invented here.
    """

    try:
        from ortus.commands import grind

        declared = inspect.signature(grind.grind).parameters[option].default
    except (ImportError, AttributeError, KeyError, TypeError, ValueError):
        return None
    value = getattr(declared, "default", declared)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def parse_stamp(value: Any) -> _dt.datetime | None:
    """One journal timestamp, aware. Journals carry both naive and aware ones."""

    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.astimezone()


def worker_started_at(snapshot: RunSnapshot) -> _dt.datetime | None:
    """When the worker now running started — not when the transaction did.

    A resumed run inherits a journal whose transaction timestamps predate the
    worker being watched, so a countdown taken from the transaction would be
    wrong by however long the earlier attempt ran. Every phase transition appends
    an attempt record carrying its own start, so the newest of those is this
    worker's. A journal written before attempts were kept falls back to the
    later of the two phase timestamps, which each transition rewrites for the
    same reason.
    """

    for attempt in reversed(snapshot.attempts):
        if not isinstance(attempt, dict):
            continue
        started = parse_stamp(attempt.get("started_at"))
        if started is not None:
            return started
    phases = [
        moment
        for moment in (
            snapshot.implementation_started_at,
            snapshot.verification_started_at,
        )
        if moment is not None
    ]
    return max(phases) if phases else None


def worker_seconds(snapshot: RunSnapshot) -> float | None:
    """How long the worker now running has been running, or None if unknown."""

    started = worker_started_at(snapshot)
    if started is None or snapshot.observed_at is None:
        return None
    return max(0.0, (snapshot.observed_at - started).total_seconds())


def watchdog_headroom(snapshot: RunSnapshot, *, cap: int) -> float | None:
    """Seconds before the watchdog kills this worker, clamped at zero.

    Zero is what a worker past its cap gets, never a negative number: the
    header renders such a worker as overdue, which is a fact an operator can
    act on, where a negative countdown would read as an arithmetic bug.
    """

    spent = worker_seconds(snapshot)
    if spent is None:
        return None
    return max(0.0, float(cap) - spent)


def run_elapsed(snapshot: RunSnapshot) -> float | None:
    """How long the transaction has been open, or ran for once it ended."""

    if snapshot.created_at is None:
        return None
    end = snapshot.updated_at if snapshot.terminal else snapshot.observed_at
    if end is None:
        end = snapshot.observed_at
    if end is None:
        return None
    return max(0.0, (end - snapshot.created_at).total_seconds())


def iteration(snapshot: RunSnapshot) -> int:
    """Which pass over the issue this is.

    The journal's counter starts at one and a journal in flight is always on at
    least its first pass, so a journal that recorded a zero — an older schema,
    or one saved before the first phase transition — reads as one rather than as a run
    that has not started.
    """

    return max(1, snapshot.attempt)


def identity_line(snapshot: RunSnapshot, identity: IssueIdentity | None = None) -> str:
    """Which issue the run claimed: its id, and its priority and title if known.

    The id is always shown and always first, because it is the one field the
    journal itself carries; everything else came from bd and may be missing.
    """

    issue = snapshot.issue_id or UNKNOWN_ISSUE
    parts = [issue]
    if identity is not None and identity.issue_id == snapshot.issue_id:
        if identity.priority is not None:
            parts.append(f"P{identity.priority}")
        if identity.title:
            parts.append(clip(printable(identity.title), HEADER_TITLE_CHARS))
    return "   ".join(parts)


def state_line(snapshot: RunSnapshot) -> str:
    """Phase, iteration and elapsed run time. Phase leads, and is never clipped."""

    parts = [f"step {snapshot.phase}", f"iteration {iteration(snapshot)}"]
    elapsed = run_elapsed(snapshot)
    if elapsed is not None:
        parts.append(f"elapsed {duration(elapsed)}")
    if snapshot.terminal:
        parts.append(HEADER_ENDED)
    return "   ".join(parts)


def budget_line(snapshot: RunSnapshot, *, cap: int | None = None) -> str:
    """Correction attempts spent against the cap that will escalate the issue.

    Spent silently is how the budget was consumed in the session this task
    exists for, so it leads its own line and is never the field that gives way
    to a narrow terminal.
    """

    limit = declared_cap(MAX_CORRECTIONS_OPTION) if cap is None else cap
    if limit is None or limit <= 0:
        return f"corrections {snapshot.corrections}/0 - retries disabled"
    return f"corrections {snapshot.corrections}/{limit}"


def watchdog_line(snapshot: RunSnapshot, *, cap: int | None = None) -> str:
    """Watchdog headroom as a countdown, or why there is none to show."""

    limit = declared_cap(WORKER_TIMEOUT_OPTION) if cap is None else cap
    if limit is None:
        return WATCHDOG_UNKNOWN
    if limit <= 0:
        return WATCHDOG_OFF
    spent = worker_seconds(snapshot)
    left = watchdog_headroom(snapshot, cap=limit)
    if spent is None or left is None:
        return WATCHDOG_NO_START
    if left <= 0.0:
        return f"watchdog overdue by {duration(spent - limit)} past {duration(limit)}"
    return f"watchdog {duration(left)} left of {duration(limit)}"


def header_line(
    snapshot: RunSnapshot,
    replay: ReplaySource | None = None,
    *,
    identity: IssueIdentity | None = None,
    worker_cap: int | None = None,
    correction_cap: int | None = None,
) -> str:
    """The header region: which issue, which phase, and how much budget is left.

    An absent journal is a valid state, not an error, so a repository with no
    run in flight opens idle rather than showing a fabricated phase. A run that
    ended while the view was open reports its terminal phase rather than
    freezing on the last live frame, and carries no countdown: a finished worker
    is not being watched by anything.

    Replay names the log it is reading, because two runs share a log directory
    and the filename is what tells them apart, and adds how the run ended. Its
    remaining lines are the live ones, so a finished run explains itself exactly
    as it did while it was running.
    """

    lines: list[str] = []
    if replay is not None:
        issue = snapshot.issue_id or UNKNOWN_ISSUE
        lines.append(f"replay {issue}   {replay.log_path.name}")
        lines.append(outcome_line(snapshot))
        if snapshot.idle:
            return "\n".join(lines)
    elif snapshot.idle:
        return HEADER_IDLE
    else:
        lines.append(identity_line(snapshot, identity))

    lines.append(state_line(snapshot))
    budget = budget_line(snapshot, cap=correction_cap)
    if replay is None and not snapshot.terminal:
        budget = f"{budget}   {watchdog_line(snapshot, cap=worker_cap)}"
    lines.append(budget)
    return "\n".join(lines)


def region_state(
    key: str,
    snapshot: RunSnapshot,
    verdict: VerdictState | None = None,
    replay: ReplaySource | None = None,
) -> str:
    """The state class for one region; colour carries meaning, not decoration."""

    if key == "header":
        if snapshot.idle:
            return "state-idle"
        return "state-ended" if snapshot.terminal else "state-live"
    if key == "warnings" and snapshot.warnings:
        return "state-failed"
    if key == "current-action":
        return action_region_state(snapshot, replay=replay)
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
    workspaces: int | None = None,
    identity: IssueIdentity | None = None,
    grok: bool = False,
    crumbs: tuple[Crumb, ...] = (),
    tools: tuple[ToolLight, ...] = (),
    crumb_rate: int = 0,
    conflict: str = "",
) -> Frame:
    """Build the frame for `snapshot` at `tick`. Pure: reads nothing, writes nothing.

    Replay builds the same frame from the same snapshot; only the header names
    its source. One renderer means a finished run is explained exactly as the
    live run was, and there is a single path to keep correct.

    The verdict state, the workspace count and the issue identity are passed in
    rather than read here, because all three come from disk or from bd and this
    stays a pure function of what it is handed. Grok crumbs and tool lights
    are likewise passed in: they accumulate across incremental tails and this
    function must not re-parse the log.
    """

    state = verdict if verdict is not None else VerdictState()
    action = action_panel(snapshot, replay=replay, workspaces=workspaces)
    feed = crumb_panel(crumbs, tools) if grok and crumbs else NO_CRUMBS
    if feed:
        action = f"{action}\n\n{feed}"
    warnings = warnings_panel(snapshot)
    if conflict:
        warnings = f"{warnings}\n{conflict}" if warnings else conflict
    return Frame(
        header=header_line(snapshot, replay, identity=identity),
        current_action=action,
        candidate=candidate_panel(snapshot),
        verdict=verdict_panel(snapshot, state),
        warnings=warnings,
        pulse=pulse_line(
            snapshot,
            tick,
            width=width,
            grok=grok,
            crumb_rate=crumb_rate,
            crumb_count=len(crumbs),
        ),
        states=tuple(
            (spec.key, region_state(spec.key, snapshot, state, replay))
            for spec in REGIONS
        ),
        crumbs=feed,
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
        """One bordered region of the layout, filled by its own task."""

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
            workspace_root: Path | None = None,
        ) -> None:
            super().__init__()
            self.repo = Path(repo)
            self.refresh_seconds = refresh_seconds
            #: Tests pin a long interval so they can drive ticks themselves.
            #: Only the default 1 Hz loop may drop to Grok's faster cadence.
            self._auto_refresh = refresh_seconds == REFRESH_SECONDS
            self._refresh_timer: Any = None
            #: The pytest temporary root the current-action region counts as
            #: evidence. Resolved once rather than per tick, and injectable so a
            #: test names its own root instead of the host's.
            self.workspace_root = (
                Path(workspace_root) if workspace_root is not None else pytest_root()
            )
            self.workspaces: int | None = None
            #: The finished run being rendered, or None for live mode. Live mode is
            #: the default and reads the newest log, exactly as before replay
            #: existed; replay only pins which log the same reader follows.
            self.replay = replay
            self.snapshot = RunSnapshot()
            #: Carried between ticks: the log tail is incremental, and an envelope
            #: is logged once, so the decision has to outlive the slice it arrived in.
            self.verdict = VerdictState()
            #: What bd says the claimed issue is. Carried between ticks because
            #: it is asked once per issue rather than once per refresh.
            self.identity = IssueIdentity()
            self.tick = 0
            self.crumbs: tuple[Crumb, ...] = ()
            self.tools: tuple[ToolLight, ...] = ()
            self.crumb_rate = 0
            self.grok = False
            self.conflict = ""
            self._crumb_log: Path | None = None
            self.last_frame = frame(
                self.snapshot,
                self.tick,
                replay=replay,
                verdict=self.verdict,
                workspaces=self.workspaces,
                identity=self.identity,
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
            self._arm_refresh()

        def _arm_refresh(self) -> None:
            if self._refresh_timer is not None:
                self._refresh_timer.stop()
            self._refresh_timer = self.set_interval(self.refresh_seconds, self.refresh_run)

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
            if self.replay is None:
                # Replay renders the run's own record and names its log rather
                # than asking bd how the issue reads now, so it spawns nothing.
                self.identity = read_identity(
                    self.repo, self.snapshot, previous=self.identity
                )
            self.workspaces = workspace_count(self.workspace_root)
            if self.snapshot.log_path != self._crumb_log:
                self.crumbs = ()
                self.tools = ()
                self._crumb_log = self.snapshot.log_path
            named = journal_backend(self.repo)
            seen = log_backend(self.snapshot.events, self.crumbs)
            self.conflict = backend_conflict(named, seen)
            self.crumbs, self.tools, arrived = ingest_crumbs(
                self.snapshot.events, self.crumbs, self.tools
            )
            self.crumb_rate = arrived
            # Log crumbs win for presentation; a journal/log mismatch is
            # recorded on the frame rather than resolved by dropping either.
            self.grok = is_grok_mode(
                self.snapshot,
                crumbs=self.crumbs,
                journal="" if self.conflict else named,
            )
            if self._auto_refresh:
                wanted = refresh_interval(grok=self.grok)
                if wanted != self.refresh_seconds:
                    self.refresh_seconds = wanted
                    if self.is_running:
                        self._arm_refresh()
            self.tick += 1
            self.last_frame = frame(
                self.snapshot,
                self.tick,
                replay=self.replay,
                verdict=self.verdict,
                workspaces=self.workspaces,
                identity=self.identity,
                grok=self.grok,
                crumbs=self.crumbs,
                tools=self.tools,
                crumb_rate=self.crumb_rate,
                conflict=self.conflict,
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
