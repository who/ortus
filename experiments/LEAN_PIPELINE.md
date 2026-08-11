# The lean pipeline: one writer, machine verification

## Status and relationship to the existing programme

An alternative to the programme in `prd/PRD-branch-scoped-candidates.md`,
written 2026-08-11 at the operator's request by the model that reviewed that
programme. It shares the keystone — a candidate is a commit on a branch — and
deliberately diverges everywhere else. The PRD reforms the implementer/verifier
split; this document removes it. Neither document is subordinate to the other:
they are two prices for the same property, and the choice between them is the
operator's.

The goal, stated as the operator stated it: **lower the end-to-end token cost
of implementing and verifying a change**, sacrificing some of the apparent
trust separation between implementer and verifier — while keeping every
invariant that the two days of measurement showed actually protects the work.

## Where the tokens actually go

Cost per landed change in the current pipeline, structurally. Let **O** be one
orientation — a fresh agent reading the packet, exploring the repository, and
building the context to act. O is the dominant unit: it is paid in full by
every fresh context, and nothing carries over.

| Phase | Cost | Of which is re-derivation |
| --- | --- | --- |
| Implement | O + the work itself | — |
| Verify | O′ + re-running checks + narrating them | Nearly all of O′: the verifier re-reads the packet, re-explores the code, re-derives what to check — knowledge the implementer held minutes earlier |
| Compose (commit message) | small model pass + validation loop | The pass re-reads a diff its author could have described from memory |
| Each correction round | O + O′ again | Two full re-orientations per round, which is how two rounds once produced byte-identical candidates: the fresh implementer could not remember its own previous attempt |
| Failed runs (~1 in 3) | everything above | Total loss |

The pattern: **the pipeline pays for orientation two to six times per change,
and orientation is the most expensive thing a model does.** The verifier's
context is almost entirely a lossy reconstruction of context the system just
discarded. The trust split's per-change cost is not the verdict — it is the
second orientation, every time, plus the correction protocol that multiplies
it.

What the measured record says that spend bought: one irreplaceable catch
(tests failing on a clean checkout), which came from *what* was tested — the
committed tree — not from *who* tested it. Three escapes it missed, all
environmental. The rest of its rejections were of problems the split itself
created.

## Design principles

1. **Machines verify facts; models make judgments — and each judgment is made
   once.** A test result, a length limit, a red-to-green transition, a check
   conclusion on CI: facts, verified deterministically at zero marginal token
   cost. What a model is for is the judgment inside the work itself. Paying a
   second model to re-derive a first model's context is the anti-pattern this
   design exists to remove.
2. **Never pay for the same orientation twice.** The context that implemented
   a change is the cheapest possible context to check and correct it. Fresh
   context is reserved for what actually needs freshness: a new issue, or a
   resumed one.
3. **Trust is not transferred to the implementer; it is retired.** The single
   agent gains no authority the split's worker lacked: it still cannot close,
   merge, or push. Its claims are never load-bearing — every claim is either
   checked by a machine or sampled by an audit. The design does not trust the
   author more; it stops needing to trust anyone per-change.
4. **Every mechanism maps to a git or tracker primitive.** Authorship is a
   commit. The seal is a SHA. The journal of what happened is the branch. A
   verdict is a check conclusion. Anything reimplementing one of these is a
   defect with a delay on it — the 2,700-line lesson, kept as law.
5. **Review is a policy, not an architecture.** Verification is a pipeline of
   steps the harness runs; an agent reviewer is one *optional* step in it,
   default off. Removing the reviewer is a configuration default, not a
   demolition — and reinstating it, globally or per risk tier, is one flag.

## The design

### One session, end to end

A worker is handed a claimed issue and its packet, on a fresh branch
`ortus/<issue-id>` cut from the integration head. In **one session** it:
orients, implements, runs the packet's acceptance commands itself, self-checks
the diff, **commits on its branch with the commit message written inline**,
and emits a completion report. There is no separate verify phase, no separate
compose pass, no handoff, no seal ceremony, no frozen worktree. The session
ends; the harness takes over.

The worker commits — the prohibition on `git commit` was always broader than
its purpose. It still never touches the integration branch, never closes,
never pushes anywhere but its own branch (and only if the harness permits even
that). The commit-message validation that exists today survives unchanged as a
deterministic gate on the commit the worker wrote: subject shortened on a word
boundary, wrong messages rejected — the same rules, applied to the author's
own words instead of a separate composer's.

