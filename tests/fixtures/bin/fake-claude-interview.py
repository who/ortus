#!/usr/bin/env python3
"""Fake claude shim used by tests/test_interview.py and tests/test_triage.py.

Echoes its argv (the test inspects the log for substituted feature_id /
prompt content) then exits 0 without touching bd.

Rewritten in Python for Windows compat. See ortus-f4bu.
"""

from __future__ import annotations

import sys


def main() -> int:
    last_arg = sys.argv[-1] if len(sys.argv) > 1 else ""
    print(f"fake-claude-interview prompt-tail: {last_arg}", flush=True)
    print(" ".join(sys.argv[1:]), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
