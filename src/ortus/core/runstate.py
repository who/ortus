"""Read-only run state for one grind run: an incremental grind-log tail.

One model of a grind run, assembled from the newest grind log already on disk.
A leftover `logs/grind-transaction.json` is ignored: live grind no longer
writes one, and dashboard frames must not require it. Every dashboard panel
reads this snapshot rather than the file, so the surfaces cannot disagree
about what a run is doing.

Two properties shape the implementation.

The log reaches megabytes inside one session, so a refresh must read only the
bytes appended since the previous call. `read_snapshot` therefore takes an
offset and hands the next one back; the module holds no file handle, and a
rotated or truncated log is detected by its size dropping below the offset
rather than by an open descriptor going stale.

The log interleaves two writers. Ortus appends plain, timestamped lines (see
`_log_writer` in `ortus.commands.grind`) while the agent streams JSON events
into the same file. Agent content quotes the ortus vocabulary verbatim
whenever a worker edits the recovery code, so writer classification keys on
whether a line parses as a JSON object and never on substring matching — the
substring shortcut is what made a stopgap script report seven phantom
timeouts. Warnings are therefore counted from plain ortus lines only.

Nothing here writes, shells out, or touches bd or git: the module is a pure
function of the grind log, so it works while a grind holds the flock and is
testable without a workspace. It must never import Textual, so the model stays
usable headless.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any

#: Grind writes one timestamped log per run under the already-ignored logs/
#: tree; the newest is the live one.
LOG_GLOB = "grind-*.log"
#: Phase reported when no grind log is present. Log absence is a valid
#: state, not an error, so panels render idle rather than a fabricated phase.
PHASE_IDLE = "idle"
#: Historical grind-log halt names. Live grind no longer writes a journal;
#: leftover logs still mention these, and the dashboard must not crash on them.
#: `finalized-*` is the old finalization-step prefix; the named members are the
#: halts those logs recorded before leaving a candidate uncommitted. Anything
#: else — including the resumable `*-timeout` names — is reported verbatim
#: and treated as live.
TERMINAL_PHASES = frozenset(
    {
        "corrections-exhausted",
        "correction-rejected",
        "plan-gap-escalated",
        "orphaned-candidate",
        "incomplete-candidate",
    }
)
_TERMINAL_PREFIX = "finalized-"
#: Warning vocabulary, taken from grind's own `write_log` calls. Matched only
#: against plain ortus lines, so agent content quoting any of it counts for
#: nothing. First match wins, so one line is at most one warning.
WARNING_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("timeout", re.compile(r"TIMEOUT after", re.IGNORECASE)),
    ("halt", re.compile(r"\bHALT\b")),
    ("exhausted", re.compile(r"attempts exhausted", re.IGNORECASE)),
    ("escalation", re.compile(r"escalat(?:ed|ion)", re.IGNORECASE)),
    ("rejected", re.compile(r"\brejected\b", re.IGNORECASE)),
    ("plan-gap", re.compile(r"plan(?:ning)? gap", re.IGNORECASE)),
)
#: Warnings accumulate across refreshes for the life of a run; keeping the
#: most recent ones bounds a long session without losing what just happened.
MAX_WARNINGS = 200
_ORTUS_LINE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] ?(.*)$")
_ORTUS_STAMP = "%Y-%m-%d %H:%M:%S"
_MAX_TEXT_CHARS = 200

#: Grok headless crumbs use these top-level `type` values. Claude stream-json
#: never emits them at the object root (its `text` lives inside assistant
#: content parts), so this set is a safe detector on a parsed JSON object.
GROK_EVENT_TYPES = frozenset(
    {
        "thought",
        "text",
        "tool_call",
        "tool_call_update",
        "usage",
        "available_commands",
        "plan",
    }
)
#: The crumbs a dashboard feed renders. `usage` and `available_commands` stay
#: dropped; `plan` is system bookkeeping, not an activity crumb.
GROK_CRUMB_TYPES = frozenset({"thought", "text", "tool_call", "tool_call_update"})
_GROK_TOOL_DETAIL_KEYS = (
    "command",
    "target_file",
    "file_path",
    "query",
    "url",
    "tool_name",
    "pattern",
)

#: opencode `run --format json` events use these top-level `type` values,
#: recorded verbatim from opencode 1.18.27. Every event wraps the part it
#: reports under `part`, whose own `type` mirrors the envelope with a hyphen
#: (`step-start`, `step-finish`, `tool`, `text`). `text` is also a Grok crumb
#: type, so the envelope, never the name alone, tells the two apart.
OPENCODE_EVENT_TYPES = frozenset({"step_start", "step_finish", "tool_use", "text"})
#: The MCP servers Ortus registers for opencode. opencode runs each server
#: itself and presents its tools to the model as flat functions named
#: `<server>_<tool>`, so a tool name carrying one of these prefixes is an
#: MCP call and the CodeGraph one is the required-mode handshake.
OPENCODE_MCP_SERVERS = frozenset({"codegraph"})
#: The `part.reason` a finished step carries once the worker has answered;
#: any other reason (`tool-calls`) means further steps follow.
OPENCODE_STEP_STOP = "stop"
_OPENCODE_TOOL_DETAIL_KEYS = (
    "command",
    "filePath",
    "file_path",
    "path",
    "pattern",
    "query",
    "url",
    "description",
)
#: Numeric stamps at or above this are epoch milliseconds, not seconds: a
#: seconds value this large names a year past 5000, which no log carries.
_EPOCH_MS_THRESHOLD = 10**11


class Writer(str, Enum):
    """Which process appended a log line."""

    ORTUS = "ortus"
    AGENT = "agent"


@dataclass(frozen=True)
class LogEvent:
    """One classified log line."""

    writer: Writer
    text: str
    at: _dt.datetime | None = None
    #: The JSON `type` of a structured event; empty for a plain ortus line.
    kind: str = ""
    #: Whether this event names something the worker is doing. Heartbeat-class
    #: events (thinking, session bookkeeping, tool results) are not actions, so
    #: they never displace the action an operator is waiting on.
    action: bool = False
    payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class RunWarning:
    """A warning plus the ortus line that produced it."""

    kind: str
    text: str
    at: _dt.datetime | None = None


@dataclass(frozen=True)
class LogTail:
    """Events parsed from one slice of the log, plus the offset to resume at."""

    events: tuple[LogEvent, ...] = ()
    offset: int = 0
    #: The log shrank or vanished since the last call, so the slice was re-read
    #: from the start rather than seeked past the end.
    truncated: bool = False


@dataclass(frozen=True)
class RunSnapshot:
    """Everything a panel needs about one grind run at one moment."""

    # --- run identity (derived from the grind log, or constructed in tests)
    issue_id: str = ""
    phase: str = PHASE_IDLE
    attempt: int = 0
    attempts: tuple[dict[str, Any], ...] = ()
    corrections: int = 0
    plan_gap_routed: bool = False
    base_head: str = ""
    candidate_hash: str = ""
    #: Where the authoritative work spec for this run was persisted, and its
    #: digest. A panel that has to list what the run is being judged against
    #: reads the work spec the run is bound to rather than re-querying bd, which
    #: would answer for the issue as it is now instead of as it was claimed.
    issue_packet_ref: str = ""
    issue_packet_hash: str = ""
    candidate_paths: tuple[str, ...] = ()
    handoff_paths: tuple[str, ...] = ()
    unrelated_paths: tuple[str, ...] = ()
    baseline_paths: tuple[str, ...] = ()
    verifier_refs: tuple[str, ...] = ()
    #: Constructor override for frame-helper tests that build a live snapshot
    #: without a log path. `read_snapshot` never sets this from a leftover
    #: journal file.
    journal_present: bool = False
    #: Unused leftover-journal notes. Kept so constructed snapshots stay valid.
    journal_notes: tuple[str, ...] = ()
    #: Backend named on the grind start line (`backend=grok`), if any.
    backend: str = ""
    created_at: _dt.datetime | None = None
    updated_at: _dt.datetime | None = None
    implementation_started_at: _dt.datetime | None = None
    implementation_finished_at: _dt.datetime | None = None
    verification_started_at: _dt.datetime | None = None
    verification_finished_at: _dt.datetime | None = None

    # --- log -------------------------------------------------------------
    log_path: Path | None = None
    #: Byte offset to pass back on the next call.
    offset: int = 0
    #: Events parsed by *this* call only; the tail is incremental.
    events: tuple[LogEvent, ...] = ()
    latest_action: str = ""
    latest_action_at: _dt.datetime | None = None
    #: How long the latest action has been the latest, clamped at zero so a
    #: local log timestamp against a UTC observation clock cannot go negative.
    blocked_seconds: float = 0.0
    warnings: tuple[RunWarning, ...] = ()
    #: The newest timestamp seen in the log, carried between calls so events
    #: that carry no time of their own can inherit one.
    clock: _dt.datetime | None = None
    observed_at: _dt.datetime | None = None

    @property
    def idle(self) -> bool:
        """No grind log is present, and no constructed run record was supplied."""

        return self.log_path is None and not self.journal_present

    @property
    def terminal(self) -> bool:
        """The log (or a constructed record) names a run that has finished."""

        return self.phase.startswith(_TERMINAL_PREFIX) or self.phase in TERMINAL_PHASES

    @property
    def warning_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for warning in self.warnings:
            counts[warning.kind] = counts.get(warning.kind, 0) + 1
        return counts


def find_log(repo: Path, *, glob: str = LOG_GLOB) -> Path | None:
    """The newest grind log under `repo/logs`, or None when there is none."""

    try:
        candidates = [path for path in (repo / "logs").glob(glob) if path.is_file()]
    except OSError:
        return None
    if not candidates:
        return None
    return max(candidates, key=lambda path: (_mtime(path), path.name))


def read_log_tail(
    path: Path | None,
    offset: int = 0,
    *,
    clock: _dt.datetime | None = None,
) -> LogTail:
    """Parse the bytes appended to `path` since `offset`.

    Only complete lines are consumed: the log is appended live, so its final
    line is routinely half written, and the returned offset stops at the last
    newline so that line is re-read once it is complete.
    """

    if path is None:
        return LogTail()
    try:
        size = path.stat().st_size
    except OSError:
        # The log vanished (rotated away, or a replay path that is now gone).
        return LogTail(truncated=True)

    start = max(0, offset)
    truncated = False
    if start > size:
        start, truncated = 0, True
    try:
        with path.open("rb") as handle:
            handle.seek(start)
            chunk = handle.read()
    except OSError:
        return LogTail(offset=offset, truncated=truncated)

    consumed = chunk.rfind(b"\n") + 1
    body = chunk[:consumed].decode("utf-8", errors="replace")
    events: list[LogEvent] = []
    for line in body.splitlines():
        event = classify_line(line)
        if event is None:
            continue
        if event.at is None:
            event = replace(event, at=clock)
        else:
            clock = event.at
        events.append(event)
    return LogTail(tuple(events), start + consumed, truncated)


def is_grok_event(obj: object) -> bool:
    """True when `obj` is a parsed Grok streaming-json event.

    Detection is the object's top-level `type`, never a substring of the
    rendered text: a Claude assistant turn that happens to say "thought" is
    not a Grok crumb. Nor is an opencode `text` event, which shares the type
    name but carries the `part` envelope Grok never writes.
    """

    return (
        isinstance(obj, dict)
        and obj.get("type") in GROK_EVENT_TYPES
        and not is_opencode_event(obj)
    )


def is_opencode_event(obj: object) -> bool:
    """True when `obj` is a parsed opencode `run --format json` event.

    Detection is the top-level `type` together with the `part` envelope
    every opencode event carries, so the shared `text` name cannot claim a
    Grok crumb and a Grok crumb cannot claim an opencode part.
    """

    return (
        isinstance(obj, dict)
        and obj.get("type") in OPENCODE_EVENT_TYPES
        and isinstance(obj.get("part"), dict)
    )


def opencode_mcp_server(name: str) -> str | None:
    """The registered MCP server an opencode tool name belongs to, if any."""

    for server in OPENCODE_MCP_SERVERS:
        prefix = f"{server}_"
        if name.startswith(prefix) and len(name) > len(prefix):
            return server
    return None


def summarize_opencode_tool(obj: dict[str, Any]) -> str:
    """One-line name plus the first useful argument of an opencode tool_use.

    Reads `part.tool` and `part.state.input` by typed path and never the
    call's output, so a summary cannot carry a file body or a command's
    stdout. A server that returns an empty tool name is shown as `tool`.
    """

    part = obj.get("part")
    part = part if isinstance(part, dict) else {}
    name = str(part.get("tool") or "tool")
    state = part.get("state")
    state = state if isinstance(state, dict) else {}
    raw = state.get("input")
    if not isinstance(raw, dict):
        return name
    for key in _OPENCODE_TOOL_DETAIL_KEYS:
        value = raw.get(key)
        if value:
            detail = str(value).replace("\n", " ")[:160]
            return f"{name}  {detail}"
    return name


def summarize_grok_tool(obj: dict[str, Any]) -> str:
    """One-line name plus the first useful argument of a Grok tool_call."""

    name = str(obj.get("toolName") or obj.get("title") or "tool")
    raw = obj.get("rawInput")
    if not isinstance(raw, dict):
        return name
    detail = ""
    for key in _GROK_TOOL_DETAIL_KEYS:
        value = raw.get(key)
        if value:
            detail = str(value).replace("\n", " ")[:160]
            break
    if not detail:
        return name
    return f"{name}  {detail}"


def classify_line(line: str) -> LogEvent | None:
    """Classify one log line by writer, or None when it carries nothing.

    A line that does not parse as a JSON object is an ortus line: ortus writes
    plain timestamped text and the agent writes JSON. The leading-brace check
    keeps an ortus line, which opens with its `[timestamp]` prefix, from being
    read as a JSON array.
    """

    text = line.rstrip("\r")
    if not text.strip():
        return None

    payload: dict[str, Any] | None = None
    if text.lstrip().startswith("{"):
        try:
            parsed = json.loads(text)
        except (ValueError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, dict):
            payload = parsed

    if payload is None:
        at, message = _split_ortus_line(text)
        return LogEvent(writer=Writer.ORTUS, text=message, at=at, action=bool(message))

    kind = str(payload.get("type", ""))
    described, action = _describe(payload, kind)
    return LogEvent(
        writer=Writer.ORTUS if kind.startswith("ortus.") else Writer.AGENT,
        text=described,
        at=_event_time(payload),
        kind=kind,
        action=action,
        payload=payload,
    )


def scan_warning(event: LogEvent) -> RunWarning | None:
    """The warning an ortus line carries, if any.

    Structured ortus events are excluded along with everything the agent
    writes: a verdict envelope carries a rejection reason in a field, and
    counting that as a fresh warning would double-report the run's own state.
    """

    if event.writer is not Writer.ORTUS or event.kind:
        return None
    for kind, pattern in WARNING_PATTERNS:
        if pattern.search(event.text):
            return RunWarning(kind=kind, text=event.text, at=event.at)
    return None


_ITER_RE = re.compile(r"\biter (\d+):")
_BACKEND_EQ_RE = re.compile(r"\bbackend=([a-z]+)\b")
_SPAWNING_RE = re.compile(r"\bspawning ([a-z]+) \(single-issue worker\)")
_READY_BACKEND_RE = re.compile(r"goal-prompt ready for \S+ \(([a-z]+)\)")
_STEP_RE = re.compile(r"\bstep ([a-z][a-z0-9-]*)")
_FINALIZED_RE = re.compile(r"\b(finalized-[a-z0-9-]+)\b")
_ISSUE_PHRASE_RES = (
    re.compile(r"goal-prompt ready for (\S+)"),
    re.compile(r"claimed issue (\S+)"),
    re.compile(r"leftover claim (\S+)"),
    re.compile(r"will claim (\S+)"),
    re.compile(r"worker closed (\S+)"),
    re.compile(r"left (\S+) in_progress"),
    re.compile(r"flagged (\S+) human"),
)
_GENERIC_ISSUE = re.compile(r"^[a-z][a-z0-9]{1,24}-[a-z0-9]{3,}(?:\.\d+)*$")
_SKIP_ISSUE_TOKENS = frozenset({"issue"})


@dataclass(frozen=True)
class _LogIdentity:
    """Issue, phase, attempt and backend inferred from one slice of the log."""

    issue_id: str = ""
    phase: str = ""
    attempt: int = 0
    backend: str = ""
    created_at: _dt.datetime | None = None
    updated_at: _dt.datetime | None = None


def _issue_token(token: str) -> str:
    cleaned = token.rstrip(".,;:)")
    if cleaned in _SKIP_ISSUE_TOKENS or cleaned.startswith("grind-"):
        return ""
    return cleaned if _GENERIC_ISSUE.fullmatch(cleaned) else ""


def _phase_from_text(text: str) -> str | None:
    """The phase one ortus line names, if it names one."""

    step = _STEP_RE.search(text)
    if step is not None:
        return step.group(1)
    finalized = _FINALIZED_RE.search(text)
    if finalized is not None:
        return finalized.group(1)
    lower = text.lower()
    if "corrections-exhausted" in lower or "correction attempts exhausted" in lower:
        return "corrections-exhausted"
    if "correction-rejected" in lower or "correction rejected" in lower:
        return "correction-rejected"
    if "plan-gap-escalated" in lower:
        return "plan-gap-escalated"
    if "orphaned-candidate" in lower:
        return "orphaned-candidate"
    if "incomplete-candidate" in lower:
        return "incomplete-candidate"
    if "timeout" in lower:
        return "implementation-timeout"
    if "worker closed" in lower or "grind ended" in lower:
        return "finalized-sync"
    if re.search(r"\bverified\b", lower):
        return "finalized-verified"
    if "verification started" in lower:
        return "verification"
    if (
        "spawning" in lower
        or "worker started" in lower
        or "goal-prompt ready" in lower
        or "in_progress" in lower
    ):
        return "implementation"
    return None


def scan_log_identity(events: tuple[LogEvent, ...]) -> _LogIdentity:
    """Issue, phase, attempt and backend carried by one slice of ortus lines."""

    issue_id = ""
    phase = ""
    attempt = 0
    backend = ""
    created_at: _dt.datetime | None = None
    updated_at: _dt.datetime | None = None
    for event in events:
        if event.writer is not Writer.ORTUS:
            if event.at is not None:
                created_at = created_at or event.at
                updated_at = event.at
            continue
        text = event.text
        if event.at is not None:
            created_at = created_at or event.at
            updated_at = event.at
        for pattern in _ISSUE_PHRASE_RES:
            match = pattern.search(text)
            if match is None:
                continue
            found = _issue_token(match.group(1))
            if found:
                issue_id = found
                break
        iter_match = _ITER_RE.search(text)
        if iter_match is not None:
            attempt = int(iter_match.group(1))
        named = _BACKEND_EQ_RE.search(text)
        if named is None:
            named = _SPAWNING_RE.search(text)
        if named is None:
            named = _READY_BACKEND_RE.search(text)
        if named is not None:
            backend = named.group(1)
        inferred = _phase_from_text(text)
        if inferred is not None:
            phase = inferred
    return _LogIdentity(
        issue_id=issue_id,
        phase=phase,
        attempt=attempt,
        backend=backend,
        created_at=created_at,
        updated_at=updated_at,
    )


def read_snapshot(
    repo: Path,
    *,
    offset: int | None = None,
    previous: RunSnapshot | None = None,
    log_path: Path | None = None,
    now: _dt.datetime | None = None,
) -> RunSnapshot:
    """One read-only snapshot of the run in `repo`.

    Pass the previous snapshot (or its `offset`) back in to read only what the
    log has grown by since. `log_path` pins a specific log, which is how a
    finished run is replayed; it defaults to the newest grind log.
    """

    repo = Path(repo)
    observed = _aware(now) or _dt.datetime.now(_dt.timezone.utc)
    path = log_path if log_path is not None else find_log(repo)

    # A different log means a different run, so nothing carries over.
    resumed = previous is not None and previous.log_path == path
    start = offset if offset is not None else (previous.offset if resumed else 0)
    clock = previous.clock if resumed and previous is not None else None
    tail = read_log_tail(path, start, clock=clock)
    carried = resumed and not tail.truncated and previous is not None

    warnings = tuple(
        warning
        for warning in (scan_warning(event) for event in tail.events)
        if warning is not None
    )
    if carried and previous is not None:
        warnings = (*previous.warnings, *warnings)[-MAX_WARNINGS:]

    latest_action = previous.latest_action if carried and previous else ""
    latest_at = previous.latest_action_at if carried and previous else None
    for event in tail.events:
        if event.action and event.text:
            latest_action, latest_at = event.text, event.at
    if latest_action and latest_at is None:
        # Nothing in the log has ever carried a time. Observation time is the
        # only bound available, and it reads as "just seen" rather than as a
        # stall that never happened.
        latest_at = observed
    for event in reversed(tail.events):
        if event.at is not None:
            clock = event.at
            break

    identity = scan_log_identity(tail.events)
    if carried and previous is not None:
        issue_id = identity.issue_id or previous.issue_id
        phase = identity.phase or previous.phase
        attempt = identity.attempt or previous.attempt
        backend = identity.backend or previous.backend
        created_at = previous.created_at or identity.created_at
        updated_at = identity.updated_at or previous.updated_at
    else:
        issue_id = identity.issue_id
        phase = identity.phase or (PHASE_IDLE if path is None else phase_for_log(path))
        attempt = identity.attempt
        backend = identity.backend
        created_at = identity.created_at
        updated_at = identity.updated_at

    started = created_at.isoformat() if created_at is not None else None
    attempts: tuple[dict[str, Any], ...] = ()
    if attempt:
        record: dict[str, Any] = {"number": attempt, "phase": phase or "implementation"}
        if started is not None:
            record["started_at"] = started
        attempts = (record,)
    elif carried and previous is not None:
        attempts = previous.attempts

    return replace(
        RunSnapshot(),
        issue_id=issue_id,
        phase=phase,
        attempt=attempt,
        attempts=attempts,
        backend=backend,
        created_at=created_at,
        updated_at=updated_at,
        implementation_started_at=created_at,
        log_path=path,
        offset=tail.offset,
        events=tail.events,
        latest_action=latest_action,
        latest_action_at=latest_at,
        blocked_seconds=_elapsed(latest_at, observed),
        warnings=warnings,
        clock=clock,
        observed_at=observed,
    )


def phase_for_log(path: Path | None) -> str:
    """A log with no phase-naming line is still a live run, not idle."""

    return "implementation" if path is not None else PHASE_IDLE


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _split_ortus_line(text: str) -> tuple[_dt.datetime | None, str]:
    """Split `[YYYY-MM-DD HH:MM:SS] message` into its parts.

    The stamp is local time, as `_log_writer` writes it, so it is read as local
    and returned aware; a run watched across a timezone boundary would
    otherwise compare against a UTC clock and produce a negative age.
    """

    match = _ORTUS_LINE.match(text)
    if match is None:
        return None, text.strip()
    try:
        stamp = _dt.datetime.strptime(match.group(1), _ORTUS_STAMP).astimezone()
    except ValueError:
        stamp = None
    return stamp, match.group(2).strip()


def _describe(payload: dict[str, Any], kind: str) -> tuple[str, bool]:
    """A one-line description of a structured event, and whether it is an action."""

    if kind == "assistant":
        return _describe_assistant(payload.get("message"))
    if kind == "user":
        return "tool result", False
    if kind == "result":
        subtype = str(payload.get("subtype", "")).strip()
        return _clip(f"worker session ended{f' ({subtype})' if subtype else ''}"), True
    if kind == "system":
        return _clip(f"session {payload.get('subtype', 'event')}"), False
    if kind == "ortus.verdict":
        decision = str(payload.get("decision", "unknown"))
        return _clip(f"verdict {decision}"), True
    if kind == "ortus.codegraph":
        return _clip(f"codegraph {payload.get('kind', 'event')}"), False
    if kind in ("item.started", "item.completed"):
        return _describe_codex_item(payload.get("item"))
    if kind == "turn.completed":
        return "turn completed", True
    if is_opencode_event(payload):
        return _describe_opencode(payload, kind)
    if is_grok_event(payload) or kind in GROK_EVENT_TYPES:
        return _describe_grok(payload, kind)
    return _clip(kind or "event"), False


def _describe_opencode(payload: dict[str, Any], kind: str) -> tuple[str, bool]:
    """Describe an opencode event. Tool calls and assistant text are actions.

    A `step_finish` whose reason is `stop` is the worker's answer, the end of
    its turn, so it is an action like codex's `turn.completed`; one that
    finished for `tool-calls` sits between tool calls and is bookkeeping, as
    is every `step_start`. A tool's output is never read here.
    """

    part = payload.get("part")
    part = part if isinstance(part, dict) else {}
    if kind == "tool_use":
        state = part.get("state")
        state = state if isinstance(state, dict) else {}
        summary = summarize_opencode_tool(payload)
        if str(state.get("status") or "") == "error":
            return _clip(f"error: {summary}"), True
        return _clip(summary), True
    if kind == "text":
        return _clip(_first_line(part.get("text")) or "message"), True
    if kind == "step_finish":
        reason = str(part.get("reason") or "")
        if reason == OPENCODE_STEP_STOP:
            return "turn completed", True
        return _clip(f"step finished{f' ({reason})' if reason else ''}"), False
    if kind == "step_start":
        return "step started", False
    return _clip(kind or "event"), False


def _describe_grok(payload: dict[str, Any], kind: str) -> tuple[str, bool]:
    """Describe a Grok crumb. Only `tool_call` is an action.

    Thought and text arrive at high frequency and must not displace the
    latest tool; they feed the dashboard crumb surface instead. Usage and
    available_commands carry no operator-facing text.
    """

    if kind in ("usage", "available_commands"):
        return "", False
    if kind == "thought":
        data = payload.get("data")
        body = "" if data is None else str(data)
        return _clip(f"think {body}" if body else "think"), False
    if kind == "text":
        data = payload.get("data")
        body = "" if data is None else str(data)
        return _clip(body or "text"), False
    if kind == "tool_call":
        return _clip(summarize_grok_tool(payload)), True
    if kind == "tool_call_update":
        status = str(payload.get("status") or "")
        if status == "completed":
            return "tool completed", False
        if status in ("failed", "error"):
            return "tool failed", False
        return "tool update", False
    if kind == "plan":
        return "plan", False
    return _clip(kind or "event"), False


def _describe_assistant(message: Any) -> tuple[str, bool]:
    """Describe a claude assistant turn: the last tool call, else its text.

    Thinking is deliberately not an action. A worker that thinks for a minute
    and then blocks on a suite should keep showing the suite it is blocked on.
    """

    content = message.get("content") if isinstance(message, dict) else None
    parts = [content] if isinstance(content, str) else content
    if not isinstance(parts, list):
        return "assistant turn", False
    text = ""
    tool = ""
    for part in parts:
        if isinstance(part, str):
            text = part or text
            continue
        if not isinstance(part, dict):
            continue
        if part.get("type") == "tool_use":
            tool = f"{part.get('name', 'tool')} {_summarize(part.get('input'))}".strip()
        elif part.get("type") == "text":
            text = str(part.get("text", "")) or text
    if tool:
        return _clip(tool), True
    first = _first_line(text)
    if first:
        return _clip(first), True
    return "assistant turn", False


def _describe_codex_item(item: Any) -> tuple[str, bool]:
    if not isinstance(item, dict):
        return "codex item", False
    itype = str(item.get("type", ""))
    if itype == "command_execution":
        return _clip(f"command: {item.get('command', '')}"), True
    if itype == "mcp_tool_call":
        return _clip(f"mcp {item.get('server', '')}.{item.get('tool', '')}"), True
    if itype == "file_change":
        return _clip(f"file change: {_summarize(item.get('changes'))}"), True
    if itype == "agent_message":
        return _clip(_first_line(item.get("text")) or "message"), True
    if itype == "error":
        return _clip(f"error: {item.get('message', '')}"), True
    return _clip(f"codex {itype or 'item'}"), False


def _summarize(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("command", "file_path", "path", "pattern", "query", "description"):
            if isinstance(value.get(key), str):
                return value[key]
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _first_line(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    lines = value.strip().splitlines()
    return lines[0] if lines else ""


def _clip(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= _MAX_TEXT_CHARS else flat[:_MAX_TEXT_CHARS] + "…"


def _event_time(payload: dict[str, Any]) -> _dt.datetime | None:
    for key in ("timestamp", "at", "time"):
        stamp = _parse_time(payload.get(key))
        if stamp is not None:
            return stamp
    return None


def _parse_time(value: Any) -> _dt.datetime | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        # opencode stamps each event in epoch milliseconds.
        seconds = value / 1000 if value >= _EPOCH_MS_THRESHOLD else value
        try:
            return _dt.datetime.fromtimestamp(seconds, tz=_dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _aware(parsed)


def _aware(value: _dt.datetime | None) -> _dt.datetime | None:
    """Attach the local zone to a naive timestamp; journals carry both kinds."""

    if value is None:
        return None
    return value if value.tzinfo is not None else value.astimezone()


def _elapsed(since: _dt.datetime | None, now: _dt.datetime) -> float:
    if since is None:
        return 0.0
    return max(0.0, (now - since).total_seconds())


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0
