"""The bounded read-only pass that writes a commit message (ortus-u1gs).

The pass itself is a model, so what is testable is everything around it: the
prompt it is held to, the envelope it must speak through, the mechanical
validation its output has to survive, and the guard that proves it changed
nothing while it ran. Each test drives that machinery directly with a fake
backend — no model is invoked anywhere in this file.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ortus.core.compose import (
    MESSAGE_PREFIX,
    SUBJECT_LIMIT,
    CommitMessage,
    ComposeExceededAuthority,
    ComposeFailed,
    ComposeRejected,
    bounded_diff,
    citations,
    compose_commit_message,
    compose_prompt,
    guard_read_only,
    parse_message,
    strip_code_spans,
    validate_message,
    with_default_model,
)
from ortus.core.profiles import AgentProfile, Phase

pytestmark = pytest.mark.fast

ISSUE = "ortus-u1gs"
TITLE = "Finalization composes the commit message with a bounded read-only LLM pass"

DIFF = '''diff --git a/src/ortus/core/compose.py b/src/ortus/core/compose.py
new file mode 100644
--- /dev/null
+++ b/src/ortus/core/compose.py
@@ -0,0 +1,9 @@
+def validate_message(message, *, issue_id, title, diff):
+    """Everything a composed message must survive to reach a commit."""
+
+    return message
+
+
+def guard_read_only(before, after):
+    """Reject a pass that moved the worktree while it ran."""
'''

BODY = (
    "Finalization assembled the commit body from structured data alone, so it "
    "restated the issue's objective — written before any code existed — and "
    "then listed the paths the commit already prints. A reader learned which "
    "files moved and nothing about what the code does.\n\n"
    "One bounded pass now writes the message from the diff that was verified, "
    "and `validate_message` decides whether what it wrote may be committed.\n\n"
    "The check is mechanical: the body must name something the diff contains, "
    "must be prose rather than an inventory, and must not narrate how the "
    "commit came to exist. Anything it refuses degrades to the body "
    "finalization already knew how to build."
)


def _message(subject: str = "Write the commit body from the verified diff", body: str = BODY) -> CommitMessage:
    return CommitMessage(subject=subject, body=body)


def _validated(message: CommitMessage, *, diff: str = DIFF, title: str = TITLE) -> CommitMessage:
    return validate_message(message, issue_id=ISSUE, title=title, diff=diff)


def _rejection(message: CommitMessage, **kwargs: str) -> str:
    with pytest.raises(ComposeRejected) as excinfo:
        _validated(message, **kwargs)  # type: ignore[arg-type]
    return str(excinfo.value)


# ---------------------------------------------------------------------------
# AC-5: the subject
# ---------------------------------------------------------------------------


def test_subject_shape_is_imperative_bounded_and_not_the_title() -> None:
    validated = _validated(_message())

    assert validated.subject == f"{ISSUE}: Write the commit body from the verified diff"
    assert len(validated.subject) <= SUBJECT_LIMIT
    assert "..." not in validated.subject

    # The id belongs to Ortus: a pass that prepends it anyway is normalized
    # rather than rejected, so the subject never carries it twice.
    prefixed = _validated(_message(subject=f"{ISSUE}: Write the commit body from the diff"))
    assert prefixed.subject.count(ISSUE) == 1

    assert "over the" in _rejection(
        _message(subject="Write the commit message body from the diff that a fresh reader can act on")
    )
    assert "ellipsis" in _rejection(_message(subject="Write the commit body from the..."))
    assert "imperative" in _rejection(_message(subject="Writes the commit body from the diff"))
    assert "imperative" in _rejection(_message(subject="Composed the commit body from the diff"))
    assert "restates the issue title" in _rejection(
        _message(subject="Finalization composes the commit message")
    )


@pytest.mark.parametrize(
    "subject",
    [
        "Address the disowned-path drop in the absorb step",
        "Focus the composer on the verified diff",
        "Re-read the worktree before composing",
        "Bring the commit body up to the review bar",
    ],
)
def test_subject_shape_accepts_verbs_the_heuristic_could_have_eaten(subject: str) -> None:
    """`Address`, `Focus` and `Bring` are imperative despite their endings."""

    assert _validated(_message(subject=subject)).subject.endswith(subject)


# ---------------------------------------------------------------------------
# AC-6: the body
# ---------------------------------------------------------------------------


def test_body_is_explanatory_prose_and_never_a_file_inventory() -> None:
    validated = _validated(_message())

    paragraphs = [block for block in validated.body.split("\n\n") if block.strip()]
    assert len(paragraphs) >= 2
    assert "validate_message" in validated.body

    assert "single paragraph" in _rejection(
        _message(body="One paragraph naming `validate_message` and stopping there.")
    )
    assert "inventories" in _rejection(
        _message(
            body=(
                "The pass now writes the body, and `validate_message` checks "
                "it.\n\nFiles touched:\n- src/ortus/core/compose.py\n"
                "- src/ortus/commands/grind.py\n"
            )
        )
    )
    assert "inventories" in _rejection(
        _message(
            body=(
                "The pass now writes the body, and `validate_message` checks "
                "it.\n\n- src/ortus/core/compose.py\n- src/ortus/commands/grind.py\n"
            )
        )
    )


# ---------------------------------------------------------------------------
# AC-7 / AC-8: process autobiography versus domain vocabulary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "narration",
    [
        "The change landed on the second attempt.",
        "After 2 corrections the shape settled.",
        "One correction round tightened the guard.",
        "The verifier passed this candidate before it was committed.",
        "A fresh read-only verifier reviewed the change.",
        "The implementation phase produced this.",
        "It left the journal in finalized-commit.",
        "Recorded against candidate a9c9a2c5531c.",
        "This change was verified end to end.",
    ],
)
def test_rejects_process_autobiography(narration: str) -> None:
    """AC-7: how the commit was produced never reaches a commit."""

    body = (
        f"{narration}\n\nThe mechanism lives in `validate_message`, which "
        "refuses a body it cannot check against the diff."
    )
    assert "narrates" in _rejection(_message(body=body))


def test_allows_domain_vocabulary_about_verification_itself() -> None:
    """AC-8: the ban is on autobiography, not on this codebase's nouns."""

    body = (
        "Verification sealed a candidate and finalization then described it "
        "from the issue packet, so the two disagreed whenever the code moved "
        "during a correction. The candidate diff is the only record of what "
        "actually changed.\n\n"
        "`validate_message` now reads that diff, and a body naming a symbol "
        "the candidate does not contain is refused. Corrections, verdicts and "
        "candidate hashing are what this subsystem is about, so the body is "
        "free to name them; what it may not do is narrate its own production."
    )
    validated = _validated(_message(body=body))
    assert "candidate" in validated.body and "verdict" in validated.body


