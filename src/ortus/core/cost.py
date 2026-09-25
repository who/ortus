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
* Grok ``usage`` — Claude's field names, and measurement says Claude's rule
  too: ``input_tokens`` counts only the uncached input, with
  ``cache_read_input_tokens`` beside it rather than inside it.

Normalizing those into one set of buckets is the whole point: an operator
comparing a judge-gated run against a plain one must be comparing the same
quantity on both sides.

Every bucket is ``None`` until a provider reports it. A turn that omits a field
leaves that bucket unset rather than zero and marks its record
``partial_usage``; a Codex turn that reports a total input without the cached
split is left unsplit rather than guessed.

Claude says what a window cost twice over, and both readings are needed. The
``result`` event carries the session totals and ``total_cost_usd``, and wins
wherever it exists. But the harness reaps a Claude worker the moment its bead
is closed and pushed — the ``/goal`` Stop hook would otherwise hold the session
open indefinitely — so a window that did its job never writes that event, which
is why whole runs of them used to report null usage and null dollars. Each
``assistant`` event also carries its own message's usage, repeated on every
content block of that message, so deduplicating by message id and summing
across ids rebuilds the window's input buckets exactly; only the output count
comes out low, because each message reports what it had produced so far.

Dollars are the provider's own number whenever one exists
(``total_cost_usd``, OpenCode ``cost``) and the row says ``cost_source:
provider``. A Claude window with buckets but no provider figure is weighted by
the versioned price table here and says ``estimated`` instead; a model the
table does not name keeps null dollars and marks the row partial rather than
inventing a price. Codex and Grok report no dollars of their own and are not
priced here, so their rows are unchanged.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Sequence

from ortus.core.profiles import Phase
from ortus.core.worker_failure import (
    FAILURE_LINE,
    PROVIDER_DEFAULT,
    WorkerFailure,
)

#: Same glob the tail and dashboard surfaces use to find run logs.
LOG_GLOB = "grind-*.log"

#: `ortus plan` writes one of these per planning session, beside the run logs.
PLAN_LOG_GLOB = "plan-*.log"

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
#: The marker line renders `Profile.display_name`, which spells the phase as
#: its declared value, so the table this keys is read with that same value.
_WORKER_PHASE = Phase.IMPLEMENT.value


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


#: What a row's `cost_source` says about where its dollars came from.
COST_PROVIDER = "provider"
COST_ESTIMATED = "estimated"

#: The revision of the price table below. Prices change, and a dollar figure
#: weighted by a table that has since moved must stay readable as such, so the
#: version travels with the report rather than living only in this file.
PRICE_TABLE_VERSION = "2026-06-24"


@dataclass(frozen=True)
class ModelPrice:
    """USD per million tokens, bucket by bucket, for one model family."""

    input_usd: float
    cache_write_usd: float
    cache_read_usd: float
    output_usd: float


#: Published per-million input and output prices, with the two cache buckets
#: derived from the documented multipliers: a cache read costs 0.1x the input
#: price (0.025x on Claude Fable 5.1) and a cache write costs 2x it at the
#: one-hour TTL the worker windows weighted here are written under. A family
#: this table does not name is not priced by guesswork — its window keeps null
#: dollars and says so.
PRICES: dict[str, ModelPrice] = {
    "claude-fable-5-1": ModelPrice(10.0, 20.0, 0.25, 50.0),
    "claude-fable-5": ModelPrice(10.0, 20.0, 1.0, 50.0),
    "claude-opus-5": ModelPrice(5.0, 10.0, 0.5, 25.0),
    "claude-opus-4-8": ModelPrice(5.0, 10.0, 0.5, 25.0),
    "claude-opus-4-7": ModelPrice(5.0, 10.0, 0.5, 25.0),
    "claude-opus-4-6": ModelPrice(5.0, 10.0, 0.5, 25.0),
    "claude-sonnet-5": ModelPrice(2.0, 4.0, 0.2, 10.0),
    "claude-sonnet-4-6": ModelPrice(3.0, 6.0, 0.3, 15.0),
    "claude-haiku-4-5": ModelPrice(1.0, 2.0, 0.1, 5.0),
}


def model_price(model: str | None) -> ModelPrice | None:
    """The price row for a model id, or None when this table does not name it.

    The id on a window is whatever the CLI printed, which may carry a context
    marker (``claude-opus-5[1m]``) or a dated snapshot suffix. Both name the
    same priced family, so the longest table key the id begins with wins. A
    stream placeholder such as ``<synthetic>`` matches nothing and stays
    unpriced, which is the whole point of refusing a prefix-free fallback.
    """

    if not model:
        return None
    name = model.split("[", 1)[0].strip().lower()
    best: str | None = None
    for key in PRICES:
        if name.startswith(key) and (best is None or len(key) > len(best)):
            best = key
    return None if best is None else PRICES[best]


