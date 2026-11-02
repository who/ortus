"""Dream's orchestrator stage: one call over every reader note, memory and decision.

The orchestrator sees the whole history a reader config has produced, not just
this run's windows: every cached notes file for the current reader key, every
current bd memory and every past Dream decision, each in its own code-made
label. Narrowing flags pick which windows a reader reads; they never filter
what the orchestrator weighs, because prevalence has to span history.

The input travels in a file for the same reason the readers' does. When it is
over `orchestrator_max_tokens`, the oldest notes sections are dropped by run
stem until it fits; memories and decisions are never dropped, so an input
that does not fit without any notes is refused rather than silently thinned.

The orchestrator may ask up to three `ORTUS_DREAM_NEED` questions. Code
answers them once, appends the answers as `[need-answer <n>]` sections and
runs the orchestrator exactly once more; needs raised on that second call are
ignored. Its PROPOSAL, FOLLOWUP and HELPED lines are parsed into frozen
records, and a line with the wrong shape is dropped and counted.
"""

from __future__ import annotations

import datetime as _dt
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol

from ortus.core.bd import BdError
from ortus.core.codegraph import CodeGraphMode
from ortus.core.dream_decisions import Decision, load_decisions, render_decision_section
from ortus.core.dream_readers import (
    BYTES_PER_TOKEN,
    INPUTS_DIR,
    RUNS_DIR,
    _add_usage,
    _events,
    _write_atomic,
    note_lines,
    reader_prompt,
)
from ortus.core.dream_transcripts import CACHE_DIR, escape_labels, scrub
from ortus.core.profiles import AgentProfile
from ortus.core.prompts import substitute
from ortus.core.verdict import assistant_text

#: One orchestrator call's wall clock.
ORCHESTRATOR_TIMEOUT_S = 1800

#: One `codegraph explore` answer's wall clock.
NEED_TIMEOUT_S = 60

#: Needs answered per run, and the longest answer any one of them gets.
MAX_NEEDS = 3
NEED_ANSWER_CHARS = 8192

NOTES_DIR = CACHE_DIR / "notes"

PROPOSAL_PREFIX = "ORTUS_DREAM_PROPOSAL:"
FOLLOWUP_PREFIX = "ORTUS_DREAM_FOLLOWUP:"
HELPED_PREFIX = "ORTUS_DREAM_HELPED:"
NEED_PREFIX = "ORTUS_DREAM_NEED:"

PROPOSAL_KINDS = frozenset({"add", "update", "prune"})


class DreamOrchestratorError(RuntimeError):
    """The orchestrator stage failed; no decision bead may be filed."""


class _Runner(Protocol):
    def run(
        self,
        prompt: str,
        *,
        repo: Path,
        log_path: Path,
        profile: AgentProfile | None = None,
        timeout: float | None = None,
        readonly: bool = False,
    ) -> int: ...

    def preflight_readonly(self, repo: Path, *, timeout: float = 60.0) -> None: ...


class _Client(Protocol):
    def memories(self) -> dict[str, str]: ...

    def show(self, issue_id: str) -> dict[str, Any]: ...

    def list_all(self) -> list[dict[str, Any]]: ...

    def comments(self, issue_id: str) -> list[dict[str, Any]]: ...


# ---------------------------------------------------------------------------
# Composing the input
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Section:
    """One labeled block of the orchestrator input."""

    label: str
    text: str
    run: str = ""

    @property
    def size(self) -> int:
        return len(self.text.encode("utf-8")) + 1


@dataclass(frozen=True)
class ComposedInput:
    """The input text, the labels it carries and how many notes were dropped."""

    text: str
    labels: frozenset[str]
    notes: int
    dropped_notes: int


def _one_line(value: Any) -> str:
    return " ".join(str(value or "").split())


