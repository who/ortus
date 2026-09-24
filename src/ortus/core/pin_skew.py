"""Tell a pin-able environment skew apart from a real planning gap.

A worker that meets a declared version, date, or toolchain value the
installed tool cannot honour has two exits. Lowering the declared value to
what the tool supports and filing a bead to restore it keeps the claim
moving; parking the issue on the `human` label stops the queue until an
operator reads the comment. The worker contracts now define the first as
the answer for that class of mismatch, and this module is the harness-side
backstop: when a claim is flagged human anyway, the PLAN-GAP comment that
flagged it is classified, and one carrying version/date/tool evidence is
tagged so it reads as a pin the run still owes rather than as generic human
parking.

The classifier is deliberately hard to satisfy. A false negative leaves an
ordinary PLAN-GAP, which is exactly today's behaviour; a false positive
tells an operator that a missing product decision was a version pin. It
therefore demands all three kinds of evidence at once: two distinct version
or date literals (the declared value and the ceiling it passed), a phrase
naming that ceiling, and a tool-shaped token beside one of the literals.
The tag never touches the `human` label, so an issue the worker parked
stays the operator's to release.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Protocol

#: Marks a human park the harness read as a pin-able environment skew. It is
#: deliberately not in `EXCLUDED_LABELS`: the tag annotates the park, it does
#: not release it.
PIN_SKEW_LABEL = "pin-skew"

#: A dotted version (`0.22.0`, `3.12`) or an ISO date (`2026-08-22`) — the two
#: shapes a pin-able knob is written in.
_LITERAL = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d+\.\d+(?:\.\d+)*\b")

#: A package, binary, or config-file name: a scoped npm name, or an
#: identifier carrying a separator (`vitest-pool-workers`, `wrangler.toml`,
#: `compatibility_date`, `pyproject.toml`).
_TOOL = re.compile(r"@[\w.-]+/[\w.-]+|\b[A-Za-z]\w*(?:[._/-][A-Za-z0-9]+)+\b")

#: Harness vocabulary that happens to carry a separator. None of it names a
#: tool, and all of it turns up near numbers in an ordinary PLAN-GAP.
_NOT_TOOLS = frozenset(
    {
        "plan-gap",
        "pin-skew",
        "in-progress",
        "in_progress",
        "follow-up",
        "work-issue",
        "session-close",
        "non-epic",
        "read-only",
        "acceptance_criteria",
    }
)

#: Phrases that name an installed ceiling rather than a wish. "requires" on
#: its own is absent by design: a planning gap says it too.
_CEILING = (
    "exceeds",
    "exceeded",
    "maximum",
    "max ",
    "supports",
    "supported",
    "unsupported",
    "incompatible",
    "newer than",
    "too new",
    "installed",
    "ceiling",
    "out of range",
)

#: Characters either side of a literal that count as beside it. One clause of
#: prose, so the tool named in the next sentence is not read as this one's.
_NEAR = 60


class _Tracker(Protocol):
    """The tracker surface this pass uses, so tests can supply a double."""

    def show(self, issue_id: str) -> dict[str, Any]: ...

    def comments(self, issue_id: str) -> list[dict[str, Any]]: ...

    def add_label(self, issue_id: str, label: str) -> None: ...

    def add_comment(self, issue_id: str, body: str) -> None: ...


@dataclass(frozen=True)
class PinSkewVerdict:
    """Whether a PLAN-GAP is pin-able, and what made it read that way."""

    pin_able: bool
    evidence: tuple[str, ...] = ()


def _tool_beside(line: str, start: int, end: int) -> str | None:
    """A tool-shaped token within `_NEAR` characters of `line[start:end]`."""
    window = line[max(0, start - _NEAR) : end + _NEAR]
    for match in _TOOL.finditer(window):
        if match.group().lower() not in _NOT_TOOLS:
            return match.group()
    return None


def classify_plan_gap(text: str) -> PinSkewVerdict:
    """Read a PLAN-GAP comment as pin-able environment skew, or not.

    All three evidence kinds must be present: two distinct version or date
    literals, each one beside a tool-shaped token, and a phrase naming an
    installed ceiling. A gap that is only prose — a contradiction, an unmade
    product decision, a credential nobody supplied — carries none of them and
    stays the planning gap it is.
    """
    if not text:
        return PinSkewVerdict(False)
    lowered = text.lower()
    ceiling = next((phrase for phrase in _CEILING if phrase in lowered), "")
    if not ceiling:
        return PinSkewVerdict(False)
    literals: set[str] = set()
    tools: set[str] = set()
    for line in text.splitlines():
        for match in _LITERAL.finditer(line):
            tool = _tool_beside(line, match.start(), match.end())
            if tool is None:
                continue
            literals.add(match.group())
            tools.add(tool)
    if len(literals) < 2 or not tools:
        return PinSkewVerdict(False)
    evidence = (
        tuple(sorted(literals)[:4]) + tuple(sorted(tools)[:2]) + (ceiling.strip(),)
    )
    return PinSkewVerdict(True, evidence)


def latest_plan_gap(comments: Iterable[Mapping[str, Any]]) -> str:
    """The last PLAN-GAP comment's text, in tracker order, or an empty string.

    The last one is the one that flagged the claim: an earlier round may have
    recorded a gap the operator already answered.
    """
    latest = ""
    for comment in comments:
        text = str(comment.get("text") or "")
        if "plan-gap" in text.lower():
            latest = text
    return latest


def pin_skew_note(issue_id: str, evidence: Iterable[str]) -> str:
    """The comment that tells an operator this park was a pin, not a gap."""
    return (
        "pin-don't-park: this PLAN-GAP reads as an environment or version skew "
        f"a pin resolves (evidence: {', '.join(evidence)}). The answer for this "
        "class is to set the declared value to what the installed tool "
        "supports, file a follow-up bead recording the value lowered and the "
        "condition for restoring it, and carry on with the claim — a "
        "credential nobody supplied and a decision nobody has made are the "
        f"gaps that belong here. The {PIN_SKEW_LABEL} label records that "
        "reading; the human label still holds the issue, so release it with: "
        f"bd label remove {issue_id} human."
    )


def tag_pin_skew_claims(
    bd: _Tracker,
    issue_ids: Iterable[str],
    *,
    window: int,
    write_log: Callable[[str], None],
) -> set[str]:
    """Tag the parks among `issue_ids` whose PLAN-GAP was a pin-able skew.

    Runs after the worker is dead, over the claims it flagged during its
    window, so the tracker already holds the comment that flagged each one.
    A claim already carrying the tag is left alone, which keeps a resumed
    issue from collecting the same note once per window. Every tracker error
    is logged against its issue and skipped: this pass annotates a park that
    has already happened and must never end the run.
    """
    tagged: set[str] = set()
    for issue_id in sorted(issue_ids):
        try:
            labels = {str(label) for label in (bd.show(issue_id).get("labels") or ())}
            if PIN_SKEW_LABEL in labels:
                continue
            text = latest_plan_gap(bd.comments(issue_id))
        except Exception as exc:
            write_log(f"iter {window}: pin-skew: could not read {issue_id} ({exc})")
            continue
        verdict = classify_plan_gap(text)
        if not verdict.pin_able:
            continue
        try:
            bd.add_label(issue_id, PIN_SKEW_LABEL)
            bd.add_comment(issue_id, pin_skew_note(issue_id, verdict.evidence))
        except Exception as exc:
            write_log(f"iter {window}: pin-skew: could not tag {issue_id} ({exc})")
            continue
        tagged.add(issue_id)
        write_log(
            f"iter {window}: pin-skew: {issue_id} parked a pin-able "
            f"environment/version skew (evidence: {', '.join(verdict.evidence)}); "
            f"labelled {PIN_SKEW_LABEL}"
        )
    return tagged
