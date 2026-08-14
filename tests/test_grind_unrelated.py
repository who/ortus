"""Disowning is re-examined when the worker then edits the path (ortus-s4km).

`_absorb_unrelated_declaration` used to honor a "not mine" declaration on two
filters alone — the path was inherited, and the journal does not attribute it to
this issue — and `_candidate_baseline` then subtracted it from every later
candidate. A worker that disowned a path and went on to edit it in the same
session therefore had that edit dropped: never committed, never reviewed, and
byte-identical on every correction attempt because no verb existed to move a
path back.

The same fingerprint comparison already lived in `_resume_or_handoff`, one call
site away. Here it is at the other one, with the changed regions deciding
between re-adoption and a plan gap.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from ortus.commands import grind as grind_mod
from ortus.core.transaction import CandidateJournal, JournalStore


pytestmark = pytest.mark.integration

DECLARATION = grind_mod._UNRELATED_DECLARATION
ISSUE = "ortus-aaaa"
CODE = "src/pkg/module.py"
DOC = "README.md"
STRANGER = "scratch/leftover.txt"

LOCATIONS = "\n".join(
    (
        f"- `{CODE}` — `render`",
        f"- `{DOC}` — the `state-graph` block",
    )
)

MODULE = "\n".join(
    (
        "CONSTANT = 1",  # 1
        "",  # 2
        "",  # 3
        "def render():",  # 4
        '    return "before"',  # 5
        "",  # 6
        "",  # 7
        "def parse():",  # 8
        '    return "before"',  # 9
    )
) + "\n"

DOCUMENT = "\n".join(
    (
        "# Ortus",  # 1
        "intro",  # 2
        "## State graph",  # 3
        "<!-- BEGIN GENERATED: state-graph -->",  # 4
        "generated body",  # 5
        "<!-- END GENERATED: state-graph -->",  # 6
        "## CodeGraph",  # 7
        "prose",  # 8
    )
) + "\n"


# ---------------------------------------------------------------------------
# fixture plumbing
# ---------------------------------------------------------------------------


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src" / "pkg").mkdir(parents=True)
    (repo / "scratch").mkdir(parents=True)
    for args in (
        ["init", "-q"],
        ["config", "user.email", "test@example.com"],
        ["config", "user.name", "test"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / CODE).write_text(MODULE, encoding="utf-8")
    (repo / DOC).write_text(DOCUMENT, encoding="utf-8")
    (repo / STRANGER).write_text("nobody's work\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True, capture_output=True
    )
    return repo


def _index(repo: Path) -> None:
    """A CodeGraph index for the seeded module, as `codegraph init` would leave."""

    (repo / ".codegraph").mkdir(exist_ok=True)
    connection = sqlite3.connect(repo / ".codegraph" / "codegraph.db")
    connection.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, kind TEXT, name TEXT, "
        "qualified_name TEXT, file_path TEXT, start_line INTEGER, end_line INTEGER)"
    )
    connection.executemany(
        "INSERT INTO nodes (file_path, name, kind, start_line, end_line) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            (CODE, "module.py", "file", 1, 9),
            (CODE, "render", "function", 4, 5),
            (CODE, "parse", "function", 8, 9),
        ],
    )
    connection.commit()
    connection.close()


def _journal(repo: Path, inherited: set[str], *, locations: str = LOCATIONS) -> tuple[
    JournalStore, CandidateJournal
]:
    store = JournalStore(repo)
    digest, ref = store.save_packet(
        ISSUE,
        {
            "id": ISSUE,
            "title": "a claimed issue",
            "description": "## Objective\n\nship it.\n",
            "design": f"## Concrete locations\n\n{locations}\n",
            "acceptance_criteria": "- AC-1: it ships.\n",
        },
    )
    journal = CandidateJournal.start(
        repo=repo,
        issue_id=ISSUE,
        base_head="0" * 40,
        baseline_paths=(),
        packet_hash=digest,
        packet_ref=ref,
    ).with_handoff(repo=repo, paths=inherited)
    store.save(journal)
    return store, journal


def _declare(repo: Path, *paths: str) -> None:
    declaration = repo / DECLARATION
    declaration.parent.mkdir(parents=True, exist_ok=True)
    declaration.write_text("\n".join(paths) + "\n", encoding="utf-8")


def _absorb(
    repo: Path, store: JournalStore, journal: CandidateJournal
) -> tuple[CandidateJournal, str]:
    log: list[str] = []
    updated = grind_mod._absorb_unrelated_declaration(repo, store, journal, log.append)
    return updated, "\n".join(log)


def _edit_render(repo: Path) -> None:
    (repo / CODE).write_text(MODULE.replace('return "before"', 'return "after"', 1))


def _edit_parse(repo: Path) -> None:
    body = MODULE.splitlines()
    body[8] = '    return "after"'
    (repo / CODE).write_text("\n".join(body) + "\n")


# ---------------------------------------------------------------------------
# AC-1 — an untouched declaration behaves exactly as before
# ---------------------------------------------------------------------------


def test_unchanged_path_stays_disowned(tmp_path: Path) -> None:
    """AC-1: the path's content still matches its handoff fingerprint, so nobody
    picked it back up and the declaration stands."""

    repo = _repo(tmp_path)
    _index(repo)
    store, journal = _journal(repo, {CODE, STRANGER})
    _declare(repo, STRANGER)

    updated, log = _absorb(repo, store, journal)

    assert updated.unrelated_paths == (STRANGER,)
    assert updated.plan_gap_routed is False
    assert "declared unrelated" in log
    assert "returns to the candidate" not in log
    assert not (repo / DECLARATION).exists()


def test_unchanged_path_stays_disowned_even_with_no_recorded_fingerprint(
    tmp_path: Path,
) -> None:
    """A journal that never recorded the path cannot tell adopted work from
    untouched work, so it leaves the judgement with the worker, as before."""

    repo = _repo(tmp_path)
    _index(repo)
    store, journal = _journal(repo, {CODE, STRANGER})
    journal = replace(journal, handoff_fingerprints={})
    store.save(journal)
    _edit_render(repo)
    _declare(repo, CODE)

    updated, _ = _absorb(repo, store, journal)

    assert updated.unrelated_paths == (CODE,)
    assert updated.plan_gap_routed is False


# ---------------------------------------------------------------------------
# AC-2 — a disowned path the worker then edited comes back
# ---------------------------------------------------------------------------


def test_edited_and_owned_is_readopted(tmp_path: Path) -> None:
    """AC-2: every changed region is `render`, which the packet's Concrete
    locations names, so the whole path returns to the candidate."""

    repo = _repo(tmp_path)
    _index(repo)
    store, journal = _journal(repo, {CODE, STRANGER})
    _edit_render(repo)
    _declare(repo, CODE, STRANGER)

    updated, log = _absorb(repo, store, journal)

    assert CODE not in updated.unrelated_paths
    assert updated.unrelated_paths == (STRANGER,)
    assert updated.plan_gap_routed is False
    assert "returns to the candidate" in log and CODE in log
    # The re-adopted path is no longer subtracted from what a candidate absorbs.
    assert CODE not in grind_mod._candidate_baseline(updated, frozenset())


def test_edited_and_owned_is_readopted_out_of_an_earlier_declaration(
    tmp_path: Path,
) -> None:
    """`with_unrelated` only ever adds, so a path an earlier worker in the same
    run disowned has to be removed explicitly or the edit stays stranded."""

    repo = _repo(tmp_path)
    _index(repo)
    store, journal = _journal(repo, {CODE})
    journal = journal.with_unrelated({CODE})
    store.save(journal)
    _edit_render(repo)
    _declare(repo, CODE)

    updated, _ = _absorb(repo, store, journal)

    assert updated.unrelated_paths == ()


def test_edited_but_foreign_stays_disowned(tmp_path: Path) -> None:
    """The worker edited it, but `parse` is nobody's concrete location here, so
    the declaration is still the right answer."""

    repo = _repo(tmp_path)
    _index(repo)
    store, journal = _journal(repo, {CODE})
    _edit_parse(repo)
    _declare(repo, CODE)

    updated, log = _absorb(repo, store, journal)

    assert updated.unrelated_paths == (CODE,)
    assert updated.plan_gap_routed is False
    assert "declared unrelated" in log


def test_edited_without_an_index_stays_disowned(tmp_path: Path) -> None:
    """AC-6 at the absorb site: no index means no attributable region, and an
    unattributable region is never absorbed."""

    repo = _repo(tmp_path)
    store, journal = _journal(repo, {CODE})
    _edit_render(repo)
    _declare(repo, CODE)

    updated, _ = _absorb(repo, store, journal)

    assert updated.unrelated_paths == (CODE,)
    assert updated.plan_gap_routed is False


# ---------------------------------------------------------------------------
# AC-3 — a path two issues both changed stops the run
# ---------------------------------------------------------------------------


def test_mixed_ownership_routes_plan_gap(tmp_path: Path) -> None:
    """AC-3: the generated block is this issue's and the CodeGraph prose is not,
    so grind names the file, the ranges, and the competing regions instead of
    splitting the file or guessing."""

    repo = _repo(tmp_path)
    _index(repo)
    store, journal = _journal(repo, {DOC})
    (repo / DOC).write_text(
        DOCUMENT.replace("generated body", "regenerated body").replace(
            "prose", "someone else's prose"
        ),
        encoding="utf-8",
    )
    _declare(repo, DOC)

    updated, log = _absorb(repo, store, journal)

    assert updated.plan_gap_routed is True
    assert "PLAN-GAP" in log
    assert DOC in log
    assert "state-graph" in log and "CodeGraph" in log
    assert "4-6" in log and "7-8" in log
    # Ownership is unresolved, so nothing moves: the path is not committed.
    assert updated.unrelated_paths == (DOC,)


def test_mixed_ownership_does_not_strand_an_unambiguous_sibling(
    tmp_path: Path,
) -> None:
    """Two declared paths disagreeing routes the plan gap — the stricter outcome
    — while the path whose regions are unambiguously ours still comes back."""

    repo = _repo(tmp_path)
    _index(repo)
    store, journal = _journal(repo, {CODE, DOC})
    _edit_render(repo)
    (repo / DOC).write_text(
        DOCUMENT.replace("generated body", "regenerated body").replace(
            "prose", "someone else's prose"
        ),
        encoding="utf-8",
    )
    _declare(repo, CODE, DOC)

    updated, log = _absorb(repo, store, journal)

    assert updated.plan_gap_routed is True
    assert updated.unrelated_paths == (DOC,)
    assert "returns to the candidate" in log and "PLAN-GAP" in log


# ---------------------------------------------------------------------------
# AC-8 — the two shapes observed on 2026-08-09
# ---------------------------------------------------------------------------


def test_regression_20260809_disowned_code_file_is_readopted(tmp_path: Path) -> None:
    """ortus-yu7w inherited 20 parked paths, disowned `grind.py`, then wrote its
    own deliverable into it. Both correction attempts produced a byte-identical
    candidate because nothing re-examined the declaration."""

    repo = _repo(tmp_path)
    _index(repo)
    store, journal = _journal(repo, {CODE, DOC, STRANGER})
    _edit_render(repo)
    _declare(repo, CODE, DOC, STRANGER)

    updated, _ = _absorb(repo, store, journal)

    assert CODE not in updated.unrelated_paths
    assert set(updated.unrelated_paths) == {DOC, STRANGER}


def test_regression_20260809_mixed_markdown_routes_plan_gap(tmp_path: Path) -> None:
    """ortus-396o stranded README.md across six attempts; the next issue then
    wrote its own README deliverable into the same file, and the verifier failed
    a half-delivered docs change. Mixed ownership is a human's call."""

    repo = _repo(tmp_path)
    _index(repo)
    store, journal = _journal(repo, {DOC})
    (repo / DOC).write_text(
        DOCUMENT.replace("generated body", "regenerated body").replace(
            "prose", "the stranded predecessor's prose"
        ),
        encoding="utf-8",
    )
    _declare(repo, DOC)

    updated, log = _absorb(repo, store, journal)

    assert updated.plan_gap_routed is True
    assert "PLAN-GAP" in log


# ---------------------------------------------------------------------------
# AC-7 — an unchanged declaration leaves the journal byte-identical
# ---------------------------------------------------------------------------


def test_nothing_declared_leaves_the_journal_untouched(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _index(repo)
    store, journal = _journal(repo, {CODE})
    before = json.loads(store.path.read_text(encoding="utf-8"))

    updated, log = _absorb(repo, store, journal)

    assert updated is journal
    assert json.loads(store.path.read_text(encoding="utf-8")) == before
    assert log == ""
