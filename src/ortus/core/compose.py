"""Bounded read-only pass that writes a commit message from a verified diff.

Finalization can assemble a commit message from structured data alone, and that
message is still what lands whenever this pass is unavailable or unconvincing.
What it cannot do is explain a change: the deterministic body restates the
issue's objective — written before any code existed — and lists paths that
``git show --stat`` already prints. This module asks one model, once, to read
the diff that was actually verified and write the explanation a reviewer would
give: what was wrong, what the code now does, how the mechanism works, and
where the design stops.

Three properties keep that from becoming a liability. The pass is read-only, so
it cannot touch the candidate it is describing; :func:`guard_read_only` is what
proves that after the fact. Its output is validated mechanically before it can
reach a commit — a message that names a symbol the diff does not contain, or
that narrates how the commit was produced, is rejected. And every failure mode
raises :class:`ComposeFailed`, which the caller degrades to the deterministic
body: a commit is never lost because the prose was.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol

from ortus.core.agent import make_runner
from ortus.core.codegraph import CodeGraphCapability
from ortus.core.profiles import AgentProfile
from ortus.core.verdict import assistant_text

#: The one line the pass is allowed to speak through, mirroring the verifier's
#: ``ORTUS_VERDICT:`` envelope so both are extracted from a transcript the same
#: way and neither can be confused for ordinary assistant prose.
MESSAGE_PREFIX = "ORTUS_COMMIT_MESSAGE:"

#: Conventional git subject ceiling, counted over the whole subject line that
#: will be committed — the ``<issue-id>: `` prefix included.
SUBJECT_LIMIT = 72
#: A commit message is read in a terminal, not paged; past this the pass has
#: written an essay rather than an explanation.
MAX_MESSAGE_CHARS = 8_000
#: How much diff the prompt carries. Beyond this the bundle is cut at a hunk
#: boundary and marked, so the pass always sees whole hunks.
MAX_DIFF_CHARS = 60_000
#: Bound on each supplied packet field, so an essay-length objective or
#: completion comment cannot crowd the diff out of the prompt.
MAX_CONTEXT_CHARS = 2_000

#: Model each backend composes with when the operator configured none for the
#: finalize phase. This is bounded writing over material it is handed, not
#: correctness reasoning, so the cheap tier is the right default. Codex has no
#: entry: its provider default already is its small model surface.
DEFAULT_COMPOSE_MODELS: dict[str, str] = {"claude": "haiku"}


class _Runner(Protocol):
    """The subset of the backend runners this pass depends on."""

    def run(
        self,
        prompt: str,
        *,
        repo: Path,
        log_path: Path,
        profile: AgentProfile | None,
        timeout: float | None,
        readonly: bool,
    ) -> int: ...


RunnerFactory = Callable[..., _Runner]


class ComposeFailed(RuntimeError):
    """The pass produced no message a commit may carry.

    Every subclass degrades to the deterministic body. Nothing raised from this
    module is allowed to strand a verified candidate.
    """


class ComposeRejected(ComposeFailed):
    """A message was produced and the mechanical validation refused it."""


class ComposeExceededAuthority(RuntimeError):
    """A read-only pass changed state it has no authority over.

    Deliberately not a :class:`ComposeFailed`: every other failure here is the
    absence of prose, but this one means the candidate a verifier passed may no
    longer be the candidate on disk. That is a finalization blocker, not a
    reason to commit a plainer message.
    """

    def __init__(self, changed: Iterable[str]) -> None:
        self.changed = tuple(sorted(changed))
        super().__init__(
            "the read-only commit-message pass changed state it does not own: "
            + ", ".join(self.changed)
        )


@dataclass(frozen=True)
class CommitMessage:
    """A validated subject and body, ready to be committed verbatim."""

    subject: str
    body: str

    @property
    def text(self) -> str:
        return f"{self.subject}\n\n{self.body}\n"


def _default_runner_factory(backend: str = "claude") -> _Runner:
    """Indirection so callers (and tests) can swap in a fake backend binary."""

    return make_runner(backend)  # type: ignore[arg-type, return-value]


def with_default_model(profile: AgentProfile) -> AgentProfile:
    """`profile` with this phase's cheap default filled in, if none was set."""

    if profile.model is not None:
        return profile
    model = DEFAULT_COMPOSE_MODELS.get(profile.backend)
    return profile if model is None else replace(profile, model=model)


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_EXAMPLE = {
    "subject": "Re-read the worktree before deciding a path is unrelated",
    "body": (
        "A path a worker disowned early in a run stayed disowned even after a "
        "later worker edited it. _candidate_baseline() unions the disowned "
        "set into the baseline every snapshot subtracts, so the edit could "
        "not enter the candidate, reach the verifier, or be committed — the "
        "work was done and then silently dropped on the floor.\n\n"
        "A disowned path is now re-adopted the moment its contents move. "
        "Disowning still means \"this is somebody else's leftover\"; it no "
        "longer means \"nobody may touch this file again this run\".\n\n"
        "_prepare_handoff() already fingerprints every inherited path when a "
        "worker is handed the tree. That record is what makes the decision "
        "possible: own_inherited_work() compares the stored fingerprint "
        "against the file on disk and drops any path whose bytes changed out "
        "of the disowned set, so the next snapshot picks it up. Attribution "
        "keys on the recorded fingerprint rather than the path string, "
        "because two issues in one subsystem would otherwise claim each "
        "other's files.\n\n"
        "Re-adoption is deliberately one-way and content-based. A worker "
        "cannot un-disown a path by asserting ownership, and a file that was "
        "merely opened and saved unchanged stays out — the guard is the bytes, "
        "not intent."
    ),
}

