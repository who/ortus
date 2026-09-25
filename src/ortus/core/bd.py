"""Wrapper around the `bd` (beads) CLI.

All methods shell out to a real `bd` binary. We never mock bd — Testing
Strategy item from PRD: bd is integration-tested against tmp `bd init`
workspaces.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
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


# Leftover worker-proposed keys still live in the same store as accepted
# lessons. `select_lessons` never selects them, so a `proposal:` memory
# cannot compose into a worker's contract.
LESSON_PROPOSAL_PREFIX = "proposal:"

# Owner policy and decisions outrank ordinary lessons: a memory whose key
# carries one of these as a hyphen-delimited token (the field convention,
# e.g. `ci-policy-owner-decision-2026-08-16-github`) is selected before the
# lexical fill, so a bounded selection over a large store still reaches a
# worker with the owner's decisions in hand. No new metadata: the key is the
# marker, and a false positive such as `decision-log` only reorders.
LESSON_PRIORITY_TOKENS = frozenset({"policy", "decision"})


def is_priority_lesson(key: str) -> bool:
    """True when the memory key carries a policy or decision token."""
    return not LESSON_PRIORITY_TOKENS.isdisjoint(key.split("-"))


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

    Two tiers: keys that :func:`is_priority_lesson` recognises come first,
    then every other key fills the remaining budget. Within a tier keys sort
    lexically so two selections over the same store always compose the same
    contract; a store without policy-token keys selects in pure lexical
    order. Each body is clipped by :func:`_clip_lesson`.
    """
    selected: list[tuple[str, str]] = []
    for key in sorted(memories, key=lambda k: (not is_priority_lesson(k), k)):
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
class BeadsTracker:
    """Public ``run`` interface for the beads tracker.

    ``BdClient`` remains the typed verb surface. This type is the named
    entry the beads-tracker test suite imports and drives.
    """

    repo: Path
    binary: str = "bd"

    def run(self, *args: str, parse_json: bool = False) -> tuple[str, Any]:
        """Invoke ``bd`` in this workspace and return stdout plus parsed JSON."""
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


@dataclass
class _Reading:
    """One tracker reading, shared by every derived query in a snapshot block.

    ``rows`` is the whole-tracker listing the id and count views are derived
    from; ``shows`` and ``comments`` memoize the per-issue reads no listing
    can answer. All three are dropped together by a write, because within a
    block a write is the only thing that can change any of them.
    """

    rows: list[dict[str, Any]] | None = None
    shows: dict[str, dict[str, Any]] = field(default_factory=dict)
    comments: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


