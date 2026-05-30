#!/usr/bin/env python3
"""Canned claude response: completes exactly one issue.

Closes the issue grind told it to work, makes no real edit. Runs in the
cwd that ClaudeRunner set (= the repo root). Real claude would do the
implementation; this canned shim represents the close-at-end behavior on
a single concrete task.

Issue selection mirrors the two grind prompt shapes:
  - default (harness-select, ortus-xo1u): the per-iteration /goal prompt
    NAMES the issue ("Work bd issue <id>.") and the harness has already
    claimed it in_progress — so it no longer shows in `bd ready`. We parse
    the id straight out of the prompt (our argv) and close THAT, exactly as
    real claude is instructed to.
  - legacy (--condition): no id in the prompt; fall back to claiming the
    first ready non-epic issue ourselves, as the old worker did.

Rewritten in Python for Windows compat. See ortus-f4bu.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys


def _id_from_prompt() -> str | None:
    """Extract the harness-injected issue id from the /goal prompt (argv).

    ClaudeRunner invokes us as `claude -p <prompt> ...`; the work-issue
    template opens with "Work bd issue <id>." Return that id, or None when the
    prompt doesn't name one (the legacy worker-selects condition)."""
    prompt = " ".join(sys.argv[1:])
    # bd ids embed dots (e.g. seed-bk0.1), so grab the whole non-space token
    # and strip only the trailing sentence period the template puts after it.
    m = re.search(r"Work bd issue\s+(\S+)", prompt)
    return m.group(1).rstrip(".") if m else None


def main() -> int:
    print('{"type":"system","subtype":"start","session_id":"one-complete"}', flush=True)

    first = _id_from_prompt()
    if first is None:
        proc = subprocess.run(
            ["bd", "ready", "--json"],
            check=True,
            capture_output=True,
            text=True,
        )
        ready = json.loads(proc.stdout)
        first = next((i["id"] for i in ready if i.get("issue_type") != "epic"), None)

    if first:
        subprocess.run(
            ["bd", "close", first, "--reason", "canned grind-one-complete shim closed this"],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        print(
            json.dumps(
                {"type": "assistant", "message": {"content": f"Closed {first}."}}
            ),
            flush=True,
        )

    print('{"type":"system","subtype":"end"}', flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
