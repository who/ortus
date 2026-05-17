#!/usr/bin/env python3
"""Canned claude response: ortus grind invoked on an empty queue.

Emits a tiny stream-json sequence that the /goal evaluator would judge
as "queue empty" after a single turn, then exits 0.

Rewritten in Python for Windows compat. See ortus-f4bu.
"""

from __future__ import annotations


def main() -> int:
    print('{"type":"system","subtype":"start","session_id":"empty-queue"}', flush=True)
    print(
        '{"type":"assistant","message":{"content":"bd ready --json returned [] — queue empty. Ending session."}}',
        flush=True,
    )
    print('{"type":"system","subtype":"end","ttf_ms":42}', flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
