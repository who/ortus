"""The injected-context inventory and the cache-friendly prompt ordering.

Every grind worker receives one prompt string that Ortus assembles from
several segments. Some of those bytes are identical for every issue in a run
— the goal pointer, the implementation instruction, the CodeGraph phase
contract, the prototype verification section, the stored prior lessons — and
some change with the claimed bead: the readiness advice vector and the Bound
issue contract. A provider prefix cache can only serve the run of bytes that
is byte-identical across requests, so a per-bead segment sitting in the
middle of the prompt truncates the cacheable prefix at that point no matter
how much stable text follows it.

:data:`INJECTED_CONTEXT` is the inventory: one entry per segment, each naming
the module and symbol that produces it and whether it is stable across beads
or per-bead. :func:`resolve_segment` loads what an entry names, so the
inventory cannot quietly drift away from the code it describes.

The reordering itself is a flag, `stable_prompt_prefix` in `.ortusrc` with
`ORTUS_STABLE_PREFIX` as a per-run override. It defaults on, because the arms
were run and the reordered one won; off composes the legacy ordering byte for
byte, which is both the kill switch and the control arm the comparison ran
against. The win was read from the billing buckets named in
:data:`CACHE_TELEMETRY_FIELDS` together with cost per closed bead, never from
prompt character count: a shorter prompt that misses the cache costs more than
a longer one that hits it.

Ortus only owns the ordering and the content of the string it hands to a
backend CLI. Whether those bytes are actually cached is the provider's
decision, made inside the `claude` / `codex` process.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from importlib import import_module
from typing import Any, Mapping

#: `.ortusrc` key and environment variable that select the ordering.
STABLE_PREFIX_CONFIG_KEY = "stable_prompt_prefix"
STABLE_PREFIX_ENV = "ORTUS_STABLE_PREFIX"

#: How a segment behaves across the beads of one run.
STABLE = "stable"
PER_BEAD = "per-bead"

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSEY = frozenset({"0", "false", "no", "off", ""})


@dataclass(frozen=True)
class InjectedSegment:
    """One segment of a composed worker prompt.

    ``entry_point`` is the ``module:attribute`` that produces the segment, so
    a reader of the inventory can go straight to the code rather than
    searching the prompt for the wording. ``stability`` is :data:`STABLE`
    when every bead in a run receives the same bytes and :data:`PER_BEAD`
    when the bytes carry the claimed issue.
    """

    name: str
    entry_point: str
    stability: str
    note: str


#: Everything grind injects into a worker prompt, in no particular order —
#: the ordering is `_compose_work_prompt`'s, and this is the map of what it
#: is ordering. Tests hold every entry point to resolving and every
#: stability value to one of the two constants above.
INJECTED_CONTEXT: tuple[InjectedSegment, ...] = (
    InjectedSegment(
        name="implementation instruction",
        entry_point="ortus.commands.grind:_IMPLEMENTATION_INSTRUCTION",
        stability=STABLE,
        note="Phase rules and the commit-message contract; a module constant.",
    ),
    InjectedSegment(
        name="goal pointer",
        entry_point="ortus.commands.grind:_GOAL_POINTER",
        stability=STABLE,
        note="The /goal condition; the loop body itself is fetched by the worker.",
    ),
    InjectedSegment(
        name="prototype goal pointer",
        entry_point="ortus.commands.grind:_PROTOTYPE_GOAL_POINTER",
        stability=STABLE,
        note="The same pointer under the prototype bar; chosen once per run.",
    ),
    InjectedSegment(
        name="CodeGraph phase contract",
        entry_point="ortus.core.codegraph:phase_contract",
        stability=STABLE,
        note="Policy plus the outer probe result, both resolved once per run.",
    ),
    InjectedSegment(
        name="prototype verification section",
        entry_point="ortus.commands.grind:_prototype_verification_section",
        stability=STABLE,
        note="The run's lint and syntax gate, resolved from .ortusrc once.",
    ),
    InjectedSegment(
        name="prior lessons",
        entry_point="ortus.commands.grind:_lessons_section",
        stability=STABLE,
        note="Crew lessons read from the tracker; selection ignores the bead.",
    ),
    InjectedSegment(
        name="backend wrapper note",
        entry_point="ortus.core.agent:compose_worker_prompt",
        stability=STABLE,
        note="The backend's own framing, appended last by the wrapper.",
    ),
    InjectedSegment(
        name="readiness advice",
        entry_point="ortus.core.judge_readiness:readiness_context",
        stability=PER_BEAD,
        note="A JSON vector describing the claimed issue's work spec.",
    ),
    InjectedSegment(
        name="bound issue contract",
        entry_point="ortus.core.judge_claim:bound_issue_section",
        stability=PER_BEAD,
        note="The claimed id as a JSON string, under an enforced judge gate.",
    ),
    InjectedSegment(
        name="work-issue template",
        entry_point="ortus.core.grind_loop:read_work_issue_condition",
        stability=STABLE,
        note="The template body carrying the two substitution placeholders.",
    ),
    InjectedSegment(
        name="issue id substitution",
        entry_point="ortus.core.grind_loop:inject_issue",
        stability=PER_BEAD,
        note="Fills the template's id and details placeholders for one bead.",
    ),
    InjectedSegment(
        name="full issue packet",
        entry_point="ortus.core.grind_loop:format_issue_details",
        stability=PER_BEAD,
        note=(
            "Inlines description, design and acceptance verbatim. The slim "
            "path replaces it with slim_issue_details."
        ),
    ),
)

#: Openings of the per-bead segments, used to find where a composed prompt
#: stops being byte-identical across beads. Each is the literal head of what
#: its entry point returns.
PER_BEAD_MARKERS: tuple[str, ...] = (
    "\n\nJev semantic readiness advice",
    "\n\n## Bound issue contract v1",
)

#: The billing-bucket fields that decide whether this change paid off. They
#: are attributes of `ortus.core.cost.UsageBuckets`, which normalizes every
#: backend's usage report into the same buckets, so a run's cache hit rate is
#: comparable across arms of the A/B and across backends.
CACHE_TELEMETRY_FIELDS: tuple[str, ...] = (
    "cached_input_tokens",
    "uncached_input_tokens",
    "cache_write_tokens",
    "input_tokens",
    "cache_hit_rate",
)

#: The packet fields a full render inlines verbatim and a slim render names.
_PACKET_FIELDS: tuple[tuple[str, str], ...] = (
    ("description", "Description"),
    ("design", "Design"),
    ("acceptance_criteria", "Acceptance criteria"),
    ("notes", "Notes"),
)


def _flag(value: object) -> bool | None:
    """A configured or exported flag value as a bool, or None when unset."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUTHY:
        return True
    if text in _FALSEY:
        return False
    return None


