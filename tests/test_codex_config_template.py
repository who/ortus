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
import os
import shutil
import subprocess
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
# Both config dirs are conditionally named (M4) — `{% if agent_cli == ... %}`
# renders to `.codex` / `.claude` or to nothing at all. Glob for them so a
# reworded condition surfaces as a missing-file error, not a stale path.
CODEX_TEMPLATE = next((REPO_ROOT / "template").glob("*.codex*/config.toml.jinja"))
CLAUDE_TEMPLATE = next((REPO_ROOT / "template").glob("*.claude*/settings.json.jinja"))

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


# ---------------------------------------------------------------------------
# M4 — conditional emission. Each backend's config dir sits behind a
# conditionally-rendered directory name; copier drops a path whose name renders
# empty, so a claude project never materialises .codex/ and vice versa.
# ---------------------------------------------------------------------------

TEMPLATE_ROOT = REPO_ROOT / "template"

copier_cli = pytest.mark.skipif(
    shutil.which("copier") is None or shutil.which("git") is None,
    reason="needs the copier CLI and git",
)


def _generate(tmp_path: Path, agent_cli: str) -> Path:
    """Generate a project with `copier copy`, from a throwaway clone of template/.

    copier renders a local template from git HEAD, so it must be given a repo
    whose HEAD holds the working tree under test — otherwise this asserts
    against the last commit. `--trust` is required (the template's jinja
    extensions are an unsafe feature copier refuses to load without it), which
    also enables _tasks — so the copy handed to copier has _tasks stripped:
    the real list ends in ./ortus/idea.sh, an interactive agent session.
    """
    src = tmp_path / "template-src"
    src.mkdir()
    spec = yaml.safe_load(COPIER_YAML.read_text())
    spec.pop("_tasks", None)
    (src / "copier.yaml").write_text(yaml.safe_dump(spec))
    for tree in ("template", "extensions"):
        shutil.copytree(REPO_ROOT / tree, src / tree)
    for cmd in (
        ["git", "init", "-q"],
        ["git", "add", "-A"],
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "t"],
    ):
        subprocess.run(cmd, cwd=src, check=True)

    dest = tmp_path / f"proj-{agent_cli}"
    subprocess.run(
        [
            "copier", "copy", "--defaults", "--quiet", "--trust",
            "--data", "project_name=demo",
            "--data", "github_username=demo",
            "--data", f"agent_cli={agent_cli}",
            str(src), str(dest),
        ],
        check=True,
    )
    return dest


@copier_cli
def test_claude_project_ships_only_the_claude_config(tmp_path: Path) -> None:
    dest = _generate(tmp_path, "claude")
    assert (dest / ".claude" / "settings.json").is_file()
    assert not (dest / ".codex").exists()


@copier_cli
def test_codex_project_ships_only_the_codex_config(tmp_path: Path) -> None:
    dest = _generate(tmp_path, "codex")
    assert (dest / ".codex" / "config.toml").is_file()
    assert not (dest / ".claude").exists()


@copier_cli
@pytest.mark.parametrize(
    "agent_cli,path,check",
    [
        # Acceptance #3 — the bd sandbox exemption, in whichever dialect the
        # chosen backend speaks. Claude Code exempts bd by command name; Codex
        # has no per-command escape, so bd's reachability rests entirely on the
        # workspace (where .beads/ lives) staying writable.
        ("claude", ".claude/settings.json",
         lambda t: json.loads(t)["sandbox"]["excludedCommands"] == ["bd", "bd *"]),
        ("codex", ".codex/config.toml",
         lambda t: tomllib.loads(t)["sandbox_mode"] == "workspace-write"),
    ],
)
def test_bd_exemption_survives_generation(tmp_path: Path, agent_cli, path, check) -> None:
    dest = _generate(tmp_path, agent_cli)
    assert check((dest / path).read_text())


@copier_cli
@pytest.mark.parametrize("agent_cli", ["claude", "codex"])
def test_bd_preflight_passes_in_a_generated_project(tmp_path: Path, agent_cli: str) -> None:
    """The generated posture must actually let bd reach its Dolt store."""
    if shutil.which("bd") is None:
        pytest.skip("needs bd on PATH")
    dest = _generate(tmp_path, agent_cli)
    # Mirror copier.yaml's _tasks, which the generator above skips. bd warns on
    # stderr about a 0755 .beads/ or an unset beads.role, and preflight folds
    # stderr into the output it parses — so a project missing these looks like
    # a bd that answered with something other than a JSON array.
    for cmd in (
        ["git", "init", "-q"],
        ["git", "config", "beads.role", "maintainer"],
        ["bd", "init", "--server"],
        ["chmod", "700", ".beads"],
    ):
        subprocess.run(cmd, cwd=dest, check=True, capture_output=True)
    result = subprocess.run(
        ["bash", "-c", 'log() { echo "$*" >&2; }; . ortus/lib/sandbox.sh; bd_preflight'],
        cwd=dest,
        env={**os.environ, "ORTUS_BACKEND": agent_cli},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
