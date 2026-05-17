#!/usr/bin/env python3
"""Canned claude scenario: interview picks a feature and runs a 1-Q interview.

Rewritten in Python for Windows compat. See ortus-f4bu.
"""

from __future__ import annotations


def main() -> int:
    print('{"type":"system","subtype":"start","session_id":"interview"}', flush=True)
    print(
        '{"type":"assistant","message":{"content":"Interview started. What problem does this solve?"}}',
        flush=True,
    )
    print(
        '{"type":"assistant","message":{"content":"OK noted; ending interview turn."}}',
        flush=True,
    )
    print('{"type":"system","subtype":"end"}', flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