def _rows_in(
    rows: list[dict[str, Any]],
    status: str,
    *,
    exclude_labels: tuple[str, ...] = (),
    labels: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """A listing's rows in `status`, under the label filters bd would apply.

    ``exclude_labels`` drops a row carrying any of them, the way
    ``--exclude-label`` does; ``labels`` keeps only rows carrying at least
    one, the way ``--label-any`` does.
    """

    selected: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("status") != status:
            continue
        row_labels = row.get("labels") or []
        if any(label in row_labels for label in exclude_labels):
            continue
        if labels and not any(label in row_labels for label in labels):
            continue
        selected.append(row)
    return selected


def _ids(rows: list[dict[str, Any]]) -> set[str]:
    return {row["id"] for row in rows if "id" in row}


@dataclass
class BdClient:
    """Thin typed surface over the bd CLI, scoped to a single repo workspace."""

    repo: Path
    binary: str = "bd"
    _fresh_claims: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _reading: _Reading | None = field(default=None, init=False, repr=False)

    # --- subprocess primitive -------------------------------------------

    def _run(self, *args: str, parse_json: bool = False) -> tuple[str, Any]:
        return BeadsTracker(self.repo, binary=self.binary).run(
            *args, parse_json=parse_json
        )

    # --- one reading per step -------------------------------------------

    @contextmanager
    def snapshot(self) -> Iterator[None]:
        """Answer this block's repeated reads from one listing of the tracker.

        Every method here is a fresh `bd` process opening the embedded
        database — about a second of it on a CI runner — so a block that
        wants the open ids, the in-progress ids and the closed count pays
        three of them for three views of one state. Inside this block those
        views are derived from a single listing, and a repeated `show` or
        `comments` of the same issue is served from the first one.

        A block is only sound where this process is the sole writer. A worker
        subprocess changes the tracker underneath an open reading, so the
        block ends before one is spawned and a new one begins when it exits.
        A write through this client invalidates the reading, so a read that
        follows one still observes it. Nesting is a no-op: the outermost
        block owns the reading, and the inner one neither restarts nor ends it.
        """

        if self._reading is not None:
            yield
            return
        self.open_snapshot()
        try:
            yield
        finally:
            self.close_snapshot()

    def open_snapshot(self) -> None:
        """Begin a reading, or restart one, where a block cannot be nested.

        The grind loop's step spans a `try`/`finally` around a worker spawn
        rather than one indented region, so it drives the reading by hand.
        """

        self._reading = _Reading()

    def close_snapshot(self) -> None:
        """End the reading; every later read goes back to the tracker."""

        self._reading = None

    @contextmanager
    def no_snapshot(self) -> Iterator[None]:
        """Suspend any open reading across work another process does.

        A turn spent in an agent, or anything else that reaches the tracker
        from outside this process, can change what a reading holds without
        passing through the invalidation here. So the reading ends at the
        top of such a block and, if one was open, a new and empty one begins
        at the bottom: the reads that follow describe the tracker as the
        other process left it.
        """

        held = self._reading is not None
        self._reading = None
        try:
            yield
        finally:
            if held:
                self.open_snapshot()

    def _invalidate(self) -> None:
        """Drop what the open reading holds, because this write changed it."""

        if self._reading is not None:
            self._reading = _Reading()

    def _listing(self) -> list[dict[str, Any]] | None:
        """The open reading's whole-tracker rows, or None outside a block.

        A listing bd could not answer is remembered as no rows at all, so
        each view derived from it reports exactly what its own failed query
        reported: zero for a count, an empty set for an id set.
        """

        reading = self._reading
        if reading is None:
            return None
        if reading.rows is None:
            try:
                _, data = self._run(
                    "list", "--all", "--limit", "0", "--json", "--brief",
                    parse_json=True,
                )
            except BdError:
                data = []
            reading.rows = data if isinstance(data, list) else []
        return reading.rows

    # --- typed surface --------------------------------------------------

    def supports_export(self) -> bool:
        """Whether this bd can regenerate its JSONL export on demand.

        Probed once by behavior, never by version parsing: `bd export --help`
        exits 0 exactly where the exporting path is available. A bd without
        it maintains the export ambiently, and the caller leaves that regime
        byte-identical.
        """
        cached = getattr(self, "_supports_export", None)
        if cached is None:
            proc = subprocess.run(
                [self.binary, "export", "--help"],
                cwd=str(self.repo),
                capture_output=True,
                text=True,
                check=False,
            )
            cached = proc.returncode == 0
            self._supports_export = cached
        return cached

    def export_issues(self) -> str:
        """Regenerate `.beads/issues.jsonl` from the database, atomically.

        Ortus owns export timing: this runs at the exact moments the export's
        bytes are consumed, so the ambient-timing race class cannot recur.
        The write lands in a temp file first and is renamed into place — a
        crash mid-export can never commit a truncated record. A locked-out
        tracker gets one retry. Returns "" on success, else the reason.
        """
        target = self.repo / ".beads" / "issues.jsonl"
        scratch = target.with_name(".issues.jsonl.export-tmp")
        last = ""
        for _attempt in (1, 2):
            proc = subprocess.run(
                [self.binary, "export", "-o", str(scratch)],
                cwd=str(self.repo),
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode == 0:
                try:
                    os.replace(scratch, target)
                except OSError as exc:
                    return f"could not move the export into place ({exc})"
                return ""
            last = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or [
                f"bd export exited {proc.returncode}"
            ]
            last = last[0]
        scratch.unlink(missing_ok=True)
        return last

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
        """`bd list --status open --limit 0 --json` → every open issue.

        ``--limit 0`` lifts bd's default cap of 50 for the same reason as
        :meth:`open_ids`: a sweep over the open queue that silently stops at
        the fiftieth row is an undercount, not a listing.
        """
        _, data = self._run(
            "list", "--status", "open", "--limit", "0", "--json", parse_json=True
        )
        return data or []

    def list_all(self) -> list[dict[str, Any]]:
        """Return every issue, including closed ones, without the default limit."""
        _, data = self._run("list", "--all", "--limit", "0", "--json", parse_json=True)
        return data or []

    def list_human(self) -> list[dict[str, Any]]:
        """`bd human list --json`: issues flagged for a human decision."""
        _, data = self._run("human", "list", "--json", parse_json=True)
        return data or []

    def children(self, parent_id: str) -> list[dict[str, Any]]:
        """`bd children <id> --json`: all children, including closed.

        `bd show` no longer embeds parent-child dependents, so rollover and
        anything else that needs the subtree must ask this command.
        """
        _, data = self._run("children", parent_id, "--json", parse_json=True)
        return data or []

    def comments(self, issue_id: str) -> list[dict[str, Any]]:
        """`bd comments <id> --json`: ordered comment list for one issue.

        Inside a snapshot block the first read of an issue's thread answers
        every later one, until a write drops the reading.
        """
        reading = self._reading
        if reading is not None and issue_id in reading.comments:
            return reading.comments[issue_id]
        _, data = self._run("comments", issue_id, "--json", parse_json=True)
        thread = data or []
        if reading is not None:
            reading.comments[issue_id] = thread
        return thread

    def add_comment(self, issue_id: str, body: str) -> None:
        """Append a durable comment without interpreting its Markdown."""
        self._invalidate()
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

    def show(self, issue_id: str) -> dict[str, Any]:
        """Return the issue's full JSON dict. `bd show --json` returns a list
        with one element when passed a single id; unwrap it.

        Inside a snapshot block the first read of an issue answers every
        later one, until a write drops the reading. The block's listing
        cannot stand in for this: it is taken `--brief`, without the
        description, design and acceptance a work spec is made of.
        """
        reading = self._reading
        if reading is not None and issue_id in reading.shows:
            return reading.shows[issue_id]
        _, data = self._run("show", "--json", "--", issue_id, parse_json=True)
        if not data:
            raise BdError([self.binary, "show", issue_id], 0, "empty JSON response")
        issue = data[0] if isinstance(data, list) else data
        if reading is not None:
            reading.shows[issue_id] = issue
        return issue

    def labels_of(self, issue_id: str) -> list[str]:
        """The issue's labels, from the open reading when it holds that row.

        A listing already carries every row's labels, so a block that has
        one answers this without starting a process. Outside a block, and
        for an issue no listing holds, it is the `bd show` it always was.
        """
        rows = self._listing()
        if rows is not None:
            for row in rows:
                if isinstance(row, dict) and row.get("id") == issue_id:
                    return list(row.get("labels") or [])
        return list(self.show(issue_id).get("labels") or [])

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
        external_ref: str | None = None,
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
        if external_ref:
            args.extend(["--external-ref", external_ref])
        self._invalidate()
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
        self._invalidate()
        self._run("update", issue_id, "--status", status)

    def require_atomic_claims(self) -> None:
        """Reject older tracker CLIs before an enforced run mutates anything."""
        update_help, _ = self._run("update", "--help")
        release_help, _ = self._run("unclaim", "--help")
        if "--claim" not in update_help or "--if-assignee" not in release_help:
            raise BdError([self.binary], 1, "atomic claim/unclaim support is required")

    def claim(self, issue_id: str, actor: str) -> dict[str, Any]:
        """Acquire a fresh claim and reload its authoritative assignee/status."""
        if not issue_id or not actor:
            raise ValueError("claim requires an issue id and a non-empty actor")
        self.require_atomic_claims()
        before = self.show(issue_id)
        if before.get("status") != "open" or "human" in (before.get("labels") or []):
            raise BdError([self.binary, "update", issue_id], 1, "issue is not claimable")
        self._invalidate()
        self._run("--actor", actor, "update", "--claim", "--", issue_id)
        claimed = self.show(issue_id)
        if (
            claimed.get("id") != issue_id
            or claimed.get("status") != "in_progress"
            or claimed.get("assignee") != actor
            or "human" in (claimed.get("labels") or [])
        ):
            raise BdError([self.binary, "show", issue_id], 1, "claim ownership changed")
        self._fresh_claims[issue_id] = actor
        return claimed

    def release_claim(self, issue_id: str, expected_assignee: str) -> None:
        """Release only this client's fresh claim using the tracker's atomic guard."""
        if not expected_assignee or self._fresh_claims.get(issue_id) != expected_assignee:
            raise BdError([self.binary, "unclaim", issue_id], 1, "not a fresh owned claim")
        current = self.show(issue_id)
        if (
            current.get("status") != "in_progress"
            or current.get("assignee") != expected_assignee
        ):
            raise BdError([self.binary, "unclaim", issue_id], 1, "claim ownership changed")
        self._run(
            "--actor", expected_assignee, "unclaim", "--if-assignee",
            expected_assignee, "--", issue_id,
        )
        del self._fresh_claims[issue_id]

    def add_label(self, issue_id: str, label: str) -> None:
        """`bd label add <id> <label>`. Used by orphan-policy=escalate."""
        self._invalidate()
        self._run("label", "add", issue_id, label)

    def remove_label(self, issue_id: str, label: str) -> None:
        """`bd label remove <id> <label>`. Used by the triage routes that unpark."""
        self._invalidate()
        self._run("label", "remove", issue_id, label)

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

        Inside a snapshot block neither route runs: the block's one listing
        already holds every row and its labels, so the count is taken there.
        """
        rows = self._listing()
        if rows is not None:
            return len(_rows_in(rows, status, exclude_labels=exclude_labels))

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
        """`bd list --status in_progress --limit 0 --json` → set of issue ids.

        Mirrors :meth:`count_by_status` w.r.t. ``exclude_labels``: passing
        ``("human",)`` drops issues that have been escalated for human
        action so the grind orchestrator's orphan-detection diff doesn't
        keep flagging them across iterations.

        The outer grind loop diffs this snapshot across a subprocess
        boundary to identify orphan claims (issues claimed but not closed
        within the iteration), and reads the queue's in_progress figure off
        the same set. ``--limit 0`` lifts bd's default cap of 50 for the
        reason :meth:`open_ids` gives: a claim beyond the fiftieth row is
        still a claim, and both the diff and that figure have to see it.

        Inside a snapshot block the ids come from the block's one listing,
        which is what lets the reap poll ask for the flagged and unflagged
        claims without paying for two.
        """
        rows = self._listing()
        if rows is not None:
            return _ids(_rows_in(rows, "in_progress", exclude_labels=exclude_labels))

        args = ["list", "--status", "in_progress"]
        for label in exclude_labels:
            args.extend(["--exclude-label", label])
        args.extend(["--limit", "0", "--json"])
        try:
            _, data = self._run(*args, parse_json=True)
        except BdError:
            return set()
        if not isinstance(data, list):
            return set()
        return {item["id"] for item in data if isinstance(item, dict) and "id" in item}

    def open_ids(self, *, labels: tuple[str, ...] = ()) -> set[str]:
        """`bd list --status open --limit 0 --json` → set of issue ids.

        ``labels`` narrows the listing to issues carrying any of them
        (``--label-any``). The grind harness passes ``("human",)`` at window
        start to remember which open issues are the operator's: a worker
        that claims one of those from `bd ready` has taken an issue it can
        never finish, and the reap that follows hands it back. ``--limit 0``
        lifts bd's default cap for the same reason as :meth:`closed_ids`.
        A failed query answers with an empty set, which means nothing is
        handed back rather than a crash mid-window. Inside a snapshot block
        the ids come from the block's one listing.
        """
        rows = self._listing()
        if rows is not None:
            return _ids(_rows_in(rows, "open", labels=labels))

        args = ["list", "--status", "open"]
        for label in labels:
            args.extend(["--label-any", label])
        args.extend(["--limit", "0", "--json"])
        try:
            _, data = self._run(*args, parse_json=True)
        except BdError:
            return set()
        if not isinstance(data, list):
            return set()
        return {item["id"] for item in data if isinstance(item, dict) and "id" in item}

    def closed_ids(self) -> set[str]:
        """`bd list --status closed --limit 0 --json` → set of issue ids.

        The grind orchestrator diffs this across a worker iteration to name
        an issue the worker claimed and closed within one window — there the
        in_progress diff is empty and the snapshot's closed count alone
        cannot say which issue landed. ``--limit 0`` lifts bd's default list
        cap so a long-lived repository's older closes can't push the fresh
        one out of the diff. Inside a snapshot block the ids come from the
        block's one listing, beside the counts taken from the same rows.
        """
        rows = self._listing()
        if rows is not None:
            return _ids(_rows_in(rows, "closed"))

        args = ["list", "--status", "closed", "--limit", "0", "--json"]
        try:
            _, data = self._run(*args, parse_json=True)
        except BdError:
            return set()
        if not isinstance(data, list):
            return set()
        return {item["id"] for item in data if isinstance(item, dict) and "id" in item}
