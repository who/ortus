"""Tests for core/init_render.py — bundled-template rendering (q075.4)."""

from __future__ import annotations

import hashlib
import json
import sys
from importlib.resources import files
from pathlib import Path

import pytest

from ortus.core import agent_files
from ortus.core.agent_files import (
    BD_CLAIM_COMMAND,
    BLOCK_SCHEMAS,
    BlockOutcome,
    apply_block,
    begin_marker,
    block_template_source,
    codegraph_section,
    end_marker,
    gitignore_match,
    read_block,
    render_block,
)
from ortus.core.init_render import (
    BACKEND_TEMPLATES,
    BUNDLED_TEMPLATES,
    RenderContext,
    list_bundled,
    merge_gitignore,
    render_all,
    render_template,
)
from ortus.core.prompts import resolve_prompt

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


# Acceptance #1 — every bundled template is accessible via importlib.resources.
def test_every_bundled_template_ships_in_the_package() -> None:
    pkg = files("ortus.templates")
    available = {p.name for p in pkg.iterdir() if p.is_file()}
    available |= {f"{p.name}/{c.name}" for p in pkg.iterdir() if p.is_dir() for c in p.iterdir()}
    # Every template name should map to a .jinja file in package data.
    # `.gitignore` left BUNDLED_TEMPLATES for the marker merge but still ships.
    for name in (*BUNDLED_TEMPLATES, *BACKEND_TEMPLATES.values(), ".gitignore"):
        jinja_name = f"{name}.jinja"
        assert (
            jinja_name in available or jinja_name.replace("/", "/") in available
        ), f"{jinja_name} not in package data: {available}"


def test_list_bundled_matches_constant() -> None:
    assert list_bundled() == list(BUNDLED_TEMPLATES)


def test_list_bundled_grok_swaps_in_project_config() -> None:
    names = list_bundled("grok")
    assert ".grok/config.toml" in names
    assert ".claude/settings.json" not in names


# Acceptance #2 — rendered settings.json is valid JSON + has excludedCommands.
def test_rendered_settings_json_validates_and_has_excluded_commands() -> None:
    ctx = RenderContext(prefix="myproj", project_type="python")
    text = render_template(".claude/settings.json", ctx)
    data = json.loads(text)
    assert data["sandbox"]["excludedCommands"] == ["bd", "bd *", "ortus", "ortus *"]


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
    # The policy is pinned explicitly rather than inherited.
    assert parsed["codegraph"] == "required"


def test_rendered_ortusrc_pins_the_selected_codegraph_mode() -> None:
    ctx = RenderContext(prefix="acme", project_type="go", codegraph="off")
    assert tomllib.loads(render_template(".ortusrc", ctx))["codegraph"] == "off"


def test_rendered_gitignore_excludes_the_codegraph_index() -> None:
    """The index is local, machine-specific, and must never be committed."""
    ctx = RenderContext(prefix="acme", project_type="go")
    assert ".codegraph/" in render_template(".gitignore", ctx)


def test_rendered_gitignore_never_hides_the_managed_agent_files(tmp_path: Path) -> None:
    """Ortus manages AGENTS.md and CLAUDE.md as tracked source, not scratch."""
    (tmp_path / ".gitignore").write_text(
        render_template(".gitignore", RenderContext(prefix="acme")), encoding="utf-8"
    )
    for name in ("AGENTS.md", "CLAUDE.md"):
        assert gitignore_match(tmp_path, name) is None


# --- marker-managed .gitignore ----------------------------------------------
#
# `.gitignore` is host-owned like AGENTS.md: ortus owns only the section
# between the hash-comment markers, and every host line outside them must
# survive a re-init byte-for-byte.


def test_merge_gitignore_creates_the_marked_file(tmp_path: Path) -> None:
    ctx = RenderContext(prefix="acme")
    assert merge_gitignore(tmp_path, ctx) is BlockOutcome.CREATED
    text = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert text.startswith("# BEGIN ortus block=gitignore schema=1 generated-by=ortus@")
    assert "# END ortus block=gitignore" in text
    assert ".codegraph/" in text


def test_merge_gitignore_is_a_no_op_when_current(tmp_path: Path) -> None:
    ctx = RenderContext(prefix="acme")
    merge_gitignore(tmp_path, ctx)
    before = (tmp_path / ".gitignore").read_bytes()
    assert merge_gitignore(tmp_path, ctx) is BlockOutcome.UNCHANGED
    assert (tmp_path / ".gitignore").read_bytes() == before