def estimate_cost(usage: UsageBuckets, model: str | None) -> float | None:
    """What this model's published prices make these buckets worth, or None.

    Only reported buckets are weighted, so an estimate over an incomplete
    record is a floor rather than an invention. That matters for the record
    this exists to price: a reaped window's per-message reconstruction matches
    the provider's own input buckets exactly, while its output count is what
    each message reported while it was still streaming and runs well under the
    session total.
    """

    price = model_price(model)
    if price is None:
        return None
    weighted = (
        (usage.uncached_input_tokens or 0) * price.input_usd
        + (usage.cache_write_tokens or 0) * price.cache_write_usd
        + (usage.cached_input_tokens or 0) * price.cache_read_usd
        + (usage.output_tokens or 0) * price.output_usd
    )
    return weighted / 1_000_000


def _largest(left: UsageBuckets, right: UsageBuckets) -> UsageBuckets:
    """Bucket-wise maximum of two readings of one message's usage.

    The CLI repeats a message's usage block on every content event it streams,
    and the counts only ever grow within a message, so the largest reading is
    the message's own bill and summing the repeats would multiply it.
    """

    def pick(one: float | None, other: float | None) -> Any:
        if one is None:
            return other
        if other is None:
            return one
        return max(one, other)

    return UsageBuckets(
        output_tokens=pick(left.output_tokens, right.output_tokens),
        uncached_input_tokens=pick(
            left.uncached_input_tokens, right.uncached_input_tokens
        ),
        cached_input_tokens=pick(left.cached_input_tokens, right.cached_input_tokens),
        cache_write_tokens=pick(left.cache_write_tokens, right.cache_write_tokens),
        reasoning_tokens=pick(left.reasoning_tokens, right.reasoning_tokens),
        cost_usd=pick(left.cost_usd, right.cost_usd),
    )


