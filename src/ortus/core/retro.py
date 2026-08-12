"""Bounded advisory retrospective over recent run records.

A completed run leaves three kinds of record — the candidate journal, the
verification reports archived under ``logs/grind-transactions``, and the grind
run logs — and all three are read only when someone is debugging. A run that
discovers a recurring failure, a flaky test, or a hazard leaves that discovery
in artifacts nobody revisits. This module reads a bounded window of those
records, asks one cheap read-only model pass for the repeated failures,
hazards, and gaps they reveal, and records what comes back as pending
proposals in the tracker: proposed lessons and proposed issues, as separate
kinds, in exactly the pending state a worker's own lesson proposal uses. One
curation step — ``ortus curate`` — then reviews both.

The pass is advisory end to end. It never creates an issue and never accepts a
proposal; its only writes are pending-prefixed tracker memories, so nothing it
produces is active or scheduled until a human accepts it. It never writes to
the worktree — the model runs read-only and is told to reason only over the
supplied records, never to read repository source. And it is an
operator-invoked step: nothing in the grind iteration calls it, and nothing
here takes the grind lock, so a retrospective can never compete with a worker
for either.

A record that cannot be read or parsed is skipped with a note rather than
failing the pass, a window with nothing in it proposes nothing, and a caller
with no model available gets a clean report instead of a launch failure.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from ortus.core.agent import make_runner
from ortus.core.profiles import AgentProfile
from ortus.core.transaction import JOURNAL_RELATIVE_PATH
from ortus.core.verdict import assistant_text

#: The one line the pass speaks through, mirroring the verifier's
#: ``ORTUS_VERDICT:`` and the composer's ``ORTUS_COMMIT_MESSAGE:`` envelopes so
#: all three are extracted from a transcript the same way.
ENVELOPE_PREFIX = "ORTUS_RETRO:"

#: How many run records one pass reads. The window is fixed rather than
#: growing with history, so the cost of a retrospective does not grow with the
#: project; older records age out of the window instead of accumulating.
MAX_RECORDS = 8
#: Per-record character bound inside the prompt. Run logs keep their tail
#: (the most recent activity), reports and journals keep their head.
MAX_RECORD_CHARS = 4_000
#: Proposal ceiling per kind. A retrospective that wants to say twenty things
#: has stopped summarizing; extras are dropped with a note, never silently.
MAX_PROPOSALS_PER_KIND = 5

#: Pending keys for proposed issues carry this prefix inside the shared
#: proposal namespace, so the two kinds stay distinguishable in one store and
#: one curation listing.
ISSUE_KEY_PREFIX = "issue-"

#: Same bounded kebab-case slug rule the worker's lesson-proposal block uses.
_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


@dataclass(frozen=True)
class RunRecord:
    """One readable run record, already clipped to its prompt budget."""

    kind: str  # "journal" | "verification" | "run-log"
    name: str
    text: str


@dataclass(frozen=True)
class Proposal:
    """One proposed finding: a durable hazard (lesson) or concrete work (issue)."""

    kind: str  # "lesson" | "issue"
    key: str
    body: str

    @property
    def pending_key(self) -> str:
        """The key this proposal is recorded under in the shared pending store."""

        return self.key if self.kind == "lesson" else ISSUE_KEY_PREFIX + self.key


@dataclass(frozen=True)
class RetroResult:
    """What one pass read, proposed, and had to leave aside."""

    records: tuple[RunRecord, ...]
    skipped: tuple[str, ...]
    recorded: tuple[Proposal, ...]
    duplicates: tuple[Proposal, ...]
    notes: tuple[str, ...]
    #: Set when the pass stopped before proposing anything — no records, or no
    #: model to run. An empty message means the pass ran to completion.
    message: str = ""


class RetroFailed(RuntimeError):
    """The pass ran and produced nothing usable. Advisory, so never fatal
    beyond the invocation that raised it — no run state is touched."""


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


class _Recorder(Protocol):
    """The one tracker write this pass is allowed: a pending proposal."""

    def propose_lesson(self, key: str, body: str) -> bool: ...


def _default_runner_factory(backend: str = "claude") -> _Runner:
    """Indirection so callers (and tests) can swap in a fake backend binary."""

    return make_runner(backend)  # type: ignore[arg-type, return-value]


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


def _clipped(text: str, limit: int, *, tail: bool) -> str:
    if len(text) <= limit:
        return text
    if tail:
        return "[…earlier output omitted]\n" + text[-limit:]
    return text[:limit] + "\n[…rest omitted]"


def collect_records(
    repo: Path, *, limit: int = MAX_RECORDS, max_chars: int = MAX_RECORD_CHARS
) -> tuple[tuple[RunRecord, ...], tuple[str, ...]]:
    """The newest `limit` run records, each clipped, plus skip notes.

    The window is chosen by modification time across all three record kinds
    before anything is read, so an unreadable file inside the window is
    skipped with a note rather than silently widening the window — the bound
    is on what the pass looks at, not on what it manages to parse.
    """

    candidates: list[tuple[Path, str]] = []
    journal = repo / JOURNAL_RELATIVE_PATH
    if journal.is_file():
        candidates.append((journal, "journal"))
    artifacts = repo / "logs" / "grind-transactions"
    candidates.extend((path, "verification") for path in artifacts.glob("*.verifier-*.md"))
    candidates.extend((path, "run-log") for path in (repo / "logs").glob("grind-*.log"))

    def _mtime(entry: tuple[Path, str]) -> float:
        try:
            return entry[0].stat().st_mtime
        except OSError:
            return 0.0

    records: list[RunRecord] = []
    skipped: list[str] = []
    for path, kind in sorted(candidates, key=_mtime, reverse=True)[: max(limit, 0)]:
        try:
            text = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as exc:
            skipped.append(f"{path.name}: unreadable ({exc.__class__.__name__})")
            continue
        if kind == "journal":
            try:
                json.loads(text)
            except ValueError:
                skipped.append(f"{path.name}: journal is not valid JSON")
                continue
        if not text:
            skipped.append(f"{path.name}: empty")
            continue
        records.append(
            RunRecord(kind, path.name, _clipped(text, max_chars, tail=kind == "run-log"))
        )
    return tuple(records), tuple(skipped)


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_EXAMPLE_ENVELOPE = ENVELOPE_PREFIX + " " + json.dumps(
    {
        "lessons": [
            {
                "key": "verifier-sandbox-readonly",
                "lesson": (
                    "the verification sandbox mounts the repo read-only; a "
                    "check that rebuilds artifacts must copy the tree first"
                ),
            }
        ],
        "issues": [
            {
                "key": "dedupe-timeout-recovery",
                "title": "grind: worker-timeout recovery re-claims the issue twice",
                "rationale": "three runs show the same double-claim log line",
            }
        ],
    },
    ensure_ascii=False,
)


def retro_prompt(records: tuple[RunRecord, ...], *, today: str) -> str:
    """The whole contract: the records, the rubric, and the envelope shape."""

    blocks = "\n\n".join(
        f"--- RECORD {index}: {record.kind} {record.name} ---\n{record.text}"
        for index, record in enumerate(records, start=1)
    )
    return f"""RETROSPECTIVE PASS (advisory, read-only, one pass only). Today is {today}.

