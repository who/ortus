"""Per-bead cost telemetry rolled up from the grind logs already on disk.

`ortus grind` tees each worker's JSONL stream into `logs/grind-<ts>.log`,
interleaved with the harness's own timestamped marker lines. Those two streams
together already answer a cost question: the markers say which bead a worker
window belongs to and when it started, and the worker stream says what the
provider billed for it. Nothing here asks a worker for new instrumentation.

Backends report the same three quantities under different names and, worse,
under different inclusion rules:

* Claude ``result.usage`` — ``input_tokens`` counts only the uncached input;
  ``cache_read_input_tokens`` and ``cache_creation_input_tokens`` stand beside
  it rather than inside it.
* Codex ``turn.completed.usage`` — ``input_tokens`` is the TOTAL input and
  ``cached_input_tokens`` is the cached part *of* it, so the uncached bucket is
  the difference, not the field.
* OpenCode ``step_finish`` — ``tokens.input`` is uncached, with the cache split
  out under ``tokens.cache.read`` / ``tokens.cache.write``.

Normalizing those into one set of buckets is the whole point: an operator
comparing a judge-gated run against a plain one must be comparing the same
quantity on both sides.

Every bucket is ``None`` until a provider reports it. A turn that omits a field
leaves that bucket unset rather than zero and marks its record
``partial_usage``; a Codex turn that reports a total input without the cached
split is left unsplit rather than guessed. Dollars are only ever the provider's
own number (Claude ``total_cost_usd``, OpenCode ``cost``) — Codex reports none,
so Codex rows carry a null cost and the buckets are what a price table weights
later.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ortus.core.worker_failure import (
    FAILURE_LINE,
    PROVIDER_DEFAULT,
    WorkerFailure,
)

#: Same glob the tail and dashboard surfaces use to find run logs.
LOG_GLOB = "grind-*.log"

#: The bead key a session with no attribution marker rolls up under.
UNATTRIBUTED = None

_STAMPED = re.compile(r"^\[(?P<stamp>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\] (?P<body>.*)$")
_RUN_START = re.compile(
    r"^=== ortus grind started \([^;)]*; backend=(?P<backend>[A-Za-z0-9_-]+)"
)
_PROFILE = re.compile(
    r"^profile: (?P<backend>[A-Za-z0-9_-]+)/(?P<phase>[a-z]+) "
    r"\(model=(?P<model>[^,]+), effort=(?P<effort>[^)]+)\)$"
)
_PREP = re.compile(r"^iter prep: worker will claim (?P<issue>\S+) via goal-prompt$")
_READY = re.compile(
    r"^iter (?P<iter>\d+): goal-prompt ready for (?P<issue>\S+) "
    r"\((?P<backend>[A-Za-z0-9_-]+)\)$"
)
_SPAWN = re.compile(r"^iter (?P<iter>\d+): spawning (?P<backend>[A-Za-z0-9_-]+) ")
_CLOSED = re.compile(r"^iter (?P<iter>\d+): worker closed (?P<issue>\S+)")
_TIMEOUT = re.compile(r"^iter (?P<iter>\d+): worker TIMEOUT after ")

#: The placeholder both the profile marker and the failure marker write for
#: "no override; the CLI picks the model".
_PROVIDER_DEFAULT = PROVIDER_DEFAULT

#: The profile whose model and effort the single-issue worker runs under.
_WORKER_PHASE = "implement"


def _add(left: int | None, right: int | None) -> int | None:
    """Sum two buckets where ``None`` means "the provider never said"."""

    if left is None:
        return right
    if right is None:
        return left
    return left + right


def _as_int(value: Any) -> int | None:
    """An integer token count, or None for anything that is not one."""

    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _as_text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


@dataclass(frozen=True)
class UsageBuckets:
    """Billing buckets normalized across backends. None means unreported."""

    output_tokens: int | None = None
    uncached_input_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    cost_usd: float | None = None

    @property
    def input_tokens(self) -> int | None:
        """Every input token billed, cached or not, including cache writes."""

        return _add(
            _add(self.uncached_input_tokens, self.cached_input_tokens),
            self.cache_write_tokens,
        )

    @property
    def cache_hit_rate(self) -> float | None:
        """Share of billed input served from cache, or None when unknowable."""

        total = self.input_tokens
        if total is None or total <= 0 or self.cached_input_tokens is None:
            return None
        return self.cached_input_tokens / total

    @property
    def is_empty(self) -> bool:
        return all(
            value is None
            for value in (
                self.output_tokens,
                self.uncached_input_tokens,
                self.cached_input_tokens,
                self.cache_write_tokens,
                self.reasoning_tokens,
                self.cost_usd,
            )
        )

    def merged(self, other: "UsageBuckets") -> "UsageBuckets":
        """This plus another set of buckets, keeping unreported ones unset."""

        cost = self.cost_usd
        if other.cost_usd is not None:
            cost = other.cost_usd if cost is None else cost + other.cost_usd
        return UsageBuckets(
            output_tokens=_add(self.output_tokens, other.output_tokens),
            uncached_input_tokens=_add(
                self.uncached_input_tokens, other.uncached_input_tokens
            ),
            cached_input_tokens=_add(
                self.cached_input_tokens, other.cached_input_tokens
            ),
            cache_write_tokens=_add(self.cache_write_tokens, other.cache_write_tokens),
            reasoning_tokens=_add(self.reasoning_tokens, other.reasoning_tokens),
            cost_usd=cost,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "output_tokens": self.output_tokens,
            "uncached_input_tokens": self.uncached_input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "input_tokens": self.input_tokens,
            "cache_hit_rate": self.cache_hit_rate,
            "cost_usd": self.cost_usd,
        }


@dataclass(frozen=True)
class EventFacts:
    """What one worker-stream event contributes to the window it sits in."""

    usage: UsageBuckets | None = None
    #: The usage payload existed but left a bucket unreported.
    partial: bool = False
    #: A provider-reported turn total for the whole session (Claude).
    turns: int | None = None
    #: This event *is* one completed turn (Codex, OpenCode).
    turn_delta: int = 0
    session_id: str | None = None
    model: str | None = None
    errors: int = 0
    #: The stream ended badly, so its usage covers only part of the work.
    aborted: bool = False


def _claude_facts(obj: dict[str, Any]) -> EventFacts | None:
    kind = obj.get("type")
    if kind == "system" and obj.get("subtype") == "init":
        return EventFacts(
            session_id=_as_text(obj.get("session_id")),
            model=_as_text(obj.get("model")),
        )
    if kind == "user":
        message = obj.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            return None
        failed = sum(
            1
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "tool_result"
            and block.get("is_error") is True
        )
        return EventFacts(errors=failed) if failed else None
    if kind != "result":
        return None
    subtype = obj.get("subtype")
    common: dict[str, Any] = {
        "turns": _as_int(obj.get("num_turns")),
        "session_id": _as_text(obj.get("session_id")),
        "errors": 1 if obj.get("is_error") is True else 0,
        "aborted": bool(subtype) and subtype != "success",
    }
    usage = obj.get("usage")
    if not isinstance(usage, dict):
        # A result with no usage block still terminates a real window; its
        # billing is simply unreported, which is not the same as zero.
        return EventFacts(partial=True, **common)
    output_tokens = _as_int(usage.get("output_tokens"))
    uncached = _as_int(usage.get("input_tokens"))
    cached = _as_int(usage.get("cache_read_input_tokens"))
    return EventFacts(
        usage=UsageBuckets(
            output_tokens=output_tokens,
            uncached_input_tokens=uncached,
            cached_input_tokens=cached,
            cache_write_tokens=_as_int(usage.get("cache_creation_input_tokens")),
            cost_usd=_as_float(obj.get("total_cost_usd")),
        ),
        partial=any(value is None for value in (output_tokens, uncached, cached)),
        **common,
    )


def _codex_facts(obj: dict[str, Any]) -> EventFacts | None:
    kind = obj.get("type")
    if kind == "thread.started":
        return EventFacts(session_id=_as_text(obj.get("thread_id")))
    if kind == "turn.failed":
        return EventFacts(turn_delta=1, errors=1, aborted=True)
    if kind == "error":
        return EventFacts(errors=1)
    if kind == "item.completed":
        item = obj.get("item")
        if not isinstance(item, dict):
            return None
        item_kind = item.get("type")
        if item_kind == "error":
            return EventFacts(errors=1)
        if item_kind == "command_execution" and item.get("status") == "failed":
            return EventFacts(errors=1)
        return None
    if kind != "turn.completed":
        return None
    usage = obj.get("usage")
    if not isinstance(usage, dict):
        return EventFacts(turn_delta=1, partial=True)
    total_input = _as_int(usage.get("input_tokens"))
    cached = _as_int(usage.get("cached_input_tokens"))
    output_tokens = _as_int(usage.get("output_tokens"))
    reasoning = _as_int(usage.get("reasoning_output_tokens"))
    # Codex folds the cached tokens into input_tokens, so the uncached bucket
    # is the remainder. Without the cached figure there is no honest split:
    # reporting the total as uncached would overstate the expensive bucket, so
    # both stay unreported and the window is marked partial instead.
    uncached = None
    if total_input is not None and cached is not None:
        uncached = max(total_input - cached, 0)
    return EventFacts(
        usage=UsageBuckets(
            output_tokens=output_tokens,
            uncached_input_tokens=uncached,
            cached_input_tokens=cached,
            reasoning_tokens=reasoning,
        ),
        partial=any(
            value is None for value in (total_input, cached, output_tokens)
        ),
        turn_delta=1,
    )


def _opencode_facts(obj: dict[str, Any]) -> EventFacts | None:
    if obj.get("type") != "step_finish":
        return None
    part = obj.get("part")
    if not isinstance(part, dict):
        return None
    session_id = _as_text(obj.get("sessionID")) or _as_text(part.get("sessionID"))
    tokens = part.get("tokens")
    if not isinstance(tokens, dict):
        return EventFacts(turn_delta=1, partial=True, session_id=session_id)
    cache = tokens.get("cache")
    cache = cache if isinstance(cache, dict) else {}
    uncached = _as_int(tokens.get("input"))
    output_tokens = _as_int(tokens.get("output"))
    cached = _as_int(cache.get("read"))
    return EventFacts(
        usage=UsageBuckets(
            output_tokens=output_tokens,
            uncached_input_tokens=uncached,
            cached_input_tokens=cached,
            cache_write_tokens=_as_int(cache.get("write")),
            reasoning_tokens=_as_int(tokens.get("reasoning")),
            cost_usd=_as_float(part.get("cost")),
        ),
        partial=any(value is None for value in (uncached, output_tokens, cached)),
        turn_delta=1,
        session_id=session_id,
    )


def event_facts(obj: Any) -> EventFacts | None:
    """Decode one worker-stream event, whichever backend wrote it.

    The three decoders key off disjoint ``type`` values, so the first one to
    claim an event owns it and order carries no meaning.
    """

    if not isinstance(obj, dict):
        return None
    for decode in (_claude_facts, _codex_facts, _opencode_facts):
        facts = decode(obj)
        if facts is not None:
            return facts
    return None


@dataclass(frozen=True)
class SessionCost:
    """One worker window: the subprocess grind spawned for one bead."""

    run_id: str
    backend: str
    iteration: int | None = None
    issue_id: str | None = None
    model: str | None = None
    effort: str | None = None
    session_id: str | None = None
    turns: int | None = None
    errors: int = 0
    wall_seconds: float | None = None
    closed: bool = False
    incomplete: bool = False
    partial_usage: bool = False
    #: The closed class this window failed under; None when it did not fail.
    failure_class: str | None = None
    usage: UsageBuckets = field(default_factory=UsageBuckets)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "iteration": self.iteration,
            "issue_id": self.issue_id,
            "backend": self.backend,
            "model": self.model,
            "effort": self.effort,
            "session_id": self.session_id,
            "turns": self.turns,
            "errors": self.errors,
            "wall_seconds": self.wall_seconds,
            "closed": self.closed,
            "incomplete": self.incomplete,
            "partial_usage": self.partial_usage,
            "failure_class": self.failure_class,
            "usage": self.usage.as_dict(),
        }


@dataclass(frozen=True)
class BeadCost:
    """Every worker window that worked one bead, summed."""

    issue_id: str | None
    sessions: int = 0
    runs: tuple[str, ...] = ()
    backends: tuple[str, ...] = ()
    models: tuple[str, ...] = ()
    efforts: tuple[str, ...] = ()
    turns: int | None = None
    errors: int = 0
    wall_seconds: float | None = None
    closed: bool = False
    incomplete: bool = False
    partial_usage: bool = False
    failure_classes: tuple[str, ...] = ()
    usage: UsageBuckets = field(default_factory=UsageBuckets)

    def as_dict(self) -> dict[str, Any]:
        return {
            "issue_id": self.issue_id,
            "sessions": self.sessions,
            "runs": list(self.runs),
            "backends": list(self.backends),
            "models": list(self.models),
            "efforts": list(self.efforts),
            "turns": self.turns,
            "errors": self.errors,
            "wall_seconds": self.wall_seconds,
            "closed": self.closed,
            "incomplete": self.incomplete,
            "partial_usage": self.partial_usage,
            "failure_classes": list(self.failure_classes),
            "usage": self.usage.as_dict(),
        }


@dataclass(frozen=True)
class RunCost:
    """One grind log, parsed."""

    run_id: str
    path: Path
    backend: str
    sessions: tuple[SessionCost, ...] = ()

    @property
    def beads(self) -> tuple[BeadCost, ...]:
        return rollup_beads(self.sessions)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "path": str(self.path),
            "backend": self.backend,
            "sessions": [session.as_dict() for session in self.sessions],
            "beads": [bead.as_dict() for bead in self.beads],
        }


class _Window:
    """A worker window under construction while the log is being walked."""

    def __init__(
        self,
        *,
        run_id: str,
        backend: str,
        iteration: int | None,
        issue_id: str | None,
        model: str | None,
        effort: str | None,
        started: _dt.datetime | None,
    ) -> None:
        self.run_id = run_id
        self.backend = backend
        self.iteration = iteration
        self.issue_id = issue_id
        self.model = model
        self.effort = effort
        self.started = started
        self.last_stamp = started
        self.usage = UsageBuckets()
        self.reported_turns: int | None = None
        self.counted_turns = 0
        self.errors = 0
        self.session_id: str | None = None
        self.closed = False
        self.incomplete = False
        self.partial_usage = False
        self.saw_usage = False
        self.failure_class: str | None = None

    def absorb(self, facts: EventFacts) -> None:
        if facts.usage is not None:
            self.usage = self.usage.merged(facts.usage)
            self.saw_usage = True
        if facts.partial:
            self.partial_usage = True
        if facts.turns is not None:
            self.reported_turns = facts.turns
        self.counted_turns += facts.turn_delta
        self.errors += facts.errors
        if facts.session_id and self.session_id is None:
            self.session_id = facts.session_id
        # An init banner names the model actually serving the window; the
        # profile marker only ever held the override the operator asked for.
        if facts.model:
            self.model = facts.model
        if facts.aborted:
            self.incomplete = True

    def freeze(self) -> SessionCost:
        turns = self.reported_turns
        if turns is None:
            turns = self.counted_turns or None
        wall = None
        if self.started is not None and self.last_stamp is not None:
            wall = max((self.last_stamp - self.started).total_seconds(), 0.0)
        return SessionCost(
            run_id=self.run_id,
            backend=self.backend,
            iteration=self.iteration,
            issue_id=self.issue_id,
            model=self.model,
            effort=self.effort,
            session_id=self.session_id,
            turns=turns,
            errors=self.errors,
            wall_seconds=wall,
            closed=self.closed,
            incomplete=self.incomplete,
            partial_usage=self.partial_usage,
            failure_class=self.failure_class,
            usage=self.usage,
        )


def _parse_stamp(text: str) -> _dt.datetime | None:
    try:
        return _dt.datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _profile_value(raw: str) -> str | None:
    """A profile marker field, with the no-override placeholder read as unset."""

    value = raw.strip()
    if not value or value == _PROVIDER_DEFAULT:
        return None
    return value


def parse_grind_log(path: Path) -> RunCost:
    """Roll one `logs/grind-*.log` up into per-worker-window cost records.

    Marker lines open and close the windows; JSON lines between them are the
    worker stream and are billed to whichever window is open. Usage that
    arrives before any spawn marker — the legacy `--condition` path, where the
    worker picks its own bead and the harness never names one — lands in a
    window with a null `issue_id` rather than being guessed at or dropped.
    """

    run_id = path.stem
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return RunCost(run_id=run_id, path=path, backend="unknown")

    backend = "unknown"
    profiles: dict[str, tuple[str | None, str | None]] = {}
    pending_issue: str | None = None
    windows: list[_Window] = []
    current: _Window | None = None

    def open_window(iteration: int | None, window_backend: str) -> _Window:
        model, effort = profiles.get(_WORKER_PHASE, (None, None))
        window = _Window(
            run_id=run_id,
            backend=window_backend,
            iteration=iteration,
            issue_id=pending_issue,
            model=model,
            effort=effort,
            started=last_stamp,
        )
        windows.append(window)
        return window

    last_stamp: _dt.datetime | None = None
    for line in text.splitlines():
        stamped = _STAMPED.match(line)
        if stamped is not None:
            stamp = _parse_stamp(stamped.group("stamp"))
            if stamp is not None:
                last_stamp = stamp
                if current is not None:
                    current.last_stamp = stamp
            body = stamped.group("body")

            start = _RUN_START.match(body)
            if start is not None:
                backend = start.group("backend")
                continue
            profile = _PROFILE.match(body)
            if profile is not None:
                profiles[profile.group("phase")] = (
                    _profile_value(profile.group("model")),
                    _profile_value(profile.group("effort")),
                )
                continue
            prep = _PREP.match(body)
            if prep is not None:
                pending_issue = prep.group("issue")
                continue
            ready = _READY.match(body)
            if ready is not None:
                pending_issue = ready.group("issue")
                continue
            spawn = _SPAWN.match(body)
            if spawn is not None:
                current = open_window(int(spawn.group("iter")), spawn.group("backend"))
                continue
            closed = _CLOSED.match(body)
            if closed is not None and current is not None:
                current.closed = True
                if current.issue_id is None:
                    current.issue_id = closed.group("issue")
                continue
            failed = FAILURE_LINE.match(body)
            if failed is not None and current is not None:
                current.failure_class = failed.group("failure")
                continue
            if _TIMEOUT.match(body) is not None and current is not None:
                current.incomplete = True
            continue

        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        facts = event_facts(obj)
        if facts is None:
            continue
        if current is None:
            current = open_window(None, backend)
        current.absorb(facts)

    return RunCost(
        run_id=run_id,
        path=path,
        backend=backend,
        sessions=tuple(window.freeze() for window in windows),
    )


def rollup_beads(sessions: tuple[SessionCost, ...]) -> tuple[BeadCost, ...]:
    """Group worker windows by bead, in the order the beads first appear."""

    order: list[str | None] = []
    grouped: dict[str | None, list[SessionCost]] = {}
    for session in sessions:
        if session.issue_id not in grouped:
            grouped[session.issue_id] = []
            order.append(session.issue_id)
        grouped[session.issue_id].append(session)

    beads: list[BeadCost] = []
    for issue_id in order:
        group = grouped[issue_id]
        usage = UsageBuckets()
        turns: int | None = None
        wall: float | None = None
        for session in group:
            usage = usage.merged(session.usage)
            turns = _add(turns, session.turns)
            if session.wall_seconds is not None:
                wall = session.wall_seconds if wall is None else wall + session.wall_seconds
        beads.append(
            BeadCost(
                issue_id=issue_id,
                sessions=len(group),
                runs=tuple(dict.fromkeys(s.run_id for s in group)),
                backends=tuple(dict.fromkeys(s.backend for s in group)),
                models=tuple(dict.fromkeys(s.model for s in group if s.model)),
                efforts=tuple(dict.fromkeys(s.effort for s in group if s.effort)),
                turns=turns,
                errors=sum(s.errors for s in group),
                wall_seconds=wall,
                closed=any(s.closed for s in group),
                incomplete=any(s.incomplete for s in group),
                partial_usage=any(s.partial_usage for s in group),
                failure_classes=tuple(
                    dict.fromkeys(s.failure_class for s in group if s.failure_class)
                ),
                usage=usage,
            )
        )
    return tuple(beads)


@dataclass(frozen=True)
class FailureRate:
    """How one backend/model pair's worker windows failed, and how often."""

    backend: str
    model: str | None
    sessions: int = 0
    failures: int = 0
    #: Every class in the closed set, zero-filled. A class that never fires
    #: still has to appear: a reader comparing two runs needs to see that a
    #: bucket was empty rather than guess whether it was even counted.
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def failure_rate(self) -> float | None:
        """Share of this pair's windows that failed, or None with no windows."""

        if self.sessions <= 0:
            return None
        return self.failures / self.sessions

    def as_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "model": self.model,
            "sessions": self.sessions,
            "failures": self.failures,
            "failure_rate": self.failure_rate,
            "counts": dict(self.counts),
        }


