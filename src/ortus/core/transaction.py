"""Recoverable ownership records for grind candidate transactions."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import subprocess
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Iterable

from ortus.core.lifecycle import (
    CORRECTION,
    FINALIZATION_STEPS,
    IMPLEMENTATION,
    PLAN_GAP_ROUTED,
    VERIFICATION,
    finalized_phase,
)

JOURNAL_SCHEMA = 4
JOURNAL_RELATIVE_PATH = Path("logs") / "grind-transaction.json"
_MAX_EVIDENCE_CHARS = 16_000
#: Handoff records are audit context, not state. Keep the recent ones so a
#: repeatedly-resumed transaction cannot grow its journal without bound.
_MAX_HANDOFFS = 8

# `FINALIZATION_STEPS` is imported above and re-exported from here for the
# callers that already read it off this module. `lifecycle` owns the
# declaration so the phase graph can derive its `finalized-*` states without
# importing the journal. Each phase transition is still journaled *after* it
# lands, so a restart replays only the steps that never completed and can
# never duplicate a comment, close, commit, or push.


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


#: The `bd show --json` keys that state what an issue asks for. A verdict is
#: bound to exactly these and to nothing else bd reports.
#:
#: An allowlist rather than a denylist, because a guard whose false positives
#: cost a whole session must fail closed: every key bd grows later is scheduling
#: or bookkeeping until someone deliberately promotes it, not load-bearing by
#: default. Three exclusions are worth naming:
#:
#: * ``dependents`` changes when a *different* issue is edited, so hashing it
#:   lets ``bd dep add`` poison a claimed issue nobody touched.
#: * ``notes`` is prose but stays out: it is where operators and workers record
#:   evidence and measurements mid-run, and hashing it makes recording evidence
#:   fatal. The criteria a verifier actually runs live in ``acceptance_criteria``.
#: * ``comments`` stays out for the same reason it always did — Ortus appends a
#:   verifier report on *every* attempt, so hashing it would reject each
#:   correction's re-verification by construction.
#:
#: Status is excluded too: it is re-checked directly against bd at each
#: lifecycle phase transition, where a stale-status failure can be reported precisely
#: instead of hiding inside an opaque hash mismatch.
CONTRACT_PACKET_FIELDS: tuple[str, ...] = (
    "acceptance_criteria",
    "description",
    "design",
    "issue_type",
    "title",
)


def authoritative_packet(packet: dict[str, Any]) -> dict[str, Any]:
    """The work-spec fields a verdict is legitimately bound to.

    Every contract field is always present in the projection, so an issue that
    omits one and an issue that leaves it empty hash identically.
    """

    return {key: packet.get(key) or "" for key in CONTRACT_PACKET_FIELDS}


def issue_packet_hash(packet: dict[str, Any]) -> str:
    """Bind verification to the exact authoritative work spec."""

    return sha256_bytes(canonical_json(authoritative_packet(packet)))


def _excerpt(value: Any, width: int) -> str:
    """One work-spec value, collapsed to a single quoted line for a message."""

    text = " ".join(str(value).split())
    if len(text) > width:
        text = text[: max(width - 1, 0)] + "…"
    return json.dumps(text, ensure_ascii=False)


def contract_packet_changes(
    before: dict[str, Any], after: dict[str, Any], *, width: int = 80
) -> tuple[str, ...]:
    """The contract fields that differ, each as ``field: before -> after``.

    This is the whole of what a hash mismatch actually knows. Grind compares two
    digests of the same issue at two times; it cannot see who wrote the change,
    so the report names the fields and leaves attribution to the operator.
    """

    left = authoritative_packet(before)
    right = authoritative_packet(after)
    return tuple(
        f"{key}: {_excerpt(left[key], width)} -> {_excerpt(right[key], width)}"
        for key in CONTRACT_PACKET_FIELDS
        if left[key] != right[key]
    )


def _path_fingerprint(repo: Path, relative: str) -> str:
    path = repo / relative
    if path.is_symlink():
        payload = b"symlink\0" + os.readlink(path).encode(
            "utf-8", errors="surrogateescape"
        )
    elif path.is_file():
        payload = b"file\0" + path.read_bytes()
    elif path.is_dir():
        payload = b"directory"
    else:
        payload = b"missing"
    return sha256_bytes(payload)


def fingerprint_paths(repo: Path, paths: Iterable[str]) -> dict[str, str]:
    """Hash worktree representations so baseline edits cannot be absorbed."""

    return {path: _path_fingerprint(repo, path) for path in sorted(set(paths))}


def candidate_diff(
    repo: Path, paths: Iterable[str], *, base: str = "", tip: str = ""
) -> bytes:
    """Return a deterministic, binary-safe diff bundle for candidate paths.

    Normal ``git diff HEAD`` covers staged, unstaged, deleted, and binary tracked
    files. Untracked files are appended as binary patches against ``/dev/null``.
    The bundle is complete on disk; prompts refer to it by immutable hash so a
    large or binary candidate is never silently truncated.

    `base` names the commit the candidate is measured against. Empty means
    HEAD — the pre-branch behavior, where a candidate is worktree state only.
    A branch-scoped candidate passes its recorded base head instead, so
    commits the worker made on its issue branch are part of the bundle rather
    than invisible to it.

    `tip` pins the other end to a ref instead of the worktree — the reading a
    primary repository parked on the integration branch needs, where the
    checkout is deliberately not the candidate's tree (ortus-bz3c). With a
    tip, the bundle is committed content only and no untracked appendix
    applies.
    """

    selected = tuple(sorted(set(paths)))
    if not selected:
        return b""
    if base:
        resolved = subprocess.run(
            ["git", "rev-parse", "--verify", base],
            cwd=repo,
            capture_output=True,
            check=False,
        )
        if resolved.returncode != 0:
            raise RuntimeError(
                f"candidate base {base!r} does not resolve: "
                + resolved.stderr.decode("utf-8", errors="replace").strip()
            )
    head = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    if not base:
        base = (
            "HEAD"
            if head.returncode == 0
            else "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
        )
    endpoints = [base, tip] if tip else [base]
    tracked = subprocess.run(
        ["git", "diff", "--binary", "--no-ext-diff", *endpoints, "--", *selected],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    if tracked.returncode != 0:
        raise RuntimeError(tracked.stderr.decode("utf-8", errors="replace").strip())
    if tip:
        return tracked.stdout
    untracked = subprocess.run(
        ["git", "ls-files", "-z", "--others", "--exclude-standard", "--", *selected],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    if untracked.returncode != 0:
        raise RuntimeError(untracked.stderr.decode("utf-8", errors="replace").strip())
    chunks = [tracked.stdout]
    for raw_path in sorted(filter(None, untracked.stdout.split(b"\0"))):
        relative = raw_path.decode("utf-8", errors="surrogateescape")
        patch = subprocess.run(
            ["git", "diff", "--no-index", "--binary", "--", "/dev/null", relative],
            cwd=repo,
            capture_output=True,
            check=False,
        )
        if patch.returncode not in (0, 1):
            raise RuntimeError(patch.stderr.decode("utf-8", errors="replace").strip())
        chunks.append(patch.stdout)
    return b"".join(chunks)


@dataclass(frozen=True)
class SealedPath:
    """Exactly what one candidate path held at the moment it was sealed.

    The bytes themselves, not a digest: a digest can only say that something
    moved, and putting a rebuilt artifact back requires the content. Symlinks
    and absent paths are recorded as such so a path deleted or replaced during
    a read-only review is restored to what it was rather than to a file.
    """

    kind: str
    content: bytes = b""
    target: str = ""
    mode: int = 0


def _seal_path(repo: Path, relative: str) -> SealedPath:
    path = repo / relative
    try:
        if path.is_symlink():
            return SealedPath(kind="symlink", target=os.readlink(path))
        if path.is_file():
            return SealedPath(
                kind="file",
                content=path.read_bytes(),
                mode=path.stat().st_mode & 0o777,
            )
        if path.is_dir():
            return SealedPath(kind="directory")
        if path.exists():
            return SealedPath(kind="other")
        return SealedPath(kind="missing")
    except OSError:
        # No error text is kept: two unreadable reads must compare equal, so a
        # path Ortus never could open is not reported as one that moved.
        return SealedPath(kind="unreadable")


def seal_paths(repo: Path, paths: Iterable[str]) -> dict[str, SealedPath]:
    """Capture the candidate's exact content before an agent runs over it."""

    return {path: _seal_path(repo, path) for path in sorted(set(paths))}


