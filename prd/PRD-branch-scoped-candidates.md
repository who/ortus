# PRD: Branch-scoped candidates and the review model

## Metadata

- **Feature ID**: ortus-branch-candidates (proposed; assign at decomposition)
- **Project Type**: Ortus core scheduler and transaction model
- **Created**: 2026-08-10
- **Status**: **Superseded** by `prd/PRD-lean-pipeline.md`, by operator decision on 2026-08-11, before implementation began. The keystone (candidate as commit on a branch), the gate leaves (`ortus-kdqt`, `ortus-6a0a.1`), Epic C, and Epic D carry forward into the lean programme unchanged. The implementer/verifier split this document reforms is retired there instead; the integrator role is replaced by rebase-on-resume. This document remains the reasoning record for everything the lean programme inherits.
- **Confidence**: High on the problem, measured rather than argued — see Background. Medium on the end state, because no one has yet changed the candidate model while candidates were in flight.
- **Source material**: `experiments/CANDIDATE_ISOLATION.md`, `experiments/REVIEW_MODEL.md`, `experiments/TEAM_STRUCTURE.md`. Those three remain the reasoning record; this document is the program they converge on.

---

## Overview

### Problem statement

Ortus defines a candidate as **worktree state** — the dirty paths, minus an operator baseline, minus whatever a worker declared unrelated — sealed by hashing a diff of that state. Every downstream difficulty follows from that one definition.

Because dirty files carry no authorship, the system must infer it. That inference is an attribution module, handoff and disowning logic in the scheduler and the transaction, and their tests: roughly 2,700 lines whose entire job is answering *whose dirty file is this*.

Because the inference is made once, at handoff, before a worker knows which files its own change will touch, an edit to a shared file that was not in scope can strand an issue's whole deliverable. That failure cost one issue six attempts and another its entire contribution, and a downstream repository hit the same wall independently.

Because a separate reviewer needs something immutable to judge, review happens against a frozen worktree in a read-only sandbox — which is not the environment the code must survive. Over two days, three changes passed review and then failed every matrix leg: one on terminal colour, one on a tool present locally and absent in CI, one on tracker exports being clean.

And because a candidate cannot span issues, work that touches more than one thing cannot be carried at all. Over the same two days, eleven commits were finalized by the pipeline and seventeen were committed by hand — the seventeen being the entangled-candidate separations, the CI repairs, the red-branch fixes, a revert of the pipeline's own work, and all documentation.

### Proposed solution

Make a candidate **a commit on a per-issue branch**, and move mechanical assurance to **checks that run on that branch before it merges**.

- Commits carry authorship, so attribution is unnecessary.
- The seal becomes a commit SHA — stronger than a diff hash, and native to the tool.
- Nothing a reviewer does to the worktree can alter what is under review, so the mutation guard is unnecessary.
- Parked work is a named branch, so a blocked issue leaves nothing in the next worker's tree.
- Non-overlapping edits to one file merge automatically instead of requiring attribution; genuinely overlapping edits become a conflict with a merge base and standard tooling.
- Checks run where the code must work, closing the environmental blind spot.

Conflicts gain an owner — an integrator that reads both issues' packets rather than only the diff text, proposes a resolution confined to the conflicted hunks, and escalates when the disagreement is about what the code should do rather than about text.

The agent reviewer is then re-scoped to what checks cannot judge — whether the change did what the packet asked — and its continued existence becomes a measurement rather than an assumption.

### Success metrics

Measured over a comparable working period, against the two-day baseline recorded in Background:

| Metric | Baseline | Target |
| --- | --- | --- |
| Commits finalized by the pipeline, as a share of all commits | 11 of 28 | Majority |
| Runs that deliver nothing | ~1 in 3 | ~1 in 10 |
| Changes that pass review and then fail the checks | 3 | 0 |
| Lines of attribution machinery | ~2,700 | 0 |
| Issues stranded by an out-of-scope edit to a shared file | 3 | 0 |

Metric 3 cannot move until `ortus-6a0a.1` lands (Phase 2): the Phase 0 branch trigger produces a signal, but only that leaf makes the merge wait for it. Metrics 4 and 5 move at Phase 2. The baseline was measured on a self-modifying repository and is an upper bound, so re-measurement must also be Ortus-on-Ortus for the comparison to hold.

---

## Background & context

### Why now

The model has been exercised hard enough to produce evidence rather than impressions. `experiments/REVIEW_MODEL.md` records the measured account; the two figures that matter most are that the pipeline could not repair its own failures, and that every escape was environmental.

### What the current model gets right

Two properties must survive any change, because they are the reason the machinery exists:

- **Review sees what the author cannot.** The reviewer's best catch was a candidate that would have landed two tests failing on a clean checkout, found by testing the committed tree while the implementer had tested its dirty worktree. That value comes from *what is tested*, not from *who tests it* — which is precisely why branch checks can supply it.
- **Nobody closes their own work.** An agent that can mark its own task complete eventually will, wrongly.

### What Ortus cannot safely do to itself

This is the constraint that shapes the phasing, and it is not theoretical:

