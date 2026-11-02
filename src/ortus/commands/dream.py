"""`ortus dream` — select and prepare grind-log bead windows for memory review.

Dream reads finished grind logs out of band. This module owns the first two
stages: every window of every grind log is prepared (and cached), the windows
that still need a reader are selected newest first under the caps, and the
token cost of reading them is estimated before any model is called. A real
run then hands each candidate to a read-only reader (`dream_readers`), caches
its notes, and runs one read-only orchestrator (`dream_orchestrator`) over
every cached note, memory and past decision. Code then validates what the
orchestrator drafted (`dream_validate`), and the report stage
(`dream_report`) writes `logs/dream-<ts>.md` and files one decision bead.

The verb holds the grind flock for the whole run, so Dream never reads a log
a live grind is still writing, and it writes nothing outside `.ortus/dream/`.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import typer

from ortus.core import output
from ortus.core.agent import BackendError, make_runner, resolve_backend
from ortus.core.bd import BdClient, BdError
from ortus.core.codegraph import CodeGraphMode
from ortus.core.config import DREAM_SINCE, dream_settings, load_config
from ortus.core.cost import find_grind_logs
from ortus.core.dream_decisions import load_decisions
from ortus.core.dream_orchestrator import (
    NOTES_DIR,
    DreamOrchestratorError,
    OrchestratorResult,
    load_notes,
    run_orchestrator,
)
from ortus.core.dream_readers import (
    ReaderResult,
    BYTES_PER_TOKEN,
    DreamReaderError,
    run_log_writer,
    run_readers,
)
from ortus.core.dream_transcripts import (
    SKIPPED_UNKNOWN_SHAPE,
    DreamCacheNotIgnored,
    PreparedWindow,
    ensure_cache_ignored,
    load_bead_header,
    load_or_prepare,
    render_window,
    split_windows,
    window_key,
)
from ortus.core.dream_report import RunFacts, deliver
from ortus.core.dream_validate import ValidationResult, validate
from ortus.core.grind_logic import FlockBusy, grind_flock
from ortus.core.profiles import AgentProfile, Phase, ProfileError, resolve_dream_profile
from ortus.core.prompts import PromptNotFound, resolve_named_prompt
from ortus.core.repo import resolve_repo

#: Bumped when reader notes change shape for the same window and brief, so
#: notes cached under the old key are read again rather than reused.
READER_VERSION = 1

#: Fixed orchestrator overhead on top of its notes: brief, memories, decisions.
ORCHESTRATOR_OVERHEAD_TOKENS = 20_000

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REFUSED = 2
EXIT_NOTHING = 3


def reader_key(backend: str, profile: AgentProfile, brief_text: str) -> str:
    """Which reader produced a note: backend, model, effort, brief and version.

    Any change to one of them is a different reader, so its notes are a
    different cache entry and an A/B between readers never mixes them.
    """

    brief = hashlib.sha256(brief_text.encode("utf-8")).hexdigest()
    material = "|".join(
        (
            backend,
            profile.model or "",
            profile.reasoning_effort or "",
            brief,
            str(READER_VERSION),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def notes_path(repo: Path, prepared: PreparedWindow, key: str) -> Path:
    """`.ortus/dream/notes/<window_key>.<reader_key>.json` for one window."""

    return repo / NOTES_DIR / f"{window_key(prepared.window)}.{key}.json"


def _tokens(size: int) -> int:
    return math.ceil(size / BYTES_PER_TOKEN)


def _since_seconds(since: str) -> float:
    count, unit = int(since[:-1]), since[-1]
    return count * (86_400 if unit == "d" else 3_600)


@dataclass(frozen=True)
class Selection:
    """What one Dream pass found, chose and expects to spend."""

    windows_total: int
    candidates: tuple[PreparedWindow, ...]
    reused: int
    skipped_unknown_shape: int
    bytes: int
    est_reader_tokens: int
    est_orchestrator_tokens: int
    chunks: int
    over_budget: bool
    logs: int
    skipped_by_reason: dict[str, int] = field(default_factory=dict)

    def summary(self, reader: AgentProfile, orchestrator: AgentProfile) -> dict[str, Any]:
        return {
            "windows_total": self.windows_total,
            "candidates": len(self.candidates),
            "reused": self.reused,
            "skipped_unknown_shape": self.skipped_unknown_shape,
            "skipped_by_reason": dict(self.skipped_by_reason),
            "bytes": self.bytes,
            "est_reader_tokens": self.est_reader_tokens,
            "est_orchestrator_tokens": self.est_orchestrator_tokens,
            "chunks": self.chunks,
            "over_budget": self.over_budget,
            "reader_profile": reader.display_name,
            "orchestrator_profile": orchestrator.display_name,
        }


def _log_paths(repo: Path, logs: list[Path]) -> list[Path]:
    """Every grind log under `logs/`, plus any `--logs` file outside it."""

    found = list(find_grind_logs(repo, newest=0))
    known = {path.resolve() for path in found}
    for path in logs:
        resolved = path.expanduser().resolve()
        if resolved not in known:
            found.append(resolved)
            known.add(resolved)
    return found


def _prepare_all(repo: Path, paths: list[Path], cut_bytes: int) -> list[PreparedWindow]:
    prepared: list[PreparedWindow] = []
    for path in paths:
        try:
            windows = split_windows(path)
        except OSError as exc:
            output.warn(f"skipping {path}: {exc.strerror or exc}")
            continue
        prepared.extend(
            load_or_prepare(window, repo=repo, cut_bytes=cut_bytes) for window in windows
        )
    return prepared


def select_windows(
    repo: Path,
    prepared: list[PreparedWindow],
    *,
    key: str,
    settings: dict[str, Any],
    logs: list[Path],
    beads: list[str],
    reread: bool,
    log_count: int,
    now: float | None = None,
) -> Selection:
    """Narrow, cap and estimate the windows a reader still has to read.

    A window matches when it passes every narrowing flag given; it is a
    candidate when it also has no notes for this reader (or `reread` is set).
    Candidates are taken newest first by log stem, then by start line. The
    orchestrator estimate counts every existing note for this reader,
    matching or not, because the orchestrator reads them all.
    """

    since = settings.get("since")
    cutoff = None if since is None else (now or time.time()) - _since_seconds(since)
    log_set = {path.expanduser().resolve() for path in logs}
    bead_set = set(beads)

    def matches(item: PreparedWindow) -> bool:
        window = item.window
        if log_set and window.log_path.resolve() not in log_set:
            return False
        if bead_set and window.bead_id not in bead_set:
            return False
        if cutoff is not None:
            try:
                if window.log_path.stat().st_mtime < cutoff:
                    return False
            except OSError:
                return False
        return True

    readable = [item for item in prepared if item.skipped is None]
    by_reason = Counter(item.skipped for item in prepared if item.skipped is not None)
    paths = {id(item): notes_path(repo, item, key) for item in readable}
    noted = {item_id: path for item_id, path in paths.items() if path.is_file()}
    pool = [
        item
        for item in readable
        if matches(item) and (reread or id(item) not in noted)
    ]
    pool.sort(key=lambda item: (item.window.run, item.window.start_line), reverse=True)
    chosen = tuple(pool[: settings["max_beads"]])
    chosen_ids = {id(item) for item in chosen}

    sizes = [len(render_window(item, "").encode("utf-8")) for item in chosen]
    total = sum(sizes)
    chunk_tokens = settings["reader_chunk_tokens"]
    chunks = sum(max(1, math.ceil(_tokens(size) / chunk_tokens)) for size in sizes)
    notes_bytes = sum(path.stat().st_size for path in noted.values())
    est_reader = _tokens(total)
    est_orchestrator = _tokens(notes_bytes) + ORCHESTRATOR_OVERHEAD_TOKENS
    return Selection(
        windows_total=len(prepared),
        candidates=chosen,
        reused=sum(1 for item_id in noted if item_id not in chosen_ids),
        skipped_unknown_shape=len(prepared) - len(readable),
        bytes=total,
        est_reader_tokens=est_reader,
        est_orchestrator_tokens=est_orchestrator,
        chunks=chunks,
        over_budget=est_reader + est_orchestrator > settings["budget_tokens"],
        logs=log_count,
        skipped_by_reason=dict(sorted(by_reason.items())),
    )


def validate_run(
    repo: Path,
    client: BdClient,
    result: OrchestratorResult,
    prepared: list[PreparedWindow],
    *,
    key: str,
    min_beads: int,
) -> ValidationResult:
    """Validate the orchestrator's answer against the caches it was drawn from."""

    validation = validate(
        result,
        windows=prepared,
        records=load_notes(repo, key),
        memories=client.memories(),
        decisions=load_decisions(client),
        min_beads=min_beads,
    )
    output.progress(
        "dream",
        f"validated: {len(validation.proposals)} proposal(s), "
        f"{len(validation.followups)} follow-up(s) kept, "
        f"{len(validation.drops)} dropped",
    )
    return validation


