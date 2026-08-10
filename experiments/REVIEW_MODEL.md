# Does the implementer/reviewer split earn its cost?

## Why this exists

Ortus separates the agent that writes a change from the agent that judges it. The
reviewer is fresh, read-only, and independent, and the reasoning is the ordinary
one: an author cannot see what an author cannot see.

After two days of running it hard enough to produce twenty-eight commits, the
question is whether the arrangement pays for itself as built. This document
records what actually happened rather than what the design intends, because the
argument had been running on impressions and the impressions disagreed.

## The measured record

**Delivery.** Twenty-eight commits landed over two days.

| | |
| --- | --- |
| Finalized by the pipeline | 11 |
| Committed by hand | 17 |

Two of the seventeen carry issue-id subjects and look automated; they are the two
entangled candidates that had to be separated by hand, and the id is in the
subject because the work belonged to those issues.

**What the seventeen were.** Not miscellaneous. Hand-splitting two entangled
candidates the pipeline produced and could not resolve. Three CI infrastructure
commits, after a pipeline-shipped change broke every matrix leg. Two repairs of a
red integration branch, both after review passed something the checks rejected.
One revert of the pipeline's own work. And all documentation and design.

The pattern is sharper than the count: **the pipeline could not repair the
pipeline's own failures, could not touch its own CI, and could not carry work
that spanned more than one issue.** Every commit in those categories ended with a
human running the push, because nothing in the ticket flow could carry it.

**Run outcomes.** Twelve runs. Three were killed by the operator. One finished
having closed nothing and released three issues back to open. Roughly a third
produced no delivered work, and each of those still consumed between twenty and
ninety minutes.

**Escapes.** Three changes passed review and then failed the checks on every
matrix leg:

| What shipped | Why review missed it |
| --- | --- |
| A test asserting a substring split by terminal colour codes | Review ran without colour enabled |
| A change making an index a hard prerequisite | Review ran on a machine where the tool was installed |
| A test asserting a log line names one path | Review ran where the tracker exports happened to be clean |

Every one is environment divergence. Not inattention — review verifies somewhere
that is not the place the code must work.

**Catches.** The reviewer's best moment was real and nothing else would have
found it: a candidate was rejected because committing it would have landed two
tests that fail immediately on a clean checkout. It found that by testing the
committed tree while the implementer had tested its own dirty worktree.

Its other rejections were mostly of problems the split itself created — a file
disowned and then edited, a candidate stranded across six attempts — and one was
accidental, catching an unrelated scheduler defect while objecting to something
else.

**Standing cost.** About 2,700 lines of code and tests exist to answer *whose
dirty file is this*: an attribution module, the handoff and disowning logic in
the scheduler and the transaction, and their tests.

## What is actually attributable

Not all of it. Documentation and design work was never filed as tickets, so its
being manual says nothing about the review model. The commit-message defects, the
unbounded-wait defect and the failed-commit diagnostics would exist with a single
agent.

What does trace to the split:

- The **candidate as frozen worktree state**. A separate reviewer needs something
  immutable to judge, and freezing dirty paths is how that is done. Attribution,
  disowning, re-adoption, the seal and the mutation guard all follow from it.
- **Per-phase handshakes**, and the resume path that skips implementation, which
  produced two defects of their own.
- **Environment divergence**, because review happens somewhere else by
  construction.
- The **read-only sandbox**, whose mounts are where a large share of the friction
  lives.

## Two design problems, named

**1. The candidate model.** Defining a candidate as worktree state rather than as
a commit is the deeper problem. It is what makes an out-of-scope edit to a shared
file — a README touched while doing something else — able to strand an issue's
entire deliverable. That failure cost one issue six attempts and another its
whole contribution, and it is the same mechanism a downstream repository hit.
Treated at length in `experiments/CANDIDATE_ISOLATION.md`.

**2. Review in a divergent environment.** Three escapes, all environmental, each
discovered by the integration branch going red rather than by the reviewer. A
reviewer that cannot see the environment the code must survive is checking the
wrong thing carefully.

## What the split genuinely buys

One thing, and it is worth stating precisely so it is not lost in a rewrite: the
reviewer tests **`HEAD` plus the candidate**, while the implementer tests its own
dirty worktree. That difference in vantage is what caught the clean-checkout
failure, and no self-reviewing implementer would have caught it.

But that value comes from *what is tested*, not from *who tests it*. Checks run
on a branch before it merges provide the same vantage, in the real environment,
without a seal, without attribution, and without a second agent re-deriving what
to look at.

## What would settle it

The sample is biased and the bias runs in one direction. These two days were
spent changing Ortus with Ortus — a scheduler finalizing changes to finalization,
a fix for truncation shipping in a truncated commit. A repository that is not its
own subject generates none of that, so the failure rate here is an upper bound.

The way to answer the question rather than argue it:

1. Move the candidate from worktree state to a commit on a branch.
2. Run the checks on that branch before it merges, in the environment the code
   must survive.
3. Keep an agent reviewer for what checks cannot judge — whether the change did
   what the packet asked.
4. Then count again. If the reviewer catches nothing the branch checks did not,
   it is not earning its cost and should go.

That sequence removes the machinery the split forced, closes the environmental
blind spot, and leaves exactly one question outstanding — which is the one worth
asking.

## The honest summary

The split caught real problems, most of which it had created. It missed every
problem it had not created. It cost roughly a third of all runs, most of a
working day of manual repair, and 2,700 lines of standing machinery.

That is not an argument for removing review. It is an argument that review is
currently in the wrong place, doing the wrong kind of checking, against the wrong
artifact.