def moved_sealed_paths(repo: Path, sealed: dict[str, SealedPath]) -> tuple[str, ...]:
    """The sealed paths the worktree no longer holds as sealed."""

    return tuple(
        path for path, seal in sorted(sealed.items()) if _seal_path(repo, path) != seal
    )


def restore_sealed_path(repo: Path, relative: str, sealed: SealedPath) -> None:
    """Put one path back to its sealed content, byte for byte.

    Raises `OSError` when the worktree will not take the sealed content back —
    an unwritable mount, a path now occupied by a directory, or a seal that
    captured no content to restore. The caller treats that as fatal: a
    candidate Ortus cannot put back is one no reviewer saw.
    """

    if sealed.kind not in ("file", "symlink", "missing"):
        raise OSError(f"{relative}: no sealed content to restore ({sealed.kind})")
    path = repo / relative
    if path.is_symlink() or path.exists():
        path.unlink()
    if sealed.kind == "missing":
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if sealed.kind == "symlink":
        os.symlink(sealed.target, path)
        return
    path.write_bytes(sealed.content)
    if sealed.mode:
        os.chmod(path, sealed.mode)


@dataclass(frozen=True)
class CandidateJournal:
    """Durable identity and evidence for one claimed candidate attempt."""

    issue_id: str
    base_head: str
    baseline_paths: tuple[str, ...]
    baseline_fingerprints: dict[str, str]
    issue_packet_hash: str = ""
    issue_packet_ref: str = ""
    candidate_paths: tuple[str, ...] = ()
    candidate_hash: str = ""
    candidate_diff_ref: str = ""
    phase: str = IMPLEMENTATION
    attempt: int = 1
    attempts: tuple[dict[str, Any], ...] = ()
    profiles: dict[str, str] = field(default_factory=dict)
    evidence: tuple[dict[str, Any], ...] = ()
    verifier_refs: tuple[str, ...] = ()
    corrections: int = 0
    plan_gap_routed: bool = False
    #: What an engineer handoff inherited: the uncommitted paths a fresh worker
    #: was shown, their state at that moment, and one record per resume.
    handoff_paths: tuple[str, ...] = ()
    handoff_fingerprints: dict[str, str] = field(default_factory=dict)
    handoffs: tuple[dict[str, Any], ...] = ()
    #: Which issue produced the inherited work, and which of it was that issue's
    #: own candidate. Only the journal that handed work off knows this; an
    #: unattributed handoff leaves both empty.
    handoff_issue_id: str = ""
    handoff_owned_paths: tuple[str, ...] = ()
    #: Handoff paths a worker judged unrelated to this issue. They stay in the
    #: worktree and out of the candidate, so finalization never commits them.
    unrelated_paths: tuple[str, ...] = ()
    #: The issue's branch (`ortus/<issue-id>`), and the last head this
    #: transaction observed on it. The name is how a human finds the work; the
    #: head is what proves which commit was meant. Both default empty so a
    #: journal written before branch-scoped finalization loads and finalizes
    #: by the pre-branch path.
    issue_branch: str = ""
    branch_head: str = ""
    #: Repo-relative path of the disposable worker workspace (a shared clone)
    #: this transaction's phases ran in. Recorded so recovery can fetch the
    #: branch home and discard the clone after a crash; empty for a legacy
    #: shared-tree run, which recovers by the pre-clone rules.
    workspace_path: str = ""
    finalization: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    implementation_started_at: str = ""
    implementation_finished_at: str = ""
    verification_started_at: str = ""
    verification_finished_at: str = ""
    schema: int = JOURNAL_SCHEMA

    @classmethod
    def start(
        cls,
        *,
        repo: Path,
        issue_id: str,
        base_head: str,
        baseline_paths: Iterable[str],
        packet_hash: str = "",
        packet_ref: str = "",
        profiles: dict[str, str] | None = None,
    ) -> CandidateJournal:
        paths = tuple(sorted(set(baseline_paths)))
        now = _now()
        return cls(
            issue_id=issue_id,
            base_head=base_head,
            baseline_paths=paths,
            baseline_fingerprints=fingerprint_paths(repo, paths),
            issue_packet_hash=packet_hash,
            issue_packet_ref=packet_ref,
            profiles={} if profiles is None else profiles,
            attempts=({"number": 1, "phase": IMPLEMENTATION, "started_at": now},),
            created_at=now,
            updated_at=now,
            implementation_started_at=now,
        )

    def with_candidate(
        self,
        paths: Iterable[str],
        *,
        phase: str,
        candidate_hash: str | None = None,
        diff_ref: str | None = None,
    ) -> CandidateJournal:
        updates: dict[str, Any] = {
            "candidate_paths": tuple(sorted(set(paths))),
            "phase": phase,
            "updated_at": _now(),
        }
        if candidate_hash is not None:
            updates["candidate_hash"] = candidate_hash
        if diff_ref is not None:
            updates["candidate_diff_ref"] = diff_ref
        return replace(self, **updates)

    def with_evidence(self, item: dict[str, Any]) -> CandidateJournal:
        bounded = dict(item)
        for key, value in tuple(bounded.items()):
            if isinstance(value, str) and len(value) > _MAX_EVIDENCE_CHARS:
                bounded[key] = value[:_MAX_EVIDENCE_CHARS] + "\n[truncated]"
        return replace(
            self,
            evidence=(*self.evidence, bounded),
            updated_at=_now(),
            implementation_finished_at=_now(),
        )

    def begin_verification(self) -> CandidateJournal:
        now = _now()
        return replace(
            self,
            phase=VERIFICATION,
            attempt=self.attempt + 1,
            attempts=(
                *self.attempts,
                {
                    "number": self.attempt + 1,
                    "phase": VERIFICATION,
                    "started_at": now,
                },
            ),
            verification_started_at=now,
            updated_at=now,
        )

    def finish_verification(self, reference: str, *, phase: str) -> CandidateJournal:
        return replace(
            self,
            phase=phase,
            verifier_refs=(*self.verifier_refs, reference),
            verification_finished_at=_now(),
            updated_at=_now(),
        )

    def begin_correction(self, *, findings: Iterable[str] = ()) -> CandidateJournal:
        """Record one bounded correction attempt before a fresh worker starts.

        The retry transition is journaled *before* the worker runs so a crash
        mid-correction still spends the attempt; an unbounded retry loop is the
        failure mode the cap exists to prevent.
        """

        now = _now()
        number = self.attempt + 1
        return replace(
            self,
            phase=CORRECTION,
            attempt=number,
            corrections=self.corrections + 1,
            attempts=(
                *self.attempts,
                {
                    "number": number,
                    "phase": CORRECTION,
                    "correction": self.corrections + 1,
                    "findings": list(findings),
                    "started_at": now,
                },
            ),
            implementation_started_at=now,
            updated_at=now,
        )

    def with_handoff(
        self,
        *,
        repo: Path,
        paths: Iterable[str],
        notes: Iterable[str] = (),
        base_head: str | None = None,
        owner: str = "",
        owned: Iterable[str] = (),
    ) -> CandidateJournal:
        """Record one engineer handoff to a fresh worker.

        `paths` is the uncommitted work the worker is about to be shown, and
        `notes` is what moved since the prior worker (HEAD, baseline, candidate,
        journal schema). Both are context: a mismatch is something the model
        assesses, never a startup failure. The fingerprints are what later lets
        Ortus tell adopted work from work the worker never touched.

        `owner` is the issue that produced `owned`, the subset of `paths` a
        prior journal recorded as that issue's own candidate. Attribution is
        recorded here because this is the only moment it is known; inherited
        work nobody can attribute is handed over unattributed rather than
        guessed at from the path strings.
        """

        selected = tuple(sorted(set(paths)))
        attributed = tuple(sorted(set(owned) & set(selected))) if owner else ()
        head = self.base_head if base_head is None else base_head
        record = {
            "at": _now(),
            "resumed_phase": self.phase,
            "base_head": head,
            "paths": list(selected),
            "owner": owner,
            "owned": list(attributed),
            "notes": list(notes),
        }
        return replace(
            self,
            base_head=head,
            handoff_paths=selected,
            handoff_fingerprints=fingerprint_paths(repo, selected),
            handoff_issue_id=owner,
            handoff_owned_paths=attributed,
            handoffs=(*self.handoffs, record)[-_MAX_HANDOFFS:],
            updated_at=_now(),
        )

    def own_inherited_work(self) -> frozenset[str]:
        """Inherited paths the claimed issue itself produced.

        Disowning exists for foreign leftovers, so the resumed issue's own prior
        attempt must not be declarable as somebody else's. Attribution keys on
        the recorded owner rather than the path string, because only the journal
        that handed the work off knows which issue produced it — a repo whose
        issues share a subsystem would misclassify on directory names alone.
        Anything unattributed resolves to nothing, which leaves the judgement
        with the worker exactly as before.
        """

        if not self.handoff_issue_id or self.handoff_issue_id != self.issue_id:
            return frozenset()
        return frozenset(self.handoff_owned_paths) - frozenset(self.unrelated_paths)

    def with_unrelated(self, paths: Iterable[str]) -> CandidateJournal:
        """Honor a worker's declaration that some handoff work is not ours.

        Declarations accumulate: a later resume must not silently re-adopt a
        path an earlier worker already disowned.
        """

        return replace(
            self,
            unrelated_paths=tuple(sorted(set(self.unrelated_paths) | set(paths))),
            updated_at=_now(),
        )

    def route_plan_gap(self) -> CandidateJournal:
        """Mark the one planning route a planning gap is allowed to consume."""

        return replace(
            self, plan_gap_routed=True, phase=PLAN_GAP_ROUTED, updated_at=_now()
        )

    def with_branch(self, name: str, head: str) -> CandidateJournal:
        """Record the issue branch and the head observed on it."""

        return replace(
            self, issue_branch=name, branch_head=head, updated_at=_now()
        )

    def with_finalization(self, step: str, value: Any = True) -> CandidateJournal:
        """Journal one completed finalization phase transition."""

        if step not in FINALIZATION_STEPS:
            raise ValueError(f"unknown finalization step {step!r}")
        record = dict(self.finalization)
        record[step] = value
        record["at"] = _now()
        return replace(
            self,
            finalization=record,
            phase=finalized_phase(step),
            updated_at=_now(),
        )

    def finalized(self, step: str) -> bool:
        """True when `step` already landed in a prior (possibly killed) run."""

        return bool(self.finalization.get(step))

    def baseline_is_unchanged(self, repo: Path) -> bool:
        return (
            fingerprint_paths(repo, self.baseline_paths) == self.baseline_fingerprints
        )