def run_facts(
    client: BdClient,
    *,
    backend: str,
    reader: AgentProfile,
    orchestrator: AgentProfile,
    selection: Selection,
    readers: list[ReaderResult],
    windows_fed: int,
    result: OrchestratorResult,
) -> RunFacts:
    """Gather the report's run facts; a bd read that fails reads as unavailable."""

    memories = client.memories()
    try:
        prime_chars: int | None = len(client.prime())
    except BdError:
        prime_chars = None
    return RunFacts(
        backend=backend,
        reader=reader.display_name,
        orchestrator=orchestrator.display_name,
        windows_read=sum(1 for item in readers if item.notes_file is not None),
        windows_reused=selection.reused,
        windows_fed=windows_fed,
        chunked_windows=sum(1 for item in readers if item.chunks > 1),
        usage=result.usage,
        cost_usd=result.cost_usd,
        memory_chars=sum(len(value) for value in memories.values()),
        memory_keys=len(memories),
        prime_chars=prime_chars,
    )


def _skip_label(reason: str) -> str:
    """`backend-grok` reads as `backend grok`; other reasons as recorded."""

    if reason.startswith("backend-"):
        return "backend " + reason.removeprefix("backend-")
    if reason == SKIPPED_UNKNOWN_SHAPE:
        return "unknown shape"
    return reason


