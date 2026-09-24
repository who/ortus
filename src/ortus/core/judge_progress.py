"""Is the running worker making progress, or repeating itself?

The reaper ends a worker window on two deterministic facts: the done bar and a
claim flagged for a human. A looping worker — re-reading the same file,
re-running the same failing command, restating the same plan — satisfies
neither, so it runs until ``--worker-timeout`` kills it and the run records a
watchdog timeout for a worker that was stuck from its third minute on.

This module supplies the missing signal. Once per interval the watch takes a
bounded, sanitized slice of the worker's own log, sends it to Jev with the
typed facts the harness already holds — elapsed seconds, whether the
integration branch advanced, the claim's tracker status — and reads back one
probability: how likely it is that this worker is looping. That probability is
an opinion. The decision is taken in code, by :func:`should_reap`, over the
window's last samples; there is no confidence floor and no human parking
anywhere in this module, and a reaped worker takes the same post-window path
as any other no-close window.

Two opt-ins arm it, because this is the only part of Ortus that sends worker
transcript text to a provider: ``jev_progress_reaper`` must name a mode other
than off, and ``judge.include_log_tail`` must be true. Shadow is the default
mode and records the decision it would have taken without touching the worker;
only enforce lets a reap reason reach the poll. Every failure — no key, no
SDK, a missed deadline, a malformed answer — is recorded and skips the check,
so a Jev outage can never end a healthy worker early.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence
from uuid import UUID, uuid4

from ortus.core.judge import JudgeConfig
from ortus.core.judge_log import (
    OutcomeStatus, _append, _clean_metadata, _common,
)
from ortus.core.judge_state import StateError, environment_secrets, sanitize_field
from ortus.core.judge_typesafe import (
    API_KEY_ENV, JudgeFailure, _answer, _default_client, _Invalid, _mapping, _number,
)
from ortus.core.user_env import judge_environment

if TYPE_CHECKING:
    from ortus.core.config import Config

#: Progress records live beside the other gate logs but in their own file: the
#: replay reader rejects any record whose event name it does not know, so a new
#: event kind in the decision log would invalidate the log it validates.
PROGRESS_LOG_NAME = "jev-progress.jsonl"

#: `.ortusrc` keys and the environment variable that overrides the mode.
PROGRESS_CONFIG_KEY = "jev_progress_reaper"
PROGRESS_INTERVAL_KEY = "jev_progress_interval_s"
PROGRESS_ENV = "ORTUS_JEV_PROGRESS"

#: Seconds between checks. Long enough that a slow-but-working step is not read
#: as a loop, and short enough to save most of a 5400-second timeout.
DEFAULT_PROGRESS_INTERVAL_S = 300

#: The tail bound the request carries, as lines and as encoded bytes. This is
#: the module's own ceiling rather than `total_bytes_cap`, which budgets the
#: packed issue state that this request does not send.
TAIL_MAX_LINES = 200
TAIL_MAX_BYTES = 16 * 1024

#: Bytes read from the end of the log before any decoding. A stream-json line
#: can be tens of kilobytes, so the line bound alone is not a read bound.
_READ_BYTES = 1 << 20

#: Per-line cap for the summarized tail, and what an unscreenable line becomes.
_LINE_CAP = 240
REDACTED = "[redacted]"

#: The reap reason this module hands the poll, and the question it asks.
JEV_LOOPING_REASON = "jev_looping"
LOOPING_QUESTION = "looping"

#: Consecutive looping samples that decide a reap, and the probability each
#: must exceed. Two because one reading of a transcript slice is a guess about
#: a moment and the pair has to span an interval with no new commits in it.
_REAP_SAMPLES = 2
_LOOPING_FLOOR = 0.5

_LOOPING_TRUE = [
    "The same command runs again with the same result and nothing new is tried.",
    "The worker restates its plan without changing any file.",
    "One error recurs and each attempt at it repeats an earlier attempt.",
]
_LOOPING_FALSE = [
    "Each step differs from the last and moves toward the issue's criteria.",
    "A long check is running and its output is still advancing.",
    "The worker is editing files it has not already edited this window.",
]


class ProgressMode(str, Enum):
    """How much the progress signal is allowed to do."""

    #: No checks, no records, no cost: today's reaper, byte for byte.
    OFF = "off"
    #: Read the vector and record what it would have done. Never reaps.
    SHADOW = "shadow"
    #: Let the decision reach the poll and end a looping worker.
    ENFORCE = "enforce"


class WindowOutcome(str, Enum):
    """How the worker window the checks watched actually ended."""

    CLOSED = "closed"
    TIMEOUT = "timeout"
    REAPED = "reaped"
    EXITED = "exited"


def progress_mode(
    config: Config | None = None, *, environ: Mapping[str, str] | None = None
) -> ProgressMode:
    """The mode this run watches workers in.

    The environment wins over `.ortusrc` so one run can be flipped without
    editing a tracked file. A value neither layer can parse never enables the
    reaper: an unreadable export falls back to the configured mode, and an
    unreadable configured mode falls back to shadow, which only records.
    """
    env = os.environ if environ is None else environ
    for value in (env.get(PROGRESS_ENV), _configured(config, PROGRESS_CONFIG_KEY)):
        if isinstance(value, str):
            try:
                return ProgressMode(value.strip().lower())
            except ValueError:
                continue
    return ProgressMode.SHADOW


def progress_interval(config: Config | None = None) -> float:
    """Seconds between checks, falling back to the default for any other value."""
    value = _configured(config, PROGRESS_INTERVAL_KEY)
    if type(value) in (int, float) and float(value) > 0:
        return float(value)
    return float(DEFAULT_PROGRESS_INTERVAL_S)


def _configured(config: Config | None, key: str) -> object:
    getter = getattr(config, "get", None)
    return getter(key) if callable(getter) else None


@dataclass(frozen=True)
class WorkerProgress:
    """The typed facts the harness already knows about this worker window."""

    elapsed_seconds: float
    head_advanced: bool
    bead_status: OutcomeStatus

    def __post_init__(self) -> None:
        if (type(self.elapsed_seconds) not in (int, float)
                or self.elapsed_seconds < 0
                or type(self.head_advanced) is not bool
                or not isinstance(self.bead_status, OutcomeStatus)):
            raise ValueError("invalid worker progress facts")

    def payload(self) -> dict:
        """The facts as they travel: no paths, no ids, no transcript text."""
        return {
            "elapsed_seconds": round(float(self.elapsed_seconds), 1),
            "head_advanced": self.head_advanced,
            "bead_status": self.bead_status.value,
        }


@dataclass(frozen=True)
class ProgressVerdict:
    """One reading of the window, or the typed reason there is none."""

    p_looping: float | None = None
    p_progressing: float | None = None
    confidence: float | None = None
    failure: JudgeFailure | None = None

    def __post_init__(self) -> None:
        if self.failure is not None:
            if (not isinstance(self.failure, JudgeFailure)
                    or self.p_looping is not None
                    or self.p_progressing is not None
                    or self.confidence is not None):
                raise ValueError("invalid progress failure")
            return
        for value in (self.p_looping, self.p_progressing, self.confidence):
            if type(value) not in (int, float) or not 0.0 <= float(value) <= 1.0:
                raise ValueError("invalid progress vector")

    @property
    def shrunk_looping(self) -> float | None:
        """:attr:`p_looping` pulled toward the coin flip by its confidence."""
        if self.failure is not None:
            return None
        return _shrunk(float(self.p_looping or 0.0), float(self.confidence or 0.0))


def _shrunk(probability: float, confidence: float) -> float:
    """Pull a probability toward the coin flip by how unsure the judge was.

    A reading reported with full confidence passes through unchanged; one
    reported with no confidence carries no information and lands on 0.5, so an
    unsure judge cannot argue for ending a worker.

    A noul reports no separate confidence, so the confidence here is derived
    from the probability itself, exactly as the pre-turn adapter derives it,
    and shrinking partly re-applies that value. The shrink is kept anyway: it
    is what the recorded and compared number is, so the rule stays correct if
    this question ever gains a confidence the provider reports.
    """
    p = min(max(probability, 0.0), 1.0)
    c = min(max(confidence, 0.0), 1.0)
    return 0.5 + (p - 0.5) * c


def _reading(looping: float) -> ProgressVerdict:
    """A noul answer as a vector: its complement, and its own distance from .5."""
    return ProgressVerdict(
        p_looping=looping,
        p_progressing=1.0 - looping,
        confidence=max(looping, 1.0 - looping),
    )


@dataclass(frozen=True)
class ProgressSample:
    """What one interval read, and the commit the repository was on for it."""

    shrunk_looping: float | None
    head_oid: str


def should_reap(history: Sequence[ProgressSample]) -> bool:
    """Whether the window's readings argue for ending this worker now.

    System Two, and the whole decision: the last two consecutive readings must
    both exceed the floor and the integration branch must not have advanced
    between them. A failed check has no reading and breaks the run, so a Jev
    outage resets the evidence instead of accumulating it, and a head that
    could not be read is not a head that stood still.
    """
    recent = list(history)[-_REAP_SAMPLES:]
    if len(recent) < _REAP_SAMPLES:
        return False
    if any(sample.shrunk_looping is None
           or sample.shrunk_looping <= _LOOPING_FLOOR for sample in recent):
        return False
    return bool(recent[0].head_oid) and len({s.head_oid for s in recent}) == 1


def build_progress_questions() -> dict:
    """The one question asked, as a System One request mapping."""
    return {LOOPING_QUESTION: {
        "type": "noul",
        "instructions": "Is this worker repeating itself without making progress?",
        "criteria": {"true": list(_LOOPING_TRUE), "false": list(_LOOPING_FALSE)},
    }}


def read_tail(log_path: Path, *, start_offset: int = 0) -> list[str]:
    """This window's own log lines, newest last, bounded before any decoding.

    The read starts at the window's offset into a log earlier windows also
    wrote, and never more than :data:`_READ_BYTES` from the end. A line the
    byte bound cut in half is dropped rather than parsed.
    """
    try:
        with open(log_path, "rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            begin = max(int(start_offset), size - _READ_BYTES, 0)
            if begin >= size:
                return []
            stream.seek(begin)
            raw = stream.read(size - begin)
    except OSError:
        return []
    lines = raw.decode("utf-8", errors="replace").splitlines()
    if begin > int(start_offset) and lines:
        lines = lines[1:]
    return lines[-TAIL_MAX_LINES:]


def _json_object(line: str) -> Mapping[str, Any] | None:
    try:
        parsed = json.loads(line)
    except ValueError:
        return None
    return parsed if isinstance(parsed, Mapping) else None


def _summarize(line: str) -> str:
    """One log line as the short signature a loop would repeat.

    A stream-json line carries a whole assistant turn and its usage counts; a
    plain harness line carries its own text. Both collapse to a kind, the tool
    names in the turn and its prose, whitespace-flattened and capped, which is
    what makes repetition legible in the space a request has for it. A format
    this cannot read falls through to its raw text.
    """
    text = line.strip()
    if not text:
        return ""
    obj = _json_object(text)
    if obj is None:
        return text[:_LINE_CAP]
    kind = obj.get("type") or obj.get("event") or obj.get("role") or "event"
    body = obj.get("message") if isinstance(obj.get("message"), Mapping) else obj
    parts: list[str] = []
    content = body.get("content")
    for item in content if isinstance(content, list) else ():
        if not isinstance(item, Mapping):
            continue
        name = item.get("name")
        if isinstance(name, str) and name.strip():
            parts.append(f"tool:{name.strip()}")
        for key in ("text", "thinking", "content"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value)
    for value in (content if isinstance(content, str) else None, obj.get("text")):
        if isinstance(value, str) and value.strip():
            parts.append(value)
    joined = " ".join(" ".join(part.split()) for part in parts)
    kind = " ".join(str(kind).split())
    return (f"{kind}: {joined}" if joined else kind)[:_LINE_CAP]


def _screen(text: str, config: JudgeConfig, secrets: tuple[str, ...]) -> str:
    """One summarized line, or the placeholder when it cannot travel."""
    try:
        screened = sanitize_field(
            text, cap=_LINE_CAP, secret_values=secrets,
            sensitive_paths=config.sensitive_paths,
        )
    except StateError:
        return REDACTED
    return REDACTED if screened.value is None else screened.value


def bounded_tail(
    lines: Sequence[str], config: JudgeConfig,
    *, environ: Mapping[str, str] | None = None,
) -> str:
    """The lines as one readable block, screened line by line and bounded.

    The whole-field sanitizer omits any value carrying a credential, a key
    file or an address, which for a transcript would omit the transcript. Per
    line it keeps the signal instead: an offending line becomes the
    placeholder and its neighbours still travel. The newest lines are the ones
    kept when the byte bound bites, because the loop to detect is the one
    happening now.
    """
    secrets = environment_secrets(environ)
    rendered = [
        _screen(summary, config, secrets)
        for summary in (_summarize(line) for line in lines[-TAIL_MAX_LINES:])
        if summary
    ]
    kept: list[str] = []
    budget = TAIL_MAX_BYTES
    for line in reversed(rendered):
        cost = len(line.encode("utf-8")) + 1
        if cost > budget:
            break
        budget -= cost
        kept.append(line)
    return "\n".join(reversed(kept))


def evaluate_progress(
    tail: str, facts: WorkerProgress, config: JudgeConfig,
    *, environ: Mapping[str, str] | None = None,
    client_factory: Callable[[JudgeConfig], Any] = _default_client,
) -> ProgressVerdict:
    """Ask one bounded noul question about a bounded tail, or name the failure.

    The caller bounds and screens the tail; this refuses one that is not
    bounded rather than sending it. The request carries no issue packet, so no
    issue prose travels with it and its whole payload is the tail plus the
    typed facts.
    """
    if not isinstance(tail, str) or not isinstance(facts, WorkerProgress):
        raise StateError("invalid progress request")
    if len(tail.encode("utf-8")) > TAIL_MAX_BYTES:
        raise StateError("progress tail exceeds the bound")
    environ = judge_environment(environ)
    payload = {"worker": facts.payload(), "log_tail": tail}
    env = os.environ if environ is None else environ
    if not str(env.get(API_KEY_ENV, "")).strip():
        return ProgressVerdict(failure=JudgeFailure.KEY_MISSING)

    async def ask() -> object:
        async with AsyncExitStack() as stack:
            client = client_factory(config)
            if hasattr(client, "__aenter__"):
                client = await stack.enter_async_context(client)
            elif hasattr(client, "aclose"):
                stack.push_async_callback(client.aclose)
            return await client.system_one(
                payload, build_progress_questions(),
                model=config.model, timeout=config.timeout_seconds,
            )

    async def bounded() -> object:
        return await asyncio.wait_for(ask(), config.timeout_seconds)

    try:
        response = asyncio.run(bounded())
    except ImportError:
        return ProgressVerdict(failure=JudgeFailure.SDK_MISSING)
    except asyncio.TimeoutError:
        return ProgressVerdict(failure=JudgeFailure.TIMEOUT)
    except Exception:  # noqa: BLE001 - provider prose never escapes
        return ProgressVerdict(failure=JudgeFailure.SERVICE_ERROR)
    try:
        dump = getattr(response, "model_dump", None)
        body = _mapping(dump(mode="json") if callable(dump) else response)
        if body.get("model") != config.model:
            raise _Invalid
        answers = _mapping(body.get("answers"))
        if set(answers) != {LOOPING_QUESTION}:
            raise _Invalid
        return _reading(
            _number(_answer(answers, LOOPING_QUESTION, "noul").get("noul"), 1.0)
        )
    except Exception:  # noqa: BLE001 - a malformed answer is a typed failure
        return ProgressVerdict(failure=JudgeFailure.INVALID_ANSWER)


@dataclass(frozen=True)
class ProgressCheck:
    """One interval's record: the vector, the facts, and what was done."""

    run_id: UUID
    decision_id: UUID
    issue_id: str
    mode: ProgressMode
    facts: WorkerProgress
    verdict: ProgressVerdict
    would_reap: bool
    reaped: bool


