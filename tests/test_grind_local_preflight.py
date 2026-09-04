"""Hermetic tests for the local-backend grind preflight.

`ortus grind --backend local` proves the served model is reachable right after
the sandbox smoke test, before the flock and before any bd read. A dead
endpoint therefore costs the serving command on stderr and exit 1, never a
claim on a worker that could only hang. The preflight is the cheap `/models`
request; `ortus check` owns the fuller row set.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.commands import grind as grind_mod
from ortus.core.codegraph import CodeGraphAdapter
from ortus.core.local_backend import LocalConfig, LocalServerError, serving_hint
from tests.test_grind import (
    _bd_repo,
    _CloseWithoutClaimsRunner,
    _create_ready_issue,
    _fake_sandbox,
    _fixture_repo,
    _grind_log,
    _issue,
    _plain,
)

runner = CliRunner()
pytestmark = pytest.mark.integration

_BASE_URL = "http://127.0.0.1:8080/v1"
_MODEL = "qwen3-coder"
_LOCAL_TABLE = f'[local]\nbase_url = "{_BASE_URL}"\nmodel = "{_MODEL}"\n'
_LOCAL_CONFIG = LocalConfig(base_url=_BASE_URL, model=_MODEL)
_DISPLAY = f"local (127.0.0.1:8080) model={_MODEL}"


def _server_down() -> LocalServerError:
    return LocalServerError(
        "unreachable",
        f"local server unreachable at {_BASE_URL}: Connection refused",
        serving_hint(_LOCAL_CONFIG),
    )


def _patch_probe(
    monkeypatch: pytest.MonkeyPatch, *, raises: LocalServerError | None = None
) -> list[LocalConfig]:
    """Replace the `/models` probe with a recorder that returns or raises."""
    calls: list[LocalConfig] = []

    def fake_probe(config: LocalConfig, **kwargs: object) -> tuple[str, ...]:
        calls.append(config)
        if raises is not None:
            raise raises
        return (config.model,)

    monkeypatch.setattr(grind_mod, "probe_models", fake_probe)
    return calls


def _install_opencode(home: Path) -> Path:
    """A stand-in opencode where the installer puts it: off PATH, found by the fallback."""
    binary = home / ".opencode" / "bin" / "opencode"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    return binary


def _isolate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, opencode: bool = True
) -> None:
    """No host sandbox, no ORTUS_BACKEND, no user-level .ortusrc, and the fake
    home's own opencode install unless `opencode` is false.

    The binary preflight must pass on a host with no opencode on PATH, and
    the developer's real `~/.opencode` must never answer for it, so the fake
    home carries a stand-in at the install path.
    """
    _fake_sandbox(monkeypatch)
    monkeypatch.delenv("ORTUS_BACKEND", raising=False)
    home = tmp_path / "fake-home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    if opencode:
        _install_opencode(home)


def _squash(text: str) -> str:
    """Console text with Rich wrapping collapsed, for phrase matching."""
    return " ".join(_plain(text).split())


class _NeverRuns:
    extra_env: dict[str, str] = {}

    def run(self, prompt: str, **kwargs: object) -> int:
        raise AssertionError("a failed preflight must not spawn any worker")


class _CountingCloseRunner(_CloseWithoutClaimsRunner):
    def __init__(self, host: Path) -> None:
        super().__init__(host)
        self.launches = 0

    def run(self, prompt: str, **kwargs: object) -> int:
        self.launches += 1
        return super().run(prompt, **kwargs)


@pytest.mark.slow
def test_server_down_aborts_before_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-1: an unreachable server exits 1 with the serving hint; nothing is claimed."""
    repo = _bd_repo(tmp_path, "local-down")
    (repo / ".ortusrc").write_text(_LOCAL_TABLE)
    issue_id = _create_ready_issue(repo, "stay open")
    _isolate(monkeypatch, tmp_path)
    calls = _patch_probe(monkeypatch, raises=_server_down())
    monkeypatch.setattr(grind_mod, "_make_runner", lambda *a, **k: _NeverRuns())

    result = runner.invoke(
        app,
        [
            "grind",
            str(repo),
            "--backend",
            "local",
            "--tasks",
            "1",
            "--idle-sleep",
            "0",
        ],
    )

    assert result.exit_code == 1, result.stdout + result.stderr
    combined = _squash(result.stdout + result.stderr)
    assert "local backend: local server unreachable" in combined
    assert "Connection refused" in combined
    assert "llama-server" in combined
    assert "--jinja" in combined
    assert calls == [_LOCAL_CONFIG]
    shown = _issue(repo, issue_id)
    assert shown["status"] == "open"
    assert not shown.get("assignee")
    assert _grind_log(repo) == "", "the preflight must abort before the log and flock"