def failure_rates(sessions: tuple[SessionCost, ...]) -> tuple[FailureRate, ...]:
    """Worker-failure counts per backend and model, in first-appearance order.

    The denominator is every window the pair ran, not just the failed ones,
    so the rate answers the question an operator actually asks of a backend
    or a model: how often does working through it end badly?
    """

    order: list[tuple[str, str | None]] = []
    grouped: dict[tuple[str, str | None], list[SessionCost]] = {}
    for session in sessions:
        key = (session.backend, session.model)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(session)

    rates: list[FailureRate] = []
    for backend, model in order:
        group = grouped[(backend, model)]
        counts = {failure.value: 0 for failure in WorkerFailure}
        for session in group:
            if session.failure_class is None:
                continue
            # A class this Ortus does not know is still a failure, and an
            # unknown name is itself the harness bug the taxonomy exists to
            # surface, so it lands in that bucket rather than a new key.
            key = (
                session.failure_class
                if session.failure_class in counts
                else WorkerFailure.ORTUS_HARNESS_BUG.value
            )
            counts[key] += 1
        rates.append(
            FailureRate(
                backend=backend,
                model=model,
                sessions=len(group),
                failures=sum(counts.values()),
                counts=counts,
            )
        )
    return tuple(rates)


def find_grind_logs(repo: Path, *, newest: int = 1) -> tuple[Path, ...]:
    """Grind logs under `repo/logs`, newest first; `newest=0` means every one."""

    try:
        candidates = [
            path for path in (repo / "logs").glob(LOG_GLOB) if path.is_file()
        ]
    except OSError:
        return ()
    ordered = sorted(
        candidates,
        key=lambda path: (path.stat().st_mtime, path.name),
        reverse=True,
    )
    return tuple(ordered if newest <= 0 else ordered[:newest])
