"""Wrapper around the `bd` (beads) CLI.

All methods shell out to a real `bd` binary. We never mock bd — Testing
Strategy item from PRD: bd is integration-tested against tmp `bd init`
workspaces.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class BdError(RuntimeError):
    """A bd subprocess invocation returned non-zero. stderr is captured verbatim."""

    def __init__(self, argv: list[str], returncode: int, stderr: str):
        self.argv = argv
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(
            f"bd command failed (exit {returncode}): {' '.join(argv)}\n{stderr}"
        )


# Memory keys carrying this prefix are worker-proposed lessons awaiting
# curation. They live in the same store as accepted lessons so recording one
# writes only to the tracker, but they are structurally not lessons yet:
# `select_lessons` never selects them, so nothing a worker proposes reaches
# another worker until `ortus curate` accepts it under an unprefixed key.
LESSON_PROPOSAL_PREFIX = "proposal:"

# Body marker for a pending proposal. bd deduplicates memories by content
# across its export round-trip, so a forgotten key whose body is byte-equal to
# a surviving memory resurrects on the next import. Marking the pending body
# keeps it distinct from the accepted lesson it may become, so accepting a
# proposal verbatim can still delete the pending copy for good.
_PENDING_BODY_MARKER = "[pending curation] "


def _clip_lesson(text: str, max_chars: int) -> str:
    """Collapse a lesson body to one line and truncate on a word boundary.

    Collapsing removes every newline, so a lesson carrying phase-contract
    delimiters (`## ...` headings) can never open a section of its own once
    composed. Truncation is marked with `[…]` rather than silent, and falls
    back to a hard cut only when the first word alone exceeds the bound.
    """
    flat = " ".join(text.split())
    if len(flat) <= max_chars:
        return flat
    cut = flat.rfind(" ", 0, max_chars + 1)
    if cut <= 0:
        cut = max_chars
    return flat[:cut].rstrip() + " […]"


def select_lessons(
    memories: dict[str, str],
    *,
    exclude_keys: frozenset[str] = frozenset(),
    limit: int,
    max_chars: int,
) -> tuple[tuple[str, str], ...]:
    """Deterministic bounded selection over a raw memory mapping.

    Keys sort lexically so two selections over the same store always compose
    the same contract; each body is clipped by :func:`_clip_lesson`.
    """
    selected: list[tuple[str, str]] = []
    for key in sorted(memories):
        if key in exclude_keys or key.startswith(LESSON_PROPOSAL_PREFIX):
            continue
        body = _clip_lesson(memories[key], max_chars)
        if not body:
            continue
        selected.append((key, body))
        if len(selected) >= limit:
            break
    return tuple(selected)


@dataclass
class BdClient:
    """Thin typed surface over the bd CLI, scoped to a single repo workspace."""

    repo: Path
    binary: str = "bd"

    # --- subprocess primitive -------------------------------------------

    def _run(self, *args: str, parse_json: bool = False) -> tuple[str, Any]:
        argv = [self.binary, *args]
        proc = subprocess.run(
            argv,
            cwd=str(self.repo),
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise BdError(argv, proc.returncode, proc.stderr)
        parsed = json.loads(proc.stdout) if parse_json and proc.stdout.strip() else None
        return proc.stdout, parsed

    # --- typed surface --------------------------------------------------

    def list_ready(
        self, *, exclude_labels: tuple[str, ...] = ()
    ) -> list[dict[str, Any]]:
        """`bd ready --json` → ready issues, ordered by priority.

        ``exclude_labels`` maps to repeated ``--exclude-label`` flags so the
        grind harness can drop human-escalated issues (mirrors
        :meth:`count_by_status`/:meth:`in_progress_ids`) before selecting the
        next issue to claim.
        """
        args = ["ready"]
        for label in exclude_labels:
            args.extend(["--exclude-label", label])
        args.append("--json")
        _, data = self._run(*args, parse_json=True)
        return data or []

    def list_open(self) -> list[dict[str, Any]]:
        _, data = self._run("list", "--status", "open", "--json", parse_json=True)
        return data or []

    def list_all(self) -> list[dict[str, Any]]:
        """Return every issue, including closed ones, without the default limit."""
        _, data = self._run("list", "--all", "--limit", "0", "--json", parse_json=True)
        return data or []

    def list_human(self) -> list[dict[str, Any]]:
        """`bd human list --json`: issues flagged for a human decision."""
        _, data = self._run("human", "list", "--json", parse_json=True)
        return data or []

    def comments(self, issue_id: str) -> list[dict[str, Any]]:
        """`bd comments <id> --json`: ordered comment list for one issue."""
        _, data = self._run("comments", issue_id, "--json", parse_json=True)
        return data or []

    def add_comment(self, issue_id: str, body: str) -> None:
        """Append a durable comment without interpreting its Markdown."""
        self._run("comments", "add", issue_id, body)

    def memories(self) -> dict[str, str]:
        """`bd --readonly --sandbox memories --json`: the raw memory store.

        Read-only plus sandbox keep the query off bd's write and auto-sync
        paths (mirrors `ortus check`'s readiness-memory probe), so reading
        during a run can never disturb a candidate.
        """
        _, data = self._run(
            "--readonly", "--sandbox", "memories", "--json", parse_json=True
        )
        if not isinstance(data, dict):
            return {}
        # The store carries a `schema_version` metadata entry alongside the
        # memories; it is not a memory even if a future bd stringifies it.
        return {
            str(key): value
            for key, value in data.items()
            if isinstance(value, str) and key != "schema_version"
        }

    def lessons(
        self,
        *,
        exclude_keys: frozenset[str] = frozenset(),
        limit: int,
        max_chars: int,
    ) -> tuple[tuple[str, str], ...]:
        """Bounded, deterministic read of stored crew lessons.

        The bounds are the caller's context budget: every lesson costs
        context in every session that receives it, so an unbounded read
        would turn the store into a tax rather than an asset.
        """
        return select_lessons(
            self.memories(),
            exclude_keys=exclude_keys,
            limit=limit,
            max_chars=max_chars,
        )

    def propose_lesson(self, key: str, body: str) -> bool:
        """Record a worker-proposed lesson in the pending state.

        Pending means stored under :data:`LESSON_PROPOSAL_PREFIX`, which
        :func:`select_lessons` never selects — a proposal cannot reach another
        worker until curation accepts it. Returns False without writing when
        an accepted lesson already carries the same key or the same body, so
        a re-proposal of known knowledge never duplicates it. `bd remember`
        updates a matching key in place, so re-recording the same proposal
        (a replayed run, a correction round) is idempotent too.
        """
        memories = self.memories()
        accepted = {
            k: v
            for k, v in memories.items()
            if not k.startswith(LESSON_PROPOSAL_PREFIX)
        }
        if key in accepted or body in set(accepted.values()):
            return False
        self._run(
            "remember",
            _PENDING_BODY_MARKER + body,
            "--key",
            LESSON_PROPOSAL_PREFIX + key,
        )
        return True

    def pending_proposals(self) -> dict[str, str]:
        """Worker-proposed lessons awaiting curation, keyed without the prefix."""
        prefix = LESSON_PROPOSAL_PREFIX
        return {
            key[len(prefix) :]: body.removeprefix(_PENDING_BODY_MARKER)
            for key, body in sorted(self.memories().items())
            if key.startswith(prefix)
        }

    def accept_proposal(self, key: str, body: str | None = None) -> bool:
        """Promote the pending proposal `key` to an accepted lesson.

        `body` replaces the proposed text when the operator edits before
        accepting; None accepts the proposal verbatim. Returns False when no
        such proposal is pending. The accepted lesson is written first so a
        failure between the two writes leaves the proposal pending rather
        than lost.
        """
        pending = self.pending_proposals()
        if key not in pending:
            return False
        self._run("remember", body if body is not None else pending[key], "--key", key)
        self._run("forget", LESSON_PROPOSAL_PREFIX + key)
        return True

    def reject_proposal(self, key: str) -> bool:
        """Delete the pending proposal `key`. Returns False when none is pending."""
        if key not in self.pending_proposals():
            return False
        self._run("forget", LESSON_PROPOSAL_PREFIX + key)
        return True

    def show(self, issue_id: str) -> dict[str, Any]:
        """Return the issue's full JSON dict. `bd show --json` returns a list
        with one element when passed a single id; unwrap it."""
        _, data = self._run("show", issue_id, "--json", parse_json=True)
        if not data:
            raise BdError([self.binary, "show", issue_id], 0, "empty JSON response")
        if isinstance(data, list):
            return data[0]
        return data

    def create(
        self,
        *,
        title: str,
        issue_type: str = "task",
        priority: int = 2,
        description: str | None = None,
        design: str | None = None,
        acceptance: str | None = None,
        notes: str | None = None,
        labels: list[str] | None = None,
    ) -> str:
        """Create an issue via `bd create --silent`. Returns the new issue id."""
        args = [
            "create",
            "--silent",
            "--title",
            title,
            "--type",
            issue_type,
            "--priority",
            str(priority),
        ]
        if description:
            args.extend(["--description", description])
        if design:
            args.extend(["--design", design])
        if acceptance:
            args.extend(["--acceptance", acceptance])
        if notes:
            args.extend(["--notes", notes])
        if labels:
            args.extend(["--labels", ",".join(labels)])
        stdout, _ = self._run(*args)
        return stdout.strip()

    def close(self, issue_id: str, *, reason: str | None = None) -> None:
        args = ["close", issue_id]
        if reason:
            args.extend(["--reason", reason])
        self._run(*args)

    def status(self, issue_id: str) -> str:
        """Current lifecycle status, or "" when the issue can't be read."""
        try:
            return str(self.show(issue_id).get("status") or "")
        except (BdError, ValueError, KeyError):
            return ""

    def has_comment(self, issue_id: str, marker: str) -> bool:
        """True when any existing comment body contains `marker`.

        Grind restarts replay finalization from the journal, but a run killed
        between writing a comment and journaling that phase transition has no journal
        evidence. Matching on the marker makes the replay idempotent anyway.
        """
        try:
            existing = self.comments(issue_id)
        except (BdError, ValueError):
            return False
        for comment in existing:
            if not isinstance(comment, dict):
                continue
            for key in ("body", "text", "comment", "content"):
                if marker in str(comment.get(key) or ""):
                    return True
        return False

    def close_once(self, issue_id: str, *, reason: str | None = None) -> bool:
        """Close `issue_id` unless it is already closed. Returns True if closed.

        The observable status is checked first so a restart after a close that
        landed — but whose journal phase transition never got written — does not issue
        a second `bd close`.
        """
        if self.status(issue_id) == "closed":
            return False
        self.close(issue_id, reason=reason)
        return True

    def update_status(self, issue_id: str, status: str) -> None:
        """`bd update <id> --status <status>`. Used by orphan-policy=revert."""
        self._run("update", issue_id, "--status", status)

    def add_label(self, issue_id: str, label: str) -> None:
        """`bd label add <id> <label>`. Used by orphan-policy=escalate."""
        self._run("label", "add", issue_id, label)

    def count_by_status(
        self, status: str, *, exclude_labels: tuple[str, ...] = ()
    ) -> int:
        """Count issues in `status`, optionally dropping ones with excluded labels.

        Routing:

        - ``exclude_labels=()`` → `bd count --status <status> --json`,
          which is the cheap path.
        - ``exclude_labels=(...)`` → `bd list --status <status>
          --exclude-label <l> ... --limit 0 --json` and take the response
          length. `bd count` does not (yet) accept ``--exclude-label``;
          falling through to `bd list` is the workaround.

        The grind orchestrator passes ``("human",)`` so human-escalated
        claims don't keep the queue artificially non-empty.

        Returns 0 if bd is missing, the status is unknown, or the response
        is malformed — the outer grind loop treats failures as "no change",
        which is the conservative branch (idle-sleep instead of false claim).
        """
        if not exclude_labels:
            try:
                _, data = self._run(
                    "count", "--status", status, "--json", parse_json=True
                )
            except BdError:
                return 0
            if not isinstance(data, dict):
                return 0
            try:
                return int(data.get("count", 0))
            except (TypeError, ValueError):
                return 0

        args = ["list", "--status", status]
        for label in exclude_labels:
            args.extend(["--exclude-label", label])
        # --limit 0 = unlimited; without it bd list caps at 50 and we'd undercount.
        args.extend(["--limit", "0", "--json"])
        try:
            _, data = self._run(*args, parse_json=True)
        except BdError:
            return 0
        if not isinstance(data, list):
            return 0
        return len(data)

    def in_progress_ids(self, *, exclude_labels: tuple[str, ...] = ()) -> set[str]:
        """`bd list --status in_progress --json` → set of issue ids.

        Mirrors :meth:`count_by_status` w.r.t. ``exclude_labels``: passing
        ``("human",)`` drops issues that have been escalated for human
        action so the grind orchestrator's orphan-detection diff doesn't
        keep flagging them across iterations.

        The outer grind loop diffs this snapshot across a subprocess
        boundary to identify orphan claims (issues claimed but not closed
        within the iteration).
        """
        args = ["list", "--status", "in_progress"]
        for label in exclude_labels:
            args.extend(["--exclude-label", label])
        args.extend(["--json"])
        try:
            _, data = self._run(*args, parse_json=True)
        except BdError:
            return set()
        if not isinstance(data, list):
            return set()
        return {item["id"] for item in data if isinstance(item, dict) and "id" in item}
