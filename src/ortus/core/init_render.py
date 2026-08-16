"""Render the bundled init templates into a target repo.

Used by `ortus init`. The templates ship as package data under
src/ortus/templates/ and are loaded via importlib.resources so they
survive both editable and wheel installs.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, Iterable

from jinja2 import Environment, StrictUndefined

from ortus import __version__ as ORTUS_VERSION
from ortus.core.agent_files import BlockOutcome, apply_hash_block
from ortus.core.config import DEFAULT_CODEGRAPH_MODE


TEMPLATE_PACKAGE = "ortus.templates"

# `AGENTS.md`, `CLAUDE.md`, and `.gitignore` are deliberately absent: they
# belong to the host repo and are written as managed blocks — the markdown
# files by ortus.core.agent_files, `.gitignore` by `merge_gitignore` below —
# never as whole-file renders that would overwrite the repo's own content.
BUNDLED_TEMPLATES: tuple[str, ...] = (
    ".claude/settings.json",
    ".ortusrc",
)

BACKEND_TEMPLATES: dict[str, str] = {
    "claude": ".claude/settings.json",
    "codex": ".codex/config.toml",
    "grok": ".grok/config.toml",
}


# Per-project-type choices for the three stack flags.
PACKAGE_MANAGER_CHOICES: dict[str, tuple[str, ...]] = {
    "python": ("uv", "pip", "none"),
    "typescript": ("bun", "npm", "pnpm", "yarn", "none"),
    "go": ("gomod", "none"),
    "rust": ("cargo", "none"),
    "polyglot": ("none",),
}
PACKAGE_MANAGER_DEFAULTS: dict[str, str] = {
    "python": "uv",
    "typescript": "npm",
    "go": "gomod",
    "rust": "cargo",
    "polyglot": "none",
}

FRAMEWORK_CHOICES: dict[str, tuple[str, ...]] = {
    "python": ("fastapi", "flask", "django", "none"),
    "typescript": ("nextjs", "express", "none"),
    "go": ("gin", "none"),
    "rust": ("actix", "axum", "none"),
    "polyglot": ("none",),
}
FRAMEWORK_DEFAULTS: dict[str, str] = {
    "python": "none",
    "typescript": "none",
    "go": "none",
    "rust": "none",
    "polyglot": "none",
}

LINTER_CHOICES: dict[str, tuple[str, ...]] = {
    "python": ("ruff", "none"),
    "typescript": ("eslint", "none"),
    "go": ("golangci", "none"),
    "rust": ("clippy", "none"),
    "polyglot": ("none",),
}
LINTER_DEFAULTS: dict[str, str] = {
    "python": "ruff",
    "typescript": "eslint",
    "go": "golangci",
    "rust": "clippy",
    "polyglot": "none",
}

PROJECT_TYPES: tuple[str, ...] = tuple(PACKAGE_MANAGER_CHOICES.keys())


@dataclass(frozen=True)
class RenderContext:
    prefix: str
    backend: str = "claude"
    project_type: str = "polyglot"
    package_manager: str = "none"
    framework: str = "none"
    linter: str = "none"
    codegraph: str = DEFAULT_CODEGRAPH_MODE
    ortus_version: str = ORTUS_VERSION
    today: str = ""  # filled in if blank

    def as_dict(self) -> dict[str, Any]:
        return {
            "prefix": self.prefix,
            "backend": self.backend,
            "project_type": self.project_type,
            "package_manager": self.package_manager,
            "framework": self.framework,
            "linter": self.linter,
            "codegraph": self.codegraph,
            "ortus_version": self.ortus_version,
            "today": self.today or _dt.date.today().isoformat(),
        }


def _read_template(name: str) -> str:
    """Read a bundled template by relative path (e.g., '.claude/settings.json')."""
    template_path = files(TEMPLATE_PACKAGE)
    parts = (f"{name}.jinja").split("/")
    resource = template_path
    for part in parts:
        resource = resource.joinpath(part)
    return resource.read_text(encoding="utf-8")


def render_template(name: str, ctx: RenderContext) -> str:
    env = Environment(
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
    template = env.from_string(_read_template(name))
    return template.render(**ctx.as_dict())


def render_all(
    target: Path,
    ctx: RenderContext,
    backends: tuple[str, ...] | None = None,
) -> list[Path]:
    """Render every bundled template into `target`. Returns list of written paths.

    `backends` widens the backend-config slot to several backends at once
    (`ortus init --backend all`); the shared files still render from `ctx`,
    whose `backend` is the concrete run backend `.ortusrc` pins.
    """
    written: list[Path] = []
    selected = backends if backends is not None else (ctx.backend,)
    names: tuple[str, ...] = tuple(
        rendered
        for name in BUNDLED_TEMPLATES
        for rendered in (
            tuple(BACKEND_TEMPLATES[b] for b in selected)
            if name == ".claude/settings.json"
            else (name,)
        )
    )
    for name in names:
        rendered = render_template(name, ctx)
        dest = target / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(rendered, encoding="utf-8")
        written.append(dest)
    return written


def list_bundled(backend: str = "claude") -> list[str]:
    """Used by tests + ortus check to enumerate what ships in the package."""
    return [
        BACKEND_TEMPLATES[backend] if name == ".claude/settings.json" else name
        for name in BUNDLED_TEMPLATES
    ]


# `.gitignore` is the third host-owned file. Ortus owns only a section fenced
# by hash-comment markers (gitignore files cannot carry the HTML comments the
# markdown blocks use); every host line outside the fence survives re-init
# byte-for-byte. Bump the schema when the section's meaning changes.
GITIGNORE_BLOCK = "gitignore"
GITIGNORE_SCHEMA = 1


def render_gitignore_section(ctx: RenderContext) -> str:
    """The ortus-owned `.gitignore` section, markers included."""
    begin = (
        f"# BEGIN ortus block={GITIGNORE_BLOCK} schema={GITIGNORE_SCHEMA} "
        f"generated-by=ortus@{ctx.ortus_version}"
    )
    end = f"# END ortus block={GITIGNORE_BLOCK}"
    body = render_template(".gitignore", ctx).strip()
    return f"{begin}\n{body}\n{end}"


def merge_gitignore(target: Path, ctx: RenderContext) -> BlockOutcome:
    """Splice the ortus section into `target/.gitignore`.

    Absent file: created holding just the section. Marked file: only the
    fenced region is rewritten. Pre-marker file: the section is appended and
    no existing line is deleted — init cannot tell a stale ortus render from
    a host's own choice of the same pattern, so it never removes either.
    """
    return apply_hash_block(
        target / ".gitignore",
        GITIGNORE_BLOCK,
        render_gitignore_section(ctx),
        schema=GITIGNORE_SCHEMA,
    )
