# Review brief: scrutinise the branch-scoped candidates programme

You are reviewing work done on 2026-08-09 and 2026-08-10 by a different model in
a session you cannot see. Everything needed to judge it is either in this
document or named by it. **You are expected to overturn decisions, not ratify
them.** Where a decision is weak I have tried to say so; where I have been wrong
today I have listed it, because my error rate is relevant evidence about how much
weight to give the rest.

## What exists

| Artifact | What it is |
| --- | --- |
| `prd/PRD-branch-scoped-candidates.md` | The programme. Read this first. |
| `experiments/REVIEW_MODEL.md` | The measured case that the current review model does not pay for itself |
| `experiments/CANDIDATE_ISOLATION.md` | The argument that a candidate should be a commit, not worktree state |
| `experiments/TEAM_STRUCTURE.md` | The collaborative model: crew memory, right of reply, the integrator |
| bd graph | 4 epics, 6 new leaves, 2 pre-existing leaves adopted. Ids below. |

The bd graph as authored:

```
Epic A  ortus-ym33  branch-scoped finalization          [human]
        ortus-32m1  land candidates on an issue branch  [human]
        ortus-eele  journal the branch; resume by checkout [human, blocked by 32m1]
Epic B  ortus-6a0a  checks before merge                  [human]
        ortus-kdqt  gate runs on issue branches          [human, blocked by 32m1]
Epic C  ortus-j6o9  make the next run cheaper
        ortus-xjdf  bounded background waits             (P1)
        ortus-z7ib  forbid worktrees, use git archive    (P2)
Epic D  ortus-ddt7  crew memory
        ortus-s0tj  read stored lessons at orientation
        ortus-axns  propose a lesson, pending until curated [blocked by s0tj]
        ortus-v8bj  retrospective proposes from records  [blocked by axns]
```

Also open and **not** part of the programme: `ortus-frht`, whose candidate is
sitting uncommitted in the working tree. See Current state.

## The measured evidence

Everything below is counted, not estimated. It is the basis for the whole
programme, so if you distrust the counts, distrust the programme.

- **28 commits over two days: 11 finalized by the pipeline, 17 by hand.** The 17
  are the entangled-candidate separations, three CI repairs, two red-branch
  fixes, one revert of the pipeline's own work, and all documentation.
- **12 runs; roughly a third delivered nothing**, each consuming 20–90 minutes.
- **3 changes passed review then failed every CI leg.** One on terminal colour,
  one on a tool present locally and absent on runners, one on tracker exports
  being clean. All three are environment divergence.
- **~2,700 lines** exist to answer *whose dirty file is this*: `attribution.py`
  (456), handoff and disowning logic in `grind.py` (174) and `transaction.py`
  (33), and their tests (2,032).
- **3 issues were stranded** by an out-of-scope edit to a shared file.
- One reviewer catch nothing else would have made: a candidate that would have
  landed two tests failing on a clean checkout.

## Decisions to scrutinise

Each has my reasoning, the strongest counterargument I know, and what would
change my mind.

### 1. One programme, not three

Three documents were merged into one PRD because they converge on a single
keystone. *Counter:* crew memory and the retrospective have nothing to do with
branches and could ship independently today. *Would change my mind:* if you think
Epic D should be its own programme, split it — it has no real dependency on
Epics A, B or E.

### 2. The keystone is candidate-as-commit-on-a-branch

Everything traces to defining a candidate as worktree state. *Counter:* the
entanglement failures might be adequately fixed by the attribution work that
already landed, at far lower risk than replacing the candidate model.
*Would change my mind:* evidence that the attribution fixes have actually held.
They are only hours old and largely untested in anger.

### 3. Phasing is driven by self-modification limits, not topic

A scheduler runs the code it started with; finalization cannot ship a change to
finalization; two leaves touching one file entangle. All three observed.
*Counter:* this makes the programme slower and more manual than it needs to be,
and a simpler mitigation — always restart grind after a change to `grind.py` —
might cover most of it. *Would change my mind:* a demonstration that a fresh
process after each core change is sufficient.

### 4. Phase 0 is landed by hand

Explicitly authorised, and it violates the normal rule that work completes
through grind. *Counter:* this is the most conservative possible reading of the
risk, and hand-landing means the pipeline never proves it can do the work.
*Would change my mind:* honestly, not much — asking a process to atomically
replace its own shipping mechanism failed three times in two days. But the scope
of what is hand-landed could shrink.

### 5. `ortus plan` was skipped; the graph was hand-authored

Because the planner would re-derive the design from the PRD without the measured
evidence. *Counter:* that is exactly the argument every human makes for skipping
process, and it means the packets have never been through the tool that normally
produces them. *Would change my mind:* if you read the leaves and find them
weaker than what planning produces, regenerate them.

