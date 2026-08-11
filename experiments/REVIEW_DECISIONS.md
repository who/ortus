# Review decisions: the branch-scoped candidates programme

Review performed 2026-08-11 against `experiments/REVIEW_BRIEF.md`, by a different
model than the one that designed the programme. Everything the brief named was
read in full: the PRD, the three experiment documents, and all twelve issues in
the bd graph. This document records every decision the review made, the
reasoning behind each, and what was deliberately left alone. Actions taken are
marked **Acted**; endorsements are marked **Upheld**.

## Verdict in one paragraph

The keystone — candidate as commit on a branch — is correct and well-evidenced,
and the graph's packets are stronger than what planning typically produces. But
Phase 0 was justified by two claims that are false by construction, and Epic B
as filed was inert: no leaf ever pushed an issue branch or made the merge wait
for its checks, so the programme's headline metric could not move under any
filed work. The review corrected the claims where they live, filed the missing
leaf, serialized three leaves that would otherwise violate the programme's own
NFR-3, landed the stray `ortus-frht` candidate, and reframed Phase 4. Nothing
was removed from the programme.

## Housekeeping the brief required

### The `ortus-frht` candidate — landed (Acted)

The working tree carried four uncommitted files implementing `ortus-frht`
(shorten an over-long composed subject instead of discarding the message). The
candidate was reviewed against its packet and found complete: every resolved
decision implemented, every named AC test present, one `shortened()` definition
serving both subject producers (AC-8), and the repair correctly ordered *after*
the correctness rules so a wrong message is still refused. Its 106-test targeted
suite was re-run with `codegraph` masked from `PATH` — the brief's own
environment-divergence rule — and passed. Landed by hand as `d648867`;
`ortus-frht` closed with the verification evidence in the close reason.

### The unpushed commits — a stale claim, not a task (Acted: verified only)

The brief said one commit was unpushed; the prompt relaying it said three. Both
were stale: `HEAD` equalled `origin/main` at session start. Nothing needed
pushing. Recorded here because it is a small instance of the failure class the
programme is about — state described from memory rather than measured at the
moment of action.

## The four questions the brief asked to have challenged

### 1. Is Phase 0 worth doing, or should it fold into Phase 2?

**Keep Phase 0 — but both of its stated justifications were wrong, and the
packets now say so (Acted).**

The brief's own counterargument (decision 7) was correct and understated. Two
claims fail by construction, not by judgment:

- *"The branch history shows how often two issues actually touch the same
  lines."* It cannot. Issues run one at a time and finalization fast-forwards
  the integration branch the moment the branch commit exists, so no two live
  branches ever coexist. The history Phase 0 produces is linear by
  construction and carries zero collision information. Collision evidence
  arrives in Phase 2, when blocked work first persists on a branch alongside
  new work.
- *"Work has a durable home before it merges"* as a fix for stranding. A
  blocked candidate, an exhausted correction budget, or a killed run never
  reaches finalization, so under Phase 0 that work still dies as dirty paths in
  the shared tree. The three stranded issues in the baseline would have been
  stranded identically. Stranding is fixed by Phase 2 (workers commit), not
  Phase 0.

What survives is narrower and real: Phase 0 exercises the branch mechanics —
creation, fast-forward, branch-guard interplay, journal fields — in production
before Phase 2 stakes worker behavior on them, and (with `ortus-eele`) an
interrupted finalization gains a durable resume point instead of state that
must be reasoned about. That is classic strangler-fig staging and worth a small
hand-landed phase. Folding it into Phase 2 would put untested branch mechanics
and the heaviest self-modification into one change, which is exactly what the
programme's own constraints forbid.

Corrections applied to `ortus-ym33`, `ortus-32m1`, and the PRD's Phase 0
section. A packet whose Behavioral context claims benefits the leaf does not
deliver would mislead the verifier that judges it; that is why the packets were
corrected rather than only the PRD.

### 2. Is the reviewer question already settled, making Phase 4 unnecessary?

**Half settled. Keep Phase 4, reframed (Acted: PRD Phase 4 section).**

The record settles the *mechanical* half: the reviewer's one irreplaceable
catch (two tests failing on a clean checkout) came from *what* was tested — the
committed tree rather than the author's dirty worktree — not from *who* tested
it. Branch checks supply that vantage in the real environment. All three
escapes were environmental, and checks close that class. So review's mechanical
duties transfer to CI with Phases 0–2 as a design decision, not a Phase 4
finding.

The *semantic* half — does a reviewer judging "did the change do what the
packet asked" catch anything CI cannot — is genuinely open. The two-day record
contains no data on it either way, and the brief is right that the sample is
biased. Two things change the framing:

