"""Tests for ortus init (q075.5 acceptance criteria).

Marked integration since they shell out to real `bd init`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.core.agent_files import BLOCK_SCHEMAS, MANAGED_FILES, read_block
from ortus.core.local_backend import DEFAULT_LOCAL_BASE_URL, LocalServerError
from ortus.core.readiness import READINESS_MEMORY_KEY

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

pytestmark = pytest.mark.integration
runner = CliRunner()


@pytest.fixture(autouse=True)
def _require_bd() -> None:
    if shutil.which("bd") is None:
        pytest.skip("bd binary not on PATH")


@pytest.fixture(autouse=True)
def _fake_codegraph(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep init's CodeGraph bootstrap hermetic.

    Init now defaults to `--codegraph required`, so every invocation below
    would otherwise shell out to a real `codegraph init` — slow, and a hard
    failure on a host without the CLI. The fake stands in for both the PATH
    lookup and the index build.
    """
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


@pytest.fixture(autouse=True)
def _fake_local_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep `--backend local` inits off the network.

    A pinned local backend gets one reachability probe after rendering, and
    an unpinned one lists the served ids for the missing-model error. The
    default fake answers "down" to both, the shape a fresh bootstrap usually
    meets; tests about a served model re-patch `probe_models` or
    `list_served_models` themselves.
    """
    import ortus.commands.init as init_mod

    def down(*args, **kwargs):
        raise LocalServerError("unreachable", "connection refused", "start it")

    monkeypatch.setattr(init_mod, "probe_models", down)
    monkeypatch.setattr(init_mod, "list_served_models", down)


def test_init_on_empty_dir_creates_all_artifacts(tmp_path: Path) -> None:
    """Acceptance #1: fresh dir → .beads/, settings.json, .ortusrc, AGENTS.md, .gitignore."""
    target = tmp_path / "fresh"
    result = runner.invoke(app, ["init", str(target)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert (target / ".beads").is_dir()
    assert (target / ".claude" / "settings.json").is_file()
    assert (target / ".ortusrc").is_file()
    assert (target / "AGENTS.md").is_file()
    assert (target / ".gitignore").is_file()
    branch = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        cwd=target,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert branch == "main"


def test_init_codex_creates_only_codex_config(tmp_path: Path) -> None:
    target = tmp_path / "codex"
    result = runner.invoke(app, ["init", str(target), "--backend", "codex"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert (target / ".codex" / "config.toml").is_file()
    assert not (target / ".claude").exists()
    assert 'backend = "codex"' in (target / ".ortusrc").read_text()


def test_init_grok_creates_only_grok_config(tmp_path: Path) -> None:
    """Official project `.grok/config.toml` is what Grok Build reads.

    docs.x.ai/build/settings: only [mcp_servers], [plugins], and [permission]
    apply in that file. This is not a decorative ignored path, and [sandbox]
    must not be written expecting the binary to honor it.
    """
    target = tmp_path / "grok"
    result = runner.invoke(app, ["init", str(target), "--backend", "grok"])
    assert result.exit_code == 0, result.stdout + result.stderr
    grok_config = target / ".grok" / "config.toml"
    assert grok_config.is_file()
    assert not (target / ".claude" / "settings.json").exists()
    assert not (target / ".claude").exists()
    assert 'backend = "grok"' in (target / ".ortusrc").read_text()
    import sys
    if sys.version_info >= (3, 11):
        import tomllib
    else:
        import tomli as tomllib
    data = tomllib.loads(grok_config.read_text())
    assert "codegraph" in data.get("mcp_servers", {})
    assert "sandbox" not in data
    assert "sandbox_mode" not in data


def test_init_default_all_provisions_every_backend(tmp_path: Path) -> None:
    """`--backend all` (the default) writes all three config dirs and pins claude."""
    target = tmp_path / "everything"
    result = runner.invoke(app, ["init", str(target)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert (target / ".claude" / "settings.json").is_file()
    assert (target / ".codex" / "config.toml").is_file()
    assert (target / ".grok" / "config.toml").is_file()
    ortusrc = (target / ".ortusrc").read_text()
    assert 'backend = "claude"' in ortusrc
    assert 'backend = "all"' not in ortusrc


def test_init_all_with_grok_cli_absent_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-1: a missing sibling CLI is a recorded skip, not an init failure."""
    import ortus.commands.init as init_mod

    monkeypatch.setattr(
        init_mod,
        "_backend_cli",
        lambda name: None if name == "grok" else f"/usr/bin/{name}",
    )
    target = tmp_path / "nogrok"
    result = runner.invoke(app, ["init", str(target)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert (target / ".claude" / "settings.json").is_file()
    assert (target / ".codex" / "config.toml").is_file()
    ortusrc = (target / ".ortusrc").read_text()
    assert 'backend = "all"' not in ortusrc
    assert 'backend = "claude"' in ortusrc
    combined = result.stdout + result.stderr
    assert "grok CLI not on PATH" in combined
    assert "ortus check" in combined


def test_init_all_with_claude_cli_absent_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pinned run backend without its CLI is a failed init, not a warning."""
    import ortus.commands.init as init_mod

    monkeypatch.setattr(
        init_mod,
        "_backend_cli",
        lambda name: None if name == "claude" else f"/usr/bin/{name}",
    )
    target = tmp_path / "noclaude"
    result = runner.invoke(app, ["init", str(target)])
    assert result.exit_code == 1
    assert "pinned run backend" in (result.stdout + result.stderr)


def test_settings_json_has_bd_excluded_and_hooks(tmp_path: Path) -> None:
    """Acceptance #2: settings has sandbox.excludedCommands and bd-prime hooks."""
    target = tmp_path / "fresh"
    runner.invoke(app, ["init", str(target)])
    data = json.loads((target / ".claude" / "settings.json").read_text())
    assert "bd" in data["sandbox"]["excludedCommands"]
    assert "bd *" in data["sandbox"]["excludedCommands"]
    hooks = data["hooks"]
    assert any(
        h["command"] == "bd prime"
        for group in hooks.get("SessionStart", [])
        for h in group["hooks"]
    )
    assert any(
        h["command"] == "bd prime"
        for group in hooks.get("PreCompact", [])
        for h in group["hooks"]
    )


def test_codex_and_grok_configs_carry_no_prime_hook(tmp_path: Path) -> None:
    """Codex and Grok reach the readiness memory without a host hook.

    Codex 0.144.x hooks must be explicitly trusted before they run, and the
    official `.grok/config.toml` accepts only [mcp_servers], [plugins], and
    [permission] — neither host runs an untrusted SessionStart command. Their
    prime path is the managed AGENTS.md block (`bd prime`, then `ortus spec`),
    so a hook stanza in either config would be dead configuration; this test
    pins that neither template grows one that the host would silently ignore.
    """
    target = tmp_path / "fresh"
    result = runner.invoke(app, ["init", str(target)])
    assert result.exit_code == 0, result.stdout + result.stderr
    for config in (".codex/config.toml", ".grok/config.toml"):
        text = (target / config).read_text()
        assert "prime" not in text, config
        assert "hook" not in text.lower(), config


def _bd_memories(repo: Path) -> dict:
    proc = subprocess.run(
        ["bd", "memories", "--json"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


def test_init_stores_readiness_memory(tmp_path: Path) -> None:
    """AC-1: the keyed pointer memory lands in the new bd workspace."""
    target = tmp_path / "fresh"
    result = runner.invoke(app, ["init", str(target)])
    assert result.exit_code == 0, result.stdout + result.stderr
    memories = _bd_memories(target)
    assert READINESS_MEMORY_KEY in memories, sorted(memories)
    assert "ortus spec" in memories[READINESS_MEMORY_KEY]


@pytest.mark.slow
def test_init_force_does_not_duplicate_readiness_memory(tmp_path: Path) -> None:
    """Re-running init over an existing workspace updates the memory in place."""
    target = tmp_path / "fresh"
    assert runner.invoke(app, ["init", str(target)]).exit_code == 0
    result = runner.invoke(app, ["init", str(target), "--force"])
    assert result.exit_code == 0, result.stdout + result.stderr
    memories = _bd_memories(target)
    matching = [k for k in memories if k.startswith(READINESS_MEMORY_KEY)]
    assert matching == [READINESS_MEMORY_KEY], matching


def test_init_readiness_memory_failure_warns_and_still_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-2: a bd too old for `remember` costs a warning, not the bootstrap."""
    import ortus.commands.init as init_mod

    def fake_run(args, **kwargs):
        if args[:2] == ["bd", "remember"]:
            raise subprocess.CalledProcessError(returncode=1, cmd=args)
        return subprocess.CompletedProcess(args=args, returncode=0)

    monkeypatch.setattr(init_mod.subprocess, "run", fake_run)
    target = tmp_path / "oldbd"
    result = runner.invoke(app, ["init", str(target)])
    assert result.exit_code == 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    compact = "".join(combined.split())
    assert "couldnotstorethereadinessmemory" in compact, combined
    assert "bdremember" in compact, combined


def test_init_refuses_existing_beads_without_force(tmp_path: Path) -> None:
    """Acceptance #3: existing .beads/ → exit 1 without --force."""
    target = tmp_path / "exists"
    target.mkdir()
    (target / ".beads").mkdir()
    result = runner.invoke(app, ["init", str(target)])
    assert result.exit_code == 1
    assert "already has a .beads/" in (result.stdout + result.stderr)


def test_init_force_rerenders_templates(tmp_path: Path) -> None:
    """Acceptance #4: --force re-renders ortus-owned files."""
    target = tmp_path / "fresh"
    runner.invoke(app, ["init", str(target)])
    settings = target / ".claude" / "settings.json"
    settings.write_text('{"corrupted": true}')
    assert json.loads(settings.read_text()) == {"corrupted": True}
    result = runner.invoke(app, ["init", str(target), "--force"])
    assert result.exit_code == 0, result.stdout + result.stderr
    data = json.loads(settings.read_text())
    assert "sandbox" in data, "settings.json should be re-rendered"


def test_init_force_preserves_host_gitignore_lines(tmp_path: Path) -> None:
    """Re-init only rewrites between the markers; host entries survive."""
    target = tmp_path / "fresh"
    result = runner.invoke(app, ["init", str(target)])
    assert result.exit_code == 0, result.stdout + result.stderr
    gitignore = target / ".gitignore"
    # bd init contributes its own entries above the ortus section, so the
    # whole first-init file — bd lines included — counts as host content.
    before = gitignore.read_text(encoding="utf-8")
    assert "# BEGIN ortus block=gitignore" in before
    host_top = "# ML artifacts\nmodels/\n.pnpm-store/\n\n"
    host_bottom = "\ntest-results/\nplaywright-report/\n"
    gitignore.write_text(host_top + before + host_bottom, encoding="utf-8")
    result = runner.invoke(app, ["init", str(target), "--force"])
    assert result.exit_code == 0, result.stdout + result.stderr
    text = gitignore.read_text(encoding="utf-8")
    assert text == host_top + before + host_bottom
    assert ".gitignore ortus section" in (result.stdout + result.stderr)


def test_init_force_adopts_a_premarker_gitignore(tmp_path: Path) -> None:
    """A `.gitignore` written before the markers existed loses nothing."""
    target = tmp_path / "fresh"
    result = runner.invoke(app, ["init", str(target)])
    assert result.exit_code == 0, result.stdout + result.stderr
    gitignore = target / ".gitignore"
    host = "# host rules\n.beads/*.flock\n*.tsbuildinfo\n"
    gitignore.write_text(host, encoding="utf-8")
    result = runner.invoke(app, ["init", str(target), "--force"])
    assert result.exit_code == 0, result.stdout + result.stderr
    text = gitignore.read_text(encoding="utf-8")
    assert text.startswith(host)
    assert "# BEGIN ortus block=gitignore" in text
    assert ".codegraph/" in text


def test_prefix_is_respected(tmp_path: Path) -> None:
    """Acceptance #5: --prefix foo causes bd issues to carry foo- prefix."""
    target = tmp_path / "fresh"
    result = runner.invoke(app, ["init", str(target), "--prefix", "myfeat"])
    assert result.exit_code == 0
    # Create an issue with bd; its id should start with the prefix.
    proc = subprocess.run(
        ["bd", "create", "--silent", "--title", "smoke", "--type", "task"],
        cwd=str(target),
        capture_output=True,
        text=True,
        check=True,
    )
    new_id = proc.stdout.strip()
    assert new_id.startswith("myfeat-"), f"got id {new_id!r}, expected myfeat- prefix"


def test_default_prefix_is_dir_basename(tmp_path: Path) -> None:
    target = tmp_path / "fancyname"
    runner.invoke(app, ["init", str(target)])
    proc = subprocess.run(
        ["bd", "create", "--silent", "--title", "smoke", "--type", "task"],
        cwd=str(target),
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.strip().startswith("fancyname-")


@pytest.mark.slow
def test_init_under_five_seconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance #6 (NFR-001): Ortus-owned init work is ≤ 5s.

    NFR-001 is wall-clock on a typical laptop, modulo ``bd init``'s own
    time. bd 1.2.1's embedded Dolt bootstrap plus agent-config scaffolding
    is ~3.8s serial and nearly the whole 5s budget, so a pytest-xdist
    worker under ``-n auto`` flakes this test without saying anything about
    Ortus (ortus-yln2). The assertion therefore excludes ``_bd_init``. The
    test is marked ``slow`` so CI's duration budget — which measures the
    whole test, including bd — does not treat that inherited cost as ours.
    """
    import ortus.commands.init as init_mod

    bd_seconds = 0.0
    real_bd_init = init_mod._bd_init

    def timed_bd_init(repo: Path, prefix: str | None) -> None:
        nonlocal bd_seconds
        started = time.monotonic()
        real_bd_init(repo, prefix)
        bd_seconds = time.monotonic() - started

    monkeypatch.setattr(init_mod, "_bd_init", timed_bd_init)
    target = tmp_path / "perf"
    t0 = time.monotonic()
    result = runner.invoke(app, ["init", str(target)])
    elapsed = time.monotonic() - t0
    assert result.exit_code == 0
    ortus_owned = elapsed - bd_seconds
    assert ortus_owned < 5.0, (
        f"ortus-owned init took {ortus_owned:.2f}s "
        f"(NFR-001 budget: 5s, excluding {bd_seconds:.2f}s of bd init)"
    )


def test_init_surfaces_bd_failure_clearly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ortus-btt3: when bd init exits non-zero its output streams straight to
    the operator, and ortus exits 1 with a clear `bd init failed (exit N)` line.
    """
    import ortus.commands.init as init_mod

    def fake_run(args, cwd, check):  # noqa: ARG001 — match subprocess.run signature
        # bd's own stderr would normally print here; the wrapper trusts that
        # the operator already saw it and just signals the failure.
        raise subprocess.CalledProcessError(returncode=7, cmd=args)

    monkeypatch.setattr(init_mod.subprocess, "run", fake_run)
    target = tmp_path / "doomed"
    result = runner.invoke(app, ["init", str(target)])
    assert result.exit_code == 1
    combined = result.stdout + result.stderr
    assert "bd init failed (exit 7)" in combined, combined


def test_init_passes_non_interactive_to_bd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ortus-btt3: `bd init` must be invoked with `--non-interactive` so it
    never blocks on hidden stdin prompts when ortus is run from a TTY.
    """
    import ortus.commands.init as init_mod

    captured: dict[str, list[str]] = {}

    def fake_run(args, cwd, check):  # noqa: ARG001 — match subprocess.run signature
        # Only the first call (bd init) is under test; init also shells out to
        # `bd remember` afterwards.
        captured.setdefault("args", list(args))
        # Pretend bd init succeeded so the rest of init can proceed.
        return subprocess.CompletedProcess(args=args, returncode=0)

    monkeypatch.setattr(init_mod.subprocess, "run", fake_run)
    target = tmp_path / "nonint"
    result = runner.invoke(app, ["init", str(target)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert captured["args"][:3] == ["bd", "init", "--non-interactive"], captured["args"]


def test_init_completes_with_closed_stdin(tmp_path: Path) -> None:
    """ortus-btt3: end-to-end check that `ortus init` completes promptly when
    invoked via a real subprocess with stdin=/dev/null (proxy for a terminal
    operator who never types anything). Regression guard for the bd-init prompt
    hang. Budget: 10s.
    """
    if shutil.which("ortus") is None:
        pytest.skip("ortus binary not on PATH")
    target = tmp_path / "closedstdin"
    t0 = time.monotonic()
    proc = subprocess.run(
        # `--codegraph off` and a concrete backend keep this about stdin: the
        # monkeypatched seams do not reach a real subprocess, and neither
        # indexing nor the host's installed backend CLIs are on trial.
        ["ortus", "init", str(target), "--codegraph", "off", "--backend", "claude"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=15,
    )
    elapsed = time.monotonic() - t0
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert elapsed < 10.0, f"ortus init took {elapsed:.2f}s with closed stdin (budget 10s)"
    assert (target / ".beads").is_dir()


def test_codegraph_bootstrap_indexes_pins_and_ignores(tmp_path: Path) -> None:
    """AC-2: init builds the index, pins the policy, ignores the dir."""
    target = tmp_path / "graphed"
    result = runner.invoke(app, ["init", str(target)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert (target / ".codegraph").is_dir()
    assert 'codegraph = "required"' in (target / ".ortusrc").read_text()
    assert ".codegraph/" in (target / ".gitignore").read_text()


def test_codegraph_bootstrap_skips_an_existing_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--force` over a repo that already has an index must not re-index."""
    import ortus.commands.init as init_mod

    target = tmp_path / "reindex"
    assert runner.invoke(app, ["init", str(target)]).exit_code == 0
    calls: list[Path] = []
    monkeypatch.setattr(init_mod, "_codegraph_index", lambda repo, **kw: calls.append(repo))
    result = runner.invoke(app, ["init", str(target), "--force"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert calls == []


def test_codegraph_index_failure_fails_init_before_rendering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-zero `codegraph init` leaves no half-written Ortus config."""
    import ortus.commands.init as init_mod

    def _boom(repo: Path, **kwargs: object) -> None:
        raise subprocess.CalledProcessError(returncode=3, cmd=["codegraph", "init"])

    monkeypatch.setattr(init_mod, "_codegraph_index", _boom)
    target = tmp_path / "badindex"
    result = runner.invoke(app, ["init", str(target)])
    assert result.exit_code == 1
    assert "codegraph init failed (exit 3)" in (result.stdout + result.stderr)
    assert not (target / ".ortusrc").exists()


# --- managed AGENTS.md / CLAUDE.md blocks ----------------------------------


def test_init_writes_both_managed_blocks(tmp_path: Path) -> None:
    """AC-1: a fresh repo gets an agents block and a pointer block."""
    target = tmp_path / "blocks"
    result = runner.invoke(app, ["init", str(target)])
    assert result.exit_code == 0, result.stdout + result.stderr
    for managed in MANAGED_FILES:
        block = read_block(target / managed.filename, managed.block)
        assert block is not None, managed.filename
        assert block.schema == BLOCK_SCHEMAS[managed.block]
    # `AGENTS.override.md` belongs to the repo; ortus never writes or reads it.
    assert not (target / "AGENTS.override.md").exists()


@pytest.mark.parametrize("backend", ["codex", "grok"])
def test_init_keeps_claude_md_for_every_backend(tmp_path: Path, backend: str) -> None:
    """The pointer file is repo instructions, not Claude backend configuration."""
    target = tmp_path / f"md-{backend}"
    result = runner.invoke(app, ["init", str(target), "--backend", backend])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert not (target / ".claude").exists()
    assert read_block(target / "CLAUDE.md", "pointer") is not None


def test_init_twice_leaves_the_agent_files_byte_identical(tmp_path: Path) -> None:
    """AC-1: the second pass is a no-op, not a rewrite."""
    target = tmp_path / "twice"
    assert runner.invoke(app, ["init", str(target)]).exit_code == 0
    before = {
        managed.filename: (target / managed.filename).read_bytes()
        for managed in MANAGED_FILES
    }
    result = runner.invoke(app, ["init", str(target), "--force"])
    assert result.exit_code == 0, result.stdout + result.stderr
    for name, content in before.items():
        assert (target / name).read_bytes() == content, name
    assert "already current" in (result.stdout + result.stderr)


def test_init_preserves_host_prose_around_the_block(tmp_path: Path) -> None:
    """AC-1: host bytes outside the markers survive byte-for-byte."""
    target = tmp_path / "hosted"
    target.mkdir()
    host = "# House rules\n\nNever force-push main.\n"
    (target / "AGENTS.md").write_text(host, encoding="utf-8")
    result = runner.invoke(app, ["init", str(target)])
    assert result.exit_code == 0, result.stdout + result.stderr
    text = (target / "AGENTS.md").read_text(encoding="utf-8")
    assert text.startswith(host)
    assert read_block(target / "AGENTS.md", "agents") is not None


def test_init_warns_when_host_prose_duplicates_block_headings(tmp_path: Path) -> None:
    """AC-1: a pre-marker render left above the block is named, never edited."""
    target = tmp_path / "premarker"
    target.mkdir()
    legacy = (
        "### Issue tracking with bd\n\nOld claim flow without bd close.\n\n"
        "### Session-close protocol\n\n1. git commit\n"
    )
    (target / "AGENTS.md").write_text(legacy, encoding="utf-8")
    result = runner.invoke(app, ["init", str(target)])
    assert result.exit_code == 0, result.stdout + result.stderr
    # The console wraps long lines, so asserts squash whitespace first.
    out = "".join((result.stdout + result.stderr).split())
    assert "duplicatesmanaged-blockheadings" in out
    assert "Issuetrackingwithbd" in out
    assert "Session-closeprotocol" in out
    # The warning is a pointer, not a migration: host bytes survive untouched.
    assert (target / "AGENTS.md").read_text(encoding="utf-8").startswith(legacy)


def test_init_refuses_a_gitignored_agent_file(tmp_path: Path) -> None:
    """AC-2 counterpart: a repo that hides AGENTS.md gets a refusal, not a block."""
    target = tmp_path / "ignored"
    target.mkdir()
    (target / ".gitignore").write_text("AGENTS.md\n", encoding="utf-8")
    result = runner.invoke(app, ["init", str(target)])
    assert result.exit_code == 1
    combined = result.stdout + result.stderr
    assert "gitignored" in combined, combined
    assert not (target / ".ortusrc").exists()


def test_init_aborts_on_malformed_markers_without_touching_the_file(
    tmp_path: Path,
) -> None:
    """An unbalanced marker is repaired by a human, never guessed at."""
    target = tmp_path / "malformed"
    target.mkdir()
    broken = "<!-- BEGIN ortus block=agents schema=1 -->\nhalf a block\n"
    (target / "AGENTS.md").write_text(broken, encoding="utf-8")
    result = runner.invoke(app, ["init", str(target)])
    assert result.exit_code == 1
    assert "malformed" in (result.stdout + result.stderr)
    # `bd init` appends its own section to AGENTS.md, so the guarantee is that
    # ortus left the broken region exactly as it found it.
    text = (target / "AGENTS.md").read_text(encoding="utf-8")
    assert text.startswith(broken)
    assert "END ortus" not in text


def test_ortusrc_round_trips_as_toml(tmp_path: Path) -> None:
    import sys
    if sys.version_info >= (3, 11):
        import tomllib
    else:
        import tomli as tomllib

    target = tmp_path / "fresh"
    runner.invoke(app, ["init", str(target), "--prefix", "abc", "--project-type", "go"])
    data = tomllib.loads((target / ".ortusrc").read_text())
    assert data["prefix"] == "abc"
    assert data["project_type"] == "go"


# --- the local backend -------------------------------------------------------
#
# `local` is opencode under its older name, so its provisioning is opencode's
# merged `opencode.json` plus a `[local]` table in `.ortusrc`; every other
# backend gets the same table as a commented reference block.


def test_init_local_writes_opencode_json_and_local_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--backend local` provisions exactly what `--backend opencode` does."""
    import ortus.commands.init as init_mod

    monkeypatch.setattr(init_mod, "probe_models", lambda config, **kwargs: ("m1",))
    target = tmp_path / "local"
    result = runner.invoke(
        app, ["init", str(target), "--backend", "local", "--local-model", "m1"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    data = json.loads((target / "opencode.json").read_text())
    assert list(data["provider"]) == ["ortuslocal"]
    assert data["provider"]["ortuslocal"]["models"] == {"m1": {}}
    assert not (target / ".claude").exists()
    assert not (target / ".grok").exists()
    combined = " ".join((result.stdout + result.stderr).split())
    assert "wrote .codex/config.toml" not in combined
    assert "created opencode.json provider ortuslocal" in combined
    ortusrc = tomllib.loads((target / ".ortusrc").read_text())
    assert ortusrc["backend"] == "local"
    assert ortusrc["local"] == {"model": "m1", "base_url": DEFAULT_LOCAL_BASE_URL}
    assert "local server reachable" in combined


def test_init_local_warns_when_the_server_is_down(tmp_path: Path) -> None:
    """A server that is down at bootstrap is a warning with the serving command."""
    target = tmp_path / "down"
    result = runner.invoke(
        app, ["init", str(target), "--backend", "local", "--local-model", "m1"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    combined = " ".join((result.stdout + result.stderr).split())
    assert "local server not reachable at http://127.0.0.1:8080/v1" in combined
    assert "start it with: llama-server" in combined
    assert tomllib.loads((target / ".ortusrc").read_text())["local"]["model"] == "m1"


def test_init_local_requires_local_model_lists_served_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a model, a reachable server's ids are the hint, one per line.

    The listing asks the base_url the pinned path would record, on the short
    init timeout, and replaces the curl line rather than adding to it. The
    init still fails naming the flag, before writing anything.
    """
    import ortus.commands.init as init_mod

    asked: list[tuple[str, str | None, float]] = []

    def served(base_url: str, *, api_key_env: str | None = None, timeout: float):
        asked.append((base_url, api_key_env, timeout))
        return ("qwen3:4b", "gemma4:26b")

    monkeypatch.setattr(init_mod, "list_served_models", served)
    target = tmp_path / "nomodel"
    result = runner.invoke(
        app,
        [
            "init", str(target),
            "--backend", "local",
            "--local-base-url", "http://127.0.0.1:11434/v1",
        ],
    )
    assert result.exit_code == 1
    combined = " ".join((result.stdout + result.stderr).split())
    assert "--backend local needs --local-model" in combined
    assert (
        "served models at http://127.0.0.1:11434/v1: - qwen3:4b - gemma4:26b"
        in combined
    )
    assert "curl" not in combined
    assert asked == [("http://127.0.0.1:11434/v1", None, init_mod.LOCAL_PROBE_TIMEOUT)]
    assert not target.exists()


def test_init_local_requires_local_model_lists_served_through_the_recorded_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A re-init on a table that names a key variable lists through that key."""
    import ortus.commands.init as init_mod

    target = tmp_path / "keyed"
    result = runner.invoke(
        app, ["init", str(target), "--backend", "local", "--local-model", "m1"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    ortusrc = target / ".ortusrc"
    ortusrc.write_text(
        ortusrc.read_text().replace('model = "m1"', 'api_key_env = "LLAMA_API_KEY"')
    )
    asked: list[tuple[str, str | None]] = []

    def served(base_url: str, *, api_key_env: str | None = None, timeout: float):
        asked.append((base_url, api_key_env))
        return ("m1",)

    monkeypatch.setattr(init_mod, "list_served_models", served)
    result = runner.invoke(app, ["init", str(target), "--force"])
    assert result.exit_code == 1
    combined = " ".join((result.stdout + result.stderr).split())
    assert "--backend local needs --local-model" in combined
    assert f"served models at {DEFAULT_LOCAL_BASE_URL}: - m1" in combined
    assert asked == [(DEFAULT_LOCAL_BASE_URL, "LLAMA_API_KEY")]


@pytest.mark.parametrize("kind", ["unreachable", "auth-demanded"])
def test_init_local_requires_local_model_unreachable_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """A server that is down or wants a key leaves the curl line, and no traceback."""
    import ortus.commands.init as init_mod

    def refuse(base_url: str, **kwargs: object) -> tuple[str, ...]:
        raise LocalServerError(kind, "no listing", "fix the server")

    monkeypatch.setattr(init_mod, "list_served_models", refuse)
    target = tmp_path / "nomodel"
    result = runner.invoke(app, ["init", str(target), "--backend", "local"])
    assert result.exit_code == 1
    assert not isinstance(result.exception, LocalServerError)
    combined = " ".join((result.stdout + result.stderr).split())
    assert "--backend local needs --local-model" in combined
    assert "list the served ids with: curl http://127.0.0.1:8080/v1/models" in combined
    assert "served models" not in combined
    assert "Traceback" not in combined
    assert not target.exists()


def test_init_local_requires_local_model_reports_no_models_served(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server that answers with nothing loaded says so, then the curl line."""
    import ortus.commands.init as init_mod

    monkeypatch.setattr(init_mod, "list_served_models", lambda base_url, **kwargs: ())
    target = tmp_path / "nomodel"
    result = runner.invoke(app, ["init", str(target), "--backend", "local"])
    assert result.exit_code == 1
    combined = " ".join((result.stdout + result.stderr).split())
    assert "--backend local needs --local-model" in combined
    assert (
        "no models served at http://127.0.0.1:8080/v1; "
        "list the served ids with: curl http://127.0.0.1:8080/v1/models"
    ) in combined
    assert not target.exists()


def test_init_all_renders_commented_local_block(tmp_path: Path) -> None:
    """`--backend all` keeps claude pinned and leaves [local] as a reference."""
    target = tmp_path / "everything"
    result = runner.invoke(app, ["init", str(target)])
    assert result.exit_code == 0, result.stdout + result.stderr
    for rel in (".claude/settings.json", ".codex/config.toml", ".grok/config.toml"):
        assert (target / rel).is_file(), rel
    text = (target / ".ortusrc").read_text()
    data = tomllib.loads(text)
    assert data["backend"] == "claude"
    assert "local" not in data
    assert "# [local]" in text
    assert "--jinja" in text
    # Rich wraps stderr at 80 columns under CliRunner; normalise before matching.
    combined = " ".join((result.stdout + result.stderr).split())
    assert "[local] left commented in .ortusrc" in combined
    assert "then ortus check --backend local" in combined


def test_init_force_preserves_local_table(tmp_path: Path) -> None:
    """Omitted local flags resolve to the recorded [local] table."""
    target = tmp_path / "keep"
    result = runner.invoke(
        app,
        [
            "init", str(target),
            "--backend", "local",
            "--local-model", "m1",
            "--local-base-url", "http://127.0.0.1:11434/v1",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    result = runner.invoke(app, ["init", str(target), "--force"])
    assert result.exit_code == 0, result.stdout + result.stderr
    data = tomllib.loads((target / ".ortusrc").read_text())
    assert data["backend"] == "local"
    assert data["local"] == {"model": "m1", "base_url": "http://127.0.0.1:11434/v1"}
    assert "re-detected" not in result.stdout + result.stderr


def test_init_force_local_model_override_prints_change_line(tmp_path: Path) -> None:
    """An explicit --local-model over a recorded one is a visible change."""
    target = tmp_path / "swap"
    result = runner.invoke(
        app, ["init", str(target), "--backend", "local", "--local-model", "m1"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    result = runner.invoke(app, ["init", str(target), "--force", "--local-model", "m2"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "re-detected local.model: m1 -> m2" in result.stdout + result.stderr
    assert tomllib.loads((target / ".ortusrc").read_text())["local"]["model"] == "m2"


def test_init_codex_notes_and_ignores_local_model(tmp_path: Path) -> None:
    """The local flags mean nothing to another backend, and say so."""
    target = tmp_path / "codex"
    result = runner.invoke(
        app, ["init", str(target), "--backend", "codex", "--local-model", "m1"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "--local-model applies only to --backend local" in (
        result.stdout + result.stderr
    )
    assert "local" not in tomllib.loads((target / ".ortusrc").read_text())


def test_init_force_rejects_an_invalid_recorded_local_table(tmp_path: Path) -> None:
    """A recorded table that breaks the config rules is an error, never re-rendered."""
    target = tmp_path / "broken"
    result = runner.invoke(
        app, ["init", str(target), "--backend", "local", "--local-model", "m1"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    ortusrc = target / ".ortusrc"
    broken = ortusrc.read_text().replace('model = "m1"', 'model = "has space"')
    ortusrc.write_text(broken)
    result = runner.invoke(app, ["init", str(target), "--force"])
    assert result.exit_code == 1
    assert "invalid local.model" in result.stdout + result.stderr
    assert ortusrc.read_text() == broken


# --- the opencode backend ----------------------------------------------------
#
# `opencode` reads the same `[local]` table as `local` and adds one project
# file of its own: `opencode.json`, which init merges by key so an operator's
# own providers, MCP servers, and settings in that file survive a re-init.


def test_init_opencode_writes_and_preserves_opencode_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--backend opencode` registers the served model without clobbering the file."""
    import ortus.commands.init as init_mod

    monkeypatch.setattr(init_mod, "probe_models", lambda config, **kwargs: ("m1",))
    target = tmp_path / "opencode"
    target.mkdir()
    host = {
        "theme": "dark",
        "provider": {"mine": {"npm": "@ai-sdk/anthropic", "models": {"x": {}}}},
        "mcp": {
            "codegraph": {
                "type": "local",
                "command": ["codegraph", "serve", "--mcp"],
                "enabled": True,
            }
        },
    }
    config = target / "opencode.json"
    config.write_text(json.dumps(host, indent=2) + "\n")
    result = runner.invoke(
        app,
        [
            "init", str(target),
            "--backend", "opencode",
            "--local-model", "m1",
            "--local-base-url", "http://127.0.0.1:11434/v1/",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    data = json.loads(config.read_text())
    assert data["theme"] == "dark"
    assert data["mcp"] == host["mcp"]
    assert data["provider"]["mine"] == host["provider"]["mine"]
    assert data["provider"]["ortuslocal"] == {
        "npm": "@ai-sdk/openai-compatible",
        "name": "Ortus local model",
        "options": {"baseURL": "http://127.0.0.1:11434/v1"},
        "models": {"m1": {}},
    }
    assert not (target / ".claude").exists()
    ortusrc = tomllib.loads((target / ".ortusrc").read_text())
    assert ortusrc["backend"] == "opencode"
    assert ortusrc["local"] == {"model": "m1", "base_url": "http://127.0.0.1:11434/v1"}
    combined = " ".join((result.stdout + result.stderr).split())
    # bd init scaffolds a `.codex/` of its own, so the filesystem cannot say
    # which backend ortus rendered; its own `wrote` lines can.
    assert "wrote .ortusrc" in combined
    assert "wrote .codex/config.toml" not in combined
    assert "wrote .grok/config.toml" not in combined
    assert "wrote .claude/settings.json" not in combined
    assert "updated opencode.json provider ortuslocal" in combined
    assert "local server reachable" in combined

    # A forced re-init with a new model rewrites only the Ortus provider.
    result = runner.invoke(app, ["init", str(target), "--force", "--local-model", "m2"])
    assert result.exit_code == 0, result.stdout + result.stderr
    data = json.loads(config.read_text())
    assert data["provider"]["ortuslocal"]["models"] == {"m2": {}}
    assert data["provider"]["mine"] == host["provider"]["mine"]
    assert data["theme"] == "dark"

    # A re-init with nothing new leaves the bytes alone.
    before = config.read_bytes()
    result = runner.invoke(app, ["init", str(target), "--force"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert config.read_bytes() == before
    combined = " ".join((result.stdout + result.stderr).split())
    assert "opencode.json provider ortuslocal already current" in combined


def test_init_opencode_creates_opencode_json_with_the_schema(tmp_path: Path) -> None:
    """A fresh dir gets a file holding the schema reference and the one provider."""
    target = tmp_path / "fresh"
    result = runner.invoke(
        app, ["init", str(target), "--backend", "opencode", "--local-model", "m1"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    data = json.loads((target / "opencode.json").read_text())
    assert data["$schema"] == "https://opencode.ai/config.json"
    assert list(data["provider"]) == ["ortuslocal"]
    assert data["provider"]["ortuslocal"]["options"] == {
        "baseURL": DEFAULT_LOCAL_BASE_URL
    }
    combined = " ".join((result.stdout + result.stderr).split())
    assert "created opencode.json provider ortuslocal" in combined
    # The default fake server is down: still a warning, never a failed init.
    assert "local server not reachable" in combined


def test_init_opencode_requires_local_model_lists_served_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The opencode name takes the same unpinned listing path as local."""
    import ortus.commands.init as init_mod

    monkeypatch.setattr(
        init_mod, "list_served_models", lambda base_url, **kwargs: ("m1",)
    )
    target = tmp_path / "nomodel"
    result = runner.invoke(app, ["init", str(target), "--backend", "opencode"])
    assert result.exit_code == 1
    combined = " ".join((result.stdout + result.stderr).split())
    assert "--backend opencode needs --local-model" in combined
    assert "served models at http://127.0.0.1:8080/v1: - m1" in combined
    assert not target.exists()


def test_init_opencode_refuses_a_malformed_opencode_json(tmp_path: Path) -> None:
    """A file the merge cannot read fails before bd init or any render."""
    target = tmp_path / "broken"
    target.mkdir()
    config = target / "opencode.json"
    config.write_text("{ not json\n")
    result = runner.invoke(
        app, ["init", str(target), "--backend", "opencode", "--local-model", "m1"]
    )
    assert result.exit_code == 1
    combined = " ".join((result.stdout + result.stderr).split())
    assert "is not valid JSON" in combined
    assert "repair opencode.json by hand" in combined
    assert config.read_text() == "{ not json\n"
    assert not (target / ".beads").exists()
    assert not (target / ".ortusrc").exists()


def test_init_opencode_mcp_entry_is_written_beside_the_provider(
    tmp_path: Path,
) -> None:
    """A fresh opencode repo registers CodeGraph the way opencode runs it: `mcp.codegraph`."""
    target = tmp_path / "fresh"
    result = runner.invoke(
        app, ["init", str(target), "--backend", "opencode", "--local-model", "m1"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    raw = (target / "opencode.json").read_text()
    data = json.loads(raw)
    assert list(data) == ["$schema", "provider", "mcp"]
    assert list(data["provider"]) == ["ortuslocal"]
    assert data["mcp"] == {
        "codegraph": {
            "type": "local",
            "command": ["codegraph", "serve", "--mcp"],
            "enabled": True,
        }
    }
    # The bare executable, never the host's resolved path: the file travels.
    assert "/usr/bin/codegraph" not in raw
    combined = " ".join((result.stdout + result.stderr).split())
    assert "created opencode.json provider ortuslocal" in combined
    assert "created opencode.json mcp codegraph" in combined


def test_init_opencode_mcp_entry_rewrites_drift_and_keeps_foreign_servers(
    tmp_path: Path,
) -> None:
    """An operator's other servers survive; their `codegraph` is Ortus's key to fix."""
    target = tmp_path / "drifted"
    target.mkdir()
    host = {
        "provider": {"mine": {"npm": "@ai-sdk/anthropic", "models": {"x": {}}}},
        "mcp": {
            "github": {"type": "remote", "url": "https://example.invalid/mcp"},
            "codegraph": {
                "type": "local",
                "command": ["/opt/codegraph", "serve", "--mcp"],
                "enabled": False,
            },
        },
    }
    config = target / "opencode.json"
    config.write_text(json.dumps(host, indent=2) + "\n")
    result = runner.invoke(
        app, ["init", str(target), "--backend", "opencode", "--local-model", "m1"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    data = json.loads(config.read_text())
    assert data["mcp"]["github"] == host["mcp"]["github"]
    assert data["mcp"]["codegraph"] == {
        "type": "local",
        "command": ["codegraph", "serve", "--mcp"],
        "enabled": True,
    }
    assert list(data["mcp"]) == ["github", "codegraph"]
    assert data["provider"]["mine"] == host["provider"]["mine"]
    combined = " ".join((result.stdout + result.stderr).split())
    assert "updated opencode.json provider ortuslocal" in combined
    assert "updated opencode.json mcp codegraph" in combined

    # `--force` re-init: the entry is current, so the bytes are left alone.
    before = config.read_bytes()
    result = runner.invoke(app, ["init", str(target), "--force"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert config.read_bytes() == before
    combined = " ".join((result.stdout + result.stderr).split())
    assert "opencode.json mcp codegraph already current" in combined


def test_init_opencode_mcp_entry_is_skipped_under_codegraph_off(
    tmp_path: Path,
) -> None:
    """`--codegraph off` registers the provider only; no server no worker will use."""
    target = tmp_path / "off"
    result = runner.invoke(
        app,
        [
            "init", str(target),
            "--backend", "opencode",
            "--local-model", "m1",
            "--codegraph", "off",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    data = json.loads((target / "opencode.json").read_text())
    assert "mcp" not in data
    assert list(data["provider"]) == ["ortuslocal"]
    combined = " ".join((result.stdout + result.stderr).split())
    assert "created opencode.json provider ortuslocal" in combined
    assert "mcp codegraph" not in combined


def test_init_all_leaves_opencode_json_to_a_pinned_init(tmp_path: Path) -> None:
    """`--backend all` has no model to register, so it says how to pin one."""
    target = tmp_path / "everything"
    result = runner.invoke(app, ["init", str(target)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert not (target / "opencode.json").exists()
    combined = " ".join((result.stdout + result.stderr).split())
    assert "opencode: opencode.json not written" in combined
    assert "ortus init --backend opencode --local-model <id>" in combined
    assert "then ortus check --backend opencode" in combined
