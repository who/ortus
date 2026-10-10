"""A read-only snapshot of what the tracker says happened to each bead.

Replay labels Jev's decisions from these facts instead of from hand-written
answers: whether a bead closed, how often it was reopened, whether it carries
the human label, and when its spec changed. Every bd call here is
`--readonly --sandbox`, so taking a snapshot can never change the tracker or
start a sync.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from ortus.core.bd import BdError, BeadsTracker
from ortus.core.judge_replay import ReplayError, _decode, _require

#: Runs one bd invocation (arguments after `bd`) and returns its parsed JSON.
BdRunner = Callable[[Sequence[str]], Any]

READONLY = ("--readonly", "--sandbox")
HUMAN_LABEL = "human"
#: The issue fields whose change between two history snapshots is a re-spec.
SPEC_FIELDS = ("description", "design", "acceptance_criteria")
ENTRY = {"status", "labels", "reopens", "human", "closed_at", "spec_changes", "history_unavailable"}
MAX_OUTCOME_BYTES = 64 * 1024 * 1024
_FRACTION = re.compile(r"(\.\d+)(?=[+-]\d\d:\d\d$)")


def bd_runner(repo: Path) -> BdRunner:
    """The real runner: bd in `repo`, always read-only and sandboxed."""
    tracker = BeadsTracker(repo)

    def run(args: Sequence[str]) -> Any:
        return tracker.run(*READONLY, *args, parse_json=True)[1]

    return run


def instant(text: object) -> str | None:
    """Normalize a bd or git timestamp to UTC ISO-8601, or None when unreadable.

    bd writes `Z` and variable-width fractions, neither of which Python 3.10's
    `fromisoformat` accepts, so both are rewritten before parsing.
    """
    if type(text) is not str or not text or len(text) > 40:
        return None
    value = text[:-1] + "+00:00" if text.endswith("Z") else text
    value = _FRACTION.sub(lambda m: (m.group(1) + "000000")[:7], value)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat()


def _history(entries: object) -> tuple[int, list[str]] | None:
    """Reopen count and re-spec times from bd history, or None when unparseable.

    bd 1.2 lists one snapshot per commit, newest first; they are sorted here
    rather than trusted to arrive in order. A reopen is a snapshot that is no
    longer closed after one that was.
    """
    if type(entries) is not list:
        return None
    snapshots = []
    for entry in entries:
        issue = entry.get("Issue") if type(entry) is dict else None
        when = instant(entry.get("CommitDate")) if type(entry) is dict else None
        if type(issue) is not dict or when is None or type(issue.get("status")) is not str:
            return None
        snapshots.append((when, issue))
    snapshots.sort(key=lambda item: item[0])
    reopens, changes = 0, []
    previous = None
    for when, issue in snapshots:
        if previous is not None:
            if previous["status"] == "closed" and issue["status"] != "closed":
                reopens += 1
            if any(previous.get(name) != issue.get(name) for name in SPEC_FIELDS):
                changes.append(when)
        previous = issue
    return reopens, changes


def snapshot_outcomes(
    repo: Path, issue_ids: Iterable[str] | None = None, *, run: BdRunner | None = None,
) -> dict:
    """Per-issue tracker outcomes for the issues the logs reference.

    `issue_ids` of None takes history for every listed issue. A referenced id
    bd no longer lists is simply absent, which replay reports as
    `issue_missing`. A history bd cannot answer, or answers in a shape this
    reader does not know, is recorded as `history_unavailable` rather than
    guessed at.
    """
    run = run if run is not None else bd_runner(repo)
    try:
        rows = run(("list", "--all", "--json", "--limit", "0"))
    except (BdError, OSError, ValueError):
        raise ReplayError("cannot list tracker issues") from None
    if type(rows) is not list:
        raise ReplayError("unexpected tracker listing")
    listed = {row["id"]: row for row in rows
              if type(row) is dict and type(row.get("id")) is str}
    wanted = sorted(listed if issue_ids is None else set(issue_ids) & set(listed))
    issues = {}
    for issue_id in wanted:
        row = listed[issue_id]
        labels = sorted({label for label in row.get("labels") or () if type(label) is str})
        try:
            history = _history(run(("history", issue_id, "--json")))
        except (BdError, OSError, ValueError):
            history = None
        issues[issue_id] = {
            "status": str(row.get("status")),
            "labels": labels,
            "human": HUMAN_LABEL in labels,
            "closed_at": instant(row.get("closed_at")),
            "reopens": None if history is None else history[0],
            "spec_changes": None if history is None else history[1],
            "history_unavailable": history is None,
        }
    return {"schema_version": 1, "issues": issues}


def referenced_issues(records: Iterable[dict]) -> set[str]:
    """Every issue id a set of Jev records names."""
    return {record[key] for record in records
            for key in ("issue_id", "observed_issue_id")
            if type(record.get(key)) is str}


def read_outcomes(path: Path) -> dict[str, dict]:
    """Read and validate a snapshot written by `ortus judge outcomes`."""
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_OUTCOME_BYTES + 1)
        _require(len(data) <= MAX_OUTCOME_BYTES)
        document = _decode(data)
        _require(type(document) is dict and set(document) == {"schema_version", "issues"})
        _require(document["schema_version"] == 1 and type(document["issues"]) is dict)
        for issue_id, entry in document["issues"].items():
            _require(type(issue_id) is str and 0 < len(issue_id) <= 160)
            _require(type(entry) is dict and set(entry) == ENTRY)
            _require(type(entry["status"]) is str and re.fullmatch(r"[a-z_]{1,32}", entry["status"]) is not None)
            _require(type(entry["labels"]) is list and all(type(x) is str for x in entry["labels"]))
            _require(type(entry["human"]) is bool and type(entry["history_unavailable"]) is bool)
            _require(entry["closed_at"] is None or instant(entry["closed_at"]) == entry["closed_at"])
            unavailable = entry["history_unavailable"]
            _require((entry["reopens"] is None) == unavailable == (entry["spec_changes"] is None))
            if not unavailable:
                _require(type(entry["reopens"]) is int and entry["reopens"] >= 0)
                _require(type(entry["spec_changes"]) is list and all(
                    instant(x) == x for x in entry["spec_changes"]))
        return document["issues"]
    except (OSError, ValueError, TypeError, AttributeError, RecursionError):
        raise ReplayError("invalid outcomes document") from None


def dumps(snapshot: dict) -> str:
    return json.dumps(snapshot, indent=2, sort_keys=True, allow_nan=False) + "\n"
