"""An in-memory stand-in for the `bd` process, for loop-logic tests.

Every `bd` call is a fresh Go process opening a Dolt database — about a
second of it on a CI runner — so a test whose contract is a grind loop
decision pays tens of seconds of subprocess time to assert something the
loop concluded from three field values. This module removes the process,
not the client: :class:`FakeBdClient` is a real :class:`~ortus.core.bd.BdClient`
whose ``_run`` answers the same argv from a dict. ``show``, ``close_once``,
``count_by_status``, ``snapshot`` and every other derived method therefore
remain the production code the real client runs, and only the tracker
behind them is fake — which is why the fake cannot quietly implement the
behavior a test is asserting.

The argv surface here is the one `BdClient` actually issues, plus the few
verbs (`dep add`, `create --parent`, `remember`) a fixture needs to seed a
shape the typed surface cannot express. An argv this does not recognize
raises rather than returning a plausible answer, so a client method that
grows a new bd call fails loudly in the first test that reaches it instead
of reading as an empty tracker.

`tests/test_fake_bd_contract.py` runs one set of cases against this and
against a real `bd` workspace, so the two cannot drift apart in silence.
"""

from __future__ import annotations

import datetime as _dt
import json
import uuid
from pathlib import Path
from typing import Any

from ortus.core.bd import BdClient, BdError

#: Fields `bd` omits from its JSON entirely when they hold nothing, rather
#: than emitting a null or an empty list. `BdClient` reads all of them
#: through `.get(...) or <empty>`, so the distinction never reaches a
#: caller — but a contract case that inspects a raw row would see it, and a
#: fake that always emitted the key would hide a real difference.
_OMIT_WHEN_EMPTY = (
    "labels",
    "dependencies",
    "description",
    "design",
    "acceptance_criteria",
    "notes",
    "assignee",
    "close_reason",
    "closed_at",
    "external_ref",
    "parent",
)

#: What `--brief` drops. Everything else — labels, the parent link, the
#: dependency rows — survives it, which is what lets one `--brief` listing
#: answer a whole snapshot block's label questions.
_BRIEF_OMITS = ("description", "design", "acceptance_criteria", "notes")


def _stamp() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def seed(client: BdClient, *args: str) -> str:
    """Run a raw bd argv against a real or fake client and return its stdout.

    A fixture needs shapes the typed surface has no verb for — a parent
    link, a blocking dependency, a stored memory. Routing those through the
    client's own process primitive keeps one argument list working against
    either backend, which is what lets a contract case seed both the same
    way and then assert only through the typed methods.
    """

    stdout, _ = client._run(*args)
    return stdout.strip()


