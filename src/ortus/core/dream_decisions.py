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
#: The words a response may open a verdict group with. A follow-up the operator
#: files is an accepted follow-up, and a skipped item is a rejected one, so the
#: plain-language reply needs no more than these two verdicts. The lookarounds
#: treat `-` as part of a word, so an id such as `skip-flaky-tests` is never
#: read as the verb `skip`.
_VERB_RE = re.compile(
    r"(?i)(?<![\w-])(?:(?P<accept>accept(?:ed)?|file\s+follow-ups?|filed)"
    r"|(?P<reject>reject(?:ed)?|skip(?:ped)?))(?![\w-])"
)
#: A reason in parentheses belongs to the verdict, never to the ids.
_REASON_RE = re.compile(r"\([^)]*\)")
#: The ids Dream numbered before items had names. Old decision beads carry
#: them in their fingerprints blocks, and old replies name them.
_LEGACY_ID_RE = re.compile(r"(?i)^[PF]\d+$")
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
#: Words a reply may carry that are never ids when no id list is supplied.
_NOT_IDS = frozenset({"none", "and", "follow-up", "follow-ups"})
#: Most words a follow-up's id keeps from its title.
FOLLOWUP_SLUG_WORDS = 5


def slugify(text: str, max_words: int | None = None) -> str:
    """Lowercase `[a-z0-9-]` words of `text`, at most `max_words` of them."""

    words = re.findall(r"[a-z0-9]+", str(text).lower())
    if max_words is not None:
        words = words[:max_words]
    return "-".join(words)


def normalize_id(item_id: str) -> str:
    """The form an id is compared in: legacy codes upper, slugs lower."""

    text = str(item_id).strip()
    return text.upper() if _LEGACY_ID_RE.match(text) else text.lower()