### Verification is a harness pipeline, not an agent

After the session ends, the harness — deterministic Python, zero model tokens
— runs the verification pipeline against the branch:

1. **The AC runner.** The packet's Criterion checks are already exact
   backticked commands; today an agent reads them and runs them. Instead the
   harness runs them directly, as subprocesses, against a **clean extraction
   of the branch tree** (`git archive | tar -x`, never a worktree). This
   preserves, mechanically and locally, the one catch the reviewer ever made
   that nothing else would have: the committed tree is tested, not the
   author's dirty one. Every command's exit status and output are recorded as
   a tracker comment — the same durable verification record as today, minus
   the agent that used to type it.
2. **The red–green proof.** The deepest objection to removing the reviewer is
   that an implementer can satisfy its own acceptance tests by writing vacuous
   ones. The mechanical answer: every AC is marked in the packet as either
   `proves-new` or `guards-existing`. A `proves-new` check must **fail on the
   merge base and pass on the branch**. A test that passes without the change
   proves nothing about the change, and the harness rejects it — no judgment,
   no tokens, just two subprocess runs. `guards-existing` checks must pass on
   both. This single rule replaces the largest class of reviewer judgment
   with an inequality.
3. **The claim diff.** The worker's completion report maps each AC to a
   claim. The harness compares claims to the AC runner's actual results; any
   discrepancy fails the pipeline outright. The worker's word is thereby
   never load-bearing — it is an assertion the machine immediately audits,
   and lying is strictly worse for the worker than silence.
4. **The gate.** Where a remote exists and gating is enabled, the branch is
   pushed and the harness waits, bounded, for the check conclusion — exactly
   `ortus-6a0a.1` from the existing programme, adopted unchanged. CI is the
   real reviewer for the failure class that actually escaped: environment
   divergence. Where no remote exists, the AC runner's clean-tree pass is the
   final mechanical word.
5. **Optional: the agent reviewer**, default off. One flag turns it back on,
   globally or per risk tier (see the trust ledger). The step slots into the
   same pipeline and reads the same branch; nothing structural changes when
   the operator changes their mind.

On full green, **the harness merges (fast-forward), closes the issue, and
pushes** — nobody closes or merges their own work, preserved as a mechanical
fact rather than a trust ritual: the worker has no tool that can do either.

### Corrections stay in context

When the pipeline fails, the failure — the exact command, its output, which
AC, which rule — is returned **to the same session** (or the same context,
resumed), not to a fresh worker via a correction packet. The author who just
wrote the change repairs it with its full context intact, at a marginal cost
of roughly the failure output plus the fix, instead of two fresh orientations
per round. Retries are bounded (two, as today); at the bound, the branch is
parked as-is — committed, durable, diagnosable — with the worker's notes as a
tracker comment, and the issue routes to a human.

This dissolves the right-of-reply problem rather than solving it. The
byte-identical-candidate pathology happened because a fresh implementer,
handed a verdict it had no context to dispute, could only comply or fail. In
context, a worker that believes an AC is unsatisfiable says so immediately —
and since a failing AC is a *fact* about a command's exit status rather than a
reviewer's opinion, the dispute is with the packet, which is exactly what the
plan-gap route already handles. (Plan gaps, the andon cord, survive unchanged:
stopping the line is still a contribution.)

### Conflicts: rebase on resume, not an integrator

With one issue in flight at a time and merge-on-green, two live branches
almost never coexist; the conflict case is a *parked* branch meeting a moved
integration head at resume. The lean answer: **the resuming worker rebases its
own branch as the first act of its session.** It is the best-placed party by
construction — it holds the packet, the branch, and (via the tracker record)
the history of why the work parked. Textual conflicts resolve in-session;
semantic disagreement between two packets is a plan gap and escalates, exactly
per the existing escalation criteria. The dedicated integrator role — a new
phase, module, and contract — is deleted from the plan. Its escalation rules
survive as three sentences in the worker prompt.

The semantic-conflict gap gets the same treatment the PRD proposed but never
filed: the harness merges the integration head forward into any parked branch
before re-running the pipeline on resume, so the combination is what gets
verified. That is a `git merge` plus a re-run — harness work, not a role.

### The packet diet: readiness schema v2

The fifteen-heading schema is the right discipline with real redundancy —
Behavioral context, Resolved decisions, Compatibility constraints, Edge cases
and Plan-gap guidance overlap heavily, and the packet is currently injected
into up to three orientations per change. In this design it is injected
**once**, and slimmed to seven sections:

- `## Objective` — absorbs Behavioral context's before/after in two sentences.
- `## Locations` — unchanged: files and symbols, backticked.
- `## Constraints` — merges Resolved decisions, Compatibility constraints and
  Edge cases: what is decided, what must not break, what will bite.
- `## Non-goals` — unchanged; the cheapest scope fence there is.
- `## Plan-gap guidance` — unchanged; the andon cord needs its wire.
- `## Acceptance` — criteria with `AC-N` ids, each tagged `proves-new` or
  `guards-existing`, each with its exact command. This section is now the
  **verification substrate**, so its discipline *tightens* while everything
  around it loosens.
- `## Targeted tests` — unchanged.

Ordered steps become optional: a well-located, well-constrained packet does
not need its implementation dictated, and the steps were the section most
often wrong by the time a worker read them. The acceptance_criteria field is
hashed at claim time; a worker cannot edit the contract it is judged by,
because the contract lives in the tracker, not the tree.

## What each current mechanism becomes

| Today | Lean pipeline |
| --- | --- |
| Verifier agent (fresh context per candidate) | AC runner + red–green proof + claim diff (deterministic) + gate (CI) + optional reviewer flag |
| Candidate seal (diff hash) | Branch head SHA |
| Attribution / disowning / re-adoption / mutation guard (~2,700 lines) | Deleted; commits carry authorship |
| Compose pass (separate model call + envelope parsing) | Worker writes the message at commit time; same deterministic validation |
| Correction packets between fresh contexts | In-context bounded retry; failure output is the packet |
| Right of reply / dispute route | Dissolves — machine results are facts; packet disputes are plan gaps |
| Integrator (new role, module, phase) | Rebase-on-resume by the issue's own worker + merge-forward before re-verification; escalation rules kept as prompt text |
| Reviewer verdict comment | AC runner's record: commands, exit codes, output — same durability, no author |
| Verification sandbox (read-only, divergent) | Clean `git archive` extraction locally; CI remotely |
| "Nobody closes their own work" | Unchanged and now purely mechanical: harness merges and closes on green |
| Crew memory (Epic D) | Kept as filed — cheaper pipeline makes lessons the main redundancy against author blind spots |
| Phase 4 "does the reviewer earn its cost" | Replaced by continuous escape tracking (below), which answers it with a number instead of a phase |

## The trust ledger

What is genuinely sacrificed, what mitigates it, and what would reverse it.

**Sacrificed: fresh-eyes semantic review of every change.** An author blind
spot that no AC captures — a subtly wrong interpretation of the objective that
still passes its own tests — can now merge. Mitigations, in order of load:
the red–green proof (the blind spot must survive an adversarial mechanical
standard the author did not choose); the packet's Acceptance section, which is
authored at planning time, hashed at claim, and is the real reviewer in this
design; and the **sampled audit** — the retrospective (Epic D's `ortus-v8bj`,
kept) reads a random fraction of merged changes against their packets with a
cheap model, asynchronously, off the critical path. Per-change review is
replaced by statistical review, which is how every mature manufacturing
process that ever removed inline inspection survived doing so.

**Sacrificed: adversarial reading of test content.** A worker could weaken an
*existing* test that a `guards-existing` check runs. The gate's full matrix
narrows this; the audit samples for it; and the escape metric (next) catches
its consequences. Named honestly: this is the least-defended surface in the
design, and the first place a reinstated reviewer tier should point.

**The escape metric decides, continuously.** Every merge is joined to what
happened next — CI on the integration branch, reverts, bugs filed against the
change. That join (cheap, deterministic, already mostly present in tracker
data) yields an **escape rate**. The falsification rule is stated in advance:
if the lean pipeline's escape rate exceeds the two-day baseline (3 escapes /
11 pipeline commits ≈ 27%) over a comparable volume, the reviewer flag turns
back on for the affected change classes. Because review is a pipeline step
and not an architecture, acting on the number is configuration, not a
migration.

**Risk tiering falls out for free.** The verification pipeline is per-tier
configuration: prompts, docs and tests run AC-runner-only; ordinary source
adds the gate; the shipping path (finalization, the scheduler loop, capture)
adds the reviewer flag *and* keeps the `human` label exactly as today. The
question the current programme deferred — should heavy process apply to light
changes — stops being a question because process weight is now a per-tier
dial rather than the pipeline's shape.