class FakeBdClient(BdClient):
    """A `BdClient` whose bd process is a dict in this interpreter.

    Construct one per fixture repo and hand it to the code under test the
    way production hands it a real client — through `grind_mod._make_bd`,
    which exists for exactly that substitution.
    """

    def __init__(self, repo: Path | str | None = None, *, prefix: str = "fake") -> None:
        super().__init__(repo=Path(repo) if repo is not None else Path("."))
        self.prefix = prefix
        #: issue id → the row `bd` would print for it.
        self.rows: dict[str, dict[str, Any]] = {}
        #: issue id → its ordered comment thread.
        self.threads: dict[str, list[dict[str, Any]]] = {}
        #: issue id → the ids it is blocked behind.
        self.blockers: dict[str, set[str]] = {}
        #: `bd remember` store, read back by `memories()`.
        self.store: dict[str, str] = {}
        #: Every argv this fake was asked to run, for tests that assert on
        #: how much tracker traffic a code path generates.
        self.calls: list[list[str]] = []
        self._sequence = 0
        self._actor = "Ortus Tests"

    # --- the one method the real client sends every bd call through -------

    def _run(self, *args: str, parse_json: bool = False) -> tuple[str, Any]:
        argv = [str(arg) for arg in args]
        self.calls.append(argv)
        stdout = self._dispatch(argv)
        parsed = json.loads(stdout) if parse_json and stdout.strip() else None
        return stdout, parsed

    def _fail(self, argv: list[str], stderr: str) -> BdError:
        return BdError([self.binary, *argv], 1, stderr)

    # --- argv dispatch ----------------------------------------------------

    def _dispatch(self, argv: list[str]) -> str:
        args = list(argv)
        while args and args[0] in ("--readonly", "--sandbox", "--actor"):
            flag = args.pop(0)
            if flag == "--actor":
                if not args:
                    raise self._fail(argv, "Error: --actor requires a value")
                self._actor = args.pop(0)
        if not args:
            raise self._fail(argv, "Error: no command given")
        verb = args.pop(0)
        handler = getattr(self, f"_bd_{verb.replace('-', '_')}", None)
        if handler is None:
            raise self._fail(argv, f"Error: unknown command {verb!r}")
        return handler(args, argv)

    # --- reads ------------------------------------------------------------

    def _bd_list(self, args: list[str], argv: list[str]) -> str:
        status: str | None = None
        exclude: list[str] = []
        label_any: list[str] = []
        brief = False
        while args:
            flag = args.pop(0)
            if flag == "--status":
                status = args.pop(0)
            elif flag == "--exclude-label":
                exclude.append(args.pop(0))
            elif flag == "--label-any":
                label_any.append(args.pop(0))
            elif flag == "--brief":
                brief = True
            elif flag == "--limit":
                args.pop(0)
            elif flag in ("--all", "--json"):
                continue
            else:
                raise self._fail(argv, f"Error: unknown flag {flag!r} for list")
        rows = [
            row
            for row in self._ordered()
            if (status is None or row["status"] == status)
            and not any(label in self._labels(row) for label in exclude)
            and (not label_any or any(label in self._labels(row) for label in label_any))
        ]
        return json.dumps([self._render(row, brief=brief) for row in rows])

    def _bd_ready(self, args: list[str], argv: list[str]) -> str:
        exclude: list[str] = []
        while args:
            flag = args.pop(0)
            if flag == "--exclude-label":
                exclude.append(args.pop(0))
            elif flag == "--json":
                continue
            else:
                raise self._fail(argv, f"Error: unknown flag {flag!r} for ready")
        rows = [
            row
            for row in self._ordered()
            if row["status"] == "open"
            and not any(label in self._labels(row) for label in exclude)
            and all(
                self.rows[blocker]["status"] == "closed"
                for blocker in self.blockers.get(row["id"], ())
                if blocker in self.rows
            )
        ]
        # bd orders the ready queue by priority and then by age; the loop
        # selects the first non-epic row off it, so the order is contract.
        rows.sort(key=lambda row: (row["priority"], row["_seq"]))
        return json.dumps([self._render(row) for row in rows])

    def _bd_show(self, args: list[str], argv: list[str]) -> str:
        wanted = [arg for arg in args if arg not in ("--json", "--")]
        rows = []
        for issue_id in wanted:
            row = self.rows.get(issue_id)
            if row is None:
                raise self._fail(
                    argv,
                    f'Error fetching {issue_id}: no issue found matching "{issue_id}"',
                )
            rows.append(self._render(row, full=True))
        return json.dumps(rows)

    def _bd_count(self, args: list[str], argv: list[str]) -> str:
        status: str | None = None
        while args:
            flag = args.pop(0)
            if flag == "--status":
                status = args.pop(0)
            elif flag == "--json":
                continue
            else:
                raise self._fail(argv, f"Error: unknown flag {flag!r} for count")
        total = sum(1 for row in self.rows.values() if row["status"] == status)
        return json.dumps({"count": total, "schema_version": 1})

    def _bd_children(self, args: list[str], argv: list[str]) -> str:
        parent = self._one_id(args, argv, "children")
        kids = [row for row in self._ordered() if row.get("parent") == parent]
        return json.dumps([self._render(row) for row in kids])

    def _bd_memories(self, args: list[str], argv: list[str]) -> str:
        payload: dict[str, Any] = dict(self.store)
        payload["schema_version"] = 1
        return json.dumps(payload)

    # --- writes -----------------------------------------------------------

    def _bd_create(self, args: list[str], argv: list[str]) -> str:
        fields: dict[str, Any] = {
            "title": "",
            "issue_type": "task",
            "priority": 2,
            "labels": [],
        }
        parent: str | None = None
        mapping = {
            "--title": "title",
            "--type": "issue_type",
            "--description": "description",
            "--design": "design",
            "--acceptance": "acceptance_criteria",
            "--notes": "notes",
            "--external-ref": "external_ref",
        }
        while args:
            flag = args.pop(0)
            if flag in mapping:
                fields[mapping[flag]] = args.pop(0)
            elif flag == "--priority":
                fields["priority"] = int(args.pop(0))
            elif flag == "--labels":
                fields["labels"] = [
                    label for label in args.pop(0).split(",") if label
                ]
            elif flag == "--parent":
                parent = args.pop(0)
            elif flag == "--silent":
                continue
            else:
                raise self._fail(argv, f"Error: unknown flag {flag!r} for create")
        if parent is not None and parent not in self.rows:
            raise self._fail(
                argv, f'Error: resolving ID {parent}: no issue found matching "{parent}"'
            )
        issue_id = self._mint(parent)
        self._sequence += 1
        row = {
            "id": issue_id,
            "status": "open",
            "owner": "ortus-tests@example.invalid",
            "created_by": self._actor,
            "created_at": _stamp(),
            "updated_at": _stamp(),
            "comment_count": 0,
            "dependency_count": 0,
            "dependent_count": 0,
            "_seq": self._sequence,
            **fields,
        }
        if parent is not None:
            row["parent"] = parent
        self.rows[issue_id] = row
        return issue_id

    def _bd_close(self, args: list[str], argv: list[str]) -> str:
        issue_id: str | None = None
        reason: str | None = None
        while args:
            arg = args.pop(0)
            if arg == "--reason":
                reason = args.pop(0)
            elif arg == "--":
                continue
            else:
                issue_id = arg
        row = self._require(issue_id, argv)
        row["status"] = "closed"
        row["closed_at"] = _stamp()
        row["updated_at"] = _stamp()
        # `bd close` without a reason leaves the reason an earlier close
        # recorded in place, which is what makes `close_once` worth having.
        if reason:
            row["close_reason"] = reason
        return f"✓ Closed {row['id']} — {row['title']}"

    def _bd_update(self, args: list[str], argv: list[str]) -> str:
        if "--help" in args:
            return "Flags:\n  --claim  claim the issue for --actor\n"
        issue_id: str | None = None
        status: str | None = None
        claim = False
        while args:
            arg = args.pop(0)
            if arg == "--status":
                status = args.pop(0)
            elif arg == "--claim":
                claim = True
            elif arg == "--":
                continue
            else:
                issue_id = arg
        row = self._require(issue_id, argv)
        if claim:
            row["status"] = "in_progress"
            row["assignee"] = self._actor
        elif status is not None:
            row["status"] = status
        row["updated_at"] = _stamp()
        return f"✓ Updated issue: {row['id']} — {row['title']}"

    def _bd_unclaim(self, args: list[str], argv: list[str]) -> str:
        if "--help" in args:
            return "Flags:\n  --if-assignee string  release only this assignee's claim\n"
        issue_id: str | None = None
        expected: str | None = None
        while args:
            arg = args.pop(0)
            if arg == "--if-assignee":
                expected = args.pop(0)
            elif arg == "--":
                continue
            else:
                issue_id = arg
        row = self._require(issue_id, argv)
        if expected is not None and row.get("assignee") != expected:
            raise self._fail(argv, f"Error: {row['id']} is not assigned to {expected}")
        row["status"] = "open"
        row.pop("assignee", None)
        row["updated_at"] = _stamp()
        return f"✓ Unclaimed {row['id']}"

    def _bd_label(self, args: list[str], argv: list[str]) -> str:
        if len(args) < 3:
            raise self._fail(argv, "Error: label takes an action, an id and a label")
        action, issue_id, label = args[0], args[1], args[2]
        row = self._require(issue_id, argv)
        labels = list(row.get("labels") or [])
        if action == "add":
            if label not in labels:
                labels.append(label)
        elif action == "remove":
            labels = [existing for existing in labels if existing != label]
        else:
            raise self._fail(argv, f"Error: unknown label action {action!r}")
        row["labels"] = labels
        return f"✓ Labels on {row['id']}: {', '.join(labels)}"

    def _bd_comments(self, args: list[str], argv: list[str]) -> str:
        if args and args[0] == "add":
            if len(args) < 3:
                raise self._fail(argv, "Error: comments add takes an id and a body")
            row = self._require(args[1], argv)
            thread = self.threads.setdefault(row["id"], [])
            thread.append(
                {
                    "id": str(uuid.uuid4()),
                    "issue_id": row["id"],
                    "author": self._actor,
                    "text": args[2],
                    "created_at": _stamp(),
                }
            )
            row["comment_count"] = len(thread)
            return f"✓ Added comment to {row['id']}"
        issue_id = self._one_id(args, argv, "comments")
        self._require(issue_id, argv)
        return json.dumps(list(self.threads.get(issue_id, [])))

    def _bd_dep(self, args: list[str], argv: list[str]) -> str:
        if len(args) < 3 or args[0] != "add":
            raise self._fail(argv, "Error: dep takes `add <id> <depends-on>`")
        row = self._require(args[1], argv)
        blocker = self._require(args[2], argv)
        self.blockers.setdefault(row["id"], set()).add(blocker["id"])
        row["dependency_count"] = len(self.blockers[row["id"]])
        blocker["dependent_count"] = blocker.get("dependent_count", 0) + 1
        return f"✓ Added dependency: {row['id']} depends on {blocker['id']}"

    def _bd_remember(self, args: list[str], argv: list[str]) -> str:
        body: str | None = None
        key: str | None = None
        while args:
            arg = args.pop(0)
            if arg == "--key":
                key = args.pop(0)
            else:
                body = arg
        if not key or body is None:
            raise self._fail(argv, "Error: remember takes a body and --key")
        self.store[key] = body
        return f"Remembered [{key}]"

    # --- shared helpers ---------------------------------------------------

    def _mint(self, parent: str | None) -> str:
        if parent is not None:
            kids = sum(1 for row in self.rows.values() if row.get("parent") == parent)
            return f"{parent}.{kids + 1}"
        index = len(self.rows) + 1
        while f"{self.prefix}-{index}" in self.rows:
            index += 1
        return f"{self.prefix}-{index}"

    def _require(self, issue_id: str | None, argv: list[str]) -> dict[str, Any]:
        row = self.rows.get(issue_id or "")
        if row is None:
            raise self._fail(
                argv,
                f'Error: resolving ID {issue_id}: no issue found matching "{issue_id}"',
            )
        return row

    def _one_id(self, args: list[str], argv: list[str], verb: str) -> str:
        ids = [arg for arg in args if not arg.startswith("--") and arg != "--"]
        if len(ids) != 1:
            raise self._fail(argv, f"Error: {verb} takes exactly one id")
        return ids[0]

    @staticmethod
    def _labels(row: dict[str, Any]) -> list[str]:
        return list(row.get("labels") or [])

    def _ordered(self) -> list[dict[str, Any]]:
        return sorted(self.rows.values(), key=lambda row: row["_seq"])

    def _render(
        self, row: dict[str, Any], *, brief: bool = False, full: bool = False
    ) -> dict[str, Any]:
        """One row as bd would print it: no bookkeeping, no empty fields."""

        rendered = {key: value for key, value in row.items() if key != "_seq"}
        rendered["dependencies"] = self._dependency_rows(row)
        if full:
            rendered.setdefault("revision", 1)
        if brief:
            for key in _BRIEF_OMITS:
                rendered.pop(key, None)
        for key in _OMIT_WHEN_EMPTY:
            if not rendered.get(key):
                rendered.pop(key, None)
        return rendered

    def _dependency_rows(self, row: dict[str, Any]) -> list[dict[str, Any]]:
        """The `dependencies` block bd embeds: the parent link, then blockers.

        Typed as bd types it — each entry a summary of the issue depended on
        plus the `dependency_type` that says which kind of edge it is — so a
        caller that tells a parent-child edge from a blocking one sees the
        same shape here.
        """

        edges: list[dict[str, Any]] = []
        for kind, target in (
            ("parent-child", row.get("parent")),
            *(("blocks", blocker) for blocker in sorted(self.blockers.get(row["id"], ()))),
        ):
            linked = self.rows.get(target or "")
            if linked is None:
                continue
            edges.append(
                {
                    "id": linked["id"],
                    "title": linked["title"],
                    "status": linked["status"],
                    "priority": linked["priority"],
                    "issue_type": linked["issue_type"],
                    "dependency_type": kind,
                }
            )
        return edges
