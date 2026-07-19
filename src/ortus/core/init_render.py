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


TEMPLATE_PACKAGE = "ortus.templates"

BUNDLED_TEMPLATES: tuple[str, ...] = (
    ".claude/settings.json",
    ".ortusrc",
    "AGENTS.md",
    ".gitignore",
)

BACKEND_TEMPLATES: dict[str, str] = {
    "claude": ".claude/settings.json",
    "codex": ".codex/config.toml",
}


# Per-project-type choices for the three stack flags. Mirrors the historical
# copier.yaml blocks for package_manager / framework / linter.
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


def render_all(target: Path, ctx: RenderContext) -> list[Path]:
    """Render every bundled template into `target`. Returns list of written paths."""
    written: list[Path] = []
    names = tuple(
        BACKEND_TEMPLATES[ctx.backend] if name == ".claude/settings.json" else name
        for name in BUNDLED_TEMPLATES
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
