# Candidate isolation

## Why this exists

A large share of the defects fixed in this repository are not about the work an
issue asked for. They are about telling that work apart from work it did not ask
for: a path one issue disowned and another edited, a candidate stranded when a
run stopped, two issues' changes tangled in one file, a reviewer blamed for a
build artifact its own checks rebuilt.

The cost is measurable.

| Where | Lines |
| --- | --- |
| `src/ortus/core/attribution.py` | 456 |
| Handoff, disown and inherited-path logic in `grind.py` | 174 |
| The same in `transaction.py` | 33 |
| `tests/test_core_attribution.py`, `test_grind_unrelated.py`, `test_grind_recovery.py` | 2,032 |

About 2,700 lines whose entire job is answering *whose dirty file is this*, plus
the incidents that produced them and the hours spent separating two issues' edits
out of one file by hand.

## The root cause is a definition, not a mechanism

The shared working tree is not the problem. The problem is that **a candidate is
defined as worktree state** — the dirty paths, minus an operator baseline, minus
whatever a worker disowned — and sealed by hashing a diff of that state.

Every mechanism above follows from that definition:

- Attribution exists because dirty files carry no owner.
- Disowning exists because a worker inherits files it did not write.
- Re-adoption exists because a worker sometimes edits a file it disowned.
- Region ownership exists because one file can hold two issues' work.
- The mutation guard exists because a reviewer running the project's checks
  changes the very thing it is judging.

If a candidate were **a commit** rather than a set of dirty paths, none of that is
needed. Commits carry authorship. The seal becomes a SHA, which is both stronger
than a diff hash and native to the tool. Nothing a reviewer does to the worktree
can alter a commit. Parked work becomes a named branch instead of debris in the
next worker's tree. Resuming becomes a checkout.

## Four shapes

**Branch per issue, one shared tree.** The worker commits to `ortus/<issue-id>`,
review reads `main...branch`, finalization squash-merges to keep one commit per
issue on the integration branch. The tree stays warm: no duplicated dependency
install, no cold build, no second index.

**Worktree per issue.** Complete isolation, paid for per issue. Each tree needs
its own dependency install and its own build. `.codegraph/` lives at the
repository root, so the index needs an answer. `.beads/` and `logs/` become
per-tree questions. In a JavaScript repository this is potentially gigabytes and
a full install for every issue.

**Branch plus pull request.** Adds the property worth the most: **checks run
before the merge rather than after it.** A morning spent with a red integration
branch, and a downstream repository broken by a change that passed local review,
were both merge-first failures.

**Worktree plus branch plus concurrent workers.** The end state if issues are
ever worked in parallel.

## The distinction that decides it

> Branches solve the correctness problem. Worktrees solve a concurrency problem
> this system does not have.

Ortus works one issue at a time. Isolation between *simultaneous* workers is not
the failure mode; isolation between *successive* ones is, and a branch provides
exactly that at almost no cost. Paying worktree costs today buys a property that
is not yet in use.

## What must survive

- **Nobody closes their own issue.** Unchanged: committing to a branch is not
  closing anything.
- **Ortus alone finalizes.** Unchanged: Ortus merges and closes.
- **Review is independent.** Improved: the reviewer reads a stable commit range
  rather than a mutable tree.
- **The candidate is sealed.** Strengthened: a commit SHA is a stronger seal than
  a hash of a diff of the worktree.

The rule that has to change is the prompt's flat prohibition on `git commit`. It
is broader than its purpose. The integration branch is protected by the branch
guard, and self-closing is forbidden separately; committing to one's own branch
violates neither.

## What it costs

**Merge conflicts do not become real. They already are, in a worse form.**

Two issues editing one file collide today. The collision simply has no name, no
markers, no merge base and no tooling: git sees a single dirty file, and the loop
has to *infer* which lines belong to whom from fingerprints, symbol ranges and
heading spans. That inference is what `attribution.py` is, and it can be wrong
silently. With several issues parked at once the inference is not two-way but
N-way, and every additional parked issue makes it harder.

Branches do not add conflicts to that. They remove most of them and give the
remainder a shape.

- **Non-overlapping edits to one file stop being an event at all.** Git merges
  them without asking anyone. Today they are precisely the case that requires
  attribution to sort out.
- **Genuinely overlapping edits produce a conflict** — with a merge base, markers
  and thirty years of tooling, instead of hunk archaeology.

This repository's own history is the argument. Two issues both edited `README.md`
and the entanglement cost six failed attempts on one, a lost section on the
other, both correction budgets, and hours of manual separation. Their hunks:

    issue A   @@ -245,0 +246,116 @@
    issue B   @@ -107 @@  @@ -118 @@  @@ -141 @@  @@ -364 @@  @@ -379 @@  @@ -393 @@

They never overlapped by a single line. On branches git would have merged them
automatically and no one would have noticed there was anything to resolve.

