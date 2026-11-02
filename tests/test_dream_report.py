"""Dream's report stage: the rendered description, the filed bead, the no-bead paths."""

from __future__ import annotations

import datetime as _dt
import json
import shlex
from pathlib import Path

import pytest

from ortus.core.bd import SRC_TAIL, BdError
from ortus.core.dream_decisions import (
    fingerprint,
    load_decisions,
    parse_fingerprints_block,
    parse_response,
)
from ortus.core.dream_orchestrator import Followup, Proposal
from ortus.core.dream_report import (
    DREAM_ACTOR,
    NOT_FILED,
    PLACEHOLDER,
    STATE_FILE,
    RunFacts,
    command,
    deliver,
    example_response,
    render_report,
    title,
)
from ortus.core.dream_validate import HelpedRow, KeptProposal, ValidationResult
from tests._fake_bd import FakeBdClient

TRICKY_BODY = "Don't run bare pytest; use `uv run pytest` and $HOME-free paths."


def _kept(kind: str = "add", key: str = "pytest-uv-run", body: str = TRICKY_BODY) -> KeptProposal:
    proposal = Proposal(
        kind=kind,
        key=key,
        body=body,
        pattern="bare pytest picks the wrong interpreter",
        why="two beads lost turns to it",
        beads=("ortus-a", "ortus-b"),
        sources=(),
        quotes=("pytest: command not found",),
        stats={},
    )
    return KeptProposal(
        proposal=proposal,
        fingerprint=fingerprint(kind, key, proposal.pattern),
        beads=("ortus-a", "ortus-b"),
        stats={"beads": 2, "windows": 3, "turns_wasted": 7},
        examples=(
            ("pytest: command not found", "[grind-log bead=ortus-a run=grind-1 lines=1-9]"),
            ("pytest: command not found", "[grind-log bead=ortus-b run=grind-2 lines=1-4]"),
        ),
    )


def _result(proposals=None, followups=None) -> ValidationResult:
    return ValidationResult(
        proposals=tuple([_kept(), _kept("prune", "stale-key", "")] if proposals is None else proposals),
        followups=tuple(
            [Followup(title="Pin the uv version", why="drift", beads=("ortus-a",), sources=("[x]",))]
            if followups is None
            else followups
        ),
        helped_rows=(
            HelpedRow(
                key="old-lesson",
                closed_at="2026-10-01T00:00:00Z",
                before_windows=10,
                before_hits=4,
                before_beads=3,
                after_windows=5,
                after_hits=1,
                after_beads=1,
            ),
        ),
        drops=(("add x", "cap"), ("add y", "cap"), ("add z", "bad-key")),
        stat_diffs=(),
    )


FACTS = RunFacts(
    backend="claude",
    reader="claude/haiku (low)",
    orchestrator="claude/opus (high)",
    windows_read=2,
    windows_reused=5,
    windows_fed=7,
    chunked_windows=1,
    usage={"input_tokens": 1200, "output_tokens": 300},
    cost_usd=0.25,
    memory_chars=480,
    memory_keys=6,
    prime_chars=None,
)


def _deliver(repo: Path, client, result=None, *, no_file=False):
    return deliver(
        _result() if result is None else result,
        FACTS,
        repo=repo,
        client=client,
        stamp="20261031-120000",
        windows=["w1", "w2"],
        reader_key="abc123",
        orchestrator_profile="claude/opus (high)",
        no_file=no_file,
        today=_dt.date(2026, 10, 31),
    )


# --- render ------------------------------------------------------------------


def test_render_carries_every_section_in_order() -> None:
    text = render_report(_result(), FACTS, "ortus-x1")

    headings = [
        "## Proposals",
        "### P1 add pytest-uv-run",
        "### P2 prune stale-key",
        "## Follow-ups (not memories)",
        "### F1 Pin the uv version",
        "## Did it help?",
        "## Run facts",
        "## How to respond",
        "```ortus-dream-fingerprints",
    ]
    positions = [text.index(heading) for heading in headings]
    assert positions == sorted(positions)
    assert "beads=2 windows=3 turns_wasted=7" in text
    assert "[grind-log bead=ortus-a run=grind-1 lines=1-9]" in text
    assert "| old-lesson | 40.0 | 20.0 | 3/1 |" in text
    assert "drops: bad-key=1, cap=2" in text
    assert "bd prime: unavailable chars" in text
    assert 'bd human respond ortus-x1 "accepted P1; rejected P2 (reason); F1 filed"' in text


def _listed_ids(text: str) -> set[str]:
    block = parse_fingerprints_block(text)
    assert block is not None
    return {row["id"] for row in block["proposals"]} | {row["id"] for row in block["followups"]}


def _rendered_example(text: str, bead_id: str) -> str:
    prefix = f'bd human respond {bead_id} "'
    line = next(line for line in text.splitlines() if line.startswith(prefix))
    return line[len(prefix) : -1]


