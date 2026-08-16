"""Integration tests for the 8-verb CLI skeleton (q075.2 acceptance criteria)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.core.repo import FR003_NO_BEADS_ERROR

runner = CliRunner()

VERBS = [
    "init",
    "plan",
    "grind",
    "interview",
    "tail",
    "human",
    "check",
    "spec",
    "dashboard",
    "prompt",
]


def test_top_help_lists_all_verbs() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for verb in VERBS:
        assert verb in result.stdout, f"--help missing verb {verb!r}"


_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# Every glyph rich may draw a table border with, across its box styles and the
# ASCII fallback it substitutes when the output encoding can't carry them.
_BORDERS = "│┃|╎┆┊╽╿"


def test_help_keeps_existing_verb_order_with_new_verbs_appended() -> None:
    """Adding a verb must append; existing verbs keep their listing order."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    # Rich draws the command table inside a box, so drop the border glyphs and
    # keep the first token of each row that names a verb. On a runner there is
    # no TTY, but typer's rich_utils sets force_terminal whenever GITHUB_ACTIONS
    # (or FORCE_COLOR / PY_COLORS) is set, so every row arrives wrapped in SGR
    # escapes. Strip those first — otherwise each row's first token is an escape
    # sequence, the inventory parses empty, and the order assertion fails for a
    # reason that has nothing to do with verb order.
    known = set(VERBS) | {"unlock"}
    listed = []
    for raw in result.stdout.splitlines():
        line = _ANSI.sub("", raw)
        for border in _BORDERS:
            line = line.replace(border, " ")
        tokens = line.split()
        if tokens and tokens[0] in known:
            listed.append(tokens[0])
    assert listed, f"parsed no verbs out of --help output:\n{result.stdout!r}"
    assert listed == [
        "init",
        "plan",
        "grind",
        "interview",
        "tail",
        "human",
        "check",
        "unlock",
        "spec",
        "dashboard",
        "prompt",
    ]


def test_curate_is_not_a_registered_verb() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "curate" not in result.stdout
    unknown = runner.invoke(app, ["curate"])
    assert unknown.exit_code != 0


@pytest.mark.parametrize("verb", VERBS)
def test_verb_help_works(verb: str) -> None:
    result = runner.invoke(app, [verb, "--help"])
    assert result.exit_code == 0, result.stdout
    assert f"ortus {verb}" in result.stdout


def test_grind_help_does_not_list_max_corrections() -> None:
    result = runner.invoke(app, ["grind", "--help"])
    assert result.exit_code == 0
    assert "--max-corrections" not in result.stdout
    unknown = runner.invoke(app, ["grind", "--max-corrections", "1", "--dry-run"])
    assert unknown.exit_code != 0


def test_grind_help_does_not_list_repair_flags() -> None:
    result = runner.invoke(app, ["grind", "--help"])
    assert result.exit_code == 0
    assert "--repair-unready" not in result.stdout
    assert "--repair-budget" not in result.stdout
    unknown = runner.invoke(app, ["grind", "--repair-unready", "--dry-run"])
    assert unknown.exit_code != 0
    unknown_budget = runner.invoke(
        app, ["grind", "--repair-budget", "1", "--dry-run"]
    )
    assert unknown_budget.exit_code != 0
    repo_root = Path(__file__).resolve().parents[1]
    assert not (repo_root / "src" / "ortus" / "core" / "repair.py").is_file()


def test_version_flag_prints_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.startswith("ortus ")


def test_grind_nonexistent_repo_emits_fr003_error(tmp_path: Path) -> None:
    bogus = tmp_path / "no-beads-here"
    bogus.mkdir()
    result = runner.invoke(app, ["grind", str(bogus)])
    assert result.exit_code == 1
    # The error message must match the FR-003 mandated string verbatim.
    assert FR003_NO_BEADS_ERROR in result.stderr


def test_repo_arg_with_beads_dir_proceeds_past_fr003(tmp_path: Path) -> None:
    """A valid repo passes the FR-003 check; grind --dry-run is a cheap way
    to confirm the routing reached the verb without spawning claude."""
    repo = tmp_path / "fake-repo"
    (repo / ".beads").mkdir(parents=True)
    result = runner.invoke(app, ["grind", str(repo), "--dry-run"])
    assert result.exit_code == 0
    assert "/goal" in result.stdout


# --- CLI output convention (ortus-s60a) -------------------------------------
#
# Every non-interactive verb must emit `[YYYY-MM-DD HH:MM:SS] <phase>` lines to
# stderr so the operator can tell "running" from "hung." The `[ortus <verb>]`
# tag was dropped as per-line reading tax (ortus-kawu). These tests guard the
# convention per-verb. See AGENTS.md "CLI output convention".