- The cost side collapses regardless of the verdict. The 2,700 lines, the
  seal, and the mutation guard die with the candidate model whether or not the
  reviewer survives. Post-migration, a reviewer is a model call over a commit
  range. So Phase 4 is not "does the reviewer earn its 2,700-line machinery" —
  it is "does a cheap semantic reviewer earn its per-run model cost," a much
  lower bar with a plausibly different answer.
- Removing the reviewer *now* would conflate two changes and destroy the
  measurement. The migration must land with the reviewer in place for Phase 4's
  count to mean anything.

### 3. Should Epic D split off as its own programme and ship now?

**No split — the question dissolves on inspection (Acted: PRD note only).**

Epic D is already independent in the graph: `ortus-s0tj` had no dependency on
any branch leaf, and the chain s0tj → axns → v8bj is grindable today. "Split
into its own programme" would produce a second PRD and no behavioral change —
paperwork, not a decision. The real lever is what grind selects first, and that
is already governed by priority (`ortus-xjdf` at P1 goes before Epic D's P2
leaves). A note was added to the PRD's Epic D entry making explicit that the
phase label orders priority, not dependency, so nobody later infers a blocking
edge that does not exist.

One caveat now enforced in the graph: `s0tj` was serialized behind the other
grind-prompt.md leaves (see NFR-3 below), which is ordering, not coupling to
the branch keystone.

### 4. Is the programme over-fitted to a repository that is its own subject?

**Over-fitted in projected magnitude; not in design. Proceed (Acted: metrics
caveat in PRD).**

Sorting the baseline failures by mechanism: entanglement and stranding come
from the shared dirty tree — a downstream, non-self-modifying repository hit
the same wall independently, which is the strongest single fact against the
over-fitting reading. Environment-divergent review afflicts any repository
whose CI differs from the review sandbox. Only the self-modification hazards
(a scheduler running the code it started with; finalization shipping changes
to finalization) are Ortus-specific, and those shape the *phasing* — what is
hand-landed — not the design. The keystone stands on general failures.

What self-modification does inflate is the *rate*: a third of runs delivering
nothing is an upper bound, and the success-metrics baseline inherits that. The
PRD now states that re-measurement must also be Ortus-on-Ortus for the
comparison to hold.

## The largest finding: Epic B was inert

**No filed leaf ever pushed an issue branch, and the fast-forward waited for
nothing (Acted: `ortus-6a0a.1` filed).**