@dataclass(frozen=True)
class ProgressWindow:
    """How the watched window ended, joined to its checks by ``run_id``."""

    run_id: UUID
    decision_id: UUID
    issue_id: str
    mode: ProgressMode
    outcome: WindowOutcome
    checks: int
    would_reap: int
    reaped: bool


def log_progress_check(
    repo: Path, config: JudgeConfig, event: ProgressCheck,
    *, environ: Mapping[str, str] | None = None,
) -> None:
    """Append one check beside the phase records, vector and failure alike.

    A shadow record carries the decision that was not taken, which is the
    whole point of the mode: ``would_reap`` is what the rule said and
    ``reaped`` is what happened to the worker.
    """
    payload = _common("progress", event.run_id, event.decision_id)
    verdict = event.verdict
    payload.update({
        "phase": "progress", "mode": event.mode.value,
        "issue_id": _clean_metadata(event.issue_id, config, environ),
        "seat": _clean_metadata(config.seat, config, environ),
        "model": config.model,
        **event.facts.payload(),
        "vector": {
            "looping": verdict.p_looping, "progressing": verdict.p_progressing,
        },
        "confidence": verdict.confidence,
        "shrunk_looping": verdict.shrunk_looping,
        "failure": verdict.failure.value if verdict.failure is not None else None,
        "would_reap": event.would_reap,
        "reaped": event.reaped,
    })
    _append(repo, payload, name=PROGRESS_LOG_NAME)