- A long-lived scheduler runs the code it started with. Three commits were written by code that predated their own fix.
- Finalization cannot ship a change to finalization. A commit-message fix shipped in the old format, a truncation fix shipped truncated, a markup fix shipped with the markup it removed.
- Two issues touching one file entangle, and separating them is manual.
- Changing the candidate model while candidates are in flight has never been attempted.

---

## Users & personas

**The operator.** Runs `ortus grind`, resolves what the system escalates, and today performs every repair the pipeline cannot. Their measure of success is how rarely they are asked to run a push for work the pipeline should have carried.

**The downstream consumer.** Runs Ortus against a repository that is not Ortus. They encounter this model's failures without the context to diagnose them, and filed one such report during the baseline period.

---

## Requirements

### Functional requirements

- **FR-1**: A candidate is a commit, or a range of commits, on a branch named for its issue.
- **FR-2**: Finalization merges that branch into the integration branch and closes the issue. No worker closes an issue.
- **FR-3**: The seal is the branch head SHA. Verification records it and judges that object.
- **FR-4**: Work belonging to a blocked or abandoned issue remains on its branch and never appears in another issue's working tree.
- **FR-5**: Checks run against the branch before it merges, in the environment the integration branch uses.
- **FR-6**: A merge conflict routes to an integrator with both issues' packets, both verification records, the merge base and both sides.
- **FR-7**: The integrator's diff is confined to conflicted hunks; anything outside them is new work.
- **FR-8**: A merged result is verified before it lands, because a merge is a change neither side's verification covered.
- **FR-9**: The integrator escalates rather than deciding when the two packets' acceptance criteria cannot both hold, when resolution would change unrequested behavior, or when one side's intent cannot be established.
- **FR-10**: Attribution, disowning, re-adoption and the candidate mutation guard are removed once nothing depends on them.

### Non-functional requirements

- **NFR-1**: Every phase leaves the system shippable. No half-migrated candidate model.
- **NFR-2**: No leaf modifies the code path that will ship it. A change to finalization, the scheduler loop, the journal or candidate capture is landed by a human, or verified by a process started after the change.
- **NFR-3**: No two open leaves modify the same file at the same time.
- **NFR-4**: A repository with no remote must finalize exactly as it does today. Branch checks before merge are opt-in where no remote exists.
- **NFR-5**: Journals written before a phase must not strand a run started after it.
- **NFR-6**: The integration branch is never left red by a phase boundary.

---

## System architecture

### What changes

- **Finalization** stops staging paths and starts merging a branch. The path-scoped commit and its staging set are replaced by a merge and a fast-forward.
- **The journal** records a branch name and a head SHA where it recorded candidate paths and a diff hash.
- **The verification contract** receives a commit range rather than a diff artifact, and is told the branch, not the worktree, is the subject.
- **The git client** gains branch creation, merge, conflict detection and branch listing; it loses nothing.
- **The worker contract** permits committing on the issue branch, and continues to forbid closing, pushing to the integration branch, and switching away from the branch it was handed.
- **CI** gains a branch trigger so checks run before merge.

### What is added

- An **integrator** phase and module: reads both packets, proposes a resolution bounded to conflicted hunks, escalates by the stated criteria.

### What is deleted

The attribution module, the handoff and disowning logic, re-adoption, the candidate mutation guard, and their tests — once FR-1 through FR-5 hold and nothing references them.

### What is explicitly preserved

Independent review of *something*, the prohibition on self-closing, bounded context per phase, and the readiness gate.

---

## Milestones & phases

### Phase 0 — Keystone (landed by hand)

Finalization commits to a branch and fast-forwards the integration branch. The CI trigger for issue branches is added. **No worker behavior changes at all.**

Landed by a human because it modifies the mechanism that would otherwise ship it.

*Corrected at review, 2026-08-11.* Phase 0 changes no failure mode by itself, and its original evidence claim is withdrawn: issues run one at a time and each branch is fast-forwarded the moment it is committed, so no two live branches ever coexist — the branch history cannot show how often issues collide. Collision evidence arrives in Phase 2, when blocked work first persists on a branch alongside new work. Phase 0's real value is narrower: the branch mechanics (creation, fast-forward, branch-guard interplay, journal fields) are exercised in production before Phase 2 stakes worker behavior on them, and an interrupted finalization gains a durable resume point. The stranding failures in Background are likewise untouched until Phase 2 — blocked work never reaches finalization, so it never reaches a branch here.

### Phase 1 — Make the next run cheaper (grindable)

Bounded background waits, the prohibition on throwaway worktrees, crew memory read and propose, and the retrospective pass. Prompts and new modules only; no shipping path touched. First because two of its leaves already exist and both reduce the cost of every subsequent run.

### Phase 2 — Candidate as commit (landed by hand, or verified by a fresh process)

Workers commit on their branch. The candidate becomes a commit range. The attribution machinery is removed. The heaviest self-modification in the programme.