### 6. Branches, not worktrees

Worktree cleanup is unrecoverable under the sandbox — files inside
`.git/worktrees/<name>/` are bind-mounted read-only, so removal fails busy for as
long as any sandbox holds them, and the stale registration persists. Verified
directly in `/proc/mounts`. *Counter:* none I find persuasive; this one I would
defend hardest.

### 7. Phase 0 is fast-forward only, with no merge handling

Keeps the first change small. *Counter:* it means Phase 0 cannot handle the very
case that motivates the programme, so its evidentiary value is limited to
counting near-misses. *Would change my mind:* if you think the branch history
from Phase 0 will not actually tell us how often issues collide, then Phase 0 is
mostly ceremony and should be merged into Phase 2.

### 8. Crew memory lives in the tracker

Because tracker exports are already carved out of candidate attribution, so a
write during a run cannot disturb a candidate. No other store has that property.
*Counter:* the tracker injects all memories into every session with no relevance
filtering, so this scales badly and the bound is unspecified. *Would change my
mind:* a concrete proposal for relevance-filtered retrieval.

### 9. Review history needs no new storage

Every verifier report is already a durable tracker comment; playbooks are a query
over closed issues whose locations overlap. *Counter:* untested. Nobody has
tried retrieving them, and the reports may be too verbose to inject usefully.

### 10. The reviewer's fate is deferred to Phase 4

Rather than removing it now. *Counter:* the operator's own read — after watching
it all day — was that the split has significant design problems, and deferring is
how designs survive on inertia. *Would change my mind:* if you judge the
evidence already sufficient, say so; Phase 4 exists because I thought the sample
was biased, not because I think the reviewer is fine.

### 11. No vocabulary renaming

`disown`, `harness`, `worker` carry the wrong connotations. Deferred because they
appear hundreds of times. *Counter:* renames get harder, never easier.

### 12. Epic F strictly after E

Delete attribution only once nothing depends on it. Low controversy, high
consequence if reordered.

## The strongest argument against the whole programme

The sample is biased and the bias runs one way. Both days were spent changing
Ortus *with* Ortus — a scheduler finalizing changes to finalization, a truncation
fix shipping truncated, a markup fix shipping with the markup it removed. A
repository that is not its own subject generates none of that. **Today's failure
rate is an upper bound, not a typical one.** If you conclude the programme is
over-fitted to a self-modifying repository, that is a legitimate reading and it
would justify doing Epic C and D only, and stopping.

## My error rate today, for calibration

I was wrong about these, in this session, and each was caught by someone or
something other than me:

- Filed an issue to strip backticks from commit messages on a premise I had not
  verified. It shipped, then had to be reverted.
- Wrote an acceptance criterion requiring a pushed commit's CI result, which
  cannot exist before the commit is pushed. It made the issue unsatisfiable and
  burned two correction attempts.
- Diagnosed a worker as deadlocked **twice**, when its output file was empty
  purely because the command piped through `tail`, which emits nothing until end
  of file. Recommended a kill on that basis.
- Asserted the read-only uv cache was causing slow runs. Disproven — the
  project-local cache is warm.
- Gated a change on a machine where a required tool was installed, pushed it, and
  broke all six CI legs. This is the same class of error the programme is meant
  to fix, made while writing the programme.
- Missed that the integration branch had been red for two hours.

Two things I got right that are load-bearing here: diagnosing the resume-flag
leak from log evidence, and diagnosing the red-branch test defect. Weigh
accordingly.

## Current state you must not trip over

- **One commit unpushed:** `b315a3f`, the PRD.
- **The working tree carries `ortus-frht`'s uncommitted candidate** — four files:
  `src/ortus/commands/grind.py`, `src/ortus/core/compose.py`,
  `tests/test_core_compose.py`, `tests/test_grind_finalization.py`. Its own
  suites pass (106 tests, 70s). The issue is open and unclaimed. **Decide this
  before any grind run**: land it, or close the issue and revert. A fresh worker
  will otherwise inherit those four files as foreign work, which is the exact
  failure the programme exists to remove.
- Phase 0 leaves carry the `human` label, so selection skips them. Epics are
  skipped by type. The first thing grind would take is `ortus-xjdf`.
- The integration branch is green as of the last check.

## What I would most like challenged

1. Is Phase 0 worth doing at all, or should it fold into Phase 2?
2. Is the reviewer question already settled by the evidence, making Phase 4
   unnecessary?
3. Should Epic D split off as its own programme and ship now?
4. Is the whole thing over-fitted to a repository that is its own subject?