def test_merge_gitignore_preserves_host_lines_and_refreshes_section(
    tmp_path: Path,
) -> None:
    ctx = RenderContext(prefix="acme")
    merge_gitignore(tmp_path, ctx)
    path = tmp_path / ".gitignore"
    section = path.read_text(encoding="utf-8")
    host_top = "# ML artifacts\nmodels/\n.pnpm-store/\n\n"
    host_bottom = "\ntest-results/\nplaywright-report/\n"
    stale = section.replace(".codegraph/", ".retired-entry/")
    path.write_text(host_top + stale + host_bottom, encoding="utf-8")
    assert merge_gitignore(tmp_path, ctx) is BlockOutcome.UPDATED
    text = path.read_text(encoding="utf-8")
    assert text.startswith(host_top)
    assert text.endswith(host_bottom)
    assert ".retired-entry/" not in text
    assert ".codegraph/" in text


def test_merge_gitignore_appends_to_a_premarker_file(tmp_path: Path) -> None:
    """A pre-marker `.gitignore` keeps every line and gains the section."""
    path = tmp_path / ".gitignore"
    host = "node_modules/\n*.tsbuildinfo\n"
    path.write_text(host, encoding="utf-8")
    assert merge_gitignore(tmp_path, RenderContext(prefix="acme")) is BlockOutcome.APPENDED
    text = path.read_text(encoding="utf-8")
    assert text.startswith(host)
    assert "# BEGIN ortus block=gitignore" in text
    # markers are comments, so the host's own rules still apply
    assert gitignore_match(tmp_path, "node_modules") is not None


def test_merge_gitignore_fills_an_empty_file(tmp_path: Path) -> None:
    path = tmp_path / ".gitignore"
    path.write_text("", encoding="utf-8")
    assert merge_gitignore(tmp_path, RenderContext(prefix="acme")) is BlockOutcome.CREATED
    assert ".codegraph/" in path.read_text(encoding="utf-8")


def test_merge_gitignore_leaves_a_newer_schema_untouched(tmp_path: Path) -> None:
    path = tmp_path / ".gitignore"
    newer = (
        "# BEGIN ortus block=gitignore schema=99 generated-by=ortus@9.9.9\n"
        "future/\n"
        "# END ortus block=gitignore\n"
    )
    path.write_text(newer, encoding="utf-8")
    assert merge_gitignore(tmp_path, RenderContext(prefix="acme")) is BlockOutcome.AHEAD
    assert path.read_text(encoding="utf-8") == newer


def test_merge_gitignore_refuses_a_dangling_begin_marker(tmp_path: Path) -> None:
    path = tmp_path / ".gitignore"
    mangled = "# BEGIN ortus block=gitignore schema=1\nrules/\n"
    path.write_text(mangled, encoding="utf-8")
    with pytest.raises(agent_files.AgentFileError):
        merge_gitignore(tmp_path, RenderContext(prefix="acme"))
    # never rewrite around a broken fence
    assert path.read_text(encoding="utf-8") == mangled


def test_codex_render_uses_codex_config_and_no_claude_dir(tmp_path: Path) -> None:
    ctx = RenderContext(prefix="acme", project_type="python", backend="codex")
    written = render_all(tmp_path, ctx)
    assert tmp_path / ".codex" / "config.toml" in written
    assert (tmp_path / ".codex" / "config.toml").is_file()
    assert not (tmp_path / ".claude").exists()
    assert 'backend = "codex"' in (tmp_path / ".ortusrc").read_text()
    # Instruction files are managed blocks, not whole-file renders, so
    # render_all deliberately leaves them to apply_block.
    assert not (tmp_path / "AGENTS.md").exists()


def test_grok_render_uses_grok_config_and_no_claude_dir(tmp_path: Path) -> None:
    ctx = RenderContext(prefix="acme", project_type="python", backend="grok")
    written = render_all(tmp_path, ctx)
    dest = tmp_path / ".grok" / "config.toml"
    assert dest in written
    assert dest.is_file()
    assert not (tmp_path / ".claude").exists()
    assert 'backend = "grok"' in (tmp_path / ".ortusrc").read_text()
    data = tomllib.loads(dest.read_text())
    assert "codegraph" in data["mcp_servers"]
    assert "sandbox" not in data
    assert "sandbox_mode" not in data


# --- managed AGENTS.md / CLAUDE.md blocks ----------------------------------
#
# The instruction files used to be whole-file Jinja renders. They are now the
# host repo's files with one Ortus-owned block spliced in, so the assertions
# that guarded the template's content moved onto the block it became.

# ortus-xhrj.5 — the always-loaded instructions must state the readiness v1
# authoring contract, or an agent following them faithfully still writes issues
# that `ortus grind` skips as unready. The section is a pointer plus the field
# split, not a copy of `ortus spec` output, so it cannot drift into a stale spec.
AUTHORING_CONTRACT_HEADING = "### Issue authoring contract (readiness v1)"


