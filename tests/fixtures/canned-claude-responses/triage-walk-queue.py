#!/usr/bin/env python3
"""Canned claude scenario: triage walks the human queue and dispositions one.

Rewritten in Python for Windows compat. See ortus-f4bu.
"""

from __future__ import annotations


def main() -> int:
    print('{"type":"system","subtype":"start","session_id":"triage"}', flush=True)
    print(
        '{"type":"assistant","message":{"content":"Reviewing first human-flagged issue."}}',
        flush=True,
    )
    print(
        '{"type":"assistant","message":{"content":"Disposition recorded; ending turn."}}',
        flush=True,
    )
    print('{"type":"system","subtype":"end"}', flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