def test_render_example_ids_name_only_what_the_report_lists() -> None:
    text = render_report(_result(), FACTS, "ortus-x1")
    example = _rendered_example(text, "ortus-x1")

    assert "P3" not in example
    assert example == example_response(_result())
    named = parse_response(example)
    assert named == {"P1": "accepted", "P2": "rejected"}
    assert set(named) <= _listed_ids(text)
    assert "F1" in example


def test_render_how_to_respond_is_agent_ready() -> None:
    text = render_report(_result(), FACTS, "ortus-x1")
    section = text[text.index("## How to respond") : text.index("```ortus-dream-fingerprints")]

    assert "`bd remember`" in section
    assert "command shown under it in Proposals" in section
    assert 'bd human respond ortus-x1 "<response>"' in section
    assert "`accepted <ids>`" in section and "`rejected <ids> (reason)`" in section
    assert "`ortus ingest --packet <dir>`" in section
    assert "`ortus ingest --stdin`" in section
    assert "Dream never files follow-ups" in section


@pytest.mark.parametrize(
    ("proposals", "followups", "expected"),
    [
        ([_kept()], [], {"P1": "accepted"}),
        ([_kept()], None, {"P1": "accepted"}),
        ([], None, {"F1": "accepted"}),
        ([], [], {}),
    ],
)
def test_render_edge_examples_parse_to_listed_ids(proposals, followups, expected) -> None:
    result = _result(proposals=proposals, followups=followups)
    text = render_report(result, FACTS, "ortus-x1")
    example = _rendered_example(text, "ortus-x1")

    assert example == example_response(result)
    named = parse_response(example)
    assert named == expected
    assert set(named) <= _listed_ids(text)


def test_render_command_quotes_a_tricky_body_with_the_real_id() -> None:
    text = render_report(_result(), FACTS, "ortus-x1")
    line = command("add", "pytest-uv-run", TRICKY_BODY, "ortus-x1")

    assert line in text
    argv = shlex.split(line)
    assert argv[:2] == ["bd", "remember"]
    assert argv[2] == f"{TRICKY_BODY} [src:ortus-x1]"
    assert argv[3:] == ["--key", "pytest-uv-run"]
    assert SRC_TAIL.search(argv[2])
    assert "bd forget stale-key" in text


def test_render_fingerprints_block_round_trips() -> None:
    block = parse_fingerprints_block(render_report(_result(), FACTS, "ortus-x1"))

    assert block is not None
    assert [row["id"] for row in block["proposals"]] == ["P1", "P2"]
    assert block["proposals"][0]["fingerprint"] == _kept().fingerprint
    assert block["proposals"][0]["quotes"] == ["pytest: command not found"] * 2
    assert block["followups"] == [{"id": "F1", "title": "Pin the uv version"}]


def test_render_title_counts_followups_only_when_present() -> None:
    today = _dt.date(2026, 10, 31)

    assert title(_result(), 7, today) == (
        "Dream 2026-10-31: 2 memory proposals from 7 windows, 1 follow-ups"
    )
    assert title(_result(followups=[]), 7, today) == (
        "Dream 2026-10-31: 2 memory proposals from 7 windows"
    )


# --- file_bead ---------------------------------------------------------------


def test_file_bead_files_one_decision_with_labels_actor_and_real_id(tmp_path: Path) -> None:
    client = FakeBdClient(tmp_path)

    delivery = _deliver(tmp_path, client)

    assert delivery.exit_code == 0
    bead_id = delivery.bead_id
    row = client.show(bead_id)
    assert row["issue_type"] == "decision"
    assert set(row["labels"]) == {"human", "dream"}
    assert row["created_by"] == DREAM_ACTOR
    assert row["title"].startswith("Dream 2026-10-31: 2 memory proposals from 7 windows")
    assert PLACEHOLDER not in row["description"]
    assert f"[src:{bead_id}]" in row["description"]
    creates = [call for call in client.calls if "create" in call]
    assert len(creates) == 1
    assert creates[0][:3] == ["--actor", DREAM_ACTOR, "create"]
    assert f"[src:{PLACEHOLDER}]" in creates[0][creates[0].index("--description") + 1]
    updates = [call for call in client.calls if "update" in call]
    assert updates == [["--actor", DREAM_ACTOR, "update", bead_id, "--description", row["description"]]]

    report = delivery.report_path.read_text(encoding="utf-8")
    assert delivery.report_path == tmp_path / "logs" / "dream-20261031-120000.md"
    assert report == row["description"]
    state = json.loads((tmp_path / STATE_FILE).read_text(encoding="utf-8"))
    assert state == [
        {
            "ts": "20261031-120000",
            "bead": bead_id,
            "windows": ["w1", "w2"],
            "reader_key": "abc123",
            "orchestrator_profile": "claude/opus (high)",
        }
    ]

    decisions = load_decisions(client)
    assert [d.bead_id for d in decisions] == [bead_id]
    assert [p.key for p in decisions[0].proposals] == ["pytest-uv-run", "stale-key"]