def _bd_repo(tmp_path: Path) -> Path:
    """Create a hermetic bd-initialized repo for verbs that require .beads/."""
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    repo = tmp_path / "conv"
    repo.mkdir()
    subprocess.run(
        ["bd", "init", "--prefix", "conv"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    return repo


def test_init_emits_progress_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    target = tmp_path / "fresh"
    # Pretend the backend CLIs are installed: init exits 1 when the pinned run
    # backend's CLI is absent, and hermetic runners have none of them.
    import ortus.commands.init as init_mod

    monkeypatch.setattr(init_mod, "_backend_cli", lambda name: f"/usr/bin/{name}")
    # This is about the progress-line shape, not the CodeGraph prerequisite,
    # and the codegraph CLI is absent on hermetic runners.
    result = runner.invoke(app, ["init", str(target), "--codegraph", "off"])
    assert result.exit_code == 0, result.stdout + result.stderr
    stderr = _ANSI.sub("", result.stderr)
    assert "[ortus init]" not in stderr
    assert re.search(r"^\[[\d\-: ]+\] target: ", stderr, re.M), result.stderr
    assert re.search(r"^\[[\d\-: ]+\] done \(", stderr, re.M), result.stderr


def test_verb_progress_lines_open_with_timestamp_then_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every progress line a real verb emits opens with the stamp, then the
    phase — no `[ortus <verb>]` tag in between (ortus-kawu)."""
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    target = tmp_path / "stamped"
    # Same hermeticity stub as test_init_emits_progress_lines: a missing
    # pinned-run-backend CLI would fail init before any lines are asserted.
    import ortus.commands.init as init_mod

    monkeypatch.setattr(init_mod, "_backend_cli", lambda name: f"/usr/bin/{name}")
    result = runner.invoke(app, ["init", str(target), "--codegraph", "off"])
    assert result.exit_code == 0, result.stdout + result.stderr
    stderr = _ANSI.sub("", result.stderr)
    stamped = [
        line
        for line in stderr.splitlines()
        if re.match(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ", line)
    ]
    assert stamped, result.stderr
    assert "[ortus " not in stderr


def test_check_emits_progress_lines(tmp_path: Path) -> None:
    repo = _bd_repo(tmp_path)
    # check may fail individual sub-checks in this hermetic env (no claude,
    # no real sandbox); we only assert the convention output is present.
    result = runner.invoke(app, ["check", str(repo)])
    stderr = _ANSI.sub("", result.stderr)
    assert re.search(r"^\[[\d\-: ]+\] backend: ", stderr, re.M), result.stderr
    assert re.search(r"^\[[\d\-: ]+\] done \(", stderr, re.M), result.stderr


def test_human_emits_progress_lines(tmp_path: Path) -> None:
    repo = _bd_repo(tmp_path)
    result = runner.invoke(app, ["human", str(repo)])
    assert result.exit_code == 0, result.stdout + result.stderr
    stderr = _ANSI.sub("", result.stderr)
    assert re.search(r"^\[[\d\-: ]+\] target: ", stderr, re.M), result.stderr
    assert re.search(r"^\[[\d\-: ]+\] done \(", stderr, re.M), result.stderr


# --- ortus prompt list/show (ortus-apv5.1) ----------------------------------


def _isolate_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep a developer's real ~/.ortus/prompts overrides out of the test."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")


def test_prompt_list_reports_bundled_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_home(monkeypatch, tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    result = runner.invoke(app, ["prompt", "list", str(repo)])
    assert result.exit_code == 0, result.stdout + result.stderr
    for name, phase in (
        ("goal", "implementation"),
        ("interview", "interview"),
        ("plan", "planning"),
    ):
        line = next(
            row for row in result.stdout.splitlines() if row.split()[0] == name
        )
        assert "bundled (default)" in line
        assert phase in line


def test_prompt_show_stdout_is_prompt_text_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pipe-clean: stdout carries exactly the resolved text, header on stderr."""
    from ortus.core.prompts import resolve_prompt

    _isolate_home(monkeypatch, tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    result = runner.invoke(app, ["prompt", "show", "goal", str(repo)])
    assert result.exit_code == 0, result.stdout + result.stderr
    expected = resolve_prompt(
        "goal-prompt", repo=repo, home=tmp_path / "home"
    ).text
    assert result.stdout == expected
    assert "bundled (default)" in result.stderr


def test_prompt_show_origin_prints_tier_and_path_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_home(monkeypatch, tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    result = runner.invoke(app, ["prompt", "show", "goal", str(repo), "--origin"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert result.stdout.startswith("bundled")
    # Tier and location only — never the prompt body.
    assert "One context window" not in result.stdout


def test_prompt_show_unknown_name_exits_nonzero_listing_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_home(monkeypatch, tmp_path)
    result = runner.invoke(app, ["prompt", "show", "nope", str(tmp_path)])
    assert result.exit_code != 0
    assert result.stdout == ""
    for name in ("goal", "interview", "plan"):
        assert name in result.stderr


def test_prompt_show_three_layer_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repo override wins and --origin names its tier and path."""
    _isolate_home(monkeypatch, tmp_path)
    repo = tmp_path / "repo"
    override = repo / ".ortus" / "prompts" / "goal-prompt.md"
    override.parent.mkdir(parents=True)
    override.write_text("REPO-LEVEL-SENTINEL")
    shown = runner.invoke(app, ["prompt", "show", "goal", str(repo)])
    assert shown.exit_code == 0
    assert shown.stdout == "REPO-LEVEL-SENTINEL"
    origin = runner.invoke(app, ["prompt", "show", "goal", str(repo), "--origin"])
    assert origin.exit_code == 0
    assert origin.stdout.split() == ["repo", str(override)]


# --- ortus prompt eject (ortus-apv5.2) --------------------------------------


def test_prompt_eject_writes_stamped_repo_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-1: the ejected file is the bundled text under a provenance stamp."""
    from ortus.core.prompts import bundled_prompt_text, bundled_sha256, parse_eject_stamp

    _isolate_home(monkeypatch, tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    result = runner.invoke(app, ["prompt", "eject", "goal", str(repo)])
    assert result.exit_code == 0, result.stdout + result.stderr
    destination = repo / ".ortus" / "prompts" / "goal-prompt.md"
    assert destination.is_file()
    stamp_line, body = destination.read_text().split("\n", 1)
    bundled = bundled_prompt_text("goal-prompt")
    assert body == bundled
    parsed = parse_eject_stamp(stamp_line)
    assert parsed is not None
    assert parsed[1] == bundled_sha256(bundled)


def test_prompt_eject_refuses_existing_destination_without_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-1: a second eject refuses; --force overwrites."""
    _isolate_home(monkeypatch, tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    assert runner.invoke(app, ["prompt", "eject", "goal", str(repo)]).exit_code == 0
    destination = repo / ".ortus" / "prompts" / "goal-prompt.md"
    destination.write_text("local edits")
    refused = runner.invoke(app, ["prompt", "eject", "goal", str(repo)])
    assert refused.exit_code != 0
    assert "--force" in refused.stderr
    assert destination.read_text() == "local edits"
    forced = runner.invoke(app, ["prompt", "eject", "goal", str(repo), "--force"])
    assert forced.exit_code == 0, forced.stdout + forced.stderr
    assert destination.read_text() != "local edits"


def test_prompt_eject_copies_bundled_even_when_an_override_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Source purity: a winning user override never feeds the ejected copy."""
    from ortus.core.prompts import bundled_prompt_text

    _isolate_home(monkeypatch, tmp_path)
    user_override = tmp_path / "home" / ".ortus" / "prompts" / "goal-prompt.md"
    user_override.parent.mkdir(parents=True)
    user_override.write_text("USER-LAYER-SENTINEL")
    repo = tmp_path / "repo"
    repo.mkdir()
    result = runner.invoke(app, ["prompt", "eject", "goal", str(repo)])
    assert result.exit_code == 0, result.stdout + result.stderr
    text = (repo / ".ortus" / "prompts" / "goal-prompt.md").read_text()
    assert "USER-LAYER-SENTINEL" not in text
    assert text.split("\n", 1)[1] == bundled_prompt_text("goal-prompt")


def test_prompt_eject_user_layer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_home(monkeypatch, tmp_path)
    result = runner.invoke(app, ["prompt", "eject", "goal", "--user"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert (tmp_path / "home" / ".ortus" / "prompts" / "goal-prompt.md").is_file()


def test_prompt_eject_requires_exactly_one_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No cwd default: name a repo or pass --user, never both."""
    _isolate_home(monkeypatch, tmp_path)
    neither = runner.invoke(app, ["prompt", "eject", "goal"])
    assert neither.exit_code == 2
    repo = tmp_path / "repo"
    repo.mkdir()
    both = runner.invoke(app, ["prompt", "eject", "goal", str(repo), "--user"])
    assert both.exit_code == 2
    assert not (repo / ".ortus").exists()


def test_prompt_eject_unknown_name_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_home(monkeypatch, tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    result = runner.invoke(app, ["prompt", "eject", "nope", str(repo)])
    assert result.exit_code == 2
    assert "goal" in result.stderr
    assert not (repo / ".ortus").exists()


def test_grind_dry_run_keeps_dry_run_output_on_stdout(tmp_path: Path) -> None:
    """--dry-run results stay on stdout (machine-readable); only progress is stderr.

    Regression guard: switching grind's pre-flock output.info calls to
    output.progress must not silently move dry-run flag dump to stderr.
    """
    repo = tmp_path / "fake-repo"
    (repo / ".beads").mkdir(parents=True)
    result = runner.invoke(app, ["grind", str(repo), "--dry-run"])
    assert result.exit_code == 0
    # Dry-run output is the verb's RESULT, not progress — keep it on stdout.
    assert "repo:" in result.stdout
    assert "/goal" in result.stdout