def test_agents_block_carries_the_authoring_contract() -> None:
    text = render_block("agents")
    # Exactly once: `ortus init --force` refreshes the block, and a duplicated
    # section would double the always-on context cost.
    assert text.count(AUTHORING_CONTRACT_HEADING) == 1
    # The three bd fields that carry the contract, plus the epic exemption.
    for field in ("`description`", "`design`", "`acceptance_criteria`"):
        assert field in text
    assert "Epics" in text
    # The full contract stays generated; the block only names the printer.
    assert "`ortus spec`" in text


def test_agents_block_authoring_contract_sits_with_the_bd_guidance() -> None:
    """Authoring fails when the issue is written, not when grind runs it."""
    text = render_block("agents")
    assert (
        text.index("### Issue tracking with bd")
        < text.index(AUTHORING_CONTRACT_HEADING)
        < text.index("### Orchestrator (ortus grind)")
    )


def test_blocks_substitute_every_placeholder_and_keep_shell_braces() -> None:
    for block in BLOCK_SCHEMAS:
        text = render_block(block)
        assert "{CLI_VERSION}" not in text
        assert "{BD_CLAIM_COMMAND}" not in text
        assert "{CODEGRAPH_SECTION}" not in text
        assert BD_CLAIM_COMMAND in text
        assert text.startswith(begin_marker(block))
        assert text.endswith(end_marker(block))