# ---------------------------------------------------------------------------
# AC-9: unsupported names and missing bodies
# ---------------------------------------------------------------------------


def test_rejects_unsupported_or_bodyless_messages() -> None:
    invented = (
        "The old body restated the packet, which the code had already left "
        "behind.\n\nNow `compose_the_universe()` writes it instead, reading "
        "`src/ortus/core/nowhere.py` for the shape."
    )
    reason = _rejection(_message(body=invented))
    assert "compose_the_universe" in reason and "does not contain" in reason

    assert "no body" in _rejection(_message(body="   "))
    assert "names nothing from the diff" in _rejection(
        _message(
            body=(
                "The commit body used to restate the issue.\n\nNow it explains "
                "the change in prose, without naming anything in particular."
            )
        )
    )


def test_citations_ignore_prose_wrapped_in_backticks() -> None:
    """A backticked command is not a claim about the diff's symbols."""

    assert citations("`git show --stat` already prints the files.") == ()
    assert citations("`validate_message` and `parse_message()` decide.") == (
        "validate_message",
        "parse_message",
    )


# ---------------------------------------------------------------------------
# ortus-ot7q — the commit body carries no markup a commit view renders
# ---------------------------------------------------------------------------


def test_strips_code_spans_from_composed_message(tmp_path: Path) -> None:
    """AC-1: what reaches the commit is the identifier, not the markup."""

    body = (
        "The body quoted every identifier in backticks, and nothing that shows "
        "a commit renders them, so the reader saw the markers themselves.\n\n"
        "`validate_message` now hands back a body a commit view can print, and "
        "`guard_read_only` is untouched by the change."
    )
    validated = _validated(_message(subject="Drop the `backticks` from the body", body=body))

    assert "`" not in validated.body and "`" not in validated.subject
    assert "validate_message now hands back" in validated.body
    assert "guard_read_only is untouched" in validated.body

    # End to end: the text the caller commits is stripped, not just the value
    # validation happened to build.
    composed = _compose(tmp_path, _FakeRunner(text=_envelope("Write the body", body)))
    assert "`" not in composed
    assert "validate_message" in composed


