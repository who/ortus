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


@dataclass(frozen=True)
class RenderContext:
    prefix: str
    project_type: str = "polyglot"
    ortus_version: str = ORTUS_VERSION
    today: str = ""  # filled in if blank

    def as_dict(self) -> dict[str, Any]:
        return {
            "prefix": self.prefix,
            "project_type": self.project_type,
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
    return resource.read_text()


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
    for name in BUNDLED_TEMPLATES:
        rendered = render_template(name, ctx)
        dest = target / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(rendered)
        written.append(dest)
    return written


def list_bundled() -> list[str]:
    """Used by tests + ortus check to enumerate what ships in the package."""
    return list(BUNDLED_TEMPLATES)
