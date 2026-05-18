#!/usr/bin/env python3
"""Canned claude scenario: triage context-phase writes one envelope per human-flagged issue.

The new triage flow (ortus-sr0b) splits work between claude (context
gathering, writes `logs/triage-envelopes.jsonl`) and the Python wrapper
(operator prompting + bd writes). This shim plays the claude side:
discovers human-flagged issues via `bd human list --json` in the cwd
(which ClaudeRunner sets to the repo root) and emits one envelope per
issue with a recommended disposition.

Rewritten in Python for Windows compat. See ortus-f4bu.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


def main() -> int:
    print('{"type":"system","subtype":"start","session_id":"triage"}', flush=True)

    proc = subprocess.run(
        ["bd", "human", "list", "--json"],
        check=False,
        capture_output=True,
        text=True,
    )
    try:
        issues = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        issues = []

    envelopes_path = Path(os.getcwd()) / "logs" / "triage-envelopes.jsonl"
    envelopes_path.parent.mkdir(parents=True, exist_ok=True)
    with envelopes_path.open("w", encoding="utf-8") as fh:
        for issue in issues:
            envelope = {
                "issue_id": issue.get("id"),
                "title": issue.get("title", ""),
                "priority": issue.get("priority", 2),
                "status": issue.get("status", "open"),
                "context_summary": "canned triage envelope for smoke test",
                "recommended_disposition": "skip",
                "rationale": "shim does not make real recommendations",
            }
            fh.write(json.dumps(envelope) + "\n")

    print(
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": f"Wrote {len(issues)} envelope(s)."},
            }
        ),
        flush=True,
    )
    print('{"type":"system","subtype":"end"}', flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
