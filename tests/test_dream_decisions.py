from __future__ import annotations

import json

import pytest

from ortus.core.dream_decisions import (
    ACCEPTED,
    PENDING,
    REJECTED,
    UNDECIDED,
    accepted,
    fingerprint,
    item_ids,
    load_decisions,
    parse_fingerprints_block,
    parse_response,
    render_decision_section,
    render_fingerprints_block,
    suppressed,
)

from tests._fake_bd import FakeBdClient, seed

PROPOSALS = (
    {
        "kind": "add",
        "key": "uv-tool-reinstall",
        "pattern": "PATH ortus is a stale uv tool install",
        "quotes": ["needs 'uv tool install --reinstall .'", "second", "third"],
    },
    {"kind": "prune", "key": "old-pin", "pattern": "pin is obsolete", "quotes": []},
    {"kind": "update", "key": "fake-bd", "pattern": "fake omits empty fields"},
)
FOLLOWUPS = ({"title": "Refresh the tool install in grind preflight"},)
#: The block a decision bead filed before items had names carried these ids.
LEGACY_PROPOSALS = tuple(
    {**proposal, "id": f"P{index}"} for index, proposal in enumerate(PROPOSALS, start=1)
)
LEGACY_FOLLOWUPS = ({**FOLLOWUPS[0], "id": "F1"},)


def _dream_bead(
    client: FakeBdClient,
    *,
    labels: str = "dream,human",
    kind: str = "decision",
    legacy: bool = True,
) -> str:
    block = (
        render_fingerprints_block(LEGACY_PROPOSALS, LEGACY_FOLLOWUPS)
        if legacy
        else render_fingerprints_block(PROPOSALS, FOLLOWUPS)
    )
    return seed(
        client,
        "create",
        "--title",
        "Dream: proposals",
        "--type",
        kind,
        "--labels",
        labels,
        "--description",
        "Proposals below.\n\n" + block + "\n",
    )


def _verdicts(decision) -> dict[str, str]:
    return {p.id: p.verdict for p in decision.proposals}


def test_fingerprint_is_stable_under_case_and_whitespace() -> None:
    base = fingerprint("add", "k", "Stale  uv\ttool\ninstall")
    assert base == fingerprint("add", "k", "  stale uv TOOL install ")
    assert len(base) == 16 and all(c in "0123456789abcdef" for c in base)
    assert base != fingerprint("prune", "k", "stale uv tool install")
    assert base != fingerprint("add", "other", "stale uv tool install")


def test_fingerprint_block_round_trips() -> None:
    block = render_fingerprints_block(PROPOSALS, FOLLOWUPS)
    parsed = parse_fingerprints_block(f"Intro text.\n\n{block}\n\nTrailer.")
    assert parsed is not None
    assert parsed["version"] == 1
    assert [p["id"] for p in parsed["proposals"]] == ["uv-tool-reinstall", "old-pin", "fake-bd"]
    assert parsed["proposals"][0]["fingerprint"] == fingerprint(
        "add", "uv-tool-reinstall", "PATH ortus is a stale uv tool install"
    )
    assert len(parsed["proposals"][0]["quotes"]) == 2
    assert parsed["followups"] == [
        {"id": "refresh-the-tool-install-in", "title": "Refresh the tool install in grind preflight"}
    ]


def test_fingerprint_block_absent_or_malformed_is_none() -> None:
    assert parse_fingerprints_block("no block here") is None
    assert parse_fingerprints_block("```ortus-dream-fingerprints\n{not json\n```") is None
    assert parse_fingerprints_block("```ortus-dream-fingerprints\n[1, 2]\n```") is None


def test_response_parses_accepted_rejected_and_undecided() -> None:
    verdicts = parse_response("accepted P1 P3; rejected P2 (one-off)")
    assert verdicts == {"P1": ACCEPTED, "P3": ACCEPTED, "P2": REJECTED}
    assert "P4" not in verdicts