_EXAMPLE_ENVELOPE = MESSAGE_PREFIX + " " + json.dumps(_EXAMPLE, ensure_ascii=False)


def _bounded(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " [truncated]"


def bounded_diff(diff: str, limit: int = MAX_DIFF_CHARS) -> str:
    """`diff` cut at a hunk boundary, with what was dropped stated in place.

    A diff is cut between hunks rather than mid-line so the pass never reasons
    about half a change it cannot see the rest of, and the marker says how many
    hunks it is missing so it can say what it does not know.
    """

    if len(diff) <= limit:
        return diff
    boundaries = [
        match.start() for match in re.finditer(r"^(?:diff --git |@@ )", diff, re.M)
    ]
    kept = [start for start in boundaries if start <= limit]
    cut = kept[-1] if len(kept) > 1 else limit
    omitted = len([start for start in boundaries if start >= cut])
    return (
        diff[:cut].rstrip()
        + f"\n\n[diff truncated: {omitted} of {len(boundaries)} hunks omitted. "
        "Describe the primary change shown above and do not guess at the rest.]\n"
    )


def compose_prompt(
    *,
    issue_id: str,
    title: str,
    objective: str,
    changes: str,
    diff: str,
) -> str:
    """The whole contract: the material, the rubric, and one worked example."""

    packet = [f"Issue: {issue_id}"]
    if title:
        packet.append(f"Issue title (do NOT restate it as the subject): {title}")
    if objective:
        packet.append(
            "Objective the issue was written with, before any code existed: "
            + _bounded(objective, MAX_CONTEXT_CHARS)
        )
    if changes:
        packet.append(
            "What the implementer recorded when it finished: "
            + _bounded(changes, MAX_CONTEXT_CHARS)
        )
    return f"""COMMIT MESSAGE COMPOSITION PASS (read-only, one pass only).

The change below has already been written and independently reviewed. You are
not reviewing it, extending it, or judging it. You write its commit message,
and that message is your entire output. You cannot edit files, change tracker
state, or decide whether this commit happens.

Emit exactly one final assistant line beginning {MESSAGE_PREFIX} followed by one
JSON object with exactly two string fields, `subject` and `body`. Do not emit
that prefix anywhere else. Newlines inside `body` are JSON-escaped.

QUALITY BAR: a senior engineer explaining this change to a junior reader who
has the code in front of them but was not in the room.

The body answers these four questions, in this order, as prose paragraphs:

1. What was wrong, and what did it cost?
2. What does the code now do?
3. How does the mechanism work? Name the real functions and files.
4. Where does the design deliberately stop, and why?

RULES

- The diff is the fact. The packet below is intent, written before the code
  existed. Where they disagree, describe the diff.
- Subject: imperative mood ("Add", "Stop", "Re-read" — never "Adds" or
  "Added"), under {SUBJECT_LIMIT} characters including the `{issue_id}: ` prefix
  Ortus prepends, so leave that prefix off. It describes the change, not the
  issue, and never restates the issue title. No trailing period, no `...`.
- Body: paragraphs of prose. Not a bullet list, and never an inventory of the
  files this commit touched — git show --stat already prints those.
- A commit body is not rendered as Markdown. Git prints it verbatim and the
  forges show it as preformatted text, so a backtick around a name is shown to
  the reader as a backtick. Write identifiers as bare words, with no backticks
  and no other markup.
- Name at least one function, class, or file that actually appears in the
  diff, and never name one that does not. Write a function or a method with
  its parentheses — compose_prompt() — so a bare word still reads as a symbol.
- Explain jargon on first use. A reader six months out has the code and
  nothing else.
- Never narrate how this commit was produced: no attempt counts, correction
  rounds, verifier verdicts, phase names, or candidate hashes. Domain
  vocabulary is not process narration — if the code under change is about
  verification, say so freely.
- One coherent explanation for the whole change, even when it spans files. A
  trivial change still gets prose, not a restated subject.

WORKED EXAMPLE — the shape and altitude to hit, not the subject matter:

{_EXAMPLE_ENVELOPE}

Note what the example does: it opens with the concrete failure and its cost,
states the new behaviour in one line, then explains the mechanism by naming the
functions that implement it, and closes on the limit the design chose. Note
what it does not do: it carries no backticks, because the surface it is written
for cannot render them.

--- ISSUE PACKET ---
{chr(10).join(packet)}

--- VERIFIED DIFF ---
{bounded_diff(diff)}
--- END DIFF ---

Write the message now. Read files only if the diff leaves you unable to answer
question 3, and emit the envelope as your final line either way.
"""


# ---------------------------------------------------------------------------
# Transcript extraction
# ---------------------------------------------------------------------------


def parse_message(log_path: Path, *, start_offset: int = 0) -> CommitMessage:
    """The one envelope this pass emitted, or a failure explaining why not."""

    envelopes: list[Any] = []
    try:
        with log_path.open("rb") as fh:
            fh.seek(start_offset)
            for raw in fh:
                try:
                    event = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(event, dict):
                    continue
                for text in assistant_text(event):
                    for line in text.splitlines():
                        stripped = line.strip()
                        if not stripped.startswith(MESSAGE_PREFIX):
                            continue
                        encoded = stripped[len(MESSAGE_PREFIX) :].strip()
                        try:
                            envelopes.append(json.loads(encoded))
                        except json.JSONDecodeError as exc:
                            raise ComposeFailed(
                                "malformed commit-message JSON"
                            ) from exc
    except OSError as exc:
        raise ComposeFailed(f"transcript unreadable ({exc})") from exc
    if len(envelopes) != 1:
        raise ComposeFailed(
            f"expected exactly one commit-message envelope; found {len(envelopes)}"
        )
    payload = envelopes[0]
    if not isinstance(payload, dict):
        raise ComposeFailed("commit-message envelope is not a JSON object")
    subject = payload.get("subject")
    body = payload.get("body")
    if not isinstance(subject, str) or not isinstance(body, str):
        raise ComposeFailed("commit-message envelope needs string subject and body")
    return CommitMessage(subject=subject, body=body)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_ELLIPSIS_RE = re.compile(r"\.\.\.|…")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]|\x1b\[")
_WORD_RE = re.compile(r"[a-z0-9]+")
#: A bullet whose entire content is one path-ish token: the inventory shape.
_PATH_BULLET_RE = re.compile(r"^\s*[-*+]\s+`?[\w./-]+\.[A-Za-z0-9_]+`?:?\s*$")
_INVENTORY_HEADING_RE = re.compile(
    r"^\s*(?:files?\s+(?:touched|changed|modified|added|committed)"
    r"|modified|added|new|changed|paths?)\s*:",
    re.IGNORECASE,
)
#: Third-person or past-tense openers. A first word ending in `es` (`Writes`),
#: in a consonant plus `s` (`Adds`), or in `ed`/`ing` is describing the change
#: rather than instructing the tree, which is what an imperative subject does.
#: `Address` and `Focus` survive — the character before the final `s` is `s` or
#: a vowel — and so does `Bring`, whose stem is too short to be a gerund.
_NON_IMPERATIVE_RE = re.compile(
    r"^(?:\w{3,}(?:ed|ing)|\w+es|\w*[^aeiousx]s)$", re.IGNORECASE
)

#: Narration of how this commit was produced. Each pattern is a phrase, never a
#: bare noun: this codebase implements verification, candidates and corrections,
#: so a commit that changes them must stay free to name them.
_AUTOBIOGRAPHY: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"\b(?:on the\s+)?(?:first|second|third|fourth|\d+(?:st|nd|rd|th)?)\s+"
            r"(?:attempt|try|pass|round|iteration)\b",
            re.IGNORECASE,
        ),
        "an attempt count",
    ),
    (
        re.compile(
            r"\bafter\s+\d+\s+(?:attempt|correction|round|iteration|pass)s?\b",
            re.IGNORECASE,
        ),
        "an attempt count",
    ),
    (
        re.compile(r"\bcorrection\s+(?:round|attempt|pass)e?s?\b", re.IGNORECASE),
        "a correction round",
    ),
    (
        re.compile(
            r"\b(?:the\s+)?verifier\s+(?:passed|failed|rejected|approved|confirmed|"
            r"accepted|signed off)\b",
            re.IGNORECASE,
        ),
        "a verifier verdict",
    ),
    (
        re.compile(r"\bfresh\s+(?:read-only\s+)?verifier\b", re.IGNORECASE),
        "a verifier verdict",
    ),
    (
        re.compile(
            r"\bverdict\s+(?:passed|failed|was\s+\w+)|\bpassing verdict\b",
            re.IGNORECASE,
        ),
        "a verifier verdict",
    ),
    (
        re.compile(
            r"\b(?:implementation|verification|correction|finalization|compose|"
            r"planning)\s+phase\b",
            re.IGNORECASE,
        ),
        "a phase name",
    ),
    (
        re.compile(
            r"\bfinalized-(?:report|close|compose|commit|sync)\b|\bverified-(?:pass|"
            r"fail)\b|\bplan-gap\b|\bcandidate-captured\b",
            re.IGNORECASE,
        ),
        "a phase name",
    ),
    (re.compile(r"\b[0-9a-f]{12,}\b"), "a candidate hash"),
    (
        re.compile(
            r"\bthis\s+(?:commit|change|candidate|work)\s+was\s+"
            r"(?:verified|produced|generated|composed|approved|reviewed)\b",
            re.IGNORECASE,
        ),
        "how this commit was produced",
    ),
)

