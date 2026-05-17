#!/usr/bin/env python3
"""Fake claude shim that emits stream-json-shaped lines so the
terminal-leak regression test can verify they end up in the log file
and NOT in the parent stdout.

Rewritten in Python for Windows compat. See ortus-f4bu.
"""

from __future__ import annotations


def main() -> int:
    print('{"type":"system","subtype":"start"}', flush=True)
    print('{"type":"assistant","message":{"content":"working"}}', flush=True)
    print('{"type":"system","subtype":"end"}', flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
