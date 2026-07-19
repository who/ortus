"""Tests for core/init_render.py — bundled-template rendering (q075.4)."""

from __future__ import annotations

import json
import sys
from importlib.resources import files
from pathlib import Path

import pytest

from ortus.core.init_render import (
    BUNDLED_TEMPLATES,
    RenderContext,
    list_bundled,
    render_all,
    render_template,
)

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


# Acceptance #1 — all 4 templates accessible via importlib.resources.
def test_all_four_templates_ship_in_package() -> None:
    pkg = files("ortus.templates")
    available = {p.name for p in pkg.iterdir() if p.is_file()}
    available |= {f"{p.name}/{c.name}" for p in pkg.iterdir() if p.is_dir() for c in p.iterdir()}
    # Every template name should map to a .jinja file in package data.
    for name in BUNDLED_TEMPLATES:
        jinja_name = f"{name}.jinja"
        assert (
            jinja_name in available or jinja_name.replace("/", "/") in available
        ), f"{jinja_name} not in package data: {available}"


def test_list_bundled_matches_constant() -> None:
    assert list_bundled() == list(BUNDLED_TEMPLATES)


# Acceptance #2 — rendered settings.json is valid JSON + has excludedCommands.
def test_rendered_settings_json_validates_and_has_excluded_commands() -> None:
    ctx = RenderContext(prefix="myproj", project_type="python")
    text = render_template(".claude/settings.json", ctx)
    data = json.loads(text)
    assert data["sandbox"]["excludedCommands"] == ["bd", "bd *"]


# Regression (ortus-5gja) — allowedDomains must include the package registries
# for the selected project_type. The bundled template originally shipped only the
# 6 base domains, so init'd projects couldn't install packages in the sandbox.
BASE_DOMAINS = {
    "api.anthropic.com",
    "github.com",
    "api.github.com",
    "codeload.github.com",
    "raw.githubusercontent.com",
    "objects.githubusercontent.com",
}
REGISTRY_DOMAINS = {
    "registry.npmjs.org",
    "pypi.org",
    "files.pythonhosted.org",
    "crates.io",
    "static.crates.io",
    "proxy.golang.org",
    "sum.golang.org",
}


@pytest.mark.parametrize(
    ("project_type", "expected_registries"),
    [
        ("typescript", {"registry.npmjs.org"}),
        ("python", {"pypi.org", "files.pythonhosted.org"}),
        ("rust", {"crates.io", "static.crates.io"}),
        ("go", {"proxy.golang.org", "sum.golang.org"}),
        ("polyglot", REGISTRY_DOMAINS),
    ],
)
def test_allowed_domains_includes_registries_for_project_type(
    project_type: str, expected_registries: set[str]
) -> None:
    ctx = RenderContext(prefix="p", project_type=project_type)
    data = json.loads(render_template(".claude/settings.json", ctx))
    domains = set(data["sandbox"]["network"]["allowedDomains"])
    # Base domains are always present.
    assert BASE_DOMAINS <= domains
    # Exactly the registries for this ecosystem appear (no extras, none missing).
    assert domains - BASE_DOMAINS == expected_registries


# ortus-oxp9 — allowedDomains also reflects the selected --package-manager's
# registry, on top of the project_type defaults. yarn pulls from
# registry.yarnpkg.com (classic mirror) plus the npm registry; bun/npm/pnpm
# use registry.npmjs.org. The npm registry is contributed by both the
# typescript project_type and these managers, so the rendered list must dedupe.
@pytest.mark.parametrize(
    ("package_manager", "expected_registries"),
    [
        ("npm", {"registry.npmjs.org"}),
        ("pnpm", {"registry.npmjs.org"}),
        ("yarn", {"registry.yarnpkg.com", "registry.npmjs.org"}),
        ("bun", {"registry.npmjs.org"}),
    ],
)
def test_allowed_domains_reflects_typescript_package_manager(
    package_manager: str, expected_registries: set[str]
) -> None:
    ctx = RenderContext(
        prefix="p", project_type="typescript", package_manager=package_manager
    )
    listed = json.loads(render_template(".claude/settings.json", ctx))[
        "sandbox"
    ]["network"]["allowedDomains"]
    domains = set(listed)
    assert BASE_DOMAINS <= domains
    # Exactly the registries for this manager appear beyond the base set.
    assert domains - BASE_DOMAINS == expected_registries
    # No duplicate entries even though typescript + the manager both
    # contribute registry.npmjs.org.
    assert len(listed) == len(domains)


# Acceptance #3 — rendered .ortusrc validates as TOML.
def test_rendered_ortusrc_validates_as_toml() -> None:
    ctx = RenderContext(prefix="acme", project_type="go", today="2026-05-16")
    text = render_template(".ortusrc", ctx)
    parsed = tomllib.loads(text)
    assert parsed["prefix"] == "acme"
    assert parsed["project_type"] == "go"
    assert parsed["backend"] == "claude"


def test_codex_render_uses_codex_config_and_no_claude_dir(tmp_path: Path) -> None:
    ctx = RenderContext(prefix="acme", project_type="python", backend="codex")
    written = render_all(tmp_path, ctx)
    assert tmp_path / ".codex" / "config.toml" in written
    assert (tmp_path / ".codex" / "config.toml").is_file()
    assert not (tmp_path / ".claude").exists()
    assert 'backend = "codex"' in (tmp_path / ".ortusrc").read_text()
    assert "plain" in (tmp_path / "AGENTS.md").read_text()


# Acceptance #4 — {% raw %} round-trips bash snippets.
def test_raw_blocks_in_agents_md_preserve_bash_braces() -> None:
    ctx = RenderContext(prefix="x", project_type="polyglot")
    text = render_template("AGENTS.md", ctx)
    # Inside {% raw %} blocks we keep {%-/-%}-style braces and `bd update <id>`
    # placeholders unevaluated. If Jinja had eaten them, "<id>" would vanish.
    assert "bd update <id>" in text
    assert "{% " not in text  # the raw markers themselves are stripped
    assert "{{ " not in text


# Acceptance #1 (broader) — render_all produces every file on disk.
def test_render_all_writes_every_template(tmp_path: Path) -> None:
    ctx = RenderContext(prefix="full", project_type="polyglot")
    written = render_all(tmp_path, ctx)
    assert len(written) == len(BUNDLED_TEMPLATES)
    for p in written:
        assert p.is_file()
        assert p.read_text(encoding="utf-8").strip(), f"{p} rendered empty"


def test_render_substitutes_today_when_blank(tmp_path: Path) -> None:
    """today defaults to today's ISO date when not provided."""
    ctx = RenderContext(prefix="d", project_type="polyglot")
    text = render_template(".ortusrc", ctx)
    import datetime as _dt

    assert _dt.date.today().isoformat() in text


def test_render_uses_supplied_version() -> None:
    ctx = RenderContext(prefix="v", project_type="polyglot", ortus_version="9.9.9")
    text = render_template(".ortusrc", ctx)
    assert "9.9.9" in text


def test_render_missing_variable_raises() -> None:
    """StrictUndefined means a typo in the template surfaces immediately."""
    from jinja2 import Environment, StrictUndefined
    from jinja2.exceptions import UndefinedError

    env = Environment(undefined=StrictUndefined)
    with pytest.raises(UndefinedError):
        env.from_string("{{ nope }}").render()