def _model_name(value: Any) -> str | None:
    """A model id, with the stream's ``<synthetic>`` placeholder read as unset."""

    name = _as_text(value)
    if name is None or (name.startswith("<") and name.endswith(">")):
        return None
    return name


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
    #: The assistant message this usage belongs to. Set only by the per-message
    #: path, whose readings are deduplicated by id rather than summed.
    message_id: str | None = None
    #: This usage is the provider's total for the whole window, so it stands in
    #: for every per-message reading rather than adding to them.
    window_total: bool = False


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
    if kind == "assistant":
        return _claude_message_facts(obj)
    if kind != "result":
        return None
    subtype = obj.get("subtype")
    common: dict[str, Any] = {
        "turns": _as_int(obj.get("num_turns")),
        "session_id": _as_text(obj.get("session_id")),
        "errors": 1 if obj.get("is_error") is True else 0,
        "aborted": bool(subtype) and subtype != "success",
        "window_total": True,
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


def _claude_message_facts(obj: dict[str, Any]) -> EventFacts | None:
    """One assistant message's own usage, keyed by the message id.

    This is the only billing a reaped window ever reports: the harness kills a
    worker as soon as its bead is closed and pushed, so the `result` event that
    carries the session totals is never written. The per-message blocks that
    did arrive are enough to rebuild the input buckets exactly.

    A message with no usage block contributes nothing rather than an empty
    reading, and a message with no id is skipped: without one there is no way
    to tell a repeat of one message from a second message, and guessing either
    way would double count or drop a real bill.
    """

    message = obj.get("message")
    if not isinstance(message, dict):
        return None
    message_id = _as_text(message.get("id"))
    usage = message.get("usage")
    if message_id is None or not isinstance(usage, dict):
        return None
    return EventFacts(
        usage=UsageBuckets(
            output_tokens=_as_int(usage.get("output_tokens")),
            uncached_input_tokens=_as_int(usage.get("input_tokens")),
            cached_input_tokens=_as_int(usage.get("cache_read_input_tokens")),
            cache_write_tokens=_as_int(usage.get("cache_creation_input_tokens")),
        ),
        message_id=message_id,
        model=_model_name(message.get("model")),
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


def _grok_facts(obj: dict[str, Any]) -> EventFacts | None:
    """Decode one Grok ``usage`` event.

    Grok borrows Claude's field names, and measurement says it borrows Claude's
    inclusion rule with them: across the Grok streams already on disk, most
    events report an ``input_tokens`` *below* their own
    ``cache_read_input_tokens`` (462 against 27520 in one turn), which cannot
    happen if the cached read sits inside the total. So ``input_tokens`` is the
    uncached bucket as it stands, with no subtraction to do.

    Each event bills one assistant turn rather than the session to date — the
    output count rises and falls from event to event instead of climbing — so
    the window sums them.
    """

    if obj.get("type") != "usage":
        return None
    usage = obj.get("usage")
    if not isinstance(usage, dict):
        return EventFacts(turn_delta=1, partial=True)
    output_tokens = _as_int(usage.get("output_tokens"))
    uncached = _as_int(usage.get("input_tokens"))
    cached = _as_int(usage.get("cache_read_input_tokens"))
    return EventFacts(
        usage=UsageBuckets(
            output_tokens=output_tokens,
            uncached_input_tokens=uncached,
            cached_input_tokens=cached,
            cache_write_tokens=_as_int(usage.get("cache_creation_input_tokens")),
            reasoning_tokens=_as_int(usage.get("reasoning_tokens")),
        ),
        partial=any(value is None for value in (output_tokens, uncached, cached)),
        turn_delta=1,
    )


def event_facts(obj: Any) -> EventFacts | None:
    """Decode one worker-stream event, whichever backend wrote it.

    The four decoders key off disjoint ``type`` values, so the first one to
    claim an event owns it and order carries no meaning.
    """

    if not isinstance(obj, dict):
        return None
    for decode in (_claude_facts, _codex_facts, _opencode_facts, _grok_facts):
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
    #: Where this row's dollars came from: the provider's own figure, this
    #: module's price table, or nowhere at all.
    cost_source: str | None = None
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
            "cost_source": self.cost_source,
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
    #: `estimated` when any window behind this bead was priced here rather than
    #: by its provider, so a bead's dollars are never read as surer than they are.
    cost_source: str | None = None
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
            "cost_source": self.cost_source,
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
        #: Each assistant message's own usage, largest reading per id.
        self.message_usage: dict[str, UsageBuckets] = {}
        #: The provider's total for the window, when it lived long enough to say.
        self.result_usage: UsageBuckets | None = None
        #: This window's billing came from the Claude stream, so the price table
        #: in this module is the right one to weight it with.
        self.claude_billed = False
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
        if facts.message_id is not None:
            self.claude_billed = True
            if facts.usage is not None:
                previous = self.message_usage.get(facts.message_id)
                self.message_usage[facts.message_id] = (
                    facts.usage if previous is None else _largest(previous, facts.usage)
                )
                self.saw_usage = True
            if facts.session_id and self.session_id is None:
                self.session_id = facts.session_id
            # The init banner and the profile marker both name the model on
            # purpose; a message only fills the gap when neither did.
            if facts.model and self.model is None:
                self.model = facts.model
            return
        if facts.window_total:
            self.claude_billed = True
            if facts.usage is not None:
                self.result_usage = (
                    facts.usage
                    if self.result_usage is None
                    else self.result_usage.merged(facts.usage)
                )
                self.saw_usage = True
        elif facts.usage is not None:
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

    def billed(self) -> tuple[UsageBuckets, str | None, bool]:
        """This window's buckets, where its dollars came from, and whether the
        billing is only partly known.

        The provider's own window total wins whenever the window lived long
        enough to emit one, so a normal window is billed exactly as before and
        nothing is counted twice. A window that was reaped mid-session falls
        back to the per-message reconstruction, and its dollars come from the
        price table because no provider figure exists to prefer.
        """

        partial = self.partial_usage
        if self.result_usage is not None:
            usage = self.result_usage
        elif self.message_usage:
            usage = UsageBuckets()
            for buckets in self.message_usage.values():
                usage = usage.merged(buckets)
        else:
            usage = self.usage
        if usage.cost_usd is not None:
            return usage, COST_PROVIDER, partial
        if not self.claude_billed or usage.is_empty:
            return usage, None, partial
        dollars = estimate_cost(usage, self.model)
        if dollars is None:
            # An unpriced model is the one case where tokens are known and
            # dollars cannot be: the row says so rather than reading as free.
            return usage, None, True
        return replace(usage, cost_usd=dollars), COST_ESTIMATED, partial

    def freeze(self) -> SessionCost:
        turns = self.reported_turns
        if turns is None:
            turns = self.counted_turns or None
        wall = None
        if self.started is not None and self.last_stamp is not None:
            wall = max((self.last_stamp - self.started).total_seconds(), 0.0)
        usage, cost_source, partial_usage = self.billed()
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
            partial_usage=partial_usage,
            failure_class=self.failure_class,
            cost_source=cost_source,
            usage=usage,
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
                cost_source=bead_cost_source(group),
                usage=usage,
            )
        )
    return tuple(beads)