def _report(selection: Selection, reader: AgentProfile, orchestrator: AgentProfile) -> None:
    rows = [
        ("windows found", str(selection.windows_total)),
        ("needing a reader", str(len(selection.candidates))),
        ("reused notes", str(selection.reused)),
        *(
            (f"skipped ({_skip_label(reason)})", str(count))
            for reason, count in selection.skipped_by_reason.items()
        ),
        ("prepared bytes", str(selection.bytes)),
        ("reader chunks", str(selection.chunks)),
        ("est. reader tokens", str(selection.est_reader_tokens)),
        ("est. orchestrator tokens", str(selection.est_orchestrator_tokens)),
        ("reader", reader.display_name),
        ("orchestrator", orchestrator.display_name),
    ]
    width = max(len(label) for label, _ in rows)
    for label, value in rows:
        typer.echo(f"{label:<{width}}  {value}")
    if selection.over_budget:
        typer.echo("over budget")


def dream(
    repo: Optional[Path] = typer.Argument(
        None, help="Repository whose grind logs Dream reads (default: cwd)."
    ),
    since: Optional[str] = typer.Option(
        None, "--since", help="Only logs modified within this age: <N>d or <N>h."
    ),
    logs: Optional[list[Path]] = typer.Option(
        None, "--logs", help="Only windows from this log file (repeatable)."
    ),
    beads: Optional[list[str]] = typer.Option(
        None, "--beads", help="Only windows for this bead id (repeatable)."
    ),
    reader_model: Optional[str] = typer.Option(
        None, "--reader-model", help="Model for the per-window readers."
    ),
    reader_reasoning_effort: Optional[str] = typer.Option(
        None, "--reader-reasoning-effort", help="Reasoning effort for the readers."
    ),
    orchestrator_model: Optional[str] = typer.Option(
        None, "--orchestrator-model", help="Model for the orchestrator."
    ),
    orchestrator_reasoning_effort: Optional[str] = typer.Option(
        None,
        "--orchestrator-reasoning-effort",
        help="Reasoning effort for the orchestrator.",
    ),
    backend: Optional[str] = typer.Option(
        None, "--backend", help="claude, codex, grok, local or opencode."
    ),
    max_beads: Optional[int] = typer.Option(
        None, "--max-beads", min=1, help="Most windows read in one pass (default 40)."
    ),
    budget_tokens: Optional[int] = typer.Option(
        None,
        "--budget-tokens",
        min=1,
        help="Refuse a run estimated above this many tokens (default 1500000).",
    ),
    min_beads: Optional[int] = typer.Option(
        None,
        "--min-beads",
        min=2,
        help="Distinct beads a pattern needs before it is proposed (default 2).",
    ),
    cut_bytes: Optional[int] = typer.Option(
        None,
        "--cut-bytes",
        min=1024,
        help="Cut any transcript string longer than this (default 8192).",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Select and estimate only; call no model."
    ),
    no_file: bool = typer.Option(
        False, "--no-file", help="Report proposals without filing a decision bead."
    ),
    reread: bool = typer.Option(
        False, "--reread", help="Read matching windows again despite cached notes."
    ),
    json_out: bool = typer.Option(
        False, "--json", help="Print the selection summary as one JSON object."
    ),
) -> None:
    """Propose memory changes from finished grind logs (read-only toward bd and git).

    Exit codes: 0 ok, 1 error, 2 refused (lock held, cache not ignored, over
    budget), 3 nothing to propose.
    """

    target = resolve_repo(repo)
    if since is not None and not DREAM_SINCE.match(since):
        output.error(f"invalid --since {since!r}; expected <N>d or <N>h, e.g. 7d")
        raise typer.Exit(code=EXIT_ERROR)
    try:
        config = load_config(repo=target)
        backend_name = resolve_backend(backend, repo=target)
        reader = resolve_dream_profile(
            config,
            backend_name,
            Phase.DREAM_READER,
            model=reader_model,
            reasoning_effort=reader_reasoning_effort,
        )
        orchestrator = resolve_dream_profile(
            config,
            backend_name,
            Phase.DREAM_ORCHESTRATOR,
            model=orchestrator_model,
            reasoning_effort=orchestrator_reasoning_effort,
        )
        brief = resolve_named_prompt("dream-reader", repo=target).text
        orchestrator_brief = resolve_named_prompt("dream-orchestrator", repo=target).text
        codegraph_mode = CodeGraphMode(config.get("codegraph", "auto"))
    except (ProfileError, BackendError, PromptNotFound) as exc:
        output.error(str(exc))
        raise typer.Exit(code=EXIT_ERROR) from exc
    except ValueError as exc:
        output.error(f"invalid codegraph policy in .ortusrc: {exc}")
        output.error(str(exc))
        raise typer.Exit(code=EXIT_ERROR) from exc

    settings = dream_settings(config)
    overrides = {
        "since": since,
        "max_beads": max_beads,
        "budget_tokens": budget_tokens,
        "min_beads": min_beads,
        "cut_bytes": cut_bytes,
    }
    settings.update({k: v for k, v in overrides.items() if v is not None})
    key = reader_key(backend_name, reader, brief)

    try:
        with grind_flock(target):
            try:
                ensure_cache_ignored(target)
            except DreamCacheNotIgnored as exc:
                output.error(str(exc))
                raise typer.Exit(code=EXIT_REFUSED) from exc
            paths = _log_paths(target, list(logs or []))
            output.progress("dream", f"preparing windows from {len(paths)} grind log(s)")
            prepared = _prepare_all(target, paths, settings["cut_bytes"])
            selection = select_windows(
                target,
                prepared,
                key=key,
                settings=settings,
                logs=list(logs or []),
                beads=list(beads or []),
                reread=reread,
                log_count=len(paths),
            )
            if json_out:
                typer.echo(json.dumps(selection.summary(reader, orchestrator)))
            else:
                _report(selection, reader, orchestrator)
            if dry_run:
                output.progress(
                    "dream",
                    f"done (dry run, {len(selection.candidates)} candidate window(s))",
                )
                return
            if not selection.logs:
                output.progress("dream", "done (no grind logs to read)")
                raise typer.Exit(code=EXIT_NOTHING)
            if selection.over_budget:
                output.error(
                    f"estimated {selection.est_reader_tokens + selection.est_orchestrator_tokens}"
                    f" tokens exceeds the budget of {settings['budget_tokens']}",
                    hint="narrow with --since, --logs, --beads or --max-beads, "
                    "or raise --budget-tokens",
                )
                raise typer.Exit(code=EXIT_REFUSED)
            try:
                runner = make_runner(backend_name, repo=target)
                bd = BdClient(repo=target)
                stamp = time.strftime("%Y%m%d-%H%M%S")
                readers = run_readers(
                    selection.candidates,
                    runner=runner,
                    repo=target,
                    header_for=lambda bead_id: load_bead_header(bd, bead_id),
                    brief=brief,
                    key=key,
                    backend=backend_name,
                    profile=reader,
                    chunk_tokens=settings["reader_chunk_tokens"],
                    cut_bytes=settings["cut_bytes"],
                    notes_path_for=lambda item: notes_path(target, item, key),
                    stamp=stamp,
                )
                result = run_orchestrator(
                    runner=runner,
                    repo=target,
                    client=bd,
                    brief=orchestrator_brief,
                    key=key,
                    profile=orchestrator,
                    min_beads=settings["min_beads"],
                    max_tokens=settings["orchestrator_max_tokens"],
                    codegraph_mode=codegraph_mode,
                    stamp=stamp,
                    log=run_log_writer(target, stamp),
                )
                validation = validate_run(
                    target,
                    bd,
                    result,
                    prepared,
                    key=key,
                    min_beads=settings["min_beads"],
                )
                fed = [
                    window_key(item.window)
                    for item in prepared
                    if item.skipped is None and notes_path(target, item, key).is_file()
                ]
                delivery = deliver(
                    validation,
                    run_facts(
                        bd,
                        backend=backend_name,
                        reader=reader,
                        orchestrator=orchestrator,
                        selection=selection,
                        readers=readers,
                        windows_fed=len(fed),
                        result=result,
                    ),
                    repo=target,
                    client=bd,
                    stamp=stamp,
                    windows=fed,
                    reader_key=key,
                    orchestrator_profile=orchestrator.display_name,
                    no_file=no_file,
                )
            except (
                BackendError,
                BdError,
                DreamReaderError,
                DreamOrchestratorError,
                OSError,
            ) as exc:
                output.error(str(exc))
                raise typer.Exit(code=EXIT_ERROR) from exc
            if delivery.error:
                output.error(delivery.error)
            if delivery.bead_id:
                typer.echo(delivery.bead_id)
            filed = delivery.bead_id or "no bead filed"
            output.progress("dream", f"done ({filed}, report {delivery.report_path})")
            if delivery.exit_code != EXIT_OK:
                raise typer.Exit(code=delivery.exit_code)
    except FlockBusy as exc:
        output.error(
            f"{exc}; Dream waits for the grind to finish",
            hint="run ortus dream after the grind exits, or ortus unlock a stale lock",
        )
        raise typer.Exit(code=EXIT_REFUSED) from exc