def test_response_later_group_wins_and_ids_are_case_insensitive() -> None:
    assert parse_response("accepted p1 f1; rejected P1") == {
        "P1": REJECTED,
        "F1": ACCEPTED,
    }
    assert parse_response("Accepted P1, rejected P2") == {"P1": ACCEPTED, "P2": REJECTED}


def test_response_on_closed_bead_drives_verdicts(tmp_path) -> None:
    client = FakeBdClient(tmp_path)
    bead = _dream_bead(client)
    client.add_comment(bead, "accepted P1 F1; rejected P2 (one-off)")
    client.close(bead, reason="Responded")

    (decision,) = load_decisions(client)
    assert decision.bead_id == bead
    assert decision.status == "closed" and decision.closed_at
    assert _verdicts(decision) == {"P1": ACCEPTED, "P2": REJECTED, "P3": UNDECIDED}
    assert [f.verdict for f in decision.followups] == [ACCEPTED]
    assert accepted([decision]) == (
        (
            "uv-tool-reinstall",
            ("needs 'uv tool install --reinstall .'", "second"),
            decision.closed_at,
        ),
    )
    section = render_decision_section(decision)
    assert section.splitlines()[0] == f"[dream-decision {bead}]"
    assert section.splitlines()[-1] == "[/dream-decision]"
    assert f"P2 rejected kind=prune key=old-pin fingerprint={fingerprint('prune', 'old-pin', 'pin is obsolete')}" in section
    assert "response: accepted P1 F1; rejected P2 (one-off)" in section


def test_response_falls_back_to_close_reason(tmp_path) -> None:
    client = FakeBdClient(tmp_path)
    bead = _dream_bead(client)
    client.close(bead, reason="rejected P1 P2 P3")
    (decision,) = load_decisions(client)
    assert set(_verdicts(decision).values()) == {REJECTED}


def test_suppress_pending_open_bead_suppresses_every_proposal(tmp_path) -> None:
    client = FakeBdClient(tmp_path)
    _dream_bead(client)
    (decision,) = load_decisions(client)
    assert set(_verdicts(decision).values()) == {PENDING}
    keys, prints = suppressed([decision])
    assert keys == {"uv-tool-reinstall", "old-pin", "fake-bd"}
    assert len(prints) == 3


def test_suppress_reopened_bead_lifts_suppression(tmp_path) -> None:
    client = FakeBdClient(tmp_path)
    bead = _dream_bead(client)
    client.add_comment(bead, "rejected P1 P2 P3")
    client.close(bead, reason="Responded")
    assert suppressed(load_decisions(client))[0] == {
        "uv-tool-reinstall",
        "old-pin",
        "fake-bd",
    }

    seed(client, "update", bead, "--status", "open")
    (decision,) = load_decisions(client)
    assert set(_verdicts(decision).values()) == {UNDECIDED}
    assert suppressed([decision]) == (frozenset(), frozenset())


def test_suppress_only_rejected_from_closed_bead(tmp_path) -> None:
    client = FakeBdClient(tmp_path)
    bead = _dream_bead(client)
    client.add_comment(bead, "accepted P1; rejected P2")
    client.close(bead, reason="Responded")
    keys, prints = suppressed(load_decisions(client))
    assert keys == {"old-pin"}
    assert prints == {fingerprint("prune", "old-pin", "pin is obsolete")}


def test_suppress_ignores_non_dream_decision_beads(tmp_path) -> None:
    client = FakeBdClient(tmp_path)
    _dream_bead(client, labels="human")
    _dream_bead(client, kind="task")
    dream = _dream_bead(client)
    assert [d.bead_id for d in load_decisions(client)] == [dream]


def test_suppress_tolerates_missing_and_future_blocks(tmp_path) -> None:
    client = FakeBdClient(tmp_path)
    bare = seed(
        client, "create", "--title", "Dream", "--type", "decision", "--labels", "dream"
    )
    future = seed(
        client,
        "create",
        "--title",
        "Dream",
        "--type",
        "decision",
        "--labels",
        "dream",
        "--description",
        "```ortus-dream-fingerprints\n"
        + json.dumps({"version": 9, "proposals": [{"id": "P1", "key": "x"}]})
        + "\n```",
    )
    by_id = {d.bead_id: d for d in load_decisions(client)}
    assert by_id[bare].proposals == () and by_id[bare].warning == ""
    assert by_id[future].proposals == ()
    assert "version 9" in by_id[future].warning
    assert suppressed(by_id.values()) == (frozenset(), frozenset())


