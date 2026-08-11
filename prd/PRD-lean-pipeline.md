# PRD: The lean pipeline — one writer, machine verification

## Metadata

- **Feature ID**: ortus-lean-pipeline
- **Project Type**: Ortus core scheduler, verification, and transaction model
- **Created**: 2026-08-11
- **Status**: Active. Supersedes `prd/PRD-branch-scoped-candidates.md` by operator decision, 2026-08-11.
- **Confidence**: High on the cost analysis, which is structural rather than estimated. Medium-high on the design, which retires machinery rather than adding it. The escape rate is instrumented and carries a pre-committed reversal threshold, so the riskiest bet is reversible by configuration.
- **Source material**: `experiments/LEAN_PIPELINE.md` (the full design and trust ledger), `experiments/REVIEW_DECISIONS.md` (the review that shaped it), and the three documents behind the superseded PRD, whose measured evidence this programme inherits.

---

## Overview

### Problem statement

The pipeline pays for orientation — a fresh agent reading the packet, exploring the repository, deriving what to check — two to six times per landed change, and orientation is the most expensive thing a model does. The verifier's context is a lossy reconstruction of context the system just discarded; each correction round rebuilds two more from nothing, which is how two rounds once produced byte-identical candidates. Meanwhile the measured record shows what that spend bought: one irreplaceable catch that came from *what* was tested (the committed tree) rather than *who* tested it, three escapes it missed (all environmental), and a rejection record dominated by problems the split itself created.

### Proposed solution

Keep the keystone — a candidate is a commit on a branch — and retire the implementer/verifier split. One session implements, checks, and commits. A deterministic harness verifies: it runs the packet's acceptance commands against a clean archive extraction of the branch, proves `proves-new` criteria red on the merge base and green on the branch, diffs the worker's claims against actual results, waits for the CI gate where one exists, and merges on green. The agent reviewer becomes an optional pipeline step, default off, one flag from returning per risk tier. Corrections return to the same session instead of spawning fresh contexts. Trust is not transferred to the implementer; it is retired: the worker still cannot close, merge, or push to the integration branch, and its claims are never load-bearing.

The full design, the trust ledger, and the failure-mode inventory are in `experiments/LEAN_PIPELINE.md`. This document is the programme.

### Success metrics

Measured against the two-day baseline in the superseded PRD, plus one new metric this programme exists for:

| Metric | Baseline | Target |
| --- | --- | --- |
| Model tokens per landed change (from run logs) | unmeasured; structurally ≥ 2 orientations + verify narration | ≤ 50% of a measured pre-pivot sample |
| Commits finalized by the pipeline, as a share of all commits | 11 of 28 | Majority |
| Runs that deliver nothing | ~1 in 3 | ~1 in 10 |
| Escape rate (merged changes later contradicted by CI, a revert, or a filed bug) | 3 of 11 (~27%) | ≤ baseline; exceeding it triggers the reviewer flag |
| Lines of attribution machinery | ~2,700 | 0 |

---

## Requirements

### Functional requirements

- **FR-1**: A candidate is a commit, or range of commits, authored by the worker on `ortus/<issue-id>`.
- **FR-2**: The worker writes the commit message at commit time; finalization validates it deterministically and repairs or replaces an invalid one by amending before merge. The separate compose pass is retired.
- **FR-3**: Verification is a deterministic harness pipeline: AC runner → red–green proof → claim diff → gate (where enabled) → optional agent reviewer (default off).
- **FR-4**: The AC runner executes the packet's Criterion checks as subprocesses in a disposable shared clone of the branch (`git clone --shared` — an ordinary directory, not a worktree), with the environment prepared by the repository's sync convention, and records commands, exit codes, and output as a tracker comment. *(Corrected 2026-08-11: `git archive` was originally specified, but an archive tree cannot build where the version derives from vcs metadata, as it does here — verified both directions. The hermeticity an archive would give is traded for checks that actually execute.)*
- **FR-5**: A criterion tagged `proves-new` must fail on the merge base and pass on the branch; `guards-existing` must pass on both; an untagged criterion runs on the branch only (legacy behavior). The tags are usable the moment the red–green leaf lands — readiness v1 accepts them as data — and every packet authored after L1 must tag its criteria by convention; schema v2 (L3) only formalizes this. Stated here so the mechanism does not land without the thing that makes it fire — the inert-trigger lesson from the superseded programme's Epic B.
- **FR-6**: The worker's completion report maps claims to criteria; any disagreement between claims and AC-runner results fails the pipeline.
- **FR-7**: Pipeline failures return to the same worker session (or resumed context) with the exact command and output; retries are bounded; at the bound the branch parks committed and the issue routes to a human.
- **FR-8**: On full green, the harness merges, closes, and pushes. No worker closes, merges, or pushes to the integration branch — mechanically, not contractually.
- **FR-9**: The `acceptance_criteria` field is hashed at claim; the worker is judged by the contract as claimed, and cannot alter it.
- **FR-10**: A parked branch is merged forward from the integration head before re-verification on resume, so the combination is what gets verified. The resuming worker resolves textual conflicts on its own branch; semantic disagreement between packets routes as a plan gap.
- **FR-11**: Every merge is joined to its outcome (integration CI, reverts, bugs filed against it), yielding a continuously computed escape rate.
- **FR-12**: Attribution, disowning, re-adoption, the candidate mutation guard, and the correction-packet machinery are removed once nothing depends on them.