Symbol-level collision detection at selection still earns its place — it keeps
the genuinely overlapping cases rare — but it is an optimization here rather than
a precondition.

**Latency, if pull requests are adopted.** Waiting for checks costs minutes per
issue and real money. Worth it to stop breaking the integration branch; not
obviously worth it for every repository, so it should be opt-in and must degrade
cleanly where no remote exists.

**The migration is not free.** Finalization, the journal, the verifier contract
and recovery all touch the current definition.

## Staging

1. **Finalization commits to a branch and fast-forwards the integration branch.**
   No worker behavior changes. This alone turns parked work into a named branch.
2. **The worker commits on its branch; a candidate becomes a commit range.** This
   is the step that starts removing the attribution machinery.
3. **Pull requests where a remote exists**, opt-in, for checks before merge.
4. **Worktrees** only if concurrent execution becomes a goal.

## The integrator

Branches leave one job that has no owner today, and it is unlike every other job
in the system: **every existing phase is scoped to one issue.** A planner writes
one packet, an implementer works one issue, a reviewer judges one candidate,
finalization ships one issue. A conflict is inherently *between* two pieces of
work, so it is the first thing here whose unit is a relationship rather than a
change.

That deserves a role rather than a branch in the scheduler.

### What it reads

The text of a conflict is the least informative thing about it. An integrator
should open with:

- **Both packets.** What each side was asked to achieve, and what it promised to
  satisfy. A conflict resolved from diff text alone is guesswork; resolved with
  both intents in view it is usually obvious which line belongs where and why.
- **Both verifier reports.** Each side was independently reviewed, and the report
  says what that side was proving.
- **The merge base and both sides**, so the question is what each changed *from*,
  not merely what each says now.
- **CodeGraph**, to see whether the two changes interact beyond the text — the
  callers one side added to a function the other rewrote.

### What authority it has

Bounded deliberately, because this role could quietly become the place changes
get smuggled in.

1. **It proposes; Ortus applies.** The resolution is a candidate like any other.
2. **Its diff is confined to the conflicted hunks.** A resolution that needs a
   change outside them is not a resolution — it is new work, and routes as such.
3. **The merged result is verified again.** A merge is a change that neither
   side's verification covered, which is precisely the semantic-conflict gap
   isolation opens. This is not optional.
4. **It states which side it took, per hunk, and why.** A resolver that silently
   picks a side loses work and nobody finds out until later.

### When it must stop

The role is only worth having if it knows what it cannot decide, and that line is
sharper than it first looks:

> A textual conflict is a merge problem. A disagreement about what the code
> should do is a planning problem wearing a merge problem's clothes.

Escalate when the two packets' acceptance criteria cannot both hold — satisfying
one falsifies the other. Escalate when resolving would change behavior neither
packet asked for. Escalate when one side's intent cannot be established because
its packet is missing or describes code that has since moved. Escalate when both
sides changed the same logic deliberately, for different stated reasons.

None of those are merges. Papering over them is the failure mode this role must
be built to avoid, and it is why the resolver proposes rather than decides.

The escalation path already exists: an issue labelled for human attention is
skipped by selection, and `ortus human` reports what is waiting. Nothing new is
needed to stop.

### Why this role wants memory most

Repeated conflicts between the same two areas are not a merge fact, they are a
decomposition fact. An integrator that remembers *these two subsystems collide
every time* has found something planning needs to know, and that is a lesson no
single-issue phase is positioned to notice.

### What it changes upstream

With a competent integrator, avoiding collisions at selection matters less than
it does now. Symbol-level detection stops being the thing that keeps the system
safe and becomes the thing that keeps it quick — worth having, no longer load
bearing.

## Where this might be wrong

Not in the conflicts. The one property the shared tree provides by accident is
that the second worker **sees the first one's uncommitted code**, and its tests
run against the combination. Isolation removes that: each branch is written and
verified against the integration branch as it was, so two changes that merge
cleanly and break each other are not discovered until after the merge. Git
cannot catch a semantic conflict, only a textual one.

That is a real regression in coverage, and it is the thing to design against
rather than the merge mechanics. The fix is cheap and deliberate: **merge the
integration branch forward into the issue branch before verification**, so the
reviewer judges the combination rather than the change in isolation. That
restores the property on purpose instead of by accident, and it moves the
discovery earlier than the shared tree ever did — before review rather than
during the next issue's run.

The residual exposure after that is a change merging between one branch's
verification and its merge. Narrow, and it is what checks on the integration
branch are for.

## The cheapest way to find out

Run stage 1 for a while. Finalization commits to a branch and fast-forwards; no
worker behavior changes at all. The resulting branch history answers the question
this document is really making a bet about — how often two issues actually touch
the same lines, as opposed to the same files — before any of the machinery it
would delete is deleted.
