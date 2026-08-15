"""Guard that github-bead ingest did not change readiness schema v1 rules."""

from __future__ import annotations

from ortus.core.readiness import validate_issue
from tests.test_readiness import ready_issue


def test_hand_authored_packet_still_validates() -> None:
    report = validate_issue(ready_issue())
    assert report.ready
    assert report.failures == ()