def _pinned(config: Any | None) -> bool:
    """True when a `.ortusrc` layer sets the key, rather than the default."""
    return any(
        STABLE_PREFIX_CONFIG_KEY in layer.data
        for layer in getattr(config, "layers", ())
        if layer.source != "defaults"
    )


def stable_prefix_enabled(
    config: Any | None = None, *, environ: Mapping[str, str] | None = None
) -> bool:
    """True when this run composes per-bead segments behind the stable ones.

    The environment wins over `.ortusrc` so one A/B run can flip the arm
    without touching a tracked file, and a value neither layer can parse
    falls back to today's ordering rather than guessing at an opt-in.
    """
    env = os.environ if environ is None else environ
    exported = _flag(env.get(STABLE_PREFIX_ENV))
    if exported is not None:
        return exported
    if config is None:
        return False
    return bool(_flag(config.get(STABLE_PREFIX_CONFIG_KEY, False)))


def stable_prefix_note(
    config: Any | None = None, *, environ: Mapping[str, str] | None = None
) -> str:
    """The active ordering with its provenance, for a log line or dry run.

    Names the layer whenever it is not the default: an A/B whose transcript
    cannot say which arm a run belonged to proves nothing. The adopted default
    is the stable ordering, so a run that pinned nothing reports the arm alone
    — crediting `.ortusrc` for a key no such layer carries would send a reader
    looking for a pin that is not there.
    """
    env = os.environ if environ is None else environ
    exported = _flag(env.get(STABLE_PREFIX_ENV))
    configured = (
        bool(_flag(config.get(STABLE_PREFIX_CONFIG_KEY, False))) if config else False
    )
    if exported is None:
        arm = "stable" if configured else "legacy"
        return f"{arm} from .ortusrc" if _pinned(config) else arm
    arm = "stable" if exported else "legacy"
    if exported == configured or not _pinned(config):
        return f"{arm} from {STABLE_PREFIX_ENV}"
    pinned = "stable" if configured else "legacy"
    return f"{arm} from {STABLE_PREFIX_ENV}, .ortusrc pins {pinned}"