def test_block_render_rejects_an_unknown_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A body variable nobody substitutes must never reach a consumer repo."""
    monkeypatch.setattr(
        agent_files, "_read_block_template", lambda block: "hello {NOPE}\n"
    )
    with pytest.raises(agent_files.AgentFileError) as excinfo:
        render_block("agents")
    assert "{NOPE}" in str(excinfo.value)


@pytest.mark.parametrize("mode", ["required", "auto", "off"])
def test_blocks_render_the_repo_codegraph_policy(mode: str) -> None:
    for block in BLOCK_SCHEMAS:
        assert codegraph_section(mode) in render_block(block, codegraph=mode)


def test_bd_claim_command_matches_the_bundled_goal_prompt(tmp_path: Path) -> None:
    """One claim command, whether the agent reads AGENTS.md or runs /goal."""
    resolved = resolve_prompt("goal-prompt", repo=tmp_path, home=tmp_path)
    assert resolved.source == "bundled"
    assert BD_CLAIM_COMMAND in resolved.text


# Template-drift gate: the bundled body is hashed with its placeholders intact,
# so editing what a block teaches without bumping its schema fails here. Bump
# BLOCK_SCHEMAS[<block>] and re-pin the digest in the same commit.
PINNED_BLOCK_TEMPLATES: dict[str, tuple[int, str]] = {
    "agents": (1, "abede8bbcbac750f3e0ce8b3243fc2c66a73158b3b834d35a99c7ed44fe54768"),
    "pointer": (1, "e20aa4135de14e37eb79a5c589277750c37cfafbd16c8a4d7378a99ac606601a"),
}


@pytest.mark.parametrize("block", sorted(BLOCK_SCHEMAS))
def test_block_template_changes_require_a_schema_bump(block: str) -> None:
    digest = hashlib.sha256(
        block_template_source(block).encode("utf-8")
    ).hexdigest()
    assert (BLOCK_SCHEMAS[block], digest) == PINNED_BLOCK_TEMPLATES[block], (
        f"the {block} block template changed; bump BLOCK_SCHEMAS[{block!r}] and "
        f"re-pin PINNED_BLOCK_TEMPLATES[{block!r}] to {digest!r}"
    )


# --- managed-block parsing and writing --------------------------------------


def test_apply_block_creates_a_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "AGENTS.md"
    assert apply_block(path, "agents", render_block("agents")) is BlockOutcome.CREATED
    assert read_block(path, "agents") is not None


def test_apply_block_appends_and_preserves_host_bytes(tmp_path: Path) -> None:
    path = tmp_path / "AGENTS.md"
    host = "# House rules\n\nNever force-push main.\n"
    path.write_text(host, encoding="utf-8")
    assert apply_block(path, "agents", render_block("agents")) is BlockOutcome.APPENDED
    text = path.read_text(encoding="utf-8")
    assert text.startswith(host)
    assert render_block("agents") in text


def test_apply_block_is_a_no_op_when_the_block_is_current(tmp_path: Path) -> None:
    path = tmp_path / "AGENTS.md"
    path.write_text("# House rules\n", encoding="utf-8")
    apply_block(path, "agents", render_block("agents"))
    before = path.read_bytes()
    assert (
        apply_block(path, "agents", render_block("agents")) is BlockOutcome.UNCHANGED
    )
    assert path.read_bytes() == before


def test_apply_block_replaces_a_stale_body_only(tmp_path: Path) -> None:
    path = tmp_path / "AGENTS.md"
    stale = render_block("agents", ortus_version="0.0.1", codegraph="off")
    path.write_text(f"# House rules\n\n{stale}\n\ntrailing host prose\n", encoding="utf-8")
    assert apply_block(path, "agents", render_block("agents")) is BlockOutcome.UPDATED
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# House rules\n")
    assert text.endswith("trailing host prose\n")
    assert stale not in text
    assert render_block("agents") in text


def test_apply_block_leaves_a_newer_schema_untouched(tmp_path: Path) -> None:
    """Never write backwards: an older Ortus must not downgrade the contract."""
    path = tmp_path / "AGENTS.md"
    future = (
        "<!-- BEGIN ortus block=agents schema=99 generated-by=ortus@9.9.9 -->\n"
        "from the future\n"
        "<!-- END ortus block=agents -->\n"
    )
    path.write_text(future, encoding="utf-8")
    assert apply_block(path, "agents", render_block("agents")) is BlockOutcome.AHEAD
    assert path.read_text(encoding="utf-8") == future


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "<!-- BEGIN ortus block=agents schema=1 -->\nbody\n",
            "has no END marker",
        ),
        (
            "<!-- END ortus block=agents -->\n",
            "with no BEGIN marker",
        ),
        (
            "<!-- BEGIN ortus schema=1 -->\nbody\n<!-- END ortus block=agents -->\n",
            "no block= attribute",
        ),
        (
            "<!-- BEGIN ortus block=agents schema=one -->\nb\n<!-- END ortus block=agents -->\n",
            "expected an integer",
        ),
        (
            "<!-- BEGIN ortus block=agents schema=1 -->\n"
            "<!-- BEGIN ortus block=pointer schema=1 -->\n"
            "<!-- END ortus block=pointer -->\n",
            "inside block=agents",
        ),
    ],
)
def test_parse_blocks_aborts_on_malformed_markers(
    tmp_path: Path, text: str, expected: str
) -> None:
    path = tmp_path / "AGENTS.md"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(agent_files.AgentFileError) as excinfo:
        read_block(path, "agents")
    message = str(excinfo.value)
    assert expected in message
    # The operator's next move is to open the file, so the line number rides along.
    assert f"{path}:" in message


def test_apply_block_refuses_to_write_a_malformed_file(tmp_path: Path) -> None:
    path = tmp_path / "AGENTS.md"
    path.write_text("<!-- BEGIN ortus block=agents schema=1 -->\nbody\n", encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(agent_files.AgentFileError):
        apply_block(path, "agents", render_block("agents"))
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    ("pattern", "ignored"),
    [
        ("AGENTS.md", True),
        ("/AGENTS.md", True),
        ("*.md", True),
        ("**/*.md", True),
        ("# AGENTS.md", False),
        ("AGENTS.override.md", False),
        ("docs/", False),
    ],
)
def test_gitignore_match_reads_the_repo_ignore_rules(
    tmp_path: Path, pattern: str, ignored: bool
) -> None:
    (tmp_path / ".gitignore").write_text(f"{pattern}\n", encoding="utf-8")
    assert (gitignore_match(tmp_path, "AGENTS.md") is not None) is ignored


def test_gitignore_match_honors_a_later_negation(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("*.md\n!AGENTS.md\n", encoding="utf-8")
    assert gitignore_match(tmp_path, "AGENTS.md") is None


# Acceptance #1 (broader) — render_all produces every file on disk.
def test_render_all_writes_every_template(tmp_path: Path) -> None:
    ctx = RenderContext(prefix="full", project_type="polyglot")
    written = render_all(tmp_path, ctx)
    assert len(written) == len(BUNDLED_TEMPLATES)
    for p in written:
        assert p.is_file()
        assert p.read_text(encoding="utf-8").strip(), f"{p} rendered empty"


def test_render_all_backends_writes_every_config_and_pins_ctx_backend(
    tmp_path: Path,
) -> None:
    """`backends=` widens the backend slot while `.ortusrc` pins ctx.backend."""
    ctx = RenderContext(prefix="acme", project_type="python", backend="claude")
    written = render_all(tmp_path, ctx, backends=("claude", "codex", "grok"))
    assert (tmp_path / ".claude" / "settings.json").is_file()
    assert (tmp_path / ".codex" / "config.toml").is_file()
    assert (tmp_path / ".grok" / "config.toml").is_file()
    ortusrc = (tmp_path / ".ortusrc").read_text()
    assert 'backend = "claude"' in ortusrc
    assert 'backend = "all"' not in ortusrc
    # three backend configs replace the single slot; shared files unchanged
    assert len(written) == len(BUNDLED_TEMPLATES) + 2


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