### Non-functional requirements

- **NFR-1**: Every phase leaves the system shippable, with verification intact in some form at every point.
- **NFR-2**: No leaf modifies the code path that will ship it. Changes to finalization, the scheduler loop, the journal, or candidate capture are landed by a human or verified by a process started after the change.
- **NFR-3**: No two open leaves modify the same file at the same time; enforced by dependencies in the graph.
- **NFR-4**: A repository with no remote verifies via the local AC runner and finalizes exactly as the gate-less path does today.
- **NFR-5**: The escape-rate reversal threshold is enforced as configuration, not re-architecture: reinstating the agent reviewer, globally or per risk tier, is one flag.
- **NFR-6**: Journal phase vocabulary changes update `src/ortus/core/lifecycle.py` and regenerate the README state-graph block, per repository law.

---

## Milestones

### Phase L0 — Keystone (landed by hand)

The worker commits on its issue branch; the harness cuts the branch at claim, merges on verification, closes, and pushes. The verifier agent survives L0 as an interim shim reading the `merge-base..head` range instead of the worktree diff, so verification is never absent. The compose pass retires; the journal records branch and head; resume is a checkout. Subsumes the superseded PRD's Phases 0 and 2 in one change — legitimate because the verification contract that forced a two-step migration is being retired, not preserved. Landed as two commits within the phase (harness cuts and merges with workers unchanged; then workers commit), preserving a bisection point at the seam where three failures occurred in two days.

### Phase L1 — Machine verification (mixed)

The AC runner and red–green proof land as new modules (grindable). The pipeline is then wired into the harness, the verifier agent becomes a default-off step, corrections move in-context (human — shipping path). Attribution and its dependents are deleted last (human).

### Phase L2 — The gate

`ortus-kdqt` and `ortus-6a0a.1`, adopted from the superseded programme unchanged, re-pointed at the L0 keystone.

### Phase L3 — The loop closes (decompose when L1 lands)

Escape tracking joins merges to outcomes. The retrospective (`ortus-v8bj`) gains a sampled audit of merged changes against their packets. Risk tiers configure the pipeline per change class. Readiness schema v2 slims packets to seven sections while tightening Acceptance. Leaves are filed when L1's shape is settled, per the decomposition precedent.

Epic C (worker cheapness) and Epic D (crew memory) continue as filed — they were always independent, and crew memory is the main redundancy against author blind spots once per-change review is off.

---

## Out of scope

- **A worktree per issue** — unchanged from the superseded PRD; the sandbox evidence stands.
- **A dedicated integrator role** — replaced by rebase-on-resume plus merge-forward-before-reverify (FR-10). Its escalation criteria survive as worker-prompt text.
- **Vocabulary renaming** — most of the loaded vocabulary (`disown`, the seal apparatus) is deleted rather than renamed by L1.
- **Mandatory pull requests** — the gate remains branch-push based and opt-in.

## Open questions

- **Session resume mechanics for in-context corrections**: resuming a completed backend session versus holding it open across the verification pipeline; decided at L1 wiring.
- **The audit sampling rate**, and whether it should concentrate on changes whose diff touched files that `guards-existing` checks execute.
- **Whether untagged criteria should eventually default to `proves-new`** once packet conventions migrate.
- **What the risk-tier boundaries are** — file-path based, label based, or packet-declared.