def test_plain_message_is_unchanged() -> None:
    """AC-3: a message with no code spans is returned byte-identical."""

    body = (
        "The deterministic body restated an objective written before the code "
        "existed, so it explained nothing about the change itself.\n\n"
        "validate_message() decides what may be committed, and a body with no "
        "markup passes through it exactly as written."
    )
    validated = _validated(_message(body=body))

    assert validated.body == body


def test_unpaired_backtick_is_preserved() -> None:
    """AC-4: a lone backtick is prose about a character, not a broken span."""

    body = (
        "A body could open a code span and never close it, and a stripper that "
        "removed the opener would silently edit the sentence.\n\n"
        "validate_message() now leaves a lone ` exactly where it was written, "
        "because a backtick without a partner is being talked about rather "
        "than marking anything up."
    )
    validated = _validated(_message(body=body))

    assert "leaves a lone ` exactly" in validated.body


def test_a_fenced_block_is_left_alone_rather_than_half_stripped() -> None:
    """A fence is not a code span, so it is not partly unwrapped."""

    fenced = "See:\n\n```\nvalidate_message(message)\n```\n\nand nothing else."
    assert strip_code_spans(fenced) == fenced


def test_code_spans_are_cleaned_not_rejected() -> None:
    """AC-5: markup is never grounds for losing an otherwise good message.

    The citation check reads a backticked name as the pass's claim about the
    diff, so stripping has to come after it — a message whose only citations
    are code spans still passes, and still arrives bare.
    """

    body = (
        "Nothing in the body named a symbol without wrapping it, and a rule "
        "that refused such a body would throw away the explanation.\n\n"
        "`validate_message` and `guard_read_only` are cited here in spans and "
        "nowhere else, which is the case that must survive."
    )
    validated = _validated(_message(body=body))

    assert "`" not in validated.body
    assert "validate_message and guard_read_only are cited" in validated.body


def test_rubric_states_commit_bodies_are_not_markdown() -> None:
    """AC-6: the rubric gives the reason, and the example demonstrates it."""

    prompt = compose_prompt(
        issue_id=ISSUE, title=TITLE, objective="", changes="", diff=DIFF
    )
    rubric = prompt.partition("--- ISSUE PACKET ---")[0]
    example = prompt.partition("WORKED EXAMPLE")[2].partition("Note what the")[0]

    assert "not rendered as Markdown" in rubric
    assert "bare words" in rubric
    # The worked example is the part an author imitates, so it demonstrates
    # the rule rather than contradicting it.
    assert example.strip(), "the rubric no longer carries a worked example"
    assert "`" not in example
    assert "_prepare_handoff()" in example


# ---------------------------------------------------------------------------
# AC-10: the pass is read-only, and the guard proves it
# ---------------------------------------------------------------------------


def test_authority_guard_passes_an_unchanged_worktree_and_tracker() -> None:
    state = {"candidate.py": "sha256:abc", "worktree": "candidate.py", "tracker": "closed"}
    guard_read_only(state, dict(state))


@pytest.mark.parametrize(
    "key, value",
    [
        ("candidate.py", "sha256:def"),
        ("worktree", "candidate.py, stray.py"),
        ("tracker", "open"),
    ],
)
def test_authority_guard_names_what_a_read_only_pass_moved(key: str, value: str) -> None:
    before = {"candidate.py": "sha256:abc", "worktree": "candidate.py", "tracker": "closed"}
    after = {**before, key: value}

    with pytest.raises(ComposeExceededAuthority) as excinfo:
        guard_read_only(before, after)
    assert excinfo.value.changed == (key,)
    # Not a ComposeFailed: this one must not degrade to a plainer message.
    assert not isinstance(excinfo.value, ComposeFailed)


# ---------------------------------------------------------------------------
# AC-12: the commit the issue was filed over
# ---------------------------------------------------------------------------

