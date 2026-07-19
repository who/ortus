"""Per-backend instruction files: AGENTS.md / CLAUDE.md (FR-009, ortus-azb6).

Codex reads `AGENTS.md` and never reads `CLAUDE.md`, so anything load-bearing that
lives only in `CLAUDE.md` is invisible to half the supported backends. The fix is
one templated `AGENTS.md` carrying all the shared operational prose, with only the
backend-variant lines conditional, and a `CLAUDE.md` that points at it.

These tests pin the three things that can silently regress:

  * the shared prose is not duplicated into `CLAUDE.md` (it would drift, and only
    the Claude backend would ever see the stale copy);
  * rendering `AGENTS.md` for the two backends differs only on lines that actually
    name a backend — the diff-confinement check from the issue's TESTING clause;
  * the Orchestrator section names the invocation for the backend it was rendered
    for.

Rendering goes through Jinja directly rather than `copier copy`, for the same
reason as test_agent_cli_question.py: copier renders a local template from git, so
a copier-driven test would assert against the last commit, not the working tree.
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path

import jinja2
import pytest

REPO_ROOT = Path(__file__).parent.parent
TEMPLATE_DIR = REPO_ROOT / "template"
AGENTS_TEMPLATE = TEMPLATE_DIR / "AGENTS.md.jinja"
CLAUDE_TEMPLATE = TEMPLATE_DIR / "CLAUDE.md.jinja"

BASE_CONTEXT = {
    "project_name": "acme",
    "project_description": "A test project.",
    "language": "python",
    "package_manager": "uv",
    "framework": "fastapi",
    "linter": "ruff",
}

# Section headings that must be reachable from AGENTS.md, because a Codex session
# has no other way to find them.
LOAD_BEARING_SECTIONS = [
    "## Session Start",
    "## AI Guidance",
    "## Work Execution Policy",
    "## Orchestrator",
    "## Sandbox Model",
    "## Technology Stack",
    "## Command Reference",
    "### Banned Commands",
    "## Session Completion",
]


def render(template: Path, **overrides) -> str:
    env = jinja2.Environment(
        undefined=jinja2.StrictUndefined, keep_trailing_newline=True
    )
    ctx = {**BASE_CONTEXT, **overrides}
    return env.from_string(template.read_text(encoding="utf-8")).render(**ctx)


@pytest.fixture(scope="module")
def rendered() -> dict[str, str]:
    return {
        backend: render(AGENTS_TEMPLATE, agent_cli=backend)
        for backend in ("claude", "codex")
    }


@pytest.mark.parametrize("backend", ["claude", "codex"])
@pytest.mark.parametrize("section", LOAD_BEARING_SECTIONS)
def test_agents_md_carries_every_load_bearing_section(rendered, backend, section):
    """Condition 1: nothing load-bearing lives only in CLAUDE.md."""
    assert section in rendered[backend], (
        f"{section!r} missing from AGENTS.md rendered for {backend}"
    )


def test_claude_md_is_a_pointer_not_a_copy():
    """Condition 3: shared prose is not duplicated across the instruction files."""
    text = render(CLAUDE_TEMPLATE, agent_cli="claude")
    assert "AGENTS.md" in text, "CLAUDE.md must point at AGENTS.md"
    # The Goal-loop policy and the session-close protocol are the two blocks most
    # likely to get copied back in. If they reappear here they will drift.
    for duplicated in ("## Work Execution Policy", "## Session Completion", "## Sandbox Model"):
        assert duplicated not in text, (
            f"{duplicated!r} is duplicated in CLAUDE.md; it belongs in AGENTS.md only"
        )


@pytest.mark.parametrize(
    "backend,expected,forbidden",
    [
        ("claude", 'claude -p "/goal CONDITION"', 'codex exec "/goal CONDITION"'),
        ("codex", 'codex exec "/goal CONDITION"', 'claude -p "/goal CONDITION"'),
    ],
)
def test_orchestrator_names_the_active_backend(rendered, backend, expected, forbidden):
    """Condition 2: the Orchestrator section describes the ACTIVE backend."""
    text = rendered[backend]
    assert expected in text
    assert forbidden not in text


def test_backend_diff_is_confined_to_backend_variant_lines(rendered):
    """TESTING clause: diff the two AGENTS.md renders; only backend lines may differ.

    Every line that appears in one render and not the other has to name a backend
    (or a backend-owned path like `.claude/` / `.codex/`). A shared paragraph that
    got accidentally forked would show up here as a diff line with no such marker.
    """
    changed = [
        line[1:]
        for line in difflib.unified_diff(
            rendered["claude"].splitlines(),
            rendered["codex"].splitlines(),
            lineterm="",
            n=0,
        )
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ]
    assert changed, "the two backends render identically — the variant lines are gone"

    marker = re.compile(r"claude|codex|CODEX_HOME|DSP", re.IGNORECASE)
    unmarked = [line for line in changed if not marker.search(line)]
    assert not unmarked, (
        "AGENTS.md diverges between backends on lines that name no backend:\n"
        + "\n".join(unmarked)
    )


def test_agents_md_renders_under_strict_undefined(rendered):
    """A missing copier answer must fail the test suite, not the user's generation."""
    for backend, text in rendered.items():
        assert "{{" not in text and "{%" not in text, f"unrendered Jinja in {backend}"
