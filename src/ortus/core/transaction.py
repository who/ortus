"""Recoverable ownership records for grind candidate transactions."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import subprocess
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable


JOURNAL_SCHEMA = 2
JOURNAL_RELATIVE_PATH = Path("logs") / "grind-transaction.json"
_MAX_EVIDENCE_CHARS = 16_000


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def issue_packet_hash(packet: dict[str, Any]) -> str:
    """Bind verification to the exact authoritative issue packet."""

    return sha256_bytes(canonical_json(packet))


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


def candidate_diff(repo: Path, paths: Iterable[str]) -> bytes:
    """Return a deterministic, binary-safe diff bundle for candidate paths.

    Normal ``git diff HEAD`` covers staged, unstaged, deleted, and binary tracked
    files. Untracked files are appended as binary patches against ``/dev/null``.
    The bundle is complete on disk; prompts refer to it by immutable hash so a
    large or binary candidate is never silently truncated.
    """

    selected = tuple(sorted(set(paths)))
    if not selected:
        return b""
    head = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    base = (
        "HEAD" if head.returncode == 0 else "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
    )
    tracked = subprocess.run(
        ["git", "diff", "--binary", "--no-ext-diff", base, "--", *selected],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    if tracked.returncode != 0:
        raise RuntimeError(tracked.stderr.decode("utf-8", errors="replace").strip())
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
    phase: str = "implementation"
    attempt: int = 1
    attempts: tuple[dict[str, Any], ...] = ()
    profiles: dict[str, str] = field(default_factory=dict)
    evidence: tuple[dict[str, Any], ...] = ()
    verifier_refs: tuple[str, ...] = ()
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
            attempts=({"number": 1, "phase": "implementation", "started_at": now},),
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
            phase="verification",
            attempt=self.attempt + 1,
            attempts=(
                *self.attempts,
                {
                    "number": self.attempt + 1,
                    "phase": "verification",
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
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            stored_schema = payload.get("schema")
            if stored_schema not in {1, JOURNAL_SCHEMA}:
                return None
            # Schema 1 was written by the parent process while an implementation
            # worker was upgrading this module to schema 2. Its path ownership
            # and baseline fingerprints remain authoritative; the outer grind
            # migrates the missing candidate and issue-packet hashes before
            # verification resumes.
            payload["schema"] = JOURNAL_SCHEMA
            payload["baseline_paths"] = tuple(payload.get("baseline_paths", ()))
            payload["candidate_paths"] = tuple(payload.get("candidate_paths", ()))
            payload["evidence"] = tuple(payload.get("evidence", ()))
            payload["verifier_refs"] = tuple(payload.get("verifier_refs", ()))
            if stored_schema == 1:
                migrated_at = _now()
                payload.setdefault("created_at", migrated_at)
                payload.setdefault("updated_at", migrated_at)
                payload.setdefault("implementation_started_at", migrated_at)
                payload["attempts"] = (
                    {
                        "number": 1,
                        "phase": str(payload.get("phase", "implementation")),
                        "started_at": migrated_at,
                        "migration": "schema-v1",
                    },
                )
            else:
                payload["attempts"] = tuple(payload.get("attempts", ()))
            return CandidateJournal(**payload)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None

    def save(self, journal: CandidateJournal) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(asdict(journal), sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def save_packet(self, issue_id: str, packet: dict[str, Any]) -> tuple[str, str]:
        payload = canonical_json(packet)
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
