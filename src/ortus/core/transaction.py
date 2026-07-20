"""Recoverable ownership records for Codex grind worktree transactions."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


JOURNAL_SCHEMA = 1
JOURNAL_RELATIVE_PATH = Path("logs") / "grind-transaction.json"


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
    return hashlib.sha256(payload).hexdigest()


def fingerprint_paths(repo: Path, paths: Iterable[str]) -> dict[str, str]:
    """Hash worktree representations so baseline edits cannot be absorbed."""

    return {path: _path_fingerprint(repo, path) for path in sorted(set(paths))}


@dataclass(frozen=True)
class CandidateJournal:
    """Durable identity for one claimed Codex candidate."""

    issue_id: str
    base_head: str
    baseline_paths: tuple[str, ...]
    baseline_fingerprints: dict[str, str]
    candidate_paths: tuple[str, ...] = ()
    phase: str = "implementation"
    schema: int = JOURNAL_SCHEMA

    @classmethod
    def start(
        cls,
        *,
        repo: Path,
        issue_id: str,
        base_head: str,
        baseline_paths: Iterable[str],
    ) -> CandidateJournal:
        paths = tuple(sorted(set(baseline_paths)))
        return cls(
            issue_id=issue_id,
            base_head=base_head,
            baseline_paths=paths,
            baseline_fingerprints=fingerprint_paths(repo, paths),
        )

    def with_candidate(self, paths: Iterable[str], *, phase: str) -> CandidateJournal:
        return CandidateJournal(
            issue_id=self.issue_id,
            base_head=self.base_head,
            baseline_paths=self.baseline_paths,
            baseline_fingerprints=self.baseline_fingerprints,
            candidate_paths=tuple(sorted(set(paths))),
            phase=phase,
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

    def load(self) -> CandidateJournal | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if payload.get("schema") != JOURNAL_SCHEMA:
                return None
            return CandidateJournal(
                issue_id=str(payload["issue_id"]),
                base_head=str(payload["base_head"]),
                baseline_paths=tuple(payload.get("baseline_paths", ())),
                baseline_fingerprints=dict(payload.get("baseline_fingerprints", {})),
                candidate_paths=tuple(payload.get("candidate_paths", ())),
                phase=str(payload.get("phase", "implementation")),
                schema=JOURNAL_SCHEMA,
            )
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

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
