#!/usr/bin/env python3
"""Canned claude response: claims an issue, hits a blocker, emits the
<promise>BLOCKED</promise> marker, exits 0 without closing.

Rewritten in Python for Windows compat. See ortus-f4bu.
"""

from __future__ import annotations


def main() -> int:
    print('{"type":"system","subtype":"start","session_id":"blocked"}', flush=True)
    print(
        '{"type":"assistant","message":{"content":"Hit a real blocker. <promise>BLOCKED</promise>"}}',
        flush=True,
    )
    print('{"type":"system","subtype":"end"}', flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