#: What counts as a citation the diff has to support: a backticked identifier
#: or path, or a bare `name()` call. Free prose in backticks (`git show --stat`)
#: carries a space and is not a claim about this diff's symbols.
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
_CITATION_RE = re.compile(r"^[\w./-]+(?:\(\))?$")
_BARE_CALL_RE = re.compile(r"\b([A-Za-z_][\w.]*)\(\)")


#: One Markdown code span: a lone backtick, its contents, and a lone closing
#: backtick. The lookarounds keep a fence out of the match rather than
#: half-stripping one, and the contents may cross a single line break so a span
#: a wrapped body split over two lines is still recognized as one span.
_CODE_SPAN_RE = re.compile(r"(?<!`)`(?!`)([^`\n]*(?:\n[^`\n]*)?)`(?!`)")


def strip_code_spans(text: str) -> str:
    """`text` with code-span backticks removed and their contents kept.

    Nothing that displays a commit message renders Markdown: git prints the
    body verbatim and the forges show it as preformatted text, so a backtick
    written around an identifier reaches every reader as a literal character.
    The word inside the span is the useful part, so only the two markers go.

    An unpaired backtick is left exactly where it is. On its own it is more
    likely prose about the character than a broken span, and removing it would
    corrupt a sentence to tidy up markup nobody wrote.
    """

    return _CODE_SPAN_RE.sub(r"\1", text)