def load_notes(
    repo: Path, key: str, *, only_beads: frozenset[str] | None = None
) -> list[dict[str, Any]]:
    """Every cached notes record for reader `key`, oldest run first.

    With `only_beads`, a record for any other bead is left out.
    """

    records: list[dict[str, Any]] = []
    for path in sorted((repo / NOTES_DIR).glob(f"*.{key}.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(record, dict) or record.get("reader_key", key) != key:
            continue
        if only_beads is not None and record.get("bead") not in only_beads:
            continue
        records.append(record)
    records.sort(key=lambda record: (str(record.get("run") or ""), str(record.get("lines") or "")))
    return records


def notes_section(record: Mapping[str, Any]) -> Section:
    """`[reader-notes bead=<id> run=<stem> lines=<a-b>]`, the NOTE lines, the close."""

    bead = _one_line(record.get("bead")) or "unknown"
    run = _one_line(record.get("run"))
    label = f"[reader-notes bead={bead} run={run}"
    lines = _one_line(record.get("lines"))
    if lines:
        label += f" lines={lines}"
    label += "]"
    notes = [note for note in record.get("notes") or () if isinstance(note, dict)]
    body = escape_labels(note_lines(notes))
    parts = [label, body, "[/reader-notes]"] if body else [label, "[/reader-notes]"]
    return Section(label=label, text="\n".join(parts), run=run)


def memory_section(key: str, body: str) -> Section:
    """`[memory <key>]`, the escaped body, the close."""

    label = f"[memory {_one_line(key)}]"
    return Section(
        label=label, text="\n".join((label, escape_labels(body), "[/memory]"))
    )


def decision_section(decision: Decision) -> Section:
    text = render_decision_section(decision)
    return Section(label=text.split("\n", 1)[0], text=text)


def compose(
    records: Iterable[Mapping[str, Any]],
    memories: Mapping[str, str],
    decisions: Iterable[Decision],
    *,
    max_tokens: int,
) -> ComposedInput:
    """Label every input, then drop the oldest notes until it fits `max_tokens`.

    Raises `DreamOrchestratorError` naming both sizes when memories and
    decisions alone are over the limit: those are never dropped.
    """

    notes = [notes_section(record) for record in records]
    fixed = [memory_section(key, body) for key, body in memories.items()]
    fixed.extend(decision_section(decision) for decision in decisions)
    limit = int(max_tokens * BYTES_PER_TOKEN)
    fixed_size = sum(section.size for section in fixed)
    if fixed_size > limit:
        raise DreamOrchestratorError(
            f"memories and past decisions alone are {fixed_size} bytes "
            f"(~{int(fixed_size / BYTES_PER_TOKEN)} tokens), over "
            f"orchestrator_max_tokens={max_tokens}; refusing to drop memories"
        )
    # Oldest first, so dropping from the front drops by run stem.
    notes.sort(key=lambda section: section.run)
    total = fixed_size + sum(section.size for section in notes)
    dropped = 0
    while notes and total > limit:
        total -= notes.pop(0).size
        dropped += 1
    sections = [*notes, *fixed]
    return ComposedInput(
        text="".join(section.text + "\n" for section in sections),
        labels=frozenset(section.label for section in sections),
        notes=len(notes),
        dropped_notes=dropped,
    )


# ---------------------------------------------------------------------------
# Parsing the answer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Proposal:
    kind: str
    key: str
    body: str
    pattern: str
    why: str
    beads: tuple[str, ...]
    sources: tuple[str, ...]
    quotes: tuple[str, ...]
    stats: Mapping[str, Any]


@dataclass(frozen=True)
class Followup:
    title: str
    why: str
    beads: tuple[str, ...]
    sources: tuple[str, ...]


@dataclass(frozen=True)
class Helped:
    key: str
    before: Mapping[str, int]
    after: Mapping[str, int]


@dataclass(frozen=True)
class ParsedOrchestrator:
    proposals: tuple[Proposal, ...]
    followups: tuple[Followup, ...]
    helped: tuple[Helped, ...]
    needs: tuple[dict[str, str], ...]
    invalid_lines: int
    usage: dict[str, Any] | None
    cost_usd: float | None


def _text(value: Any, *, required: bool = True) -> str | None:
    if not isinstance(value, str):
        return None
    if required and not value.strip():
        return None
    return value


def _strings(value: Any, *, required: bool = True) -> tuple[str, ...] | None:
    if not isinstance(value, list) or (required and not value):
        return None
    if not all(isinstance(item, str) for item in value):
        return None
    return tuple(value)


def _counts(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    for name in ("beads", "windows"):
        count = value.get(name)
        if not isinstance(count, int) or isinstance(count, bool):
            return None
    return dict(value)


def parse_proposal(data: Any) -> Proposal | None:
    if not isinstance(data, dict) or data.get("kind") not in PROPOSAL_KINDS:
        return None
    key = _text(data.get("key"))
    body = _text(data.get("body"), required=data.get("kind") != "prune")
    pattern = _text(data.get("pattern"))
    why = _text(data.get("why"))
    beads = _strings(data.get("beads"))
    sources = _strings(data.get("sources"))
    quotes = _strings(data.get("quotes"))
    stats = data.get("stats")
    if None in (key, body, pattern, why, beads, sources, quotes) or not isinstance(stats, dict):
        return None
    return Proposal(
        kind=data["kind"],
        key=key,
        body=body,
        pattern=pattern,
        why=why,
        beads=beads,
        sources=sources,
        quotes=quotes,
        stats=dict(stats),
    )


def parse_followup(data: Any) -> Followup | None:
    if not isinstance(data, dict):
        return None
    title = _text(data.get("title"))
    why = _text(data.get("why"))
    beads = _strings(data.get("beads"))
    sources = _strings(data.get("sources"))
    if None in (title, why, beads, sources):
        return None
    return Followup(title=title, why=why, beads=beads, sources=sources)


def parse_helped(data: Any) -> Helped | None:
    if not isinstance(data, dict):
        return None
    key = _text(data.get("key"))
    before = _counts(data.get("before"))
    after = _counts(data.get("after"))
    if None in (key, before, after):
        return None
    return Helped(key=key, before=before, after=after)


def parse_need(data: Any) -> dict[str, str] | None:
    if not isinstance(data, dict) or len(data) != 1:
        return None
    (name, value), = data.items()
    if name not in ("codegraph", "bd_show") or _text(value) is None:
        return None
    return {name: value}


_PARSERS: tuple[tuple[str, Callable[[Any], Any]], ...] = (
    (PROPOSAL_PREFIX, parse_proposal),
    (FOLLOWUP_PREFIX, parse_followup),
    (HELPED_PREFIX, parse_helped),
    (NEED_PREFIX, parse_need),
)


def parse_answer(log_path: Path) -> ParsedOrchestrator:
    """Every PROPOSAL, FOLLOWUP, HELPED and NEED line from one call's log."""

    found: dict[str, list[Any]] = {prefix: [] for prefix, _ in _PARSERS}
    invalid = 0
    result: dict[str, Any] | None = None
    for event in _events(log_path):
        if event.get("type") == "result":
            result = event
        for text in assistant_text(event):
            for line in text.splitlines():
                stripped = line.strip()
                for prefix, parse in _PARSERS:
                    if not stripped.startswith(prefix):
                        continue
                    try:
                        parsed = parse(json.loads(stripped[len(prefix):].strip()))
                    except json.JSONDecodeError:
                        parsed = None
                    if parsed is None:
                        invalid += 1
                    else:
                        found[prefix].append(parsed)
                    break
    usage = result.get("usage") if result is not None else None
    cost = result.get("total_cost_usd") if result is not None else None
    return ParsedOrchestrator(
        proposals=tuple(found[PROPOSAL_PREFIX]),
        followups=tuple(found[FOLLOWUP_PREFIX]),
        helped=tuple(found[HELPED_PREFIX]),
        needs=tuple(found[NEED_PREFIX]),
        invalid_lines=invalid,
        usage=usage if isinstance(usage, dict) else None,
        cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
    )


# ---------------------------------------------------------------------------
# Answering needs
# ---------------------------------------------------------------------------


def codegraph_explore(repo: Path, query: str, mode: CodeGraphMode) -> str:
    """`codegraph explore <query>`, or why it cannot be asked."""

    if mode is CodeGraphMode.OFF:
        return "unavailable: codegraph off"
    binary = shutil.which("codegraph")
    if binary is None:
        return "unavailable: codegraph not on PATH"
    try:
        proc = subprocess.run(
            [binary, "explore", query],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=NEED_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return f"unavailable: codegraph explore timed out after {NEED_TIMEOUT_S}s"
    except OSError as exc:
        return f"unavailable: {exc}"
    if proc.returncode != 0:
        return f"codegraph explore exited {proc.returncode}\n{proc.stdout}{proc.stderr}"
    return proc.stdout


def answer_need(
    need: Mapping[str, str],
    *,
    client: _Client,
    explore: Callable[[str], str],
) -> str:
    """The raw answer to one need, before clipping and scrubbing."""

    if "codegraph" in need:
        return explore(need["codegraph"])
    try:
        return json.dumps(client.show(need["bd_show"]), ensure_ascii=False)
    except BdError as exc:
        return f"unavailable: bd show failed: {_one_line(exc.stderr) or exc.returncode}"


def need_section(index: int, need: Mapping[str, str], answer: str, repo: Path) -> Section:
    label = f"[need-answer {index}]"
    clipped = answer[:NEED_ANSWER_CHARS]
    body = escape_labels(scrub(clipped, repo))
    asked = json.dumps(dict(need), ensure_ascii=False)
    return Section(label=label, text="\n".join((label, f"need: {asked}", body, "[/need-answer]")))


# ---------------------------------------------------------------------------
# Running the orchestrator
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrchestratorResult:
    proposals: tuple[Proposal, ...]
    followups: tuple[Followup, ...]
    helped: tuple[Helped, ...]
    invalid_lines: int
    input_labels: frozenset[str]
    dropped_notes: int
    usage: dict[str, Any] | None
    cost_usd: float | None
    needs_answered: int


def _call(
    runner: _Runner,
    *,
    repo: Path,
    prompt: str,
    profile: AgentProfile,
    log_path: Path,
    what: str,
) -> ParsedOrchestrator:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        code = runner.run(
            prompt,
            repo=repo,
            log_path=log_path,
            profile=profile,
            timeout=ORCHESTRATOR_TIMEOUT_S,
            readonly=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise DreamOrchestratorError(
            f"{what} timed out after {ORCHESTRATOR_TIMEOUT_S // 60} min"
        ) from exc
    if code != 0:
        raise DreamOrchestratorError(f"{what} exited {code}; see {log_path}")
    return parse_answer(log_path)


def run_orchestrator(
    *,
    runner: _Runner,
    repo: Path,
    client: _Client,
    brief: str,
    key: str,
    profile: AgentProfile,
    min_beads: int,
    max_tokens: int,
    codegraph_mode: CodeGraphMode,
    stamp: str | None = None,
    log: Callable[[str], None] = lambda message: None,
    explore: Callable[[str], str] | None = None,
    only_beads: frozenset[str] | None = None,
) -> OrchestratorResult:
    """Compose the input, run the orchestrator, answer its needs once, parse.

    `only_beads`, when given, limits the notes fed to those beads; the other
    cached notes stay on disk. Raises `DreamOrchestratorError` on a timeout,
    a nonzero exit, a failed read-only preflight or memories too large to
    fit; `BdError` from reading memories or decisions propagates.
    """

    records = load_notes(repo, key, only_beads=only_beads)
    memories = client.memories()
    if not records and not memories:
        log("orchestrator: no reader notes and no memories; nothing to weigh")
        return OrchestratorResult(
            proposals=(),
            followups=(),
            helped=(),
            invalid_lines=0,
            input_labels=frozenset(),
            dropped_notes=0,
            usage=None,
            cost_usd=None,
            needs_answered=0,
        )
    decisions = load_decisions(client)
    composed = compose(records, memories, decisions, max_tokens=max_tokens)
    if composed.dropped_notes:
        log(
            f"orchestrator: input over {max_tokens} tokens; dropped the "
            f"{composed.dropped_notes} oldest notes section(s)"
        )
    try:
        runner.preflight_readonly(repo)
    except RuntimeError as exc:
        raise DreamOrchestratorError(f"read-only preflight failed: {exc}") from exc

    stamp = stamp or _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    filled = substitute(brief, min_beads=str(min_beads))
    input_path = repo / INPUTS_DIR / f"orchestrator-{stamp}.md"
    _write_atomic(input_path, composed.text)
    run_dir = repo / RUNS_DIR / stamp
    log(
        f"orchestrator: {composed.notes} notes section(s), {len(memories)} "
        f"memory(ies), {len(decisions)} decision(s) with {profile.display_name} "
        f"(up to {ORCHESTRATOR_TIMEOUT_S // 60} min)"
    )
    first = _call(
        runner,
        repo=repo,
        prompt=reader_prompt(filled, input_path),
        profile=profile,
        log_path=run_dir / "orchestrator.jsonl",
        what="orchestrator",
    )
    final = first
    labels = set(composed.labels)
    needs = first.needs[:MAX_NEEDS]
    if needs:
        ask = explore or (lambda query: codegraph_explore(repo, query, codegraph_mode))
        answers = [
            need_section(index, need, answer_need(need, client=client, explore=ask), repo)
            for index, need in enumerate(needs, start=1)
        ]
        labels.update(section.label for section in answers)
        need_input = repo / INPUTS_DIR / f"orchestrator-{stamp}.need.md"
        _write_atomic(
            need_input,
            composed.text + "".join(section.text + "\n" for section in answers),
        )
        log(f"orchestrator: answered {len(needs)} need(s); running once more")
        final = _call(
            runner,
            repo=repo,
            prompt=reader_prompt(filled, need_input),
            profile=profile,
            log_path=run_dir / "orchestrator.need.jsonl",
            what="orchestrator need re-run",
        )

    usage = None
    cost = None
    for call in {id(first): first, id(final): final}.values():
        if call.usage is not None:
            usage = _add_usage(usage, call.usage)
        if call.cost_usd is not None:
            cost = (cost or 0.0) + call.cost_usd
    result = OrchestratorResult(
        proposals=final.proposals,
        followups=final.followups,
        helped=final.helped,
        invalid_lines=final.invalid_lines,
        input_labels=frozenset(labels),
        dropped_notes=composed.dropped_notes,
        usage=usage,
        cost_usd=cost,
        needs_answered=len(needs),
    )
    log(
        f"orchestrator: {len(result.proposals)} proposal(s), "
        f"{len(result.followups)} follow-up(s), {len(result.helped)} helped, "
        f"{result.invalid_lines} invalid line(s)"
    )
    return result
