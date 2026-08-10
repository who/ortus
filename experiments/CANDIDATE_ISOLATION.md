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

**Merge conflicts become real.** Sequential single-tree work never merges today.
With branches, two issues touching one file conflict at merge time. That is a
better problem — tooled, understood, with a resolution path — but the pain moves
rather than disappearing, and it arrives *later*, once the work is already done.
Symbol-level collision detection at selection is what keeps that acceptable, and
it is the reason these two proposals interact.

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

## Where this might be wrong

Conflicts surface *after* the work is done; attribution surfaces during it. In a
repository with heavy cross-cutting churn, branch-per-issue could trade a bug
class for a merge-pain class, and the trade would be worse rather than better.
The judgement that merges are a solved problem and attribution is not is doing
most of the work in this document, and it deserves a real test before the
machinery it would delete is deleted.

A cheap way to find out: run stage 1 for a while. It changes no worker behavior
and still produces the branch history that would show how often two issues would
have collided.