Verified in code: finalization's sync step pushes the *integration* branch
only (`git.push(integration_branch)` in `_finalize_candidate`), and
`ortus-32m1`'s scope creates and fast-forwards the issue branch without
pushing it. So `ortus-kdqt`'s CI trigger would fire only on a manual branch
push — and even then the fast-forward has already happened, making the branch
run a duplicate of the integration run on the same SHA. As filed, Epic B was
either inert or wasteful, and FR-5 ("checks run against the branch before it
merges") had no leaf delivering its gating half anywhere in the programme.
`ortus-kdqt`'s After-text — "a failure is visible on the branch, before the
fast-forward" — was false.

Filed `ortus-6a0a.1`: finalization pushes the issue branch, waits a bounded
time for the branch's check result, fast-forwards only on green, and reports a
blocker on red or timeout with the work left parked on its pushed branch — the
first point in the programme where a *failing* candidate gains a durable home.
Key packet decisions: opt-in via `.ortusrc` defaulting off (waiting minutes per
finalization is a cost the operator chooses; the default flips later with
evidence); timeout blocks, never merges; results are consulted only after the
push, which avoids re-committing the session's recorded unsatisfiable-criterion
error (requiring CI evidence for an unpushed commit). Human-labeled — it
modifies finalization — and blocked by `ortus-32m1` and `ortus-kdqt`.
`ortus-kdqt` keeps its place as the Phase 0 trigger, with its description
corrected to say it is the prerequisite, not the fix.

## The programme's NFR-3 was not enforced on its own graph

**Serialized the grind-prompt.md leaves (Acted: two dependencies added).**

NFR-2 ("no leaf modifies the code path that will ship it") was enforced via the
`human` label, but NFR-3 ("no two open leaves modify the same file at the same
time") was enforced for Epic D — s0tj blocks axns — and not for Epic C, whose
`ortus-xjdf` and `ortus-z7ib` both modify `src/ortus/prompts/grind-prompt.md`
and `tests/test_grind_prompt_content.py` with no ordering between them, and
`ortus-s0tj` touches the same prompt file across the epic boundary. Under the
current worktree-state candidate model this is precisely the entanglement setup
that cost six attempts and a lost deliverable in the baseline: if the first
leaf blocks and leaves dirty prompt state, the next worker inherits it.

Added `z7ib` depends-on `xjdf` and `s0tj` depends-on `z7ib`, serializing the
whole chain: xjdf → z7ib → s0tj → axns. A dependency does what priority cannot:
if a leaf blocks, its successors stay unclaimable rather than walking into the
dirty file. Rationale recorded as comments on both issues.

## The brief's twelve decisions, dispositioned

| # | Decision | Disposition |
| --- | --- | --- |
| 1 | One programme, not three | **Upheld.** Epic D's independence is real but is a graph fact, not a document fact; noted in the PRD instead of splitting. |
| 2 | Keystone: candidate-as-commit | **Upheld.** Even if the hours-old attribution fixes hold, 2,700 lines of inference that can be silently wrong is standing risk against a property git provides natively. The counterargument would justify delay, never the machinery. |
| 3 | Phasing by self-modification limits | **Upheld.** The "just restart grind" mitigation covers the stale-scheduler hazard but not finalization shipping changes to finalization, which happens within a run — observed three times. |
| 4 | Phase 0 landed by hand | **Upheld.** Three failures in two days at exactly this seam; the brief said little would change its mind and nothing found here should. |
| 5 | `ortus plan` skipped, graph hand-authored | **Upheld.** The leaves were read in full and are stronger than typical generated packets — concrete symbols with line numbers, resolved decisions with reasons, edge cases that bite. Regenerating would lose the measured evidence for no structural gain. |
| 6 | Branches, not worktrees | **Upheld.** The `/proc/mounts` evidence is dispositive and the deeper point stands: worktrees solve a concurrency problem this system does not have. |
| 7 | Phase 0 fast-forward only | **Corrected.** The counter was right and understated — see above. Phase 0 kept, re-justified honestly; evidence claim withdrawn in PRD and packets. |
| 8 | Crew memory in the tracker | **Upheld.** The attribution carve-out is the decisive property while the worktree-state model lives; the scaling concern is real but already bounded by s0tj's own packet (count and length caps, deterministic selection). |
| 9 | Review history needs no new storage | **Upheld as a deferral.** Untested, but nothing is filed against it yet, so nothing to correct; it becomes testable once retrieval is attempted. |
| 10 | Reviewer's fate deferred to Phase 4 | **Reframed.** Mechanical half settled now; semantic half measured at Phase 4 against a per-run cost bar, with the reviewer kept through the migration so the measurement is clean. |
| 11 | No vocabulary renaming | **Upheld.** "Renames get harder, never easier" is true and still loses: renaming amid a live migration risks the migration for connotation. After Phase 2 the worst term (`disown`) is deleted with its machinery anyway. |
| 12 | Epic F strictly after E | **Upheld.** Trivially correct, high consequence if reordered. |

## What was deliberately not changed

- **The experiment documents.** `CANDIDATE_ISOLATION.md` repeats the
  collision-evidence claim this review withdrew ("The cheapest way to find
  out"). It was left as written: the experiments are the dated reasoning
  record, the PRD is the operative document, and the correction lives where
  decisions are read. Rewriting the record to agree with its own review would
  destroy the audit trail.
- **The `human` labels.** All correctly placed. `ortus-kdqt` looks like an
  exception (CI configuration is grindable by the brief's own rule) but is
  not: its verification requires pushing a branch and reading `gh run list`,
  and workers are forbidden to push.
- **Leaf packets outside Phase 0.** `xjdf`, `z7ib`, `s0tj`, `axns`, `v8bj`
  were read in full and found sound; no edits.
- **The success-metric targets.** The baseline is an upper bound, but the
  targets are directional and the comparison constraint is now stated;
  re-deriving numbers from a biased sample would be false precision.

## Where this review is most likely wrong

- The Phase 0 critique assumes strictly serial issue execution with immediate
  fast-forward. If the scheduler ever interleaves finalizations, some
  collision evidence could appear earlier than claimed here.
- `ortus-6a0a.1` commits to reading check results from the forge without
  specifying the mechanism (`gh` vs API). If the grind environment cannot hold
  a credential, the leaf's plan-gap guidance routes it back rather than
  improvising — but that is a deferral, not an answer.
- The serialization chain (xjdf → z7ib → s0tj) trades throughput for safety.
  If xjdf stalls on a plan gap, three leaves stall behind it. Under the
  current candidate model that is the correct trade; after Phase 2 those
  dependencies should be dropped as obsolete.

## Record of actions

| Action | Where |
| --- | --- |
| Landed `ortus-frht` candidate, closed the issue | commit `d648867` |
| Filed the merge-gating leaf | `ortus-6a0a.1`, human, blocked by 32m1 + kdqt |
| Corrected Phase 0 claims | `ortus-ym33`, `ortus-32m1`, `ortus-kdqt`, PRD |
| Reframed Phase 4, caveated metrics, noted Epic D independence | PRD |
| Serialized grind-prompt.md leaves | deps z7ib→xjdf, s0tj→z7ib, with comments |
| Verified graph readiness and selection order | `ortus grind --dry-run`: silent; first grindable leaf is `ortus-xjdf` |
| Pushed all of it | commit `71d3174` and this document |
