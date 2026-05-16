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