def item_ids(
    proposals: Iterable[Mapping[str, Any]],
    followups: Iterable[Mapping[str, Any]] = (),
) -> tuple[list[str], list[str]]:
    """Descriptive ids for one report's items, unique across both lists.

    A proposal is named by its memory key, a follow-up by the first words of
    its title. A proposal with no key falls back to its kind, then to
    `memory-change-<n>`; a follow-up with no title to `follow-up-<n>`. A
    repeated name gains `-2`, `-3`, ... A name shaped like a legacy `P<n>` code
    is prefixed so it cannot be mistaken for one. An item that already carries
    an `id` keeps it.
    """

    taken: set[str] = set()

    def claim(item: Mapping[str, Any], base: str, prefix: str) -> str:
        given = str(item.get("id") or "")
        if given:
            taken.add(normalize_id(given))
            return given
        if _LEGACY_ID_RE.match(base):
            base = f"{prefix}-{base}"
        name, suffix = base, 2
        while name in taken:
            name, suffix = f"{base}-{suffix}", suffix + 1
        taken.add(name)
        return name

    proposal_ids = [
        claim(
            item,
            slugify(item.get("key") or "")
            or slugify(item.get("kind") or "")
            or f"memory-change-{index}",
            "memory",
        )
        for index, item in enumerate(proposals, start=1)
    ]
    followup_ids = [
        claim(
            item,
            slugify(item.get("title") or "", FOLLOWUP_SLUG_WORDS)
            or f"follow-up-{index}",
            "follow-up",
        )
        for index, item in enumerate(followups, start=1)
    ]
    return proposal_ids, followup_ids


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
    `fingerprint`); ids default to the descriptive names `item_ids` gives.
    """

    proposals = list(proposals)
    followups = list(followups)
    proposal_ids, followup_ids = item_ids(proposals, followups)
    rows: list[dict[str, Any]] = []
    for item_id, proposal in zip(proposal_ids, proposals):
        kind = str(proposal.get("kind") or "")
        key = str(proposal.get("key") or "")
        rows.append(
            {
                "id": item_id,
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
        {"id": item_id, "title": str(item.get("title") or "")}
        for item_id, item in zip(followup_ids, followups)
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


def _id_tokens(text: str, known: frozenset[str] | None) -> list[str]:
    tokens = (
        token.strip(".:!?\"'`")
        for token in re.split(r"[\s,]+", _REASON_RE.sub(" ", text))
    )
    found: list[str] = []
    for token in tokens:
        if not token:
            continue
        if _LEGACY_ID_RE.match(token):
            found.append(normalize_id(token))
        elif known is not None:
            if token.lower() in known:
                found.append(token.lower())
        elif _SLUG_RE.match(token.lower()) and token.lower() not in _NOT_IDS:
            found.append(token.lower())
    return found


def parse_response(text: str, known_ids: Iterable[str] | None = None) -> dict[str, str]:
    """Map each id the response names to its verdict.

    `accept commit-message-rules, pinned-prompt-hash-tests; reject stale-key
    (one-off); file follow-up dolt-ssh-push` names four. A verb names the ids
    that follow it up to the next verb or `;`: `accept`/`accepted`,
    `file follow-up` and `filed` accept, `reject`/`rejected` and
    `skip`/`skipped` reject, and a parenthesized reason is ignored.

    With `known_ids` only those ids are matched, case-insensitively; without
    them any slug-shaped word is. Legacy `P<n>` / `F<n>` codes are always
    matched and returned uppercase, so `accepted P1 P2; F2 filed` reads as it
    always did; slugs are returned lowercase. An id named twice takes the
    later verdict. An id the text does not name is absent, which callers read
    as undecided.
    """

    known = (
        None if known_ids is None else frozenset(normalize_id(i).lower() for i in known_ids)
    )
    verdicts: dict[str, str] = {}
    for group in (text or "").split(";"):
        verbs = list(_VERB_RE.finditer(group))
        for index, verb in enumerate(verbs):
            end = verbs[index + 1].start() if index + 1 < len(verbs) else len(group)
            verdict = ACCEPTED if verb.group("accept") else REJECTED
            for found in _id_tokens(group[verb.end() : end], known):
                verdicts[found] = verdict
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


def _verdict_for(
    item_id: str, status: str, response: str, named: dict[str, str], legacy: str = ""
) -> str:
    if status != "closed":
        # Open and unanswered is still awaiting Andy; open with an answer was
        # reopened, which lifts the earlier verdicts.
        return UNDECIDED if response else PENDING
    # A reply in the old numbered form still reaches a named item by position.
    return named.get(normalize_id(item_id)) or named.get(legacy, UNDECIDED)


def _decision(row: Mapping[str, Any], comments: list[dict[str, Any]]) -> Decision:
    bead_id = str(row.get("id") or "")
    status = str(row.get("status") or "")
    response = _response_text(row, comments)
    block = parse_fingerprints_block(str(row.get("description") or ""))
    known = [
        str(item.get("id") or "")
        for section in ("proposals", "followups")
        for item in ((block or {}).get(section) or ())
        if isinstance(item, dict)
    ]
    named = parse_response(response, known if block is not None else None)
    warning = ""
    proposals: list[DecidedProposal] = []
    followups: list[DecidedFollowup] = []
    if block is not None and block.get("version") != BLOCK_VERSION:
        warning = (
            f"{bead_id}: fingerprints block version {block.get('version')!r} "
            f"is not {BLOCK_VERSION}; its proposals are ignored"
        )
    elif block is not None:
        listed = [item for item in block.get("proposals") or () if isinstance(item, dict)]
        for position, item in enumerate(listed, start=1):
            item_id = str(item.get("id") or "")
            proposals.append(
                DecidedProposal(
                    id=item_id,
                    kind=str(item.get("kind") or ""),
                    key=str(item.get("key") or ""),
                    fingerprint=str(item.get("fingerprint") or ""),
                    quotes=tuple(str(q) for q in item.get("quotes") or ())[:MAX_QUOTES],
                    verdict=_verdict_for(
                        item_id, status, response, named, f"P{position}"
                    ),
                )
            )
        listed = [item for item in block.get("followups") or () if isinstance(item, dict)]
        for position, item in enumerate(listed, start=1):
            item_id = str(item.get("id") or "")
            followups.append(
                DecidedFollowup(
                    id=item_id,
                    title=str(item.get("title") or ""),
                    verdict=_verdict_for(
                        item_id, status, response, named, f"F{position}"
                    ),
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
