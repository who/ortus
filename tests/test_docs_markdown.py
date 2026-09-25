"""The shape of README.md and the flat docs pages, enforced rather than asked for.

Documentation rots in ways prose cannot police: a README grows past the length
anyone reads, a moved page leaves a dangling link, a new verb never reaches the
command reference. Each rule below is one of those failures written as a check,
so the regression is a red test in the window that caused it rather than a
reader's dead end months later.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from ortus.cli import app

REPO = Path(__file__).resolve().parents[1]
README = REPO / "README.md"
DOCS = REPO / "docs"

#: README is an entry point, not the manual. Past this it stops being read top
#: to bottom, which is the only way an entry point works.
README_LINE_BUDGET = 200

#: The level-2 headings README carries, in order. Restated here rather than
#: derived from the file, so reordering or dropping one is a failing test and
#: not a silent change of shape.
README_HEADINGS = (
    "How it works",
    "Install",
    "Quick start",
    "Prerequisites",
    "The verbs",
    "Why ortus",
    "Configuration",
    "Documentation",
    "Development",
    "License",
)

#: Pages whose subject is a flow and is therefore owed a diagram. A wall of
#: prose describing a sequence is the thing these pages exist to replace.
DIAGRAM_PAGES = (
    "README.md",
    "docs/grind.md",
    "docs/authoring.md",
    "docs/backends.md",
    "docs/cost.md",
    "docs/judge.md",
)

#: Root pages that moved under docs/. Their old paths must not be linked by
#: anything tracked, because a link that 404s is worse than no link.
MOVED_PAGES = {"LABELS.md": "docs/labels.md", "ZFC.md": "docs/zfc.md"}

_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_CODE_SPAN = re.compile(r"(`+)[^`]*?\1")
_AUTOLINK = re.compile(r"<(?:https?://|mailto:)[^>\s]*>")
_HTML = re.compile(r"<[A-Za-z/!?]")
_LINK = re.compile(r"\[[^\]]*\]\(\s*([^)\s]+)")
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")


def _pages() -> list[Path]:
    """README plus every flat docs page, which is the whole enforced surface."""

    return [README, *sorted(DOCS.glob("*.md"))]


def _rel(path: Path) -> str:
    return path.relative_to(REPO).as_posix()


def _without_code(text: str) -> list[str]:
    """`text` line by line with code blanked out, so line numbers still line up.

    Every rule here is about prose: a fenced example is allowed to contain an
    HTML tag, a `<repo>` placeholder, or a link that points nowhere. Blanking
    rather than deleting keeps a failure message able to name the real line.
    """

    lines: list[str] = []
    fence: str | None = None
    for raw in text.splitlines():
        opener = _FENCE.match(raw)
        if fence is None:
            if opener:
                fence = opener.group(1)[0] * 3
                lines.append("")
                continue
        else:
            lines.append("")
            if opener and opener.group(1)[0] * 3 == fence:
                fence = None
            continue
        stripped = _CODE_SPAN.sub("", raw)
        lines.append(_AUTOLINK.sub("", stripped))
    return lines


def _headings(text: str) -> list[tuple[int, str]]:
    """(level, title) for every heading outside fenced code."""

    found = []
    for line in _without_code(text):
        match = _HEADING.match(line)
        if match:
            found.append((len(match.group(1)), match.group(2)))
    return found


def _slug(heading: str) -> str:
    """GitHub's heading anchor: lowercase, punctuation dropped, spaces hyphened."""

    text = re.sub(r"[^\w\s-]", "", heading.strip().lower())
    return re.sub(r"\s+", "-", text)


def _links(text: str) -> list[tuple[int, str]]:
    """(line number, target) for every inline link outside code."""

    found = []
    for number, line in enumerate(_without_code(text), start=1):
        for target in _LINK.findall(line):
            found.append((number, target))
    return found