def bead_cost_source(sessions: Sequence[SessionCost]) -> str | None:
    """How sure a group's dollars are, taken from its least sure window.

    One estimated window makes the sum an estimate: a reader comparing two
    arms has to know that a bead's figure leans on the price table before
    treating a difference between them as measured.
    """

    sources = {session.cost_source for session in sessions}
    if COST_ESTIMATED in sources:
        return COST_ESTIMATED
    if COST_PROVIDER in sources:
        return COST_PROVIDER
    return None


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


def _find_logs(repo: Path, glob: str, newest: int) -> tuple[Path, ...]:
    """Logs under `repo/logs` matching one glob, newest first."""

    try:
        candidates = [path for path in (repo / "logs").glob(glob) if path.is_file()]
    except OSError:
        return ()
    ordered = sorted(
        candidates,
        key=lambda path: (path.stat().st_mtime, path.name),
        reverse=True,
    )
    return tuple(ordered if newest <= 0 else ordered[:newest])


def find_grind_logs(repo: Path, *, newest: int = 1) -> tuple[Path, ...]:
    """Grind logs under `repo/logs`, newest first; `newest=0` means every one."""

    return _find_logs(repo, LOG_GLOB, newest)


def find_plan_logs(repo: Path, *, newest: int = 0) -> tuple[Path, ...]:
    """Plan logs under `repo/logs`, newest first; `newest=0` means every one.

    A planning session writes one log of its own and carries no harness marker
    lines, so it parses as a single unattributed window — which is exactly what
    it is: one agent, no bead of its own, spending on behalf of every bead it
    went on to create.
    """

    return _find_logs(repo, PLAN_LOG_GLOB, newest)


@dataclass(frozen=True)
class TreeCost:
    """What one repository's logs cost, planner and workers together.

    The A/B comparisons ask what a closed bead costs, and a bead that no
    planner wrote would not exist to close, so a tree that reported only worker
    windows would flatter every arm by the same hidden amount — which is worse
    than a wrong number, because it is invisible.
    """

    planner: tuple[SessionCost, ...] = ()
    workers: tuple[SessionCost, ...] = ()

    @property
    def beads(self) -> tuple[BeadCost, ...]:
        return rollup_beads(self.workers)

    @property
    def closed_beads(self) -> int:
        return sum(1 for bead in self.beads if bead.closed)

    @property
    def planner_usd(self) -> float | None:
        return _sum_dollars(self.planner)

    @property
    def worker_usd(self) -> float | None:
        return _sum_dollars(self.workers)

    @property
    def total_usd(self) -> float | None:
        return _add_dollars(self.planner_usd, self.worker_usd)

    @property
    def usd_per_closed_bead(self) -> float | None:
        """Total spend over beads actually closed, or None when none were.

        Nothing closed is not a zero-cost run: it is a run with no denominator,
        and dividing by it would report the cheapest arm as the one that
        finished nothing.
        """

        total = self.total_usd
        if total is None or self.closed_beads <= 0:
            return None
        return total / self.closed_beads

    def source_counts(self) -> dict[str, int]:
        """How many windows the provider priced, this table priced, and neither."""

        counts = {COST_PROVIDER: 0, COST_ESTIMATED: 0, "unpriced": 0}
        for session in (*self.planner, *self.workers):
            counts[session.cost_source or "unpriced"] += 1
        return counts

    def as_dict(self) -> dict[str, Any]:
        return {
            "price_table_version": PRICE_TABLE_VERSION,
            "planner_sessions": [session.as_dict() for session in self.planner],
            "worker_sessions": [session.as_dict() for session in self.workers],
            "beads": [bead.as_dict() for bead in self.beads],
            "planner_usd": self.planner_usd,
            "worker_usd": self.worker_usd,
            "total_usd": self.total_usd,
            "closed_beads": self.closed_beads,
            "usd_per_closed_bead": self.usd_per_closed_bead,
            "cost_sources": self.source_counts(),
        }


def _add_dollars(left: float | None, right: float | None) -> float | None:
    if left is None:
        return right
    if right is None:
        return left
    return left + right


def _sum_dollars(sessions: Sequence[SessionCost]) -> float | None:
    """These windows' dollars, or None when not one of them was priced."""

    total: float | None = None
    for session in sessions:
        total = _add_dollars(total, session.usage.cost_usd)
    return total


def parse_tree(repo: Path, *, runs: int = 0) -> TreeCost:
    """Every planning and grind log the repository holds, as one rollup.

    `runs` bounds the grind logs the way `ortus cost` already does; the plan
    logs are always read whole, because a planning session that ran before the
    newest grind still paid for the beads that grind is closing.
    """

    planner = tuple(
        session
        for path in find_plan_logs(repo)
        for session in parse_grind_log(path).sessions
    )
    workers = tuple(
        session
        for path in find_grind_logs(repo, newest=runs)
        for session in parse_grind_log(path).sessions
    )
    return TreeCost(planner=planner, workers=workers)