def _normalized(text: str) -> str:
    return " ".join(_WORD_RE.findall(text.casefold()))


def _printable(text: str) -> str:
    """`text` with anything the commit encoding cannot carry replaced."""

    return str(text).encode("utf-8", "replace").decode("utf-8", "replace")


def citations(body: str) -> tuple[str, ...]:
    """Names in `body` that assert something about the diff's contents."""

    found: list[str] = []
    for span in _BACKTICK_RE.findall(body):
        candidate = span.strip()
        if _CITATION_RE.match(candidate):
            found.append(candidate.removesuffix("()"))
    for name in _BARE_CALL_RE.findall(body):
        found.append(name)
    return tuple(dict.fromkeys(name for name in found if name))


def _unsupported(names: Iterable[str], diff: str) -> tuple[str, ...]:
    """Citations the diff does not contain, in the order they were made."""

    return tuple(name for name in names if name not in diff)


def _autobiography(text: str) -> str:
    for pattern, label in _AUTOBIOGRAPHY:
        if pattern.search(text):
            return label
    return ""


def _is_inventory(body: str) -> bool:
    bare_paths = 0
    for line in body.splitlines():
        if _INVENTORY_HEADING_RE.match(line):
            return True
        if _PATH_BULLET_RE.match(line):
            bare_paths += 1
    return bare_paths >= 2