def log_progress_window(
    repo: Path, config: JudgeConfig, event: ProgressWindow,
    *, environ: Mapping[str, str] | None = None,
) -> None:
    """Append the window's fate, so a shadow reading can be scored against it."""
    payload = _common("progress_window", event.run_id, event.decision_id)
    payload.update({
        "phase": "progress", "mode": event.mode.value,
        "issue_id": _clean_metadata(event.issue_id, config, environ),
        "seat": _clean_metadata(config.seat, config, environ),
        "outcome": event.outcome.value,
        "checks": event.checks,
        "would_reap": event.would_reap,
        "reaped": event.reaped,
    })
    _append(repo, payload, name=PROGRESS_LOG_NAME)


@dataclass
class ProgressWatch:
    """One worker window's progress signal: the interval, the samples, the log.

    Built by the caller that owns the reap poll and asked on every poll, so the
    interval gate is a clock read and the Jev request happens at most once per
    interval, bounded by ``judge.timeout_seconds``. The tracker and repository
    are reached through callables that answer with a default rather than
    raising: an unanswered fact must never end a live worker.
    """

    repo: Path
    config: JudgeConfig
    mode: ProgressMode
    run_id: UUID
    issue_id: str
    log_path: Path
    head_oid: Callable[[], str]
    bead_status: Callable[[], OutcomeStatus]
    write_log: Callable[[str], None]
    interval: float = DEFAULT_PROGRESS_INTERVAL_S
    start_offset: int = 0
    clock: Callable[[], float] = time.monotonic
    client_factory: Callable[[JudgeConfig], Any] = _default_client
    environ: Mapping[str, str] | None = None
    decision_id: UUID = field(default_factory=uuid4)
    samples: list[ProgressSample] = field(default_factory=list)
    checks: int = 0
    would_reap: int = 0
    reaped: bool = False
    started: float = field(init=False, default=0.0)
    last_check: float = field(init=False, default=0.0)
    start_head: str = field(init=False, default="")

    def __post_init__(self) -> None:
        self.started = self.clock()
        self.last_check = self.started
        self.start_head = self._head()

    def reason(self) -> str | None:
        """Why this worker should be reaped for looping now, or None.

        Shadow records and returns None however the vector reads, and a window
        that has already produced a reap reason is not asked again.
        """
        if self.reaped or self.mode is ProgressMode.OFF:
            return None
        now = self.clock()
        if now - self.last_check < self.interval:
            return None
        self.last_check = now
        try:
            return self._check(now)
        except Exception as exc:  # noqa: BLE001 - a poll never ends a worker
            self.write_log(
                f"jev progress: check failed ({exc}); the worker keeps its window"
            )
            return None

    def _check(self, now: float) -> str | None:
        lines = read_tail(self.log_path, start_offset=self.start_offset)
        tail = bounded_tail(lines, self.config, environ=self.environ) if lines else ""
        if not tail:
            self.write_log("jev progress: no readable worker log yet; check skipped")
            return None
        head = self._head()
        facts = WorkerProgress(
            elapsed_seconds=max(now - self.started, 0.0),
            head_advanced=bool(head and self.start_head and head != self.start_head),
            bead_status=self._status(),
        )
        verdict = evaluate_progress(
            tail, facts, self.config,
            environ=self.environ, client_factory=self.client_factory,
        )
        self.checks += 1
        self.samples.append(ProgressSample(verdict.shrunk_looping, head))
        argued = should_reap(self.samples)
        self.would_reap += int(argued)
        self.reaped = argued and self.mode is ProgressMode.ENFORCE
        self._record(facts, verdict, argued)
        if verdict.failure is not None:
            self.write_log(
                f"jev progress: {verdict.failure.value}; check skipped "
                "and the worker keeps its window"
            )
            return None
        self.write_log(
            f"jev progress check {self.checks} on {self.issue_id}: "
            f"looping={verdict.shrunk_looping:.3f} "
            f"advanced={facts.head_advanced} would_reap={argued} "
            f"mode={self.mode.value}"
        )
        if not self.reaped:
            return None
        return (
            f"{JEV_LOOPING_REASON} (looping={verdict.shrunk_looping:.2f} over "
            f"{_REAP_SAMPLES} checks with no new commits)"
        )

    def record_window(self, outcome: WindowOutcome) -> None:
        """Append how the window ended, once, for the checks it actually made."""
        if not self.checks:
            return
        try:
            log_progress_window(self.repo, self.config, ProgressWindow(
                run_id=self.run_id, decision_id=self.decision_id,
                issue_id=self.issue_id, mode=self.mode, outcome=outcome,
                checks=self.checks, would_reap=self.would_reap,
                reaped=self.reaped,
            ), environ=self.environ)
        except Exception as exc:  # noqa: BLE001 - a log is not worth a run
            self.write_log(
                f"jev progress: could not record the window outcome ({exc})"
            )

    def _record(
        self, facts: WorkerProgress, verdict: ProgressVerdict, argued: bool,
    ) -> None:
        try:
            log_progress_check(self.repo, self.config, ProgressCheck(
                run_id=self.run_id, decision_id=self.decision_id,
                issue_id=self.issue_id, mode=self.mode, facts=facts,
                verdict=verdict, would_reap=argued, reaped=self.reaped,
            ), environ=self.environ)
        except Exception as exc:  # noqa: BLE001 - the decision still stands
            self.write_log(f"jev progress: could not record the check ({exc})")

    def _head(self) -> str:
        try:
            value = self.head_oid()
        except Exception:  # noqa: BLE001 - an unread head is not an advance
            return ""
        return value if isinstance(value, str) else ""

    def _status(self) -> OutcomeStatus:
        try:
            status = self.bead_status()
        except Exception:  # noqa: BLE001 - an unread status is unknown
            return OutcomeStatus.UNKNOWN
        return status if isinstance(status, OutcomeStatus) else OutcomeStatus.UNKNOWN