#: An excerpt of a9c9a2c, the commit whose body was a file list. Vendored
#: rather than read with `git show` so the test still runs in a shallow clone.
GOLDEN_DIFF = '''diff --git a/src/ortus/commands/grind.py b/src/ortus/commands/grind.py
--- a/src/ortus/commands/grind.py
+++ b/src/ortus/commands/grind.py
@@ -462,6 +468,14 @@ def _absorb_unrelated_declaration(
     own_work = sorted(honored & journal.own_inherited_work())
     honored -= set(own_work)
+    readopted, gaps = _reclassify_edited_declarations(repo, journal, honored)
+    honored -= set(readopted)
@@ -477,20 +491,114 @@ def _absorb_unrelated_declaration(
+def _reclassify_edited_declarations(
+    repo: Path, journal: CandidateJournal, honored: set[str]
+) -> tuple[list[str], list[str]]:
+    """Split declared paths the worker then edited into re-adopted and blocked."""
+
+    current = fingerprint_paths(repo, honored)
+    edited = sorted(
+        path for path in honored
+        if current.get(path) != journal.handoff_fingerprints.get(path)
+    )
+    for path in edited:
+        decision = path_ownership(repo, path, journal.issue_id)
+        if decision.ownership is Ownership.OWN:
+            readopted.append(path)
+        elif decision.ownership is Ownership.MIXED:
+            gaps.append(describe(decision))
diff --git a/src/ortus/core/attribution.py b/src/ortus/core/attribution.py
new file mode 100644
--- /dev/null
+++ b/src/ortus/core/attribution.py
@@ -0,0 +1,40 @@
+"""Which issue owns the changed regions of one worktree path."""
+
+class Ownership(Enum):
+    OWN = "own"
+    MIXED = "mixed"
+    FOREIGN = "foreign"
+
+
+def path_ownership(repo, path, issue_id):
+    """Whole-path decision from the enclosing regions of the changed lines."""
'''

GOLDEN_TITLE = "grind: a disowned path edited later in the run is never re-adopted"


def test_golden_example_a9c9a2c_replaces_a_file_list_with_an_explanation() -> None:
    """AC-12: what shipped is refused; what should have shipped is accepted."""

    shipped = CommitMessage(
        subject="grind: a disowned path edited later in the run is never r...",
        body=(
            "A path a worker declared unrelated, and then edited during the "
            "same session, must not be silently dropped from the candidate.\n\n"
            "Files touched:\n"
            "- src/ortus/commands/grind.py\n"
            "- src/ortus/core/attribution.py\n"
        ),
    )
    with pytest.raises(ComposeRejected):
        validate_message(shipped, issue_id="ortus-s4km", title=GOLDEN_TITLE, diff=GOLDEN_DIFF)

    composed = CommitMessage(
        subject="Re-adopt a disowned path the same worker went on to edit",
        body=(
            "A worker that declared a path unrelated and then edited it in the "
            "same session lost the edit. The declaration was honored once, "
            "before the worker started, so nothing re-asked the question "
            "afterwards and the changed file never entered the candidate, "
            "reached review, or got committed.\n\n"
            "The declaration is now re-examined at the moment it is absorbed, "
            "and a path whose contents moved since the worker was handed the "
            "tree is decided by what changed inside it rather than by what was "
            "said about it.\n\n"
            "`_reclassify_edited_declarations` compares each declared path "
            "against the fingerprint recorded at handoff — a hash of the file "
            "as the worker first saw it — and treats a mismatch as a "
            "deliberate pick-up. For those paths `path_ownership` reads the "
            "enclosing region of every changed line: wholly the claimed "
            "issue's regions re-adopt the path, and a file carrying regions "
            "owned by more than one issue is routed to a human instead.\n\n"
            "The decision is whole-path on purpose. Splitting a mixed file "
            "hunk by hunk would commit half of somebody else's work, so "
            "`Ownership.MIXED` stops rather than guesses, and a region nothing "
            "can attribute leaves the declaration exactly as the worker wrote "
            "it."
        ),
    )
    validated = validate_message(
        composed, issue_id="ortus-s4km", title=GOLDEN_TITLE, diff=GOLDEN_DIFF
    )

    assert validated.subject == "ortus-s4km: Re-adopt a disowned path the same worker went on to edit"
    assert "fingerprint" in validated.body
    assert "region" in validated.body
    assert "Files touched" not in validated.body