def validate_message(
    message: CommitMessage,
    *,
    issue_id: str,
    title: str,
    diff: str,
) -> CommitMessage:
    """The composed message, normalized, or a rejection naming what failed.

    Every check here is mechanical. The pass is asked for judgement and given
    latitude in the prompt; what reaches a commit is only what can be checked
    against the diff and the issue in front of us.

    Code spans are stripped rather than refused, and stripped last: the
    citation check reads backticks as the pass's claim about the diff, and an
    otherwise good message must not be thrown away — degrading to the
    deterministic body — over two characters of markup that render nowhere.
    """

    subject = " ".join(_printable(message.subject).split())
    prefix = f"{issue_id}: "
    if subject.lower().startswith(prefix.lower()):
        subject = subject[len(prefix) :].strip()
    body = _printable(message.body).strip()

    if not subject:
        raise ComposeRejected("the composed subject is empty")
    if not body:
        raise ComposeRejected("the composed message has a subject and no body")
    if _ELLIPSIS_RE.search(subject):
        raise ComposeRejected("the composed subject trails off in an ellipsis")
    if len(prefix + subject) > SUBJECT_LIMIT:
        raise ComposeRejected(
            f"the composed subject runs to {len(prefix + subject)} characters, "
            f"over the {SUBJECT_LIMIT}-character limit"
        )
    first = subject.split()[0]
    if _NON_IMPERATIVE_RE.match(first):
        raise ComposeRejected(
            f"the composed subject opens with {first!r}, which is not imperative"
        )
    normal_subject, normal_title = _normalized(subject), _normalized(title)
    if normal_title and (
        normal_subject == normal_title or normal_title.startswith(normal_subject)
    ):
        raise ComposeRejected("the composed subject restates the issue title")

    if len(subject) + len(body) > MAX_MESSAGE_CHARS:
        raise ComposeRejected(
            f"the composed message runs to {len(body)} characters of body, past "
            f"the {MAX_MESSAGE_CHARS}-character ceiling"
        )
    for label, text in (("subject", subject), ("body", body)):
        if _CONTROL_RE.search(text) or MESSAGE_PREFIX in text:
            raise ComposeRejected(f"the composed {label} carries control tokens")
    if len([block for block in re.split(r"\n\s*\n", body) if block.strip()]) < 2:
        raise ComposeRejected("the composed body is a single paragraph")
    if _is_inventory(body):
        raise ComposeRejected("the composed body inventories the committed files")
    narration = _autobiography(f"{subject}\n{body}")
    if narration:
        raise ComposeRejected(f"the composed message narrates {narration}")

    named = citations(body)
    if not named:
        raise ComposeRejected("the composed body names nothing from the diff")
    unsupported = _unsupported(named, diff)
    if unsupported:
        raise ComposeRejected(
            "the composed body names "
            + ", ".join(unsupported)
            + ", which the diff does not contain"
        )
    return CommitMessage(
        subject=prefix + strip_code_spans(subject), body=strip_code_spans(body)
    )


