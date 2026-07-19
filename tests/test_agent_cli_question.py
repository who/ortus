"""Tests for the `agent_cli` copier question (FR-001, ortus-1xvv).

The question picks which backend a generated project defaults to. Three things
have to hold and none of them can drift apart:

  * copier.yaml offers exactly claude/codex and defaults to claude (NFR-001 —
    existing users must see zero change);
  * the AnswersValidator context hook rejects anything else, because copier's
    own `validator:` only fires during interactive prompting and is bypassed
    by --data;
  * the rendered ortus/lib/backend-default.sh carries the answer through to
    ORTUS_BACKEND_DEFAULT, the last link in FR-002's precedence chain.

Rendering goes through Jinja directly rather than `copier copy`: copier renders
a local template from git, so a copier-driven test would assert against the
last commit instead of the working tree (same reasoning as
test_codex_config_template.py).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import jinja2
import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
COPIER_YAML = REPO_ROOT / "copier.yaml"
BACKEND_DEFAULT_TEMPLATE = (
    REPO_ROOT / "template" / "ortus" / "lib" / "backend-default.sh.jinja"
)
BACKEND_SH = REPO_ROOT / "ortus" / "lib" / "backend.sh"

VALIDATORS = REPO_ROOT / "extensions" / "validators.py"


@pytest.fixture(scope="module")
def validator():
    """AnswersValidator, or skip — copier is a CLI tool, not a test dependency."""
    pytest.importorskip("copier", reason="copier is installed as a uv tool, not a lib")
    sys.path.insert(0, str(REPO_ROOT))
    from extensions.validators import AnswersValidator

    return AnswersValidator()


@pytest.fixture(scope="module")
def user_message_error():
    pytest.importorskip("copier", reason="copier is installed as a uv tool, not a lib")
    from copier.errors import UserMessageError

    return UserMessageError


@pytest.fixture(scope="module")
def question() -> dict:
    spec = yaml.safe_load(COPIER_YAML.read_text())
    assert "agent_cli" in spec, "copier.yaml is missing the agent_cli question"
    return spec["agent_cli"]


def test_default_is_claude(question):
    """NFR-001/G3: an existing user re-running copier must not change backend."""
    assert question["default"] == "claude"


def test_choices_are_exactly_claude_and_codex(question):
    assert sorted(question["choices"].values()) == ["claude", "codex"]


def test_choices_match_backend_sh(question):
    """A value copier accepts must be one resolve_backend() accepts."""
    line = next(
        ln for ln in BACKEND_SH.read_text().splitlines()
        if ln.startswith("BACKEND_CHOICES=")
    )
    shell_choices = line.split("=", 1)[1].strip('"').split()
    assert sorted(question["choices"].values()) == sorted(shell_choices)


def test_validator_source_covers_both_backends(question):
    """Runs even without copier installed: the hook's list matches copier.yaml."""
    src = VALIDATORS.read_text()
    line = next(
        ln for ln in src.splitlines() if ln.strip().startswith("VALID_AGENT_CLIS")
    )
    listed = sorted(w.strip("\"' ()") for w in line.split("=", 1)[1].split(",") if w.strip(" ()"))
    assert listed == sorted(question["choices"].values())


@pytest.mark.parametrize("value", ["claude", "codex"])
def test_validator_accepts_valid_backends(validator, value):
    assert validator.hook({"agent_cli": value}) == {}


@pytest.mark.parametrize("value", ["gemini", "Claude", "", "codex "])
def test_validator_rejects_invalid_backends(validator, user_message_error, value):
    """--data bypasses copier's `validator:`, so the hook is the real gate."""
    with pytest.raises(user_message_error, match="agent_cli must be one of"):
        validator.hook({"agent_cli": value})


def test_validator_ignores_absent_answer(validator):
    """Older answer files predate the question; absence must not raise."""
    assert validator.hook({}) == {}


@pytest.mark.parametrize("value", ["claude", "codex"])
def test_backend_default_renders_the_answer(value, tmp_path):
    env = jinja2.Environment(
        undefined=jinja2.StrictUndefined, keep_trailing_newline=True
    )
    rendered = env.from_string(BACKEND_DEFAULT_TEMPLATE.read_text()).render(
        agent_cli=value
    )
    assert f'ORTUS_BACKEND_DEFAULT:={value}' in rendered

    # And it actually resolves that way once backend.sh sources it.
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "backend-default.sh").write_text(rendered)
    (lib / "backend.sh").write_text(BACKEND_SH.read_text())
    out = subprocess.run(
        ["bash", "-c", f'source "{lib}/backend.sh"; resolve_backend ""'],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == value


def test_backend_sh_falls_back_to_claude_without_the_generated_file(tmp_path):
    """The Ortus repo itself ships no backend-default.sh; it must still work."""
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "backend.sh").write_text(BACKEND_SH.read_text())
    out = subprocess.run(
        ["bash", "-c", f'source "{lib}/backend.sh"; resolve_backend ""'],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "claude"


@pytest.mark.parametrize("value", ["claude", "codex"])
def test_env_override_beats_the_generated_default(value, tmp_path):
    """FR-002 precedence: ORTUS_BACKEND outranks the generated default."""
    other = "codex" if value == "claude" else "claude"
    env = jinja2.Environment(
        undefined=jinja2.StrictUndefined, keep_trailing_newline=True
    )
    rendered = env.from_string(BACKEND_DEFAULT_TEMPLATE.read_text()).render(
        agent_cli=value
    )
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "backend-default.sh").write_text(rendered)
    (lib / "backend.sh").write_text(BACKEND_SH.read_text())
    out = subprocess.run(
        ["bash", "-c", f'source "{lib}/backend.sh"; resolve_backend ""'],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "ORTUS_BACKEND": other},
        check=True,
    )
    assert out.stdout.strip() == other