Below are recent run records from this repository's autonomous pipeline:
candidate journals, verification reports, and grind run logs. You are not
debugging a run, fixing anything, or judging the code. You read what the runs
actually did and propose what they revealed, so findings survive the run that
produced them. Nothing you emit becomes active on its own — a human reviews
every proposal — and you must not create issues, close issues, or change any
tracker state yourself.

Reason only over the records supplied here. Do not open repository source
files to form conclusions about code: a conclusion about code goes stale the
moment the code moves, and the code can be asked fresh when it matters.

Look for exactly three things across the records:

1. Repeated failures — the same symptom appearing in more than one run.
2. Hazards — a trap or defect the records reveal, including defects in the
   pipeline's own behavior, that a future worker or operator would pay for
   again without warning.
3. Gaps — work the records show is needed that no run addressed.

Emit exactly one final assistant line beginning {ENVELOPE_PREFIX} followed by one
JSON object with exactly two array fields, `lessons` and `issues`. Do not emit
that prefix anywhere else.

- A lesson is a durable hazard later sessions must know. Each entry has a
  `key` and a `lesson`. It must be falsifiable — checkable against the
  repository later — and must not restate what the code already says.
- An issue is concrete work someone should do once. Each entry has a `key`, a
  `title`, and a one-sentence `rationale` citing what the records show.
- A finding is a lesson or an issue, never both. Durable hazards nobody can
  act on directly are lessons; actionable work is an issue.
- Every `key` is a bounded kebab-case slug (lowercase letters, digits,
  hyphens, at most 64 characters).
- At most {MAX_PROPOSALS_PER_KIND} entries per array. Empty arrays are a
  correct and common answer: a window of clean runs reveals nothing, and a
  padded retrospective is worse than a silent one.

WORKED EXAMPLE — the shape, not the content:

{_EXAMPLE_ENVELOPE}

--- RUN RECORDS ({len(records)}) ---

{blocks}

--- END RUN RECORDS ---

