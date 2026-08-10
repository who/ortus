"""Region attribution for one changed path (ortus-s4km).

Ownership of a file two issues both touched is decided by the regions its
changed lines fall in, not by the path string. Code regions come from the
CodeGraph index, Markdown regions from heading spans and generated markers, and
anything neither can name is foreign — so an absent index refuses to absorb
rather than guessing.
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

from ortus.core.attribution import (
    Ownership,
    Region,
    changed_lines,
    decide_ownership,
    describe,
    path_ownership,
    region_map,
)


CODE = "src/pkg/module.py"
DOC = "docs/guide.md"


# ---------------------------------------------------------------------------
# fixture plumbing
# ---------------------------------------------------------------------------


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src" / "pkg").mkdir(parents=True)
    (repo / "docs").mkdir(parents=True)
    for args in (
        ["init", "-q"],
        ["config", "user.email", "test@example.com"],
        ["config", "user.name", "test"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    return repo


def _commit(repo: Path, message: str = "seed") -> None:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", message], cwd=repo, check=True, capture_output=True
    )


def _index(tmp_path: Path, rows: list[tuple[str, str, str, int, int]]) -> Path:
    """A minimal CodeGraph index carrying only the columns attribution reads."""

    database = tmp_path / "codegraph.db"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, kind TEXT, name TEXT, "
        "qualified_name TEXT, file_path TEXT, start_line INTEGER, end_line INTEGER)"
    )
    connection.executemany(
        "INSERT INTO nodes (file_path, name, kind, start_line, end_line) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    connection.commit()
    connection.close()
    return database


# ---------------------------------------------------------------------------
# AC-4 — code regions resolve to the smallest enclosing node
# ---------------------------------------------------------------------------


def test_smallest_enclosing_symbol_wins_over_every_wider_node(tmp_path: Path) -> None:
    """AC-4: nesting is resolved by span width, so a line inside a method is the
    method's rather than the class's or the file's."""

    index = _index(
        tmp_path,
        [
            (CODE, "module.py", "file", 1, 40),
            (CODE, "Widget", "class", 5, 30),
            (CODE, "render", "function", 12, 20),
            (CODE, "helper", "function", 22, 28),
        ],
    )
    regions = region_map(tmp_path, CODE, {15}, index=index)

    assert [region.name for region in regions] == ["render"]
    assert (regions[0].start, regions[0].end) == (12, 20)


def test_smallest_enclosing_symbol_falls_back_to_the_file_node(tmp_path: Path) -> None:
    """Module-level work is still attributable: the file node encloses it, and a
    packet that names the file names that region."""

    index = _index(
        tmp_path,
        [
            (CODE, "module.py", "file", 1, 40),
            (CODE, "Widget", "class", 5, 30),
        ],
    )
    regions = region_map(tmp_path, CODE, {2}, index=index)

    assert [region.name for region in regions] == ["module.py"]
    assert decide_ownership(regions, f"- `{CODE}` — `Widget`").ownership is Ownership.OWN


# ---------------------------------------------------------------------------
# AC-5 — Markdown regions resolve from headings and generated markers
# ---------------------------------------------------------------------------