# ---------------------------------------------------------------------------
# The prompt, the diff bound, and the envelope
# ---------------------------------------------------------------------------


def test_prompt_states_the_rubric_and_shows_a_worked_example() -> None:
    prompt = compose_prompt(
        issue_id=ISSUE,
        title=TITLE,
        objective="One bounded read-only pass writes the commit message.",
        changes="**Changes**\n- added src/ortus/core/compose.py",
        diff=DIFF,
    )

    assert prompt.count(MESSAGE_PREFIX) >= 2  # the contract, and the example
    for question in (
        "What was wrong",
        "What does the code now do",
        "How does the mechanism work",
        "Where does the design deliberately stop",
    ):
        assert question in prompt
    assert "imperative mood" in prompt
    assert "git show --stat" in prompt
    assert "never name one that does not" in prompt
    assert TITLE in prompt and "validate_message" in prompt
    # The worked example must itself be a well-formed envelope.
    example = [line for line in prompt.splitlines() if line.startswith(MESSAGE_PREFIX)][0]
    payload = json.loads(example[len(MESSAGE_PREFIX) :])
    assert payload["subject"] and payload["body"].count("\n\n") >= 3


def test_bounded_diff_cuts_on_a_hunk_boundary_and_says_so() -> None:
    hunks = "".join(
        f"@@ -{n},2 +{n},3 @@\n+line {n}\n" + "x" * 200 + "\n" for n in range(1, 20)
    )
    bounded = bounded_diff("diff --git a/a.py b/a.py\n" + hunks, limit=600)

    assert "hunks omitted" in bounded
    assert len(bounded) < len(hunks)
    # Cut between hunks, never mid-hunk.
    body, _, marker = bounded.partition("\n\n[diff truncated")
    assert marker
    assert not body.rstrip().endswith("@@")
    assert bounded_diff(DIFF, limit=len(DIFF)) == DIFF


def _event(text: str) -> str:
    return json.dumps(
        {"type": "item.completed", "item": {"type": "agent_message", "text": text}}
    )


def _envelope(subject: str, body: str) -> str:
    return MESSAGE_PREFIX + " " + json.dumps({"subject": subject, "body": body})


def test_parse_message_reads_exactly_one_envelope(tmp_path: Path) -> None:
    log = tmp_path / "grind.log"
    log.write_text(
        _event("thinking out loud")
        + "\n"
        + _event(_envelope("Write the body from the diff", BODY))
        + "\n",
        encoding="utf-8",
    )

    parsed = parse_message(log)
    assert parsed.subject == "Write the body from the diff"
    assert parsed.body == BODY


@pytest.mark.parametrize(
    "text, reason",
    [
        ("no envelope here", "found 0"),
        (MESSAGE_PREFIX + " {not json}", "malformed"),
        (MESSAGE_PREFIX + ' {"subject": "Write it"}', "string subject and body"),
        (MESSAGE_PREFIX + " [1, 2]", "not a JSON object"),
    ],
)
def test_parse_message_failures_are_all_compose_failures(
    tmp_path: Path, text: str, reason: str
) -> None:
    log = tmp_path / "grind.log"
    log.write_text(_event(text) + "\n", encoding="utf-8")

    with pytest.raises(ComposeFailed) as excinfo:
        parse_message(log)
    assert reason in str(excinfo.value)


def test_parse_message_reads_only_past_the_offset(tmp_path: Path) -> None:
    log = tmp_path / "grind.log"
    first = _event(_envelope("Write an earlier message", BODY)) + "\n"
    log.write_text(first, encoding="utf-8")
    offset = log.stat().st_size
    with log.open("a", encoding="utf-8") as fh:
        fh.write(_event(_envelope("Write this run's message", BODY)) + "\n")

    assert parse_message(log, start_offset=offset).subject == "Write this run's message"


# ---------------------------------------------------------------------------
# The pass end to end, against a fake backend
# ---------------------------------------------------------------------------


