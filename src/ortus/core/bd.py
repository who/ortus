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

    def list_ready(self) -> list[dict[str, Any]]:
        _, data = self._run("ready", "--json", parse_json=True)
        return data or []

    def list_open(self) -> list[dict[str, Any]]:
        _, data = self._run("list", "--status", "open", "--json", parse_json=True)
        return data or []

    def list_human(self) -> list[dict[str, Any]]:
        """`bd human list --json`: issues flagged for a human decision."""
        _, data = self._run("human", "list", "--json", parse_json=True)
        return data or []

    def comments(self, issue_id: str) -> list[dict[str, Any]]:
        """`bd comments <id> --json`: ordered comment list for one issue."""
        _, data = self._run("comments", issue_id, "--json", parse_json=True)
        return data or []

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
        args = ["create", "--silent", "--title", title, "--type", issue_type, "--priority", str(priority)]
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
