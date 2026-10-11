"""Jev stays removed: no source, test or doc brings the TypeSafe judge back.

The one place `src/` may still name it is the legacy-config shim in
`ortus.core.config`, which drops a pinned `[judge]` table and `jev_*` keys from
other repositories' `.ortusrc` files. Its test and this guard are the only
other files allowed to match.
"""

from __future__ import annotations

import ast
import hashlib
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FORBIDDEN = re.compile(r"jev|typesafe", re.IGNORECASE)
CONFIG = "src/ortus/core/config.py"
ALLOWED = frozenset({CONFIG, "tests/test_legacy_judge_config.py", "tests/test_no_jev.py"})
#: The shim's names in `config.py`. Every match there must fall inside the span
#: they cover, from the comment above the first constant to the warning helper.
SHIM_START = "LEGACY_JUDGE_TABLE"
SHIM_END = "_warn_legacy_judge"
REMOVED_DOCS = ("docs/judge.md", "docs/jev-prereg.md")
#: sha256 of the released 0.4.0 section, so the removal entry cannot be written
#: by rewriting history.
CHANGELOG_040_SHA256 = "7129f0264a07872aa4884dd5b60e5a84633876b2e71d0d7fdfa3a7c0efc6603f"


def _text_files(root: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        try:
            yield path, path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue


def _matching_lines(text: str) -> list[int]:
    return [n for n, line in enumerate(text.splitlines(), 1) if FORBIDDEN.search(line)]


def _shim_span(text: str) -> range:
    """Lines the legacy shim owns: its leading comment through the warning helper."""

    tree = ast.parse(text)
    start = end = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == SHIM_START for t in node.targets
        ):
            start = node.lineno
        if isinstance(node, ast.FunctionDef) and node.name == SHIM_END:
            end = node.end_lineno
    assert start is not None and end is not None, "legacy shim not found in config.py"
    lines = text.splitlines()
    while start > 1 and lines[start - 2].lstrip().startswith("#"):
        start -= 1
    return range(start, end + 1)


def test_no_jev_outside_the_legacy_shim():
    offenders = []
    for root in ("src", "tests"):
        for path, text in _text_files(REPO / root):
            rel = path.relative_to(REPO).as_posix()
            if rel not in ALLOWED and _matching_lines(text):
                offenders.append(rel)
    assert offenders == [], f"Jev or TypeSafe named outside the shim: {offenders}"


def test_config_names_jev_only_inside_the_legacy_shim():
    text = (REPO / CONFIG).read_text(encoding="utf-8")
    span = _shim_span(text)
    stray = [n for n in _matching_lines(text) if n not in span]
    assert stray == [], f"{CONFIG} names Jev outside the shim at lines {stray}"


def test_docs_drop_the_jev_pages_and_keep_one_removal_note():
    for page in REMOVED_DOCS:
        assert not (REPO / page).exists(), f"{page} should be deleted"
    pages = [REPO / "README.md", *sorted((REPO / "docs").glob("*.md"))]
    for page in pages:
        text = page.read_text(encoding="utf-8")
        for removed in REMOVED_DOCS:
            assert Path(removed).name not in text, f"{page.name} links {removed}"
    configuration = (REPO / "docs/configuration.md").read_text(encoding="utf-8")
    assert "### Removed: Jev" in configuration
    for page in pages:
        if page.name == "configuration.md":
            continue
        assert not _matching_lines(page.read_text(encoding="utf-8")), (
            f"{page.name} still describes Jev"
        )


def test_changelog_records_the_removal_without_rewriting_040():
    text = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    unreleased = text.index("## [Unreleased]")
    released = text.index("## [0.4.0]")
    assert unreleased < released
    entry = text[unreleased:released]
    assert "### Removed" in entry
    assert "Jev" in entry and "TypeSafe" in entry
    section = text[released : text.index("## [0.3.0]")]
    assert hashlib.sha256(section.encode()).hexdigest() == CHANGELOG_040_SHA256