def test_markdown_heading_and_marker_blocks_are_regions(tmp_path: Path) -> None:
    """AC-5: the deepest heading owns a line, except inside a generated block,
    whose marker names its own generator."""

    repo = _repo(tmp_path)
    (repo / DOC).write_text(
        "\n".join(
            [
                "# Guide",  # 1
                "intro",  # 2
                "## Install",  # 3
                "run it",  # 4
                "### Windows",  # 5
                "quirks",  # 6
                "## State graph",  # 7
                "<!-- BEGIN GENERATED: state-graph -->",  # 8
                "```mermaid",  # 9
                "# not a heading",  # 10
                "```",  # 11
                "## also generated output",  # 12
                "<!-- END GENERATED: state-graph -->",  # 13
                "after",  # 14
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    named = {
        region.start: region.name
        for region in region_map(repo, DOC, {2, 4, 6, 10, 12}, index=None)
    }
    assert named[1] == "Guide"
    assert named[3] == "Install"
    assert named[5] == "Windows"
    # Both lines inside the markers belong to the generated block, including the
    # heading the generator itself emitted.
    assert named[8] == "state-graph"
    assert len([start for start in named if start == 8]) == 1

    kinds = {region.name: region.kind for region in region_map(repo, DOC, {6, 10})}
    assert kinds == {"Windows": "heading", "state-graph": "marker"}


def test_markdown_heading_and_marker_names_decide_ownership(tmp_path: Path) -> None:
    """A heading is prose, so it matches the packet's wording; a marker name is
    an identifier and matches the way the packet writes one."""

    repo = _repo(tmp_path)
    (repo / DOC).write_text(
        "# Guide\nintro\n## Install\nrun it\n", encoding="utf-8"
    )
    regions = region_map(repo, DOC, {4})

    assert decide_ownership(regions, "the Install section of `docs/guide.md`").ownership is (
        Ownership.OWN
    )
    assert decide_ownership(regions, "`docs/guide.md` — the Usage section").ownership is (
        Ownership.FOREIGN
    )


# ---------------------------------------------------------------------------
# AC-6 — nothing unattributable is ever absorbed
# ---------------------------------------------------------------------------


def test_unattributable_is_foreign_without_an_index(tmp_path: Path) -> None:
    """AC-6: with no CodeGraph index there are no code regions, so every changed
    line is unattributed and the path can never be silently absorbed."""

    regions = region_map(tmp_path, CODE, {4, 5, 9}, index=tmp_path / "missing.db")

    assert [(r.name, r.start, r.end) for r in regions] == [("", 4, 5), ("", 9, 9)]
    assert all(not region.attributable for region in regions)
    assert decide_ownership(regions, f"- `{CODE}` — `render`").ownership is (
        Ownership.FOREIGN
    )


def test_unattributable_is_foreign_beside_an_owned_region(tmp_path: Path) -> None:
    """A line the index cannot name sits beside one it can, which is a mix — the
    stricter outcome, not a quiet absorb of the unnamed part."""

    index = _index(tmp_path, [(CODE, "render", "function", 12, 20)])
    regions = region_map(tmp_path, CODE, {15, 60}, index=index)

    assert decide_ownership(regions, "- `render`").ownership is Ownership.MIXED


def test_unattributable_covers_a_path_with_no_regions_at_all(tmp_path: Path) -> None:
    """A deleted or binary path has nothing to attribute, so the declaration
    stands rather than becoming a plan gap."""

    assert region_map(tmp_path, CODE, set()) == ()
    assert decide_ownership((), "- `render`").ownership is Ownership.FOREIGN


# ---------------------------------------------------------------------------
# changed lines and the whole-path decision
# ---------------------------------------------------------------------------


def test_changed_lines_cover_edits_additions_and_untracked_files(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    (repo / CODE).write_text("\n".join(f"line {n}" for n in range(1, 11)) + "\n")
    _commit(repo)

    body = [f"line {n}" for n in range(1, 11)]
    body[4] = "line 5 edited"
    (repo / CODE).write_text("\n".join(body) + "\n")
    assert changed_lines(repo, CODE) == frozenset({5})

    (repo / "docs" / "new.md").write_text("a\nb\nc\n")
    assert changed_lines(repo, "docs/new.md") == frozenset({1, 2, 3})

    (repo / CODE).unlink()
    assert changed_lines(repo, CODE) == frozenset()


def test_changed_lines_report_the_seam_a_deletion_leaves(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / CODE).write_text("\n".join(f"line {n}" for n in range(1, 11)) + "\n")
    _commit(repo)
    kept = [f"line {n}" for n in range(1, 11) if n not in (5, 6)]
    (repo / CODE).write_text("\n".join(kept) + "\n")

    assert changed_lines(repo, CODE) == frozenset({4})


def test_path_ownership_is_whole_path_and_names_its_ranges(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / DOC).write_text("# Guide\nintro\n## Install\nrun it\n", encoding="utf-8")
    _commit(repo)
    (repo / DOC).write_text(
        "# Guide\nintro edited\n## Install\nrun it differently\n", encoding="utf-8"
    )

    decision = path_ownership(repo, DOC, "the Install section")
    assert decision.ownership is Ownership.MIXED
    report = describe(DOC, decision)
    assert DOC in report and "1-4" in report and "3-4" in report and "Guide" in report


def test_regions_are_named_once_however_many_lines_hit_them(tmp_path: Path) -> None:
    index = _index(tmp_path, [(CODE, "render", "function", 12, 20)])

    assert region_map(tmp_path, CODE, {12, 15, 20}, index=index) == (
        Region("render", 12, 20, "function", ()),
    )


@pytest.mark.parametrize(
    "locations, expected",
    [
        ("- `src/pkg/module.py` — `render`", Ownership.OWN),
        ("- `src/pkg/other.py` — `parse`", Ownership.FOREIGN),
        ("- `parse` — the loop around render also moves", Ownership.FOREIGN),
    ],
)
def test_identifiers_match_only_how_a_packet_writes_them(
    tmp_path: Path, locations: str, expected: Ownership
) -> None:
    """Backticks are how Concrete locations names a symbol, so when the section
    has any, the prose around them is not a claim."""

    index = _index(tmp_path, [(CODE, "render", "function", 12, 20)])
    regions = region_map(tmp_path, CODE, {15}, index=index)

    assert decide_ownership(regions, locations).ownership is expected