Write the envelope now, as your final line.
"""


# ---------------------------------------------------------------------------
# Transcript extraction
# ---------------------------------------------------------------------------


def _proposal(kind: str, entry: Any) -> Proposal | str:
    """One validated proposal, or a note saying why the entry was dropped."""

    if not isinstance(entry, dict):
        return f"a proposed {kind} is not a JSON object"
    key = str(entry.get("key") or "").strip()
    if not _KEY_RE.match(key):
        return f"proposed {kind} key {key!r} is not a bounded kebab-case slug"
    if kind == "lesson":
        body = " ".join(str(entry.get("lesson") or "").split())
        if not body:
            return f"proposed lesson {key!r} carries no lesson text"
        return Proposal("lesson", key, body)
    title = " ".join(str(entry.get("title") or "").split())
    if not title:
        return f"proposed issue {key!r} carries no title"
    rationale = " ".join(str(entry.get("rationale") or "").split())
    body = f"proposed issue: {title}" + (f" — {rationale}" if rationale else "")
    return Proposal("issue", key, body)


def parse_proposals(
    log_path: Path, *, start_offset: int = 0
) -> tuple[tuple[Proposal, ...], tuple[str, ...]]:
    """The one envelope this pass emitted, validated entry by entry.

    A malformed entry is dropped with a note rather than voiding the envelope
    — the model's other findings are not hostage to its worst one — but a
    transcript with no envelope, more than one, or a payload that is not the
    two-array object is a failed pass.
    """

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
                        if not stripped.startswith(ENVELOPE_PREFIX):
                            continue
                        encoded = stripped[len(ENVELOPE_PREFIX) :].strip()
                        try:
                            envelopes.append(json.loads(encoded))
                        except json.JSONDecodeError as exc:
                            raise RetroFailed("malformed retrospective JSON") from exc
    except OSError as exc:
        raise RetroFailed(f"transcript unreadable ({exc})") from exc
    if len(envelopes) != 1:
        raise RetroFailed(
            f"expected exactly one retrospective envelope; found {len(envelopes)}"
        )
    payload = envelopes[0]
    if not isinstance(payload, dict):
        raise RetroFailed("retrospective envelope is not a JSON object")

    proposals: list[Proposal] = []
    notes: list[str] = []
    seen: set[tuple[str, str]] = set()
    for kind, field in (("lesson", "lessons"), ("issue", "issues")):
        entries = payload.get(field)
        if not isinstance(entries, list):
            notes.append(f"envelope field {field!r} is not an array; ignored")
            continue
        kept = 0
        for entry in entries:
            validated = _proposal(kind, entry)
            if isinstance(validated, str):
                notes.append(validated)
                continue
            if (validated.kind, validated.key) in seen:
                continue
            if kept >= MAX_PROPOSALS_PER_KIND:
                notes.append(
                    f"proposed {kind} {validated.key!r} dropped: past the "
                    f"{MAX_PROPOSALS_PER_KIND}-per-kind ceiling"
                )
                continue
            seen.add((validated.kind, validated.key))
            proposals.append(validated)
            kept += 1
    return tuple(proposals), tuple(notes)


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


def record_proposals(
    bd: _Recorder, proposals: tuple[Proposal, ...], *, today: str
) -> tuple[tuple[Proposal, ...], tuple[Proposal, ...]]:
    """Record proposals pending, exactly as a worker's lesson proposal is.

    Both kinds land under the shared pending prefix — lessons under their own
    key, issues under :data:`ISSUE_KEY_PREFIX` — so ``ortus curate`` is the
    one review path for everything a retrospective produces. The date travels
    inside the body, as the worker contract requires. Returns
    ``(recorded, duplicates)``; a duplicate is a proposal an accepted lesson
    already covers, which is not recorded twice.
    """

    recorded: list[Proposal] = []
    duplicates: list[Proposal] = []
    for proposal in proposals:
        if bd.propose_lesson(proposal.pending_key, f"{proposal.body} ({today})"):
            recorded.append(proposal)
        else:
            duplicates.append(proposal)
    return tuple(recorded), tuple(duplicates)


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------


def run_retrospective(
    repo: Path,
    *,
    bd: _Recorder,
    today: str,
    log_path: Path,
    backend: str = "claude",
    profile: AgentProfile | None,
    timeout: float | None = None,
    limit: int = MAX_RECORDS,
    max_chars: int = MAX_RECORD_CHARS,
    runner_factory: RunnerFactory = _default_runner_factory,
) -> RetroResult:
    """One bounded advisory pass: read records, propose, record pending.

    Returns a :class:`RetroResult` whose ``message`` explains a pass that
    stopped cleanly before proposing — no records to read (a repository that
    has never run produces nothing), or ``profile`` is None, which is how a
    caller says no model is available to run the pass. Raises
    :class:`RetroFailed` only for a pass that launched and produced nothing
    usable.
    """

    records, skipped = collect_records(repo, limit=limit, max_chars=max_chars)
    if not records:
        return RetroResult(
            records,
            skipped,
            (),
            (),
            (),
            message="no run records found; the retrospective proposes nothing",
        )
    if profile is None:
        return RetroResult(
            records,
            skipped,
            (),
            (),
            (),
            message="no model configured; the retrospective proposes nothing",
        )

    runner = runner_factory() if backend == "claude" else runner_factory("codex")
    configure = getattr(runner, "configure_codegraph", None)
    if callable(configure):
        # The pass reasons over supplied records only; it gets no graph.
        configure(None)
    prompt = retro_prompt(records, today=today)
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
        raise RetroFailed(f"the pass timed out after {timeout}s") from exc
    except Exception as exc:  # noqa: BLE001 - a launch failure is advisory too
        raise RetroFailed(f"the pass could not run ({exc})") from exc
    if rc != 0:
        raise RetroFailed(f"the pass exited {rc}")
    proposals, notes = parse_proposals(log_path, start_offset=offset)
    recorded, duplicates = record_proposals(bd, proposals, today=today)
    return RetroResult(records, skipped, recorded, duplicates, notes)
