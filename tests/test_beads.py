"""Tests for the beads tracker ``run`` interface."""

from __future__ import annotations

from pathlib import Path

import pytest

from ortus.core.bd import BeadsTracker
from tests.conftest import copy_bd_workspace

pytestmark = pytest.mark.integration


def test_beads_tracker_run_lists_ready_on_bare_workspace(tmp_path: Path) -> None:
    workspace = copy_bd_workspace(tmp_path / "repo", "bare")
    stdout, parsed = BeadsTracker(workspace.path).run("ready", "--json", parse_json=True)
    assert parsed == []
    assert stdout.strip() == "[]"