class JournalStore:
    """Atomic JSON persistence under the already-ignored logs directory."""

    def __init__(self, repo: Path):
        self.repo = repo
        self.path = repo / JOURNAL_RELATIVE_PATH
        self.artifacts = repo / "logs" / "grind-transactions"

    def load(self) -> CandidateJournal | None:
        journal, _ = self.load_state()
        return journal

    def load_state(self) -> tuple[CandidateJournal | None, tuple[str, ...]]:
        """Load the journal plus whatever about it is worth handing to a worker.

        A journal is routing context, not a cryptographic gate: an older, newer,
        or partly-unreadable one still names the issue that owns the uncommitted
        work. So anything unexpected becomes a note the fresh worker (and the
        operator log) sees, and only a journal with no usable identity at all
        loads as `None`.
        """

        notes: list[str] = []
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None, ()
        except OSError as exc:
            return None, (f"transaction journal could not be read ({exc})",)
        try:
            payload = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            return None, ("transaction journal is not valid JSON",)
        if not isinstance(payload, dict):
            return None, ("transaction journal is not a JSON object",)

        stored_schema = payload.get("schema")
        if stored_schema != JOURNAL_SCHEMA:
            notes.append(
                f"journal schema {stored_schema!r} is not the supported "
                f"{JOURNAL_SCHEMA}; its ownership record is still used"
            )
        # Schema 1 was written by the parent process while an implementation
        # worker was upgrading this module to schema 2. Its path ownership and
        # baseline fingerprints remain authoritative; the outer grind migrates
        # the missing candidate and work-spec hashes before verification
        # resumes.
        payload["schema"] = JOURNAL_SCHEMA
        for key in ("baseline_paths", "candidate_paths", "evidence", "verifier_refs"):
            payload[key] = tuple(payload.get(key, ()))
        for key in (
            "handoff_paths",
            "handoff_owned_paths",
            "unrelated_paths",
            "handoffs",
        ):
            payload[key] = tuple(payload.get(key, ()))
        # Schemas 1 and 2 predate correction accounting and finalization
        # phase transitions. Defaulting them to "nothing has landed yet" is the safe
        # migration: a resumed run re-checks observable bd and git state before
        # repeating any step.
        payload["finalization"] = dict(payload.get("finalization", {}))
        if stored_schema == 1:
            migrated_at = _now()
            payload.setdefault("created_at", migrated_at)
            payload.setdefault("updated_at", migrated_at)
            payload.setdefault("implementation_started_at", migrated_at)
            payload["attempts"] = (
                {
                    "number": 1,
                    "phase": str(payload.get("phase", IMPLEMENTATION)),
                    "started_at": migrated_at,
                    "migration": "schema-v1",
                },
            )
        else:
            payload["attempts"] = tuple(payload.get("attempts", ()))

        # A newer writer can add fields this build has never heard of. Dropping
        # them with a note keeps the shared identity usable instead of throwing
        # the whole handoff away over a key we do not need.
        known = {declared.name for declared in fields(CandidateJournal)}
        unknown = sorted(set(payload) - known)
        if unknown:
            notes.append(
                "journal carries unknown fields ignored on load: "
                + ", ".join(unknown[:5])
            )
            payload = {key: payload[key] for key in payload if key in known}
        try:
            return CandidateJournal(**payload), tuple(notes)
        except (TypeError, ValueError, KeyError) as exc:
            return None, (*notes, f"transaction journal is unusable ({exc})")

    def save(self, journal: CandidateJournal) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(asdict(journal), sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def save_packet(self, issue_id: str, packet: dict[str, Any]) -> tuple[str, str]:
        # Normalized, so the artifact bytes and the advertised digest agree —
        # and so the verifier is handed the work spec without the prior attempts'
        # reports, which it must not read as part of its own instructions.
        payload = canonical_json(authoritative_packet(packet))
        digest = issue_packet_hash(packet)
        self.artifacts.mkdir(parents=True, exist_ok=True)
        path = self.artifacts / f"{issue_id}-{digest}.issue.json"
        # Exactly the hashed bytes, with no trailing newline: the verifier is
        # told to rehash this file, so the artifact and the advertised digest
        # must agree bytewise.
        path.write_bytes(payload)
        return digest, str(path.relative_to(self.repo))

    def save_diff(self, diff: bytes) -> tuple[str, str]:
        digest = sha256_bytes(diff)
        self.artifacts.mkdir(parents=True, exist_ok=True)
        path = self.artifacts / f"{digest}.diff"
        path.write_bytes(diff)
        return digest, str(path.relative_to(self.repo))

    def save_report(self, digest: str, report: str, *, attempt: int = 1) -> str:
        self.artifacts.mkdir(parents=True, exist_ok=True)
        path = self.artifacts / f"{digest}.verifier-{attempt}.md"
        path.write_text(report, encoding="utf-8")
        return str(path.relative_to(self.repo))

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