def test_file_bead_failed_substitution_keeps_placeholder_and_exits_1(tmp_path: Path) -> None:
    class NoUpdate(FakeBdClient):
        def update_description(self, issue_id, text, *, actor=None):
            raise BdError(["bd", "update"], 1, "unknown flag --description")

    client = NoUpdate(tmp_path)

    delivery = _deliver(tmp_path, client)

    assert delivery.exit_code == 1
    assert delivery.bead_id in delivery.error
    assert PLACEHOLDER in client.show(delivery.bead_id)["description"]


def test_file_bead_create_failure_writes_a_not_filed_report(tmp_path: Path) -> None:
    class NoCreate(FakeBdClient):
        def create(self, **kwargs):
            raise BdError(["bd", "create"], 1, "boom")

    delivery = _deliver(tmp_path, NoCreate(tmp_path))

    assert delivery.exit_code == 1
    assert delivery.bead_id is None
    assert f"Decision bead: {NOT_FILED}" in delivery.report_path.read_text(encoding="utf-8")


def test_file_bead_existing_create_and_label_argv_unchanged(tmp_path: Path) -> None:
    client = FakeBdClient(tmp_path)

    issue_id = client.create(title="t", labels=["a"])
    client.add_label(issue_id, "b")

    assert client.calls == [
        ["create", "--silent", "--title", "t", "--type", "task", "--priority", "2", "--labels", "a"],
        ["label", "add", issue_id, "b"],
    ]
    client.add_label(issue_id, "c", actor="someone")
    assert client.calls[-1] == ["--actor", "someone", "label", "add", issue_id, "c"]


# --- no_bead -----------------------------------------------------------------


def test_no_bead_empty_result_exits_3_and_writes_the_report(tmp_path: Path) -> None:
    client = FakeBdClient(tmp_path)

    delivery = _deliver(tmp_path, client, _result(proposals=[], followups=[]))

    assert delivery.exit_code == 3
    assert delivery.bead_id is None
    assert client.calls == []
    report = delivery.report_path.read_text(encoding="utf-8")
    assert "## Did it help?" in report
    assert "| old-lesson |" in report
    assert f"Decision bead: {NOT_FILED}" in report


def test_no_bead_no_file_writes_the_report_only(tmp_path: Path) -> None:
    client = FakeBdClient(tmp_path)

    delivery = _deliver(tmp_path, client, no_file=True)

    assert delivery.exit_code == 0
    assert delivery.bead_id is None
    assert client.calls == []
    report = delivery.report_path.read_text(encoding="utf-8")
    assert "### P1 add pytest-uv-run" in report
    assert f"Decision bead: {NOT_FILED}" in report
    state = json.loads((tmp_path / STATE_FILE).read_text(encoding="utf-8"))
    assert state[0]["bead"] is None


def test_no_bead_cli_no_file_exits_0_and_empty_exits_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from ortus.cli import app
    from ortus.commands import dream as dream_module
    from tests.test_dream_cli import FIXTURE

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("ORTUS_BACKEND", raising=False)
    repo = tmp_path / "repo"
    (repo / ".beads").mkdir(parents=True)
    (repo / "logs").mkdir()
    (repo / ".gitignore").write_text(".ortus/dream/\n", encoding="utf-8")
    (repo / "logs" / FIXTURE.name).write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

    fake = FakeBdClient(repo)
    created: list[dict] = []
    monkeypatch.setattr(dream_module, "BdClient", lambda repo=None: fake)
    monkeypatch.setattr(dream_module, "make_runner", lambda backend, repo=None: object())
    monkeypatch.setattr(dream_module, "run_readers", lambda *args, **kwargs: [])
    monkeypatch.setattr(dream_module, "run_orchestrator", lambda **kwargs: _Usage())
    monkeypatch.setattr(fake, "create", lambda **kwargs: created.append(kwargs) or "x")

    monkeypatch.setattr(dream_module, "validate_run", lambda *args, **kwargs: _result())
    ran = CliRunner().invoke(app, ["dream", str(repo), "--no-file"])
    assert ran.exit_code == 0, ran.output
    assert created == []

    monkeypatch.setattr(
        dream_module, "validate_run", lambda *args, **kwargs: _result(proposals=[], followups=[])
    )
    ran = CliRunner().invoke(app, ["dream", str(repo)])
    assert ran.exit_code == 3, ran.output
    assert created == []
    assert len(list((repo / "logs").glob("dream-*.md"))) >= 1


class _Usage:
    usage = None
    cost_usd = None

