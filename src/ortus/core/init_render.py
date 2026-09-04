"""Render the bundled init templates into a target repo.

Used by `ortus init`. The templates ship as package data under
src/ortus/templates/ and are loaded via importlib.resources so they
survive both editable and wheel installs.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, Iterable

from jinja2 import Environment, StrictUndefined

from ortus import __version__ as ORTUS_VERSION
from ortus.core.agent_files import BlockOutcome, apply_hash_block
from ortus.core.config import DEFAULT_CODEGRAPH_MODE
from ortus.core.local_backend import (
    DEFAULT_LOCAL_BASE_URL,
    OPENCODE_CONFIG_FILE,
    OPENCODE_MCP_SERVER,
    OPENCODE_PROVIDER_ID,
    OPENCODE_SCHEMA_URL,
    LocalConfig,
    opencode_mcp_entry,
    opencode_provider_block,
)


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
    # `opencode` reads the `[local]` table `.ortusrc` renders and registers
    # the served model in its own project file. That file is host-owned JSON
    # with no room for comment markers, so it is a keyed merge
    # (`merge_opencode_config`) that `render_all` skips, never a whole-file
    # render. `local` is opencode under its older name and shares the file.
    "local": OPENCODE_CONFIG_FILE,
    "opencode": OPENCODE_CONFIG_FILE,
}

#: Backend config files init merges by key instead of rendering whole.
MERGED_CONFIGS: frozenset[str] = frozenset({OPENCODE_CONFIG_FILE})


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
    # The `[local]` table `.ortusrc` renders active under `backend == "local"`
    # and as a commented reference block otherwise. `local_model` is None until
    # an init pins one; the template shows a placeholder in its place.
    local_base_url: str = DEFAULT_LOCAL_BASE_URL
    local_model: str | None = None
    local_api_key_env: str | None = None

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
            "local_base_url": self.local_base_url,
            "local_model": self.local_model,
            "local_api_key_env": self.local_api_key_env,
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
    # `local` and `opencode` share a config file, so widening to every backend
    # would name it twice; the ordered de-duplication renders each file once
    # while keeping the slot order the backends were given in. A merged config
    # has no template and is left to its merge.
    names: tuple[str, ...] = tuple(
        dict.fromkeys(
            rendered
            for name in BUNDLED_TEMPLATES
            for rendered in (
                tuple(BACKEND_TEMPLATES[b] for b in selected)
                if name == ".claude/settings.json"
                else (name,)
            )
            if rendered not in MERGED_CONFIGS
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
    """Used by tests + ortus check to enumerate what ships in the package.

    A backend whose config is merged rather than rendered ships no template
    for it, so only the shared files are listed.
    """
    names = [
        BACKEND_TEMPLATES[backend] if name == ".claude/settings.json" else name
        for name in BUNDLED_TEMPLATES
    ]
    return [name for name in names if name not in MERGED_CONFIGS]


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


# `opencode.json` is the fourth host-owned file. JSON carries no comments, so
# the marker fence the other three use cannot fence a region of it; Ortus owns
# exactly two keys instead — `provider.<OPENCODE_PROVIDER_ID>` and
# `mcp.<OPENCODE_MCP_SERVER>` — and a merge rewrites those keys only. Every
# other key the operator wrote (their own providers and `mcp` servers, a
# theme) survives in its original order.


def read_opencode_config(target: Path) -> dict[str, Any] | None:
    """The parsed `target/opencode.json`, or None when absent or empty.

    Raises `ValueError` naming the file when it is not a JSON object or its
    `provider` or `mcp` key is not one: init refuses such a file rather than
    guessing at what the operator meant, and it does so before writing
    anything.
    """
    path = target / OPENCODE_CONFIG_FILE
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    for table in ("provider", "mcp"):
        section = data.get(table)
        if section is not None and not isinstance(section, dict):
            raise ValueError(f'{path}: "{table}" is not a JSON object')
    return data


@dataclass(frozen=True)
class OpenCodeMerge:
    """What one `merge_opencode_config` call did to each key Ortus owns.

    Reported per key rather than per file so init can say which entry it
    created, rewrote, or left alone: an operator whose own `mcp.codegraph`
    was just replaced should hear that, not that the file was "updated".
    `mcp` is None when CodeGraph is off for the repo and the entry was not
    considered at all.
    """

    provider: BlockOutcome
    mcp: BlockOutcome | None

    @property
    def changed(self) -> bool:
        """True when the merge wrote the file."""
        return any(
            outcome is not BlockOutcome.UNCHANGED
            for outcome in (self.provider, self.mcp)
            if outcome is not None
        )


def merge_opencode_config(
    target: Path, local: LocalConfig, *, register_codegraph: bool = True
) -> OpenCodeMerge:
    """Register `local` and the CodeGraph server in `target/opencode.json`.

    Absent (or empty) file: created with the schema reference and the Ortus
    entries. Existing file: each Ortus entry replaced when it differs and
    nothing else touched, and nothing written at all when both are already
    current, so a hand-formatted file is not re-indented for no change.
    `register_codegraph` false (the repo's CodeGraph policy is `off`) leaves
    the `mcp` table exactly as it was, an operator's own `codegraph` entry
    included: init writes no registration that check would not then verify.
    """
    existing = read_opencode_config(target)
    written = BlockOutcome.CREATED if existing is None else BlockOutcome.UPDATED
    data: dict[str, Any] = (
        {"$schema": OPENCODE_SCHEMA_URL} if existing is None else existing
    )

    def own(table: str, key: str, entry: dict[str, Any]) -> BlockOutcome:
        section = data.setdefault(table, {})
        if section.get(key) == entry:
            return BlockOutcome.UNCHANGED
        section[key] = entry
        return written

    merge = OpenCodeMerge(
        provider=own("provider", OPENCODE_PROVIDER_ID, opencode_provider_block(local)),
        mcp=(
            own("mcp", OPENCODE_MCP_SERVER, opencode_mcp_entry())
            if register_codegraph
            else None
        ),
    )
    if merge.changed:
        (target / OPENCODE_CONFIG_FILE).write_text(
            json.dumps(data, indent=2) + "\n", encoding="utf-8"
        )
    return merge
