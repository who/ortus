"""Tests for template/.codex/config.toml.jinja and the shared network allowlist.

FR-005: a Codex project gets a project-local .codex/config.toml whose network
allowlist comes from the SAME language_profile logic that fills allowedDomains
in .claude/settings.json. These tests render both templates from the one
computed `network_allowlist` copier answer and assert they agree, so a future
edit to the profile logic cannot drift the two backends apart.

Rendering happens through Jinja directly rather than through `copier copy`:
copier renders a local template from git HEAD, so a copier-driven test would
assert against the last commit instead of the working tree.
"""

from __future__ import annotations

import json
import sys

import jinja2
import pytest
import yaml

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
COPIER_YAML = REPO_ROOT / "copier.yaml"
CODEX_TEMPLATE = REPO_ROOT / "template" / ".codex" / "config.toml.jinja"
CLAUDE_TEMPLATE = REPO_ROOT / "template" / ".claude" / "settings.json.jinja"

PROFILES = ("python", "javascript", "go", "rust", "polyglot")

BASE_DOMAINS = [
    "api.anthropic.com",
    "github.com",
    "api.github.com",
    "codeload.github.com",
    "raw.githubusercontent.com",
    "objects.githubusercontent.com",
]
REGISTRIES = {
    "python": ["pypi.org", "files.pythonhosted.org"],
    "javascript": ["registry.npmjs.org"],
    "go": ["proxy.golang.org", "sum.golang.org"],
    "rust": ["crates.io", "static.crates.io"],
}


def _env() -> jinja2.Environment:
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, keep_trailing_newline=True)
    env.filters["tojson"] = json.dumps
    return env


def network_allowlist(profile: str) -> list[str]:
    """Evaluate copier.yaml's computed `network_allowlist` default for a profile.

    Reading the expression out of copier.yaml (rather than restating it) is what
    makes this a test of the real derivation and not of a second copy of it.
    """
    spec = yaml.safe_load(COPIER_YAML.read_text())["network_allowlist"]
    assert spec["when"] is False, "network_allowlist must stay computed, never asked"
    rendered = _env().from_string(spec["default"]).render(language_profile=profile)
    return json.loads(rendered)


def render(template: Path, profile: str) -> str:
    return _env().from_string(template.read_text()).render(
        project_name="demo",
        language_profile=profile,
        network_allowlist=network_allowlist(profile),
    )


@pytest.mark.parametrize("profile", PROFILES)
def test_allowlist_matches_the_profile(profile: str) -> None:
    expected = BASE_DOMAINS + [
        d
        for lang, domains in REGISTRIES.items()
        if profile in (lang, "polyglot")
        for d in domains
    ]
    assert sorted(network_allowlist(profile)) == sorted(expected)


# Acceptance #1 — the generated TOML carries every key FR-005 lists.
@pytest.mark.parametrize("profile", PROFILES)
def test_codex_config_has_required_keys(profile: str) -> None:
    data = tomllib.loads(render(CODEX_TEMPLATE, profile))
    assert data["sandbox_mode"] == "workspace-write"
    assert data["approval_policy"] == "never"
    assert data["sandbox_workspace_write"]["network_access"] is True
    assert data["ortus"]["network_allowlist"]


# Acceptance #2 — one derivation, two backends. If these ever disagree, someone
# has reintroduced a copy of the profile logic.
@pytest.mark.parametrize("profile", PROFILES)
def test_both_backends_get_the_same_allowlist(profile: str) -> None:
    codex = tomllib.loads(render(CODEX_TEMPLATE, profile))["ortus"]["network_allowlist"]
    claude = json.loads(render(CLAUDE_TEMPLATE, profile))["sandbox"]["network"]["allowedDomains"]
    assert codex == claude == network_allowlist(profile)


def test_neither_template_hardcodes_the_registry_logic() -> None:
    """Both templates must consume the computed answer, not restate the profile
    conditionals — the copy-paste this issue exists to prevent."""
    for template in (CODEX_TEMPLATE, CLAUDE_TEMPLATE):
        body = template.read_text()
        assert "network_allowlist" in body, f"{template.name} ignores the shared answer"
        assert "registry.npmjs.org" not in body, f"{template.name} re-hardcodes registries"