def _tracked_markdown() -> list[Path]:
    """Tracked `.md` files, minus the two histories nothing may rewrite."""

    listing = subprocess.run(
        ["git", "ls-files", "-z", "*.md"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    return [
        REPO / name
        for name in listing.stdout.split("\0")
        if name and not name.startswith(".beads/") and name != "CHANGELOG.md"
    ]


# ---------------------------------------------------------------------------
# AC-1: README is a short, ordered entry point
# ---------------------------------------------------------------------------


def test_readme_is_a_short_ordered_entry_point() -> None:
    text = README.read_text(encoding="utf-8")
    length = len(text.splitlines())

    assert length <= README_LINE_BUDGET, (
        f"README.md is {length} lines, over the {README_LINE_BUDGET}-line budget; "
        "move a section to a docs page rather than raising the budget"
    )
    assert [title for level, title in _headings(text) if level == 2] == list(
        README_HEADINGS
    )


# ---------------------------------------------------------------------------
# AC-2: Markdown only
# ---------------------------------------------------------------------------


def test_no_raw_html_outside_code_spans_and_fences() -> None:
    offenders = []
    for page in _pages():
        for number, line in enumerate(_without_code(page.read_text("utf-8")), start=1):
            if _HTML.search(line):
                offenders.append(f"{_rel(page)}:{number}: {line.strip()}")

    assert offenders == [], (
        "these pages carry raw HTML outside code; GitHub-flavored Markdown "
        "renders everything these pages need:\n" + "\n".join(offenders)
    )


# ---------------------------------------------------------------------------
# AC-3: every relative link and anchor resolves
# ---------------------------------------------------------------------------


def test_relative_links_and_anchors_resolve() -> None:
    broken = []
    for page in _pages():
        for number, target in _links(page.read_text("utf-8")):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            path_part, _, anchor = target.partition("#")
            resolved = page if not path_part else (page.parent / path_part).resolve()
            if path_part and not resolved.exists():
                broken.append(f"{_rel(page)}:{number}: {target} does not exist")
                continue
            if not anchor or resolved.suffix not in {".md", ".markdown"}:
                continue
            slugs = {
                _slug(title)
                for _, title in _headings(resolved.read_text(encoding="utf-8"))
            }
            if anchor.lower() not in slugs:
                broken.append(f"{_rel(page)}:{number}: {target} has no such heading")

    assert broken == [], "\n".join(broken)


# ---------------------------------------------------------------------------
# AC-4: flow pages carry a diagram
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", DIAGRAM_PAGES)
def test_flow_pages_carry_a_mermaid_diagram(name: str) -> None:
    text = (REPO / name).read_text(encoding="utf-8")

    assert "```mermaid" in text, f"{name} describes a flow and owes a mermaid diagram"


# ---------------------------------------------------------------------------
# AC-5: the command reference is complete
# ---------------------------------------------------------------------------


def _registered_invocations() -> list[str]:
    """Every `ortus <verb>` a user can type, group subcommands included."""

    invocations = [
        f"ortus {command.name or command.callback.__name__}"
        for command in app.registered_commands
    ]
    for group in app.registered_groups:
        for command in group.typer_instance.registered_commands:
            leaf = command.name or command.callback.__name__
            invocations.append(f"ortus {group.name} {leaf}")
    return invocations


def test_commands_page_names_every_registered_verb() -> None:
    page = (DOCS / "commands.md").read_text(encoding="utf-8")
    missing = [name for name in _registered_invocations() if name not in page]

    assert missing == [], (
        "docs/commands.md is the full command reference and does not name: "
        + ", ".join(missing)
    )


# ---------------------------------------------------------------------------
# AC-6: the README index reaches every page
# ---------------------------------------------------------------------------


def test_documentation_index_links_every_docs_page() -> None:
    text = README.read_text(encoding="utf-8")
    start = text.index("## Documentation")
    end = text.index("\n## ", start + 1)
    section = text[start:end]
    missing = [
        _rel(page) for page in sorted(DOCS.glob("*.md")) if _rel(page) not in section
    ]

    assert missing == [], (
        "the README Documentation section is the index of record and omits: "
        + ", ".join(missing)
    )


# ---------------------------------------------------------------------------
# AC-7: the moved root pages live under docs/ only
# ---------------------------------------------------------------------------


def test_moved_root_pages_live_only_under_docs() -> None:
    for old, new in MOVED_PAGES.items():
        assert not (REPO / old).exists(), f"{old} still sits at the repository root"
        assert (REPO / new).exists(), f"{new} is missing"

    stale = []
    for page in _tracked_markdown():
        for number, target in _links(page.read_text("utf-8")):
            if Path(target.partition("#")[0]).name in MOVED_PAGES:
                stale.append(f"{_rel(page)}:{number}: {target}")

    assert stale == [], "these tracked pages still link a moved root page:\n" + "\n".join(
        stale
    )
