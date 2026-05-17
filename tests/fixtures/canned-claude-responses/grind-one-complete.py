#!/usr/bin/env python3
"""Canned claude response: completes exactly one issue.

Claims the first ready non-epic issue, makes a dummy edit, closes the
issue. Runs in the cwd that ClaudeRunner set (= the repo root). Real
claude would do the implementation; this canned shim represents the
close-at-end behavior on a single concrete task.

Rewritten in Python for Windows compat. See ortus-f4bu.
"""

from __future__ import annotations

import json
import subprocess


def main() -> int:
    print('{"type":"system","subtype":"start","session_id":"one-complete"}', flush=True)

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