@pytest.mark.slow
def test_reachable_launches_local_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-2: a reachable server logs the endpoint and hands the loop one local runner."""
    repo = _bd_repo(tmp_path, "local-up")
    (repo / ".ortusrc").write_text(_LOCAL_TABLE)
    issue_id = _create_ready_issue(repo, "close me")
    _isolate(monkeypatch, tmp_path)
    calls = _patch_probe(monkeypatch)
    made: list[tuple[tuple[object, ...], dict[str, object]]] = []
    fake = _CountingCloseRunner(repo)

    def make_runner(*args: object, **kwargs: object) -> _CountingCloseRunner:
        made.append((args, kwargs))
        return fake

    monkeypatch.setattr(grind_mod, "_make_runner", make_runner)

    result = runner.invoke(
        app,
        [
            "grind",
            str(repo),
            "--backend",
            "local",
            "--tasks",
            "1",
            "--idle-sleep",
            "0",
        ],
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    assert calls == [_LOCAL_CONFIG]
    assert fake.launches == 1
    assert made == [(("local",), {"repo": repo.resolve()})]
    assert _issue(repo, issue_id)["status"] == "closed"
    combined = _squash(result.stdout + result.stderr)
    assert f"local server reachable: {_DISPLAY}" in combined


@pytest.mark.slow
def test_opencode_missing_binary_aborts_before_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-2: no opencode anywhere exits 1 naming both fixes; no traceback, no claim.

    The executable is resolved before the server is asked, so a dead server
    cannot mask a missing install, and the abort names `local` as the
    backend that was launched. The installer's path is looked up under the
    fake home, so the developer's own install cannot answer.
    """
    repo = _bd_repo(tmp_path, "local-no-binary")
    (repo / ".ortusrc").write_text(_LOCAL_TABLE)
    issue_id = _create_ready_issue(repo, "stay open")
    _isolate(monkeypatch, tmp_path, opencode=False)
    real_which = shutil.which
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name, *a, **k: (
            None if name == "opencode" else real_which(name, *a, **k)
        ),
    )
    calls = _patch_probe(monkeypatch)
    monkeypatch.setattr(grind_mod, "_make_runner", lambda *a, **k: _NeverRuns())

    result = runner.invoke(
        app,
        [
            "grind",
            str(repo),
            "--backend",
            "local",
            "--tasks",
            "1",
            "--idle-sleep",
            "0",
        ],
    )

    assert result.exit_code == 1, result.stdout + result.stderr
    # A clean exit, not an exception the runner caught for us.
    assert result.exception is None or isinstance(result.exception, SystemExit)
    text = _plain(result.stdout + result.stderr)
    combined = " ".join(text.split())
    assert "local backend: opencode CLI not on PATH" in combined
    assert "install opencode" in combined
    assert "Traceback" not in combined and "FileNotFoundError" not in combined
    # Rich may fold a long path mid-word, so compare with all whitespace gone.
    install_dir = tmp_path / "fake-home" / ".opencode" / "bin"
    assert f"add{install_dir}toPATH" in "".join(text.split())
    assert calls == [], "the executable is resolved before the server is asked"
    shown = _issue(repo, issue_id)
    assert shown["status"] == "open"
    assert not shown.get("assignee")
    assert _grind_log(repo) == "", "the preflight must abort before the log and flock"