Phase 2 also lands the leaf that makes Epic B real: finalization pushes the issue branch and gates the fast-forward on the branch's check result (opt-in, bounded wait, blocker on red or timeout). Until that leaf lands, the Phase 0 trigger is inert — nothing pushes issue branches and the fast-forward waits for nothing — and success metric 3 (review-passed, checks-failed changes: 3 → 0) cannot move. Filed as `ortus-6a0a.1`, human-landed because it modifies finalization.

### Phase 3 — Integrator (grindable)

Conflict ownership, bounded resolution, escalation, and verification of merged results. Mostly new code.

### Phase 4 — Decide the review model (not code)

Re-measure against the Success Metrics. If the agent reviewer catches nothing the branch checks did not, remove it.

*Reframed at review, 2026-08-11.* The two-day record already settles the mechanical half of the question: the reviewer's one irreplaceable catch (the clean-checkout failure) came from testing the committed tree rather than the dirty one, and branch checks supply exactly that vantage in the real environment — so review's mechanical duties transfer to checks with Phases 0–2, not as a Phase 4 finding. What Phase 4 measures is only the semantic half: whether a reviewer judging "did the change do what the packet asked" catches anything CI cannot. Note the cost side of that measurement changes too — the 2,700 lines, the seal and the mutation guard die with the candidate model regardless of the reviewer's fate, so the bar the semantic reviewer must clear is its per-run model cost, which is far lower than the machinery it is currently blamed for. Do not remove the reviewer before Phase 4: removing it alongside the candidate migration would conflate two changes and destroy the measurement.

---

## Epic breakdown (proposed; refine at decomposition)

- **Epic A — Branch-scoped finalization.** Branch creation and naming; merge and fast-forward in place of path-scoped commit; journal records branch and head; recovery resumes by checkout. *Phase 0.*
- **Epic B — Checks before merge.** Branch trigger (*Phase 0*); pushing the issue branch and gating the fast-forward on its checks (*Phase 2*, `ortus-6a0a.1`); degradation where no remote exists.
- **Epic C — Worker cheapness.** Bounded waits; worktree prohibition; parallel sweeps. *Phase 1.*
- **Epic D — Crew memory.** Read at orientation, propose at completion, curate, prune; the retrospective that feeds it. *Phase 1.* Independent of the branch keystone: its leaves are unblocked in the graph and may grind now, before or alongside Phase 0 — the phase label orders priority, not dependency.
- **Epic E — Candidate as commit.** Worker commits; commit-range candidate; seal as SHA; verification contract. *Phase 2.*
- **Epic F — Remove attribution.** Delete the module, the handoff and disowning logic, the mutation guard and their tests. *Phase 2, strictly after E.*
- **Epic G — Integrator.** Phase, module, bounded resolution, escalation, post-merge verification. *Phase 3.*
- **Epic H — Measure the reviewer.** Outcome tracking joining each verdict to what happened next; re-measure; decide. *Phase 4.*

---

## Open questions

- **Does the agent reviewer survive Phase 4?** Deliberately unanswered. The programme exists partly to make the question answerable.
- **How large may the always-loaded memory tier grow** before it taxes every worker? Thirteen entries today, injected wholesale with no relevance filtering.
- **Should merges be risk-tiered** — automatic for low-risk changes, human approval for medium, human-only for critical paths? Documented practice elsewhere; interacts directly with Epic G.
- **How are semantic conflicts caught?** Two changes can merge cleanly and break together. The proposed answer is merging the integration branch forward into the issue branch before verification, so the reviewer judges the combination.
- **What is the right branch retention policy** for issues that are abandoned rather than blocked?

---

## Out of scope

- **A worktree per issue.** Cleanup is unrecoverable under the sandbox: individual files inside `.git/worktrees/<name>/` are bind-mounted read-only, so removal fails with a busy device for as long as any sandbox holds them, and the stale registration persists. Isolation between *successive* workers is the actual failure mode, and a branch supplies it without a duplicated dependency install, a cold build or a second index.
- **Renaming the vocabulary.** `disown`, `harness` and `worker` carry connotations the new design does not, but they appear hundreds of times across code, prompts and logs. A rename follows a settled design; it does not lead one.
- **Naming the human's seat** in the team model. Deferred deliberately.
- **Mandatory pull requests.** Checks before merge do not require a forge. Repositories without a remote must keep working.

---

## Appendix

### Appendix A — The measured baseline

Two days of continuous use produced twenty-eight commits: eleven finalized by the pipeline, seventeen committed by hand. Twelve runs, of which roughly a third delivered nothing while consuming twenty to ninety minutes each. Three changes passed review and then failed every matrix leg, each because review ran somewhere unlike the place the code had to work. Full account in `experiments/REVIEW_MODEL.md`.

### Appendix B — Why non-overlapping edits are the common case

Two issues both edited one file during the baseline period and cost six attempts, a lost deliverable and hours of manual separation. Their hunks were at line 246 and at lines 107, 118, 141, 364, 379 and 393 — no overlap at all. Under branches, git merges that without asking anyone.

### Appendix C — Self-modification hazards to design against

A scheduler holds the code it started with, so a change to the loop needs a fresh process to take effect. Finalization cannot ship a change to finalization. Two open leaves touching one file entangle. Each is a constraint on how this programme is executed, not a problem it solves.