def guard_read_only(before: Mapping[str, str], after: Mapping[str, str]) -> None:
    """Reject a pass that moved the worktree or the tracker while it ran.

    The candidate was sealed by a passing verdict, so a composition pass that
    changed anything has falsified the thing it was describing. Raising here
    stops the commit; it is the one failure in this module that does not
    degrade to plainer prose.
    """

    changed = [
        key
        for key in {*before, *after}
        if before.get(key, "") != after.get(key, "")
    ]
    if changed:
        raise ComposeExceededAuthority(changed)


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------


def compose_commit_message(
    repo: Path,
    *,
    issue_id: str,
    title: str,
    objective: str,
    changes: str,
    diff: str,
    log_path: Path,
    backend: str,
    profile: AgentProfile,
    capability: CodeGraphCapability | None = None,
    timeout: float | None = None,
    runner_factory: RunnerFactory = _default_runner_factory,
) -> str:
    """Run one bounded read-only pass and return the message it earned.

    Raises :class:`ComposeFailed` for every outcome short of a validated
    message — an unreadable diff, a runner that failed or timed out, a
    transcript with no envelope, a message the validation refused.
    """

    if not diff.strip():
        raise ComposeFailed("the candidate diff is empty or unreadable")
    runner = runner_factory() if backend == "claude" else runner_factory("codex")
    configure = getattr(runner, "configure_codegraph", None)
    if callable(configure):
        configure(capability)
    prompt = compose_prompt(
        issue_id=issue_id,
        title=title,
        objective=objective,
        changes=changes,
        diff=diff,
    )
    offset = log_path.stat().st_size if log_path.exists() else 0
    try:
        rc = runner.run(
            prompt,
            repo=repo,
            log_path=log_path,
            profile=profile,
            timeout=timeout,
            readonly=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise ComposeFailed(f"the pass timed out after {timeout}s") from exc
    except Exception as exc:  # noqa: BLE001 - a launch failure is not a lost commit
        raise ComposeFailed(f"the pass could not run ({exc})") from exc
    if rc != 0:
        raise ComposeFailed(f"the pass exited {rc}")
    message = validate_message(
        parse_message(log_path, start_offset=offset),
        issue_id=issue_id,
        title=title,
        diff=diff,
    )
    return message.text
