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
from string import Template

PROMPT_PACKAGE = "ortus.prompts"

# The token the bundled plan prompt carries in place of the generated readiness
# contract. `ortus check` reads it to spot an override copied before it existed.
READINESS_SPEC_PLACEHOLDER = "$readiness_spec"


@dataclass(frozen=True)
class PromptInfo:
    """One bundled runtime prompt as the registry names it."""

    name: str  # short user-facing name, e.g. "goal"
    filename: str  # bundled stem without .md, e.g. "goal-prompt"
    phase: str  # workflow phase the prompt drives
    description: str  # one line for `ortus prompt list`


# The only enumerator of the bundled runtime prompts. Every `*.md` directly
# under src/ortus/prompts/ (not conditions/) has exactly one entry here;
# tests reconcile the two so a new file cannot ship unnamed.
PROMPT_REGISTRY: tuple[PromptInfo, ...] = (
    PromptInfo(
        name="goal",
        filename="goal-prompt",
        phase="implementation",
        description="One-issue worker loop grind points /goal workers at.",
    ),
    PromptInfo(
        name="interview",
        filename="interview-prompt",
        phase="interview",
        description="Interactive PRD-building interview script.",
    ),
    PromptInfo(
        name="plan",
        filename="plan-prompt",
        phase="planning",
        description="PRD decomposition into readiness-v1 bd issues.",
    ),
)


@dataclass(frozen=True)
class ResolvedPrompt:
    """A resolved prompt with its on-disk source and content."""

    name: str
    source: str  # "repo", "user", or "bundled"
    path: Path | None  # None for bundled (lives inside the package, may be a zip)
    text: str


class PromptNotFound(LookupError):
    """Raised when no prompt by that name exists in any layer."""


def registry_entry(name: str) -> PromptInfo:
    """The registry entry for a short prompt name, or PromptNotFound.

    The error names the valid choices so a CLI caller can print it verbatim.
    """
    for entry in PROMPT_REGISTRY:
        if entry.name == name:
            return entry
    valid = ", ".join(entry.name for entry in PROMPT_REGISTRY)
    raise PromptNotFound(f"unknown prompt {name!r}; valid names: {valid}")


def prompts_in_package() -> tuple[str, ...]:
    """Stems of every bundled `*.md` directly under the prompt package.

    Excludes conditions/ by construction (subdirectories are not files).
    Works on both unpacked and zip-backed installs via Traversable.
    """
    root = files(PROMPT_PACKAGE)
    return tuple(
        sorted(
            entry.name[: -len(".md")]
            for entry in root.iterdir()
            if entry.is_file() and entry.name.endswith(".md")
        )
    )


def resolve_named_prompt(
    name: str,
    *,
    repo: Path | None = None,
    home: Path | None = None,
) -> ResolvedPrompt:
    """Thin registry-name alias for resolve_prompt (`goal` -> `goal-prompt`)."""
    return resolve_prompt(registry_entry(name).filename, repo=repo, home=home)


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
        name: prompt basename without the .md extension (e.g., "plan-prompt").
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
                name=name, source="repo", path=candidate, text=candidate.read_text(encoding="utf-8")
            )

    candidate = _user_layer_path(home, name)
    if candidate.is_file():
        return ResolvedPrompt(
            name=name, source="user", path=candidate, text=candidate.read_text(encoding="utf-8")
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
    bundled_text = bundled.read_text(encoding="utf-8")
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


def substitute(text: str, /, **values: str) -> str:
    """Fill `$name` placeholders in a resolved prompt, tolerantly.

    Safe-substitution semantics on purpose. Prompts are shell-heavy — `$(...)`,
    `$ID_FEATURE_A`, `--arg t "$title"` — and a user's private override may
    predate any placeholder we add later. Silently degrading to an older
    contract is bad, but crashing every run that loads such an override is
    worse, so unknown and malformed tokens pass through untouched and
    `ortus check` reports the staleness instead.
    """

    return Template(text).safe_substitute(**values)