**Preserved without qualification:** nobody closes or merges their own work;
the seal (stronger, as a SHA); bounded context per issue; the readiness gate
and selection invisibility of unready packets; plan-gap routing; the `human`
label; CodeGraph policy injection; deterministic finalization; the branch
guard.

## Token accounting

In units of O (one orientation), with W the implementation work, V the
verifier's checking-and-narrating, and k correction rounds. Numbers are
structural estimates, not measurements — the point of the pilot is to replace
them with counted ones.

| | Current | Lean |
| --- | --- | --- |
| Happy path | O + W + **O′ + V** + compose | O + W + self-check (≈0.1 O) |
| Per correction round | **2O** + partial W + V | failure output + fix (≈0.1–0.3 O) |
| Verification | agent re-run + narration | **0 model tokens** (subprocesses + CI) |
| Commit message | separate model pass + retry loop | inline, ≈0 marginal |
| A run that hits a wall | often total loss | parked branch + diagnosis at bound |

Structural estimate: **40–60% reduction on the happy path** (the second
orientation and the verify narration were roughly half the model spend), and
**70%+ on corrected changes**, which is where the current pipeline bleeds
worst. A third of runs delivering nothing improves for independent reasons:
in-context walls produce parked, committed, diagnosable branches instead of
abandoned dirty trees.

## Failure modes of this design, named before they are met

- **Vacuous tests** → the red–green proof, structurally; the audit,
  statistically.
- **Weakened existing tests** → gate matrix + audit + escape metric; weakest
  point, said above, watched first.
- **Self-check theater** — the model agreeing with itself → the self-check is
  advisory and costs ~nothing; nothing load-bearing reads it. Load-bearing
  checks are all mechanical.
- **Context rot in long sessions** → the session covers one change and at
  most two bounded retries — the context that degrades is cross-issue
  accumulation, which still resets per issue. Parking ends the session;
  resume is a fresh orientation against a *committed* branch, which is the
  cheap kind.
- **An unsatisfiable AC burning retries** → the worker disputes it in-context
  as a plan gap on the first failure, not after two blind resubmissions.
- **Gate latency and runner cost** → same shape and same answer as
  `ortus-6a0a.1`: opt-in, bounded, per-tier.
- **A packet-quality regression under schema v2** → the Acceptance section
  tightens (kinds + hashing) while prose sections merge; if leaner packets
  measurably raise plan-gap or escape rates, v2 rolls back to v1 per field —
  the schema is versioned per packet already.

## Migration, relative to the existing programme

The lean pipeline is *fewer* steps from today than the PRD is, because it
never has to preserve the verifier contract through the candidate migration.

1. **L0 — keystone, merged.** Workers commit on `ortus/<issue-id>`; harness
   merges on green and closes. Subsumes the PRD's Phases 0 and 2 in one
   hand-landed change — legitimate now because the verification contract that
   made a two-step migration necessary is being retired, not preserved. The
   `human` label discipline applies in full.
2. **L1 — machine verification.** AC runner, red–green proof, claim diff;
   verifier agent off by default; corrections in-context. Attribution and the
   mutation guard delete here (the PRD's Epic F).
3. **L2 — the gate.** Adopt `ortus-kdqt` and `ortus-6a0a.1` exactly as filed.
4. **L3 — the loop closes.** Escape tracking; sampled audit via the
   retrospective; risk tiers configured. Epic D lands as filed (before or
   during any of this — it was always independent).

Adopted from the existing graph unchanged: `ortus-xjdf`, `ortus-z7ib`, all of
Epic D, `ortus-kdqt`, `ortus-6a0a.1`. Retired from the plan: the integrator
epic (G), the verifier re-scoping, the correction-packet machinery, Phase 4 as
a phase. `ortus-32m1`/`ortus-eele` are absorbed by L0 rather than landed
separately.

## What this design refuses to claim

It does not claim the reviewer never earned anything — it claims the two
things the reviewer demonstrably earned (the committed-tree vantage; an
independent standard the author can't negotiate with) are supplied here by an
archive extraction and a hashed acceptance contract, at zero marginal tokens.
It does not claim escapes will not rise — it instruments for exactly that and
pre-commits to the reversal threshold. And it does not claim the trust
problem is imaginary — it claims per-change adversarial review is the most
expensive possible answer to it, and that a system whose every merge is
gated by machines, sampled by audits, and joined to its outcomes needs the
expensive answer only where the numbers say so.

The elegant version of trust is not a second opinion on everything. It is a
first opinion that is cheap to check.
