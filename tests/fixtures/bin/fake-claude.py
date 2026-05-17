#!/usr/bin/env python3
"""Fake claude shim used by tests/test_core_claude.py and friends.

Emits a few stream-json-ish lines then exits. Honors FAKE_CLAUDE_SLEEP
(seconds to sleep before exit, lets SIGINT propagation be tested) and
FAKE_CLAUDE_EXIT (custom exit code, default 0).

Behavior mirrors the historical POSIX-shell version; rewritten in Python
so it runs on Windows too (no shebang dependency). See ortus-f4bu.
"""

from __future__ import annotations

import os
import sys
import time


def main() -> int:
    args = " ".join(sys.argv[1:])
    print(f"fake-claude argv: {args}", flush=True)
    print(f"fake-claude pid:  {os.getpid()}", flush=True)
    sleep = os.environ.get("FAKE_CLAUDE_SLEEP")
    if sleep:
        time.sleep(float(sleep))
    print("fake-claude done", flush=True)
    return int(os.environ.get("FAKE_CLAUDE_EXIT", "0"))


if __name__ == "__main__":
    raise SystemExit(main())