def test_dry_run_prints_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-3: `--dry-run` shows the host and model and never contacts the server."""
    repo = _fixture_repo(tmp_path)
    (repo / ".ortusrc").write_text(_LOCAL_TABLE)
    _isolate(monkeypatch, tmp_path)
    calls = _patch_probe(monkeypatch, raises=_server_down())

    result = runner.invoke(app, ["grind", str(repo), "--dry-run", "--backend", "local"])

    assert result.exit_code == 0, result.stdout + result.stderr
    plain = _plain(result.stdout)
    assert "backend:        local" in plain
    assert f"local:          {_DISPLAY}" in plain
    assert calls == []
    prompt = plain.split("--- per-iteration prompt ---", 1)[1]
    assert not prompt.lstrip().startswith("/goal")


def test_missing_local_table_exits_before_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--backend local` with no `[local]` table names local.model and never probes."""
    repo = _fixture_repo(tmp_path)
    _isolate(monkeypatch, tmp_path)
    calls = _patch_probe(monkeypatch)

    result = runner.invoke(app, ["grind", str(repo), "--backend", "local"])

    assert result.exit_code == 1, result.stdout + result.stderr
    assert "local.model" in _squash(result.stdout + result.stderr)
    assert calls == []


@pytest.mark.slow
def test_other_backends_skip_local_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-4: a codex grind never probes, even with a `[local]` table present."""
    repo = _bd_repo(tmp_path, "codex-no-probe")
    (repo / ".ortusrc").write_text('backend = "codex"\n' + _LOCAL_TABLE)
    issue_id = _create_ready_issue(repo, "codex closes")
    _isolate(monkeypatch, tmp_path)
    calls = _patch_probe(monkeypatch, raises=_server_down())
    monkeypatch.setattr(
        grind_mod, "_make_runner", lambda *a, **k: _CloseWithoutClaimsRunner(repo)
    )

    result = runner.invoke(
        app,
        [
            "grind",
            str(repo),
            "--backend",
            "codex",
            "--tasks",
            "1",
            "--idle-sleep",
            "0",
        ],
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    assert calls == []
    assert _issue(repo, issue_id)["status"] == "closed"
    combined = _plain(result.stdout + result.stderr)
    assert "local server reachable" not in combined
    assert "local backend:" not in combined


# --- opencode -----------------------------------------------------------------
#
# The same `[local]` table drives the opencode backend, so the same `/models`
# request is its preflight. What differs: the abort names the backend the
# operator launched, and CodeGraph policy reaches the file-backed registration,
# because opencode runs the MCP server itself from `opencode.json` and a worker
# without that entry could only fail its handshake after a claim. Under
# `required` that stops the run at the probe, before the server is asked.

_OPENCODE_ARGS = ["--backend", "opencode", "--tasks", "1", "--idle-sleep", "0"]
_OPENCODE_MCP = {
    "mcp": {
        "codegraph": {
            "type": "local",
            "command": ["codegraph", "serve", "--mcp"],
            "enabled": True,
        }
    }
}


@pytest.mark.slow
def test_opencode_server_down_aborts_before_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-1: an unreachable server exits 1 naming opencode, with the serving hint; nothing is claimed."""
    repo = _bd_repo(tmp_path, "opencode-down")
    (repo / ".ortusrc").write_text(_LOCAL_TABLE)
    issue_id = _create_ready_issue(repo, "stay open")
    _isolate(monkeypatch, tmp_path)
    calls = _patch_probe(monkeypatch, raises=_server_down())
    monkeypatch.setattr(grind_mod, "_make_runner", lambda *a, **k: _NeverRuns())

    result = runner.invoke(app, ["grind", str(repo), *_OPENCODE_ARGS])

    assert result.exit_code == 1, result.stdout + result.stderr
    combined = _squash(result.stdout + result.stderr)
    assert "opencode backend: local server unreachable" in combined
    assert "local backend:" not in combined
    assert "Connection refused" in combined
    assert "llama-server" in combined
    assert "--jinja" in combined
    assert calls == [_LOCAL_CONFIG]
    shown = _issue(repo, issue_id)
    assert shown["status"] == "open"
    assert not shown.get("assignee")
    assert _grind_log(repo) == "", "the preflight must abort before the log and flock"


