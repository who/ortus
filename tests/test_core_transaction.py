"""Journal load compatibility after f2he.3 dropped fingerprint ownership."""

from __future__ import annotations

import json
from pathlib import Path

from ortus.core.transaction import JournalStore


def test_loads_historical_fingerprints(tmp_path: Path) -> None:
    """AC-3: a journal that still has handoff_fingerprints loads without error."""
    store = JournalStore(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 2,
        "issue_id": "hist-1",
        "base_head": "a" * 40,
        "baseline_paths": ["src/x.py"],
        "baseline_fingerprints": {"src/x.py": "b" * 64},
        "handoff_fingerprints": {"src/x.py": "c" * 64},
        "handoff_paths": ["src/x.py"],
        "candidate_paths": ["src/x.py"],
        "phase": "implementation",
    }
    store.path.write_text(json.dumps(payload), encoding="utf-8")
    journal = store.load()
    assert journal is not None
    assert journal.issue_id == "hist-1"
    assert journal.handoff_fingerprints["src/x.py"] == "c" * 64
