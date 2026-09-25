"""Tests for --package-manager / --framework / --linter on `ortus init`.

Covers default resolution per language, explicit overrides, and invalid
combinations. Run targeted: `uv run pytest tests/test_init_flags.py`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.core.init_render import (
    FRAMEWORK_DEFAULTS,
    LINTER_DEFAULTS,
    PACKAGE_MANAGER_DEFAULTS,
    RenderContext,
)

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("bd_init_from_template"),
]
runner = CliRunner()


@pytest.fixture(autouse=True)
def _require_bd() -> None:
    if shutil.which("bd") is None:
        pytest.skip("bd binary not on PATH")


@pytest.fixture(autouse=True)
def _fake_codegraph(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in for the CodeGraph CLI so stack-flag tests stay hermetic."""
    import ortus.commands.init as init_mod

    monkeypatch.setattr(init_mod, "_codegraph_cli", lambda: "/usr/bin/codegraph")
    monkeypatch.setattr(
        init_mod,
        "_codegraph_index",
        lambda repo, **kwargs: (repo / ".codegraph").mkdir(parents=True, exist_ok=True),
    )


@pytest.fixture(autouse=True)
def _fake_backend_clis(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend every backend CLI is installed.

    The default `--backend all` summarizes CLI availability per backend and
    fails when the pinned run backend's CLI is absent, so an unfaked lookup
    would make these tests answer for the host's installs. Tests about a
    missing CLI re-patch `_backend_cli` themselves.
    """
    import ortus.commands.init as init_mod

    monkeypatch.setattr(init_mod, "_backend_cli", lambda name: f"/usr/bin/{name}")


def _ortusrc(target: Path) -> dict:
    return tomllib.loads((target / ".ortusrc").read_text())


@pytest.mark.parametrize(
    "project_type,expected_pm,expected_lint",
    [
        ("python", "uv", "ruff"),
        ("typescript", "npm", "eslint"),
        ("go", "gomod", "golangci"),
        ("rust", "cargo", "clippy"),
        ("polyglot", "none", "none"),
    ],
)
def test_per_language_defaults(
    tmp_path: Path, project_type: str, expected_pm: str, expected_lint: str
) -> None:
    target = tmp_path / project_type
    result = runner.invoke(
        app, ["init", str(target), "--project-type", project_type]
    )
    assert result.exit_code == 0, (result.stdout or "") + (result.stderr or "")
    assert PACKAGE_MANAGER_DEFAULTS[project_type] == expected_pm
    assert LINTER_DEFAULTS[project_type] == expected_lint
    assert FRAMEWORK_DEFAULTS[project_type] == "none"


def test_explicit_package_manager_override(tmp_path: Path) -> None:
    target = tmp_path / "py"
    result = runner.invoke(
        app,
        ["init", str(target), "--project-type", "python", "--package-manager", "pip"],
    )
    assert result.exit_code == 0, (result.stdout or "") + (result.stderr or "")


def test_explicit_framework_override(tmp_path: Path) -> None:
    target = tmp_path / "py"
    result = runner.invoke(
        app,
        ["init", str(target), "--project-type", "python", "--framework", "fastapi"],
    )
    assert result.exit_code == 0, (result.stdout or "") + (result.stderr or "")


def test_explicit_linter_override(tmp_path: Path) -> None:
    target = tmp_path / "ts"
    result = runner.invoke(
        app,
        ["init", str(target), "--project-type", "typescript", "--linter", "none"],
    )
    assert result.exit_code == 0, (result.stdout or "") + (result.stderr or "")


def test_invalid_package_manager_for_language(tmp_path: Path) -> None:
    target = tmp_path / "bad"
    result = runner.invoke(
        app,
        ["init", str(target), "--project-type", "python", "--package-manager", "bun"],
    )
    assert result.exit_code == 1
    combined = (result.stdout or "") + (result.stderr or "")
    assert "--package-manager" in combined
    assert "'bun'" in combined or "bun" in combined
    # nothing should have been bootstrapped on a flag rejection
    assert not (target / ".beads").exists()


def test_invalid_framework_for_language(tmp_path: Path) -> None:
    target = tmp_path / "bad"
    result = runner.invoke(
        app,
        ["init", str(target), "--project-type", "go", "--framework", "nextjs"],
    )
    assert result.exit_code == 1
    combined = (result.stdout or "") + (result.stderr or "")
    assert "--framework" in combined


def test_invalid_linter_for_language(tmp_path: Path) -> None:
    target = tmp_path / "bad"
    result = runner.invoke(
        app,
        ["init", str(target), "--project-type", "rust", "--linter", "ruff"],
    )
    assert result.exit_code == 1
    combined = (result.stdout or "") + (result.stderr or "")
    assert "--linter" in combined


def test_invalid_project_type_rejected(tmp_path: Path) -> None:
    target = tmp_path / "bad"
    result = runner.invoke(app, ["init", str(target), "--project-type", "cobol"])
    assert result.exit_code == 1
    combined = (result.stdout or "") + (result.stderr or "")
    assert "--project-type" in combined


def test_codegraph_cli_missing_fails_under_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-3: no CLI under the default policy fails before writing."""
    import ortus.commands.init as init_mod

    monkeypatch.setattr(init_mod, "_codegraph_cli", lambda: None)
    target = tmp_path / "nocli"
    result = runner.invoke(app, ["init", str(target)])
    assert result.exit_code == 1
    combined = (result.stdout or "") + (result.stderr or "")
    compact = "".join(combined.split())
    assert "codegraphCLIisnotonPATH" in compact, combined
    assert "--codegraphoff" in compact, combined
    # nothing bootstrapped on the rejection
    assert not (target / ".beads").exists()


def test_codegraph_off_skips_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-4: `off` never touches the CLI and pins itself."""
    import ortus.commands.init as init_mod

    def _never_lookup() -> str:
        raise AssertionError("--codegraph off must not look for the CodeGraph CLI")

    def _never_index(repo: Path, **kwargs: object) -> None:
        raise AssertionError("--codegraph off must not invoke the CodeGraph CLI")

    monkeypatch.setattr(init_mod, "_codegraph_cli", _never_lookup)
    monkeypatch.setattr(init_mod, "_codegraph_index", _never_index)
    target = tmp_path / "graphless"
    result = runner.invoke(app, ["init", str(target), "--codegraph", "off"])
    assert result.exit_code == 0, (result.stdout or "") + (result.stderr or "")
    assert _ortusrc(target)["codegraph"] == "off"
    assert not (target / ".codegraph").exists()


def test_invalid_codegraph_mode_rejected(tmp_path: Path) -> None:
    target = tmp_path / "badmode"
    result = runner.invoke(app, ["init", str(target), "--codegraph", "maybe"])
    assert result.exit_code == 1
    combined = (result.stdout or "") + (result.stderr or "")
    assert "--codegraph" in combined
    assert not (target / ".beads").exists()


# --- forced re-init preserves recorded .ortusrc facts -----------------------


def test_force_reinit_preserves_recorded_facts(tmp_path: Path) -> None:
    """Omitted flags resolve to the recorded values, not detection defaults."""
    target = tmp_path / "500"  # basename deliberately differs from the prefix
    result = runner.invoke(
        app,
        [
            "init", str(target),
            "--prefix", "fh",
            "--project-type", "python",
            "--backend", "codex",
            "--codegraph", "auto",
        ],
    )
    assert result.exit_code == 0, (result.stdout or "") + (result.stderr or "")
    result = runner.invoke(app, ["init", str(target), "--force"])
    assert result.exit_code == 0, (result.stdout or "") + (result.stderr or "")
    after = _ortusrc(target)
    assert after["prefix"] == "fh"
    assert after["project_type"] == "python"
    assert after["backend"] == "codex"
    assert after["codegraph"] == "auto"
    combined = (result.stdout or "") + (result.stderr or "")
    assert "re-detected" not in combined


def test_force_reinit_explicit_override_prints_change_lines(tmp_path: Path) -> None:
    """Explicit flags still win, and each changed recorded fact gets a line."""
    target = tmp_path / "override"
    result = runner.invoke(
        app,
        ["init", str(target), "--prefix", "fh", "--project-type", "python",
         "--backend", "codex"],
    )
    assert result.exit_code == 0, (result.stdout or "") + (result.stderr or "")
    result = runner.invoke(
        app,
        ["init", str(target), "--force", "--prefix", "new",
         "--project-type", "go", "--backend", "all"],
    )
    assert result.exit_code == 0, (result.stdout or "") + (result.stderr or "")
    combined = (result.stdout or "") + (result.stderr or "")
    assert "re-detected prefix: fh -> new" in combined
    assert "re-detected project_type: python -> go" in combined
    # an explicit `all` pins claude exactly as on a fresh init, but visibly
    assert "re-detected backend: codex -> claude" in combined
    after = _ortusrc(target)
    assert after["prefix"] == "new"
    assert after["project_type"] == "go"
    assert after["backend"] == "claude"


def test_force_reinit_explicit_flag_equal_to_recorded_is_silent(
    tmp_path: Path,
) -> None:
    target = tmp_path / "same"
    assert runner.invoke(
        app, ["init", str(target), "--prefix", "fh"]
    ).exit_code == 0
    result = runner.invoke(app, ["init", str(target), "--force", "--prefix", "fh"])
    assert result.exit_code == 0, (result.stdout or "") + (result.stderr or "")
    combined = (result.stdout or "") + (result.stderr or "")
    assert "re-detected" not in combined


def test_fresh_init_defaults_unchanged(tmp_path: Path) -> None:
    """No `.ortusrc` means detection defaults exactly as before."""
    target = tmp_path / "fresh"
    result = runner.invoke(app, ["init", str(target)])
    assert result.exit_code == 0, (result.stdout or "") + (result.stderr or "")
    rc = _ortusrc(target)
    assert rc["prefix"] == "fresh"
    assert rc["project_type"] == "polyglot"
    assert rc["backend"] == "claude"
    assert rc["codegraph"] == "required"
    combined = (result.stdout or "") + (result.stderr or "")
    assert "re-detected" not in combined


def test_invalid_recorded_project_type_fails_with_hint(tmp_path: Path) -> None:
    """A recorded value that fails validation errors instead of falling back."""
    target = tmp_path / "badrec"
    assert runner.invoke(app, ["init", str(target)]).exit_code == 0
    rc_path = target / ".ortusrc"
    rc_path.write_text(
        rc_path.read_text().replace(
            'project_type = "polyglot"', 'project_type = "cobol"'
        )
    )
    result = runner.invoke(app, ["init", str(target), "--force"])
    assert result.exit_code == 1
    combined = (result.stdout or "") + (result.stderr or "")
    assert "cobol" in combined
    assert "--project-type" in combined


def test_recorded_backend_all_rejected(tmp_path: Path) -> None:
    target = tmp_path / "allrec"
    assert runner.invoke(app, ["init", str(target)]).exit_code == 0
    rc_path = target / ".ortusrc"
    rc_path.write_text(
        rc_path.read_text().replace('backend = "claude"', 'backend = "all"')
    )
    result = runner.invoke(app, ["init", str(target), "--force"])
    assert result.exit_code == 1
    combined = (result.stdout or "") + (result.stderr or "")
    assert "init provisioning option" in combined


def test_malformed_ortusrc_fails_with_repair_hint(tmp_path: Path) -> None:
    target = tmp_path / "mangled"
    assert runner.invoke(app, ["init", str(target)]).exit_code == 0
    (target / ".ortusrc").write_text("prefix = \n")
    result = runner.invoke(app, ["init", str(target), "--force"])
    assert result.exit_code == 1
    combined = (result.stdout or "") + (result.stderr or "")
    assert "not valid TOML" in combined


def test_render_context_carries_new_fields() -> None:
    ctx = RenderContext(
        prefix="x",
        project_type="python",
        package_manager="uv",
        framework="fastapi",
        linter="ruff",
    )
    d = ctx.as_dict()
    assert d["package_manager"] == "uv"
    assert d["framework"] == "fastapi"
    assert d["linter"] == "ruff"


def test_help_advertises_new_flags() -> None:
    # NO_COLOR + TERM=dumb stop Rich from interleaving SGR escape sequences
    # through flag names in option tables, which otherwise break the literal
    # substring assertions below in CI's terminal.
    result = runner.invoke(
        app, ["init", "--help"], env={"NO_COLOR": "1", "TERM": "dumb"}
    )
    assert result.exit_code == 0
    out = result.stdout
    assert "--package-manager" in out
    assert "--framework" in out
    assert "--linter" in out
    assert "claude|codex|grok" in out
    # the help text should defer per-language detail rather than enumerate it
    assert "depend on --project-type" in out


def test_help_advertises_local_flags() -> None:
    result = runner.invoke(
        app, ["init", "--help"], env={"NO_COLOR": "1", "TERM": "dumb"}
    )
    assert result.exit_code == 0
    out = result.stdout
    assert "--local-model" in out
    assert "--local-base-url" in out
    assert "claude|codex|grok|local" in out


@pytest.mark.real_bd
def test_init_installs_the_tracker_git_hooks(tmp_path: Path) -> None:
    """A bootstrapped project can actually run the hooks beads ships.

    The assertion follows `core.hooksPath` rather than assuming `.git/hooks/`:
    with the Dolt backend the hooks live in the tracked `.beads/hooks/` and git
    is pointed there, while a clone that carries no such setting takes them in
    `.git/hooks/`. Either way the claim is the same one that matters — the hook
    git will run on the next commit is on disk and executable.
    """
    target = tmp_path / "hooked"
    result = runner.invoke(app, ["init", str(target), "--project-type", "python"])
    assert result.exit_code == 0, (result.stdout or "") + (result.stderr or "")
    configured = subprocess.run(
        ["git", "config", "--get", "core.hooksPath"],
        cwd=target,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    hooks_dir = Path(configured) if configured else target / ".git" / "hooks"
    if not hooks_dir.is_absolute():
        hooks_dir = target / hooks_dir
    hook = hooks_dir / "pre-commit"
    assert hook.is_file(), sorted(p.name for p in hooks_dir.iterdir())
    assert os.access(hook, os.X_OK), "git only runs a hook it may execute"
    assert "bd hooks run pre-commit" in hook.read_text(encoding="utf-8")
