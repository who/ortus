"""Focused cross-backend verifier isolation contract."""

from __future__ import annotations

from pathlib import Path

import pytest

from ortus.core.agent import CodexRunner
from ortus.core.claude import ClaudeRunner, _readonly_wrapper


def test_readonly_verifier_postures_are_technically_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex_argv = CodexRunner().build_argv("verify", readonly=True)
    assert codex_argv[codex_argv.index("--sandbox") + 1] == "read-only"

    claude_argv = ClaudeRunner().build_argv("verify", readonly=True)
    assert "--disallowedTools" in claude_argv
    monkeypatch.setattr("ortus.core.claude.platform.system", lambda: "Linux")
    wrapped = _readonly_wrapper(claude_argv, tmp_path)
    assert wrapped[:4] == ["bwrap", "--ro-bind", "/", "/"]
    assert ["--chdir", str(tmp_path.resolve())] == wrapped[
        wrapped.index("--chdir") : wrapped.index("--chdir") + 2
    ]