def test_slug_ids_fall_back_and_stay_unique() -> None:
    proposals, followups = item_ids(
        [
            {"kind": "add", "key": "Commit_Message rules"},
            {"kind": "add", "key": "commit-message-rules"},
            {"kind": "prune", "key": ""},
            {"kind": "", "key": ""},
            {"kind": "add", "key": "p1"},
        ],
        [{"title": "Commit message rules"}, {"title": ""}],
    )
    assert proposals == [
        "commit-message-rules",
        "commit-message-rules-2",
        "prune",
        "memory-change-4",
        "memory-p1",
    ]
    assert followups == ["commit-message-rules-3", "follow-up-2"]


def test_slug_reply_parses_new_verbs_against_known_ids() -> None:
    known = ["commit-message-rules", "pinned-prompt-hash-tests", "skip-flaky", "dolt-ssh-push"]
    reply = (
        "accept Commit-Message-Rules, pinned-prompt-hash-tests; "
        "skip skip-flaky (one-off); file follow-up dolt-ssh-push"
    )
    assert parse_response(reply, known) == {
        "commit-message-rules": ACCEPTED,
        "pinned-prompt-hash-tests": ACCEPTED,
        "skip-flaky": REJECTED,
        "dolt-ssh-push": ACCEPTED,
    }
    assert parse_response("rejected unknown-thing; filed dolt-ssh-push", known) == {
        "dolt-ssh-push": ACCEPTED
    }
    assert parse_response("accept a-b, c (because x-y); skipped d") == {
        "a-b": ACCEPTED,
        "c": ACCEPTED,
        "d": REJECTED,
    }


def test_slug_reply_drives_verdicts_on_a_closed_bead(tmp_path) -> None:
    client = FakeBdClient(tmp_path)
    bead = _dream_bead(client, legacy=False)
    client.add_comment(
        bead,
        "accept uv-tool-reinstall; reject old-pin (one-off); "
        "file follow-up refresh-the-tool-install-in",
    )
    client.close(bead, reason="Responded")

    (decision,) = load_decisions(client)
    assert _verdicts(decision) == {
        "uv-tool-reinstall": ACCEPTED,
        "old-pin": REJECTED,
        "fake-bd": UNDECIDED,
    }
    assert [f.verdict for f in decision.followups] == [ACCEPTED]


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("accepted P1 P2; F1 filed", {"P1": ACCEPTED, "P2": ACCEPTED, "P3": UNDECIDED}),
        ("accepted P1, rejected P2", {"P1": ACCEPTED, "P2": REJECTED, "P3": UNDECIDED}),
    ],
)
def test_legacy_replies_keep_their_verdicts_against_an_old_block(
    tmp_path, reply, expected
) -> None:
    assert {
        key: value for key, value in parse_response(reply).items() if key.startswith("P")
    } == {key: value for key, value in expected.items() if value != UNDECIDED}
    client = FakeBdClient(tmp_path)
    bead = _dream_bead(client)
    client.add_comment(bead, reply)
    client.close(bead, reason="Responded")

    (decision,) = load_decisions(client)
    assert _verdicts(decision) == expected
    assert [f.verdict for f in decision.followups] == [UNDECIDED]


def test_legacy_reply_reaches_a_named_block_by_position(tmp_path) -> None:
    client = FakeBdClient(tmp_path)
    bead = _dream_bead(client, legacy=False)
    client.add_comment(bead, "accepted P1 F1; rejected P2")
    client.close(bead, reason="Responded")

    (decision,) = load_decisions(client)
    assert _verdicts(decision) == {
        "uv-tool-reinstall": ACCEPTED,
        "old-pin": REJECTED,
        "fake-bd": UNDECIDED,
    }
    assert [f.verdict for f in decision.followups] == [ACCEPTED]