@pytest.mark.slow
def test_opencode_reachable_launches_opencode_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reachable server logs the endpoint and hands the loop one opencode runner."""
    repo = _bd_repo(tmp_path, "opencode-up")
    (repo / ".ortusrc").write_text(_LOCAL_TABLE)
    issue_id = _create_ready_issue(repo, "close me")
    _isolate(monkeypatch, tmp_path)
    calls = _patch_probe(monkeypatch)
    made: list[tuple[tuple[object, ...], dict[str, object]]] = []
    fake = _CountingCloseRunner(repo)

    def make_runner(*args: object, **kwargs: object) -> _CountingCloseRunner:
        made.append((args, kwargs))
        return fake

    monkeypatch.setattr(grind_mod, "_make_runner", make_runner)

    result = runner.invoke(app, ["grind", str(repo), *_OPENCODE_ARGS])

    assert result.exit_code == 0, result.stdout + result.stderr
    assert calls == [_LOCAL_CONFIG]
    assert fake.launches == 1
    assert made == [(("opencode",), {"repo": repo.resolve()})]
    assert _issue(repo, issue_id)["status"] == "closed"
    combined = _squash(result.stdout + result.stderr)
    assert f"local server reachable: {_DISPLAY}" in combined


def test_opencode_required_without_mcp_aborts_before_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under `required`, no codegraph entry in `opencode.json` stops the run at the CodeGraph probe.

    The index and CLI are present, so the file-backed registration is the
    only gap. The server is never asked anything and no claim is made. With
    the entry `ortus init` writes in place, the same run reaches the server
    preflight instead.
    """
    repo = _bd_repo(tmp_path, "opencode-unregistered")
    (repo / ".ortusrc").write_text('codegraph = "required"\n' + _LOCAL_TABLE)
    (repo / ".codegraph").mkdir()
    issue_id = _create_ready_issue(repo, "stay open")
    _isolate(monkeypatch, tmp_path)
    calls = _patch_probe(monkeypatch)
    real_which = shutil.which
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name, *a, **k: (
            "/bin/codegraph" if name == "codegraph" else real_which(name, *a, **k)
        ),
    )
    rpcs: list[object] = []
    monkeypatch.setattr(
        CodeGraphAdapter,
        "mcp_tools_call",
        lambda self, *a, **k: rpcs.append(a) or {"content": []},
    )
    monkeypatch.setattr(grind_mod, "_make_runner", lambda *a, **k: _NeverRuns())

    result = runner.invoke(app, ["grind", str(repo), *_OPENCODE_ARGS])

    assert result.exit_code == 1, result.stdout + result.stderr
    combined = _squash(result.stdout + result.stderr)
    assert "CodeGraph required but unavailable" in combined
    assert "opencode.json does not register a codegraph MCP server" in combined
    assert calls == [] and rpcs == []
    assert _issue(repo, issue_id)["status"] == "open"
    assert _grind_log(repo) == ""

    (repo / "opencode.json").write_text(json.dumps(_OPENCODE_MCP))
    calls = _patch_probe(monkeypatch, raises=_server_down())

    result = runner.invoke(app, ["grind", str(repo), *_OPENCODE_ARGS])

    assert result.exit_code == 1, result.stdout + result.stderr
    combined = _squash(result.stdout + result.stderr)
    assert "CodeGraph required but unavailable" not in combined
    assert "opencode backend: local server unreachable" in combined
    assert calls == [_LOCAL_CONFIG] and len(rpcs) == 1
    assert _issue(repo, issue_id)["status"] == "open"
    assert _grind_log(repo) == ""
