"""Past Dream decisions: the fingerprint, the block that carries it, and read-back.

A Dream run ends by filing one decision bead, and that bead's description
carries a fenced `ortus-dream-fingerprints` block naming every proposal it
made. Andy answers with `bd human respond`, which adds the answer as a comment
and closes the bead. The next run reads every such bead back so the
orchestrator never re-proposes what was rejected or is still awaiting an
answer, and so the validator can ask whether an accepted memory helped.

The fingerprint is what makes "the same proposal" checkable across runs: it
hashes the kind, the key and the pattern with case and whitespace folded, so
an orchestrator rewording its spacing does not slip a rejected idea back in.

Reading is tolerant by design. A bead without a block, or with a block from a
version this code does not know, is still listed — with no proposals and, for
the unknown version, a warning — so one odd bead cannot fail a Dream pass.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Protocol

BLOCK_INFO = "ortus-dream-fingerprints"
BLOCK_VERSION = 1

#: The label and issue type every Dream decision bead carries.
DREAM_LABEL = "dream"
DECISION_TYPE = "decision"

#: How many stored quotes a proposal keeps for the Did-it-help check.
MAX_QUOTES = 2

#: How much of the response text a decision section repeats.
RESPONSE_CHARS = 300

ACCEPTED = "accepted"
REJECTED = "rejected"
UNDECIDED = "undecided"
PENDING = "pending"

_BLOCK_RE = re.compile(
    rf"^```{re.escape(BLOCK_INFO)}[ \t]*\n(?P<body>.*?)^```[ \t]*$",
    re.MULTILINE | re.DOTALL,
)
#: One verdict group: the word, then everything up to the next verdict word or
#: `;`. Stopping at the next word as well as the semicolon keeps
#: `accepted P1, rejected P2` from reading P2 as accepted.
_VERDICT_RE = re.compile(
    r"(?i)\b(accepted|rejected)\b(.*?)(?=\b(?:accepted|rejected)\b|;|$)",
    re.DOTALL,
)
_ID_RE = re.compile(r"(?i)\b([PF]\d+)\b")


def fingerprint(kind: str, key: str, pattern: str) -> str:
    """The first 16 hex of sha256 over `kind|key|<pattern, folded>`."""

    folded = " ".join(pattern.lower().split())
    return hashlib.sha256(f"{kind}|{key}|{folded}".encode("utf-8")).hexdigest()[:16]


def render_fingerprints_block(
    proposals: Iterable[Mapping[str, Any]],
    followups: Iterable[Mapping[str, Any]] = (),
) -> str:
    """The fenced block a decision bead's description carries.

    Each proposal needs `kind`, `key` and `pattern` (or a precomputed
    `fingerprint`); ids default to `P1`, `P2`, ... and `F1`, ... in order.
    """

    rows: list[dict[str, Any]] = []
    for index, proposal in enumerate(proposals, start=1):
        kind = str(proposal.get("kind") or "")
        key = str(proposal.get("key") or "")
        rows.append(
            {
                "id": str(proposal.get("id") or f"P{index}"),
                "kind": kind,
                "key": key,
                "fingerprint": str(
                    proposal.get("fingerprint")
                    or fingerprint(kind, key, str(proposal.get("pattern") or ""))
                ),
                "quotes": [str(q) for q in proposal.get("quotes") or ()][:MAX_QUOTES],
            }
        )
    follow = [
        {
            "id": str(item.get("id") or f"F{index}"),
            "title": str(item.get("title") or ""),
        }
        for index, item in enumerate(followups, start=1)
    ]
    payload = {"version": BLOCK_VERSION, "proposals": rows, "followups": follow}
    return f"```{BLOCK_INFO}\n{json.dumps(payload, indent=2)}\n```"


def parse_fingerprints_block(description: str) -> dict[str, Any] | None:
    """The block's JSON object, or None when it is absent or malformed.

    Malformed is the caller's to log; nothing here raises on a bad bead.
    """

    match = _BLOCK_RE.search(description or "")
    if match is None:
        return None
    try:
        data = json.loads(match.group("body"))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def parse_response(text: str) -> dict[str, str]:
    """Map each `P<n>` / `F<n>` id the response names to its verdict.

    `accepted P1 P3; rejected P2 (one-off)` names three. An id named twice
    takes the later verdict; ids are matched case-insensitively and returned
    uppercase. An id the text does not name is absent, which callers read as
    undecided.
    """

    verdicts: dict[str, str] = {}
    for match in _VERDICT_RE.finditer(text or ""):
        verdict = match.group(1).lower()
        for found in _ID_RE.findall(match.group(2)):
            verdicts[found.upper()] = verdict
    return verdicts


@dataclass(frozen=True)
class DecidedProposal:
    id: str
    kind: str
    key: str
    fingerprint: str
    quotes: tuple[str, ...]
    verdict: str


@dataclass(frozen=True)
class DecidedFollowup:
    id: str
    title: str
    verdict: str


@dataclass(frozen=True)
class Decision:
    """One past Dream decision bead, read back with a verdict per proposal."""

    bead_id: str
    status: str
    closed_at: str
    proposals: tuple[DecidedProposal, ...]
    followups: tuple[DecidedFollowup, ...]
    response: str
    warning: str = ""


class _DecisionReader(Protocol):
    def list_all(self) -> list[dict[str, Any]]: ...

    def comments(self, issue_id: str) -> list[dict[str, Any]]: ...


def _response_text(row: Mapping[str, Any], comments: list[dict[str, Any]]) -> str:
    """The newest comment, which is what `bd human respond` adds, else the close reason."""

    for comment in reversed(comments):
        if isinstance(comment, dict):
            text = str(comment.get("text") or "").strip()
            if text:
                return text
    return str(row.get("close_reason") or "").strip()


def _verdict_for(item_id: str, status: str, response: str, named: dict[str, str]) -> str:
    if status != "closed":
        # Open and unanswered is still awaiting Andy; open with an answer was
        # reopened, which lifts the earlier verdicts.
        return UNDECIDED if response else PENDING
    return named.get(item_id.upper(), UNDECIDED)


def _decision(row: Mapping[str, Any], comments: list[dict[str, Any]]) -> Decision:
    bead_id = str(row.get("id") or "")
    status = str(row.get("status") or "")
    response = _response_text(row, comments)
    named = parse_response(response)
    block = parse_fingerprints_block(str(row.get("description") or ""))
    warning = ""
    proposals: list[DecidedProposal] = []
    followups: list[DecidedFollowup] = []
    if block is not None and block.get("version") != BLOCK_VERSION:
        warning = (
            f"{bead_id}: fingerprints block version {block.get('version')!r} "
            f"is not {BLOCK_VERSION}; its proposals are ignored"
        )
    elif block is not None:
        for item in block.get("proposals") or ():
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "")
            proposals.append(
                DecidedProposal(
                    id=item_id,
                    kind=str(item.get("kind") or ""),
                    key=str(item.get("key") or ""),
                    fingerprint=str(item.get("fingerprint") or ""),
                    quotes=tuple(str(q) for q in item.get("quotes") or ())[:MAX_QUOTES],
                    verdict=_verdict_for(item_id, status, response, named),
                )
            )
        for item in block.get("followups") or ():
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "")
            followups.append(
                DecidedFollowup(
                    id=item_id,
                    title=str(item.get("title") or ""),
                    verdict=_verdict_for(item_id, status, response, named),
                )
            )
    return Decision(
        bead_id=bead_id,
        status=status,
        closed_at=str(row.get("closed_at") or ""),
        proposals=tuple(proposals),
        followups=tuple(followups),
        response=response,
        warning=warning,
    )


def load_decisions(client: _DecisionReader) -> tuple[Decision, ...]:
    """Every bead labeled `dream` with type `decision`, read back in listing order."""

    decisions: list[Decision] = []
    for row in client.list_all():
        if not isinstance(row, dict):
            continue
        if DREAM_LABEL not in (row.get("labels") or ()):
            continue
        if row.get("issue_type") != DECISION_TYPE:
            continue
        decisions.append(_decision(row, client.comments(str(row.get("id") or ""))))
    return tuple(decisions)


def suppressed(
    decisions: Iterable[Decision],
) -> tuple[frozenset[str], frozenset[str]]:
    """(keys, fingerprints) a new run must not propose: rejected or still pending."""

    keys: set[str] = set()
    prints: set[str] = set()
    for decision in decisions:
        for proposal in decision.proposals:
            if proposal.verdict in (REJECTED, PENDING):
                if proposal.key:
                    keys.add(proposal.key)
                if proposal.fingerprint:
                    prints.add(proposal.fingerprint)
    return frozenset(keys), frozenset(prints)


def accepted(
    decisions: Iterable[Decision],
) -> tuple[tuple[str, tuple[str, ...], str], ...]:
    """(key, quotes, closed_at) for every accepted proposal, for the Did-it-help check."""

    return tuple(
        (proposal.key, proposal.quotes, decision.closed_at)
        for decision in decisions
        for proposal in decision.proposals
        if proposal.verdict == ACCEPTED
    )


def render_decision_section(decision: Decision) -> str:
    """The `[dream-decision <id>]` section the orchestrator receives."""

    lines = [f"[dream-decision {decision.bead_id}]"]
    lines.extend(
        f"{p.id} {p.verdict} kind={p.kind} key={p.key} fingerprint={p.fingerprint}"
        for p in decision.proposals
    )
    response = " ".join(decision.response.split())[:RESPONSE_CHARS]
    lines.append(f"response: {response}")
    lines.append("[/dream-decision]")
    return "\n".join(lines)
