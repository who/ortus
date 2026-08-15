"""Packet hashing, path fingerprints, and sealed-path restore for grind."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

JOURNAL_RELATIVE_PATH = Path("logs") / "grind-transaction.json"


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
