"""The bd template masters stay byte-identical from build to last copy.

Two guarantees, one for each half of the flake in ortus-we44: git never
performs housekeeping against a template, and the pristine guard reads a stray
lock file as housekeeping rather than as a modified template.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests import conftest
from tests.conftest import BdWorkspace

pytestmark = pytest.mark.integration


def _config(repo: Path, key: str) -> str:
    """Read one git config value. A read never dirties the template."""
    return subprocess.run(
        ["git", "config", "--get", key],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_template_disables_git_maintenance(bd_template_bare: BdWorkspace) -> None:
    """Auto gc and maintenance are off in the template's own config.

    All three keys, because a git version may honour one and ignore another;
    together they cover both the detached gc and the maintenance hook that
    wrote `.git/objects/maintenance.lock` into the master on CI.
    """
    repo = bd_template_bare.path
    assert _config(repo, "gc.auto") == "0"
    assert _config(repo, "gc.autoDetach") == "false"
    assert _config(repo, "maintenance.auto") == "false"


def test_guard_ignores_git_lock_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A planted lock passes the guard; a content change still fails it.

    Driven against a stand-in template registered under its own kind, so the
    session's real masters are never written to in order to test the guard.
    """
    template = tmp_path / "template"
    (template / ".git" / "objects").mkdir(parents=True)
    (template / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    monkeypatch.setitem(conftest._TEMPLATES, "stand-in", BdWorkspace(template, ()))
    monkeypatch.setitem(
        conftest._TEMPLATE_DIGESTS, "stand-in", conftest._digests(template)
    )

    (template / ".git" / "objects" / "maintenance.lock").write_text("", encoding="utf-8")
    conftest._assert_template_pristine("stand-in")

    (template / "tracked.txt").write_text("mutated\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="was modified after it was built"):
        conftest._assert_template_pristine("stand-in")