def resolve_segment(segment: InjectedSegment) -> object:
    """The live object an inventory entry names.

    Raises ``LookupError`` when the module or attribute is gone: an inventory
    that describes segments the assembler no longer produces is worse than no
    inventory, because the ordering decision is made from it.
    """
    module_name, _, attribute = segment.entry_point.partition(":")
    try:
        module = import_module(module_name)
    except ImportError as exc:
        raise LookupError(f"{segment.entry_point} is not importable: {exc}") from exc
    try:
        return getattr(module, attribute)
    except AttributeError as exc:
        raise LookupError(f"{segment.entry_point} no longer exists") from exc


def unresolved_segments() -> tuple[str, ...]:
    """Entry points of inventory segments that no longer resolve."""
    missing: list[str] = []
    for segment in INJECTED_CONTEXT:
        try:
            resolve_segment(segment)
        except LookupError:
            missing.append(segment.entry_point)
    return tuple(missing)


def segments_by_stability(stability: str) -> tuple[InjectedSegment, ...]:
    """Every inventory entry with the given stability, in inventory order."""
    return tuple(
        segment for segment in INJECTED_CONTEXT if segment.stability == stability
    )


def stable_prefix_of(prompt: str) -> str:
    """The leading run of a composed prompt that is not bead-specific.

    The prefix ends at the earliest per-bead marker the prompt carries. A
    prompt with no per-bead segment at all — an unbound worker in a run with
    no readiness advice — is stable end to end and returns unchanged.
    """
    cut = len(prompt)
    for marker in PER_BEAD_MARKERS:
        found = prompt.find(marker)
        if found != -1:
            cut = min(cut, found)
    return prompt[:cut]


def slim_issue_details(issue: Mapping[str, Any]) -> str:
    """Render a claimed issue as its id, title and a pointer to the packet.

    The full description, design and acceptance bodies are the largest
    per-bead segment Ortus injects and the least load-bearing: a worker that
    holds the id can read all three from the tracker at the moment it needs
    them. This render keeps what orients a worker in one line each and names
    which packet fields exist so the worker knows there is more to fetch.

    The id is never interpolated into a command here. It is tracker data, and
    the worker is already told how to quote it.
    """
    issue_id = str(issue.get("id") or "").strip()
    lines: list[str] = []
    if issue_id:
        lines.append(f"Id: {issue_id}")

    title = str(issue.get("title") or "").strip()
    if title:
        lines.append(f"Title: {title}")

    issue_type = str(issue.get("issue_type") or issue.get("type") or "").strip()
    if issue_type:
        lines.append(f"Type: {issue_type}")

    priority = issue.get("priority")
    if priority is not None:
        lines.append(f"Priority: {priority}")

    labels = issue.get("labels") or []
    if labels:
        lines.append("Labels: " + ", ".join(str(label) for label in labels))

    present = tuple(
        heading
        for field, heading in _PACKET_FIELDS
        if str(issue.get(field) or "").strip()
    )
    if present:
        lines.append(
            "The work spec also carries: "
            + ", ".join(present)
            + ". Read them with `bd show` on the id above, quoting it as one "
            "argument after `--`, and use CodeGraph for the code they name."
        )

    return "\n".join(lines).strip()


__all__ = [
    "CACHE_TELEMETRY_FIELDS",
    "INJECTED_CONTEXT",
    "PER_BEAD",
    "PER_BEAD_MARKERS",
    "STABLE",
    "STABLE_PREFIX_CONFIG_KEY",
    "STABLE_PREFIX_ENV",
    "InjectedSegment",
    "resolve_segment",
    "segments_by_stability",
    "slim_issue_details",
    "stable_prefix_enabled",
    "stable_prefix_note",
    "stable_prefix_of",
    "unresolved_segments",
]
