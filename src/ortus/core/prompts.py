"""Three-layer prompt resolution (FR-025).

Precedence (first existing file wins):
  1. <repo>/.ortus/prompts/<name>.md   per-repo override
  2. ~/.ortus/prompts/<name>.md        user-wide override
  3. bundled src/ortus/prompts/<name>.md  installed default (package data)

Bundled prompts are loaded via importlib.resources so they survive
wheel/sdist installs without filesystem assumptions.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

PROMPT_PACKAGE = "ortus.prompts"


@dataclass(frozen=True)
class ResolvedPrompt:
    """A resolved prompt with its on-disk source and content."""

    name: str
    source: str  # "repo", "user", or "bundled"
    path: Path | None  # None for bundled (lives inside the package, may be a zip)
    text: str


class PromptNotFound(LookupError):
    """Raised when no prompt by that name exists in any layer."""


def _repo_layer_path(repo: Path, name: str) -> Path:
    return repo / ".ortus" / "prompts" / f"{name}.md"


def _user_layer_path(home: Path, name: str) -> Path:
    return home / ".ortus" / "prompts" / f"{name}.md"


def resolve_prompt(
    name: str,
    *,
    repo: Path | None = None,
    home: Path | None = None,
) -> ResolvedPrompt:
    """Resolve <name>-prompt.md across the three layers.

    Args:
        name: prompt basename without the .md extension (e.g., "grind-prompt").
        repo: per-repo override root. When None, the repo layer is skipped.
        home: user-wide override root (defaults to Path.home()).

    Returns:
        ResolvedPrompt naming the layer that won and its content.

    Raises:
        PromptNotFound: if the bundled layer is missing too — indicates a
            broken install. Repo and user layers are optional by design.
    """
    if home is None:
        home = Path.home()

    if repo is not None:
        candidate = _repo_layer_path(repo, name)
        if candidate.is_file():
            return ResolvedPrompt(
                name=name, source="repo", path=candidate, text=candidate.read_text()
            )

    candidate = _user_layer_path(home, name)
    if candidate.is_file():
        return ResolvedPrompt(
            name=name, source="user", path=candidate, text=candidate.read_text()
        )

    bundled = files(PROMPT_PACKAGE).joinpath(f"{name}.md")
    if not bundled.is_file():
        raise PromptNotFound(
            f"{name}.md is not in any of: "
            f"{_repo_layer_path(repo, name) if repo else '(no repo)'}, "
            f"{_user_layer_path(home, name)}, "
            f"bundled {PROMPT_PACKAGE}"
        )
    # importlib.resources.Traversable: read_text() works on both
    # filesystem and zip-backed packages.
    bundled_text = bundled.read_text()
    bundled_path: Path | None
    try:
        # When the package is unpacked on disk, Traversable resolves to a Path.
        bundled_path = Path(str(bundled))
        if not bundled_path.is_file():
            bundled_path = None
    except (TypeError, ValueError):
        bundled_path = None
    return ResolvedPrompt(
        name=name, source="bundled", path=bundled_path, text=bundled_text
    )