class _FakeRunner:
    """Records one composition invocation without launching a backend."""

    def __init__(self, *, text: str | None = None, rc: int = 0, timeout: bool = False):
        self.text = text
        self.rc = rc
        self.timeout = timeout
        self.calls: list[dict[str, object]] = []
        self.capabilities: list[object] = []

    def configure_codegraph(self, capability: object) -> None:
        self.capabilities.append(capability)

    def run(self, prompt: str, **kwargs: object) -> int:
        self.calls.append({"prompt": prompt, **kwargs})
        if self.timeout:
            raise subprocess.TimeoutExpired(cmd="claude", timeout=1)
        log_path = kwargs["log_path"]
        assert isinstance(log_path, Path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            if self.text is not None:
                fh.write(_event(self.text) + "\n")
        return self.rc


def _profile() -> AgentProfile:
    return AgentProfile(backend="claude", phase=Phase.FINALIZE, model="haiku")


def _compose(tmp_path: Path, runner: _FakeRunner, *, diff: str = DIFF) -> str:
    return compose_commit_message(
        tmp_path,
        issue_id=ISSUE,
        title=TITLE,
        objective="One bounded read-only pass writes the commit message.",
        changes="**Changes**\n- added src/ortus/core/compose.py",
        diff=diff,
        log_path=tmp_path / "logs" / "grind.log",
        backend="claude",
        profile=_profile(),
        capability=None,
        timeout=30.0,
        runner_factory=lambda *_: runner,
    )


def test_the_pass_runs_read_only_and_returns_a_validated_message(tmp_path: Path) -> None:
    runner = _FakeRunner(text=_envelope("Write the commit body from the diff", BODY))

    message = _compose(tmp_path, runner)

    assert message.startswith(f"{ISSUE}: Write the commit body from the diff\n\n")
    assert message.endswith("\n")
    call = runner.calls[0]
    assert call["readonly"] is True
    assert call["timeout"] == 30.0
    assert call["profile"] is not None and call["profile"].model == "haiku"  # type: ignore[union-attr]
    assert runner.capabilities == [None]


@pytest.mark.parametrize(
    "runner, reason",
    [
        (_FakeRunner(text=None), "found 0"),
        (_FakeRunner(text=_envelope("Write it", BODY), rc=2), "exited 2"),
        (_FakeRunner(timeout=True), "timed out"),
        (
            _FakeRunner(text=_envelope("Writes the body", BODY)),
            "not imperative",
        ),
    ],
)
def test_every_failure_mode_raises_compose_failed(
    tmp_path: Path, runner: _FakeRunner, reason: str
) -> None:
    with pytest.raises(ComposeFailed) as excinfo:
        _compose(tmp_path, runner)
    assert reason in str(excinfo.value)


def test_an_empty_diff_never_reaches_the_backend(tmp_path: Path) -> None:
    runner = _FakeRunner(text=_envelope("Write it", BODY))

    with pytest.raises(ComposeFailed) as excinfo:
        _compose(tmp_path, runner, diff="   ")
    assert "empty or unreadable" in str(excinfo.value)
    assert runner.calls == []


def test_a_launch_failure_degrades_instead_of_propagating(tmp_path: Path) -> None:
    class _Broken(_FakeRunner):
        def run(self, prompt: str, **kwargs: object) -> int:
            raise OSError("claude: not found")

    with pytest.raises(ComposeFailed) as excinfo:
        _compose(tmp_path, _Broken())
    assert "could not run" in str(excinfo.value)


def test_default_model_is_cheap_and_never_overrides_the_operator() -> None:
    default = with_default_model(AgentProfile(backend="claude", phase=Phase.FINALIZE))
    assert default.model == "haiku"

    chosen = AgentProfile(backend="claude", phase=Phase.FINALIZE, model="opus")
    assert with_default_model(chosen) is chosen

    codex = AgentProfile(backend="codex", phase=Phase.FINALIZE)
    assert with_default_model(codex).model is None


def test_a_non_ascii_path_survives_validation() -> None:
    diff = "diff --git a/docs/café.md b/docs/café.md\n+++ b/docs/café.md\n+# Café\n"
    message = CommitMessage(
        subject="Document the café endpoint",
        body=(
            "The endpoint shipped without documentation, so callers guessed at "
            "its shape.\n\n`docs/café.md` now describes it, including the "
            "encoding the path itself depends on."
        ),
    )

    validated = validate_message(message, issue_id=ISSUE, title=TITLE, diff=diff)
    assert "café" in validated.body
