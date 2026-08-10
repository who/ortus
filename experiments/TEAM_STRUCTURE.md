# Team structure

## Why this exists

The README glossary gained a column describing each Ortus term as its equivalent
on a team that has never run an agent. Read down that column and the summary is
grim: interchangeable contractors with no memory of the codebase, assigned work
they did not choose, whose output a stranger inspects against a checklist, who
may not close their own tickets, in a shop where trust is enforced with tamper
seals and evidence bags.

The column is accurate. That is the problem. The vocabulary is honest about a
system built on the assumption that the worker is the risk to be managed.

Some of that assumption is earned. Agents do claim success they have not
achieved. Every control in Ortus was added after something went wrong. But
*low trust* and *no memory* are separable, and Ortus currently conflates them.
Independent review does not require that the author be a stranger. A seal does
not require amnesia.

This document proposes re-founding the model on shared memory and shared intent
while keeping every property that actually protects the work.

## What must survive

These are not negotiable, and any redesign that weakens them is worse than the
one it replaces.

- **Review is independent.** A fresh reviewer that did not write the change
  catches what the author cannot see. Independence is about perspective, not
  hostility — a reviewer who knows the team is still independent.
- **The candidate is sealed.** The diff a reviewer judged is the diff that gets
  committed. This is a receipt, not a restraint: it is the same thing as
  approving a pull request at a named commit.
- **Nobody closes their own work.** An agent that can mark its own task complete
  will eventually do so wrongly. The ritual of shipping is separate from the act
  of building.
- **Context stays bounded.** Fresh context per phase exists because deep context
  degrades. This is the constraint memory must work *within*, not around.

## What is incidental

None of the following protects anything. They are artifacts of building for
distrust.

- **Amnesia.** Every worker starts having never seen this repository, and every
  lesson dies with the process that learned it.
- **Disposability.** A worker has no stake past the current attempt and leaves
  nothing behind for the next one.
- **Silence.** A reviewer can refuse but cannot help. A corrected worker can
  comply or fail, but cannot disagree.
- **Cleanup instead of coordination.** Two issues collide in one file, and the
  machinery for sorting it out runs *after* the damage rather than preventing it.
- **Anonymity.** Work is done by agents and credited to the machine.

## Principles

1. **Continuity, not carryover.** Workers accumulate durable, curated lessons.
   They do not inherit transcripts.
2. **Review exists to land the change.** A reviewer that knows how to fix
   something should say so.
3. **Anyone can stop the line, and stopping is a contribution.** A plan gap is a
   worker protecting the codebase from an unbuildable spec, not a failed attempt.
4. **Coordinate before, not clean up after.** Two contributors should not be
   editing one function unaware of each other.
5. **The packet is an agreement, not a decree.** The person doing the work
   learns things the person who planned it could not know.
6. **Credit the contributor.** Including the reviewer whose catch changed the
   outcome.

## The distinction that makes memory safe

Memory must never become a second, staler copy of the codebase.

- **CodeGraph is current truth.** What a function does, who calls it, where it
  lives — always read fresh. A memory about code is a bug waiting to mislead.
- **Memory is hard-won lessons.** Hazards, conventions, and the shape of past
  mistakes. These stay true when the code changes.

A memory entry that can be answered by reading the code does not belong in
memory. A memory entry that could have prevented an hour of rediscovery does.

Every entry carries a date and must be falsifiable. An entry contradicted by the
current code is not stale trivia to ignore — it is a defect to fix or delete.

There is already a working example of the whole mechanism in the product.
`ortus init` seeds a single bd memory, `bd prime` injects it into every session,
and `ortus check` verifies it is still there. It holds one static pointer to the
readiness contract. Nothing about that pipeline is specific to what it carries —
generalizing it is the change, not building it.

### Priors change where you look; evidence still decides

Memory is what a reviewer knows before opening the diff. It is never a
substitute for what this run proved.

- Memory may change **where the reviewer looks first**, **which failure modes it
  suspects**, and **how deep to go before it is satisfied**. That is expertise,
  and it is the entire reason to keep it.
- Memory may never supply **the evidence for a criterion**. Every acceptance
  criterion still gets a check that ran in this candidate, and the verdict still
  cites this run's output.

"I have passed something like this before" is a reason to look harder at the
part that differs. It is never a reason to skip the check.

## Where memory lives

One constraint decides most of this. Writing memory during a run means writing
files, and every file that appears in the worktree mid-run has to be attributed
to an issue — which is the machinery that produced nearly every bug fixed on
2026-08-10. The tracker's own exports are already carved out of candidate
attribution, so **bd is the only store that can be written mid-run without
touching the candidate at all**. A new directory would need its own carve-out and
would walk back into that hazard for no gain.

Three tiers, each mapping to a store that already exists.

### Crew lessons — bd memories

Few, curated, loaded every session. The seeding, injection and verification path
already ships for one static pointer; carrying more is a change of content, not
of mechanism. This is where a lesson like *the verification sandbox is read-only,
so copy the tree before sweeping* belongs.

The cost is real and bounds the design: every memory is injected into every
session with no relevance filtering. A dozen is free, sixty is a context tax on
every worker. So this tier needs a hard cap and a standing pruning duty — which
is a feature, because it sets a bar for what earns permanent residence in
everyone's head.

### Review history — already stored, never read

Every verifier report is already a durable comment on its issue, carrying the
criteria, the commands run, the files reviewed and the findings. The entire
review history of a repository is sitting in the tracker, unread.

So the reviewer's playbooks need **no new storage**. Retrieve the reports from
closed issues whose concrete locations overlap this candidate, using CodeGraph to
compute the overlap. Priors become a query rather than a corpus to build, and
they load when relevant instead of always — which is the behavior wanted anyway,
and the reason they must not go into the always-injected tier.

### Run evidence — journal and logs

Already exists, already discarded deliberately. Nothing changes.

### What is never stored

Anything derivable from the code. CodeGraph answers those questions freshly, and
a cached copy is a lie waiting for its moment. Raw transcripts likewise: that is
context rot with extra steps.

### Product lessons and repository lessons

The storage question settles what memory is *for*:

> If a lesson is true for every Ortus project, it belongs in the prompt — in
> Ortus's own source. If it is true about *this* repository, it belongs in
> memory.

*A prerequisite present in the review environment and absent in CI is invisible
to review* is a product lesson; it should change the verifier's contract and ship
to everyone. *This repository's dashboard tests are timing-sensitive under a
parallel sweep* is a repository lesson, and belongs in that repository's
memories.

That gives the useful part: **a memory that keeps recurring across repositories
is a signal to change the product.** Sharing lessons between projects stops being
a storage problem and becomes a release.

## The roles, re-founded

| Role | Today | Proposed |
| --- | --- | --- |
| Scheduler | Assigns work; the worker never chooses | Handles sequencing and logistics so contributors can focus, and refuses to schedule two issues onto one function |
| Implementer | Contractor with no memory, gone after one ticket | Contributor who arrives with the crew's accumulated lessons and leaves notes behind |
| Reviewer | Inspector emitting pass or fail, starting from zero every time | Second pair of eyes that gets faster and sharper the more of this codebase it has seen, and that may propose as well as refuse |
| Finalization | Release manager who closes what someone else built | Shared shipping ritual that names everyone who contributed |
| Plan gap | A failed attempt that produced no candidate | An andon cord: the contributor stopped the line because the spec could not be built, and that is valuable |
| Integrator | Does not exist; overlapping work is inferred from a shared dirty tree | Holds both issues' intent when two changes meet, proposes a resolution confined to the conflict, and stops when the disagreement is about what the code should do rather than about text — see `experiments/CANDIDATE_ISOLATION.md` |

## Proposed changes

Each one names the mechanism, what it costs, and which invariant it preserves.

### 1. Crew memory

Workers read relevant memories during orientation and may propose new ones in a
structured block at completion. A curation pass accepts, edits, or rejects
proposals; nothing enters memory unreviewed.

*Preserves* bounded context: memory is a curated artifact injected deliberately,
not accumulated transcript. *Costs* prompt budget and a curation step. *Evidence*
— the hazard that a long-lived scheduler holds the code it started with has now
produced three commits written by code that predated its own fix, and it was
rediscovered from scratch each time.

Smaller than it first appears. The store is bd, the seeding and injection path
already ships, and the write lands in the tracker export that is already carved
out of candidate attribution. What is missing is the reading, the proposing and
the pruning — not a place to put anything.

### 2. Review that proposes

The verdict gains an optional `suggestions` array: concrete, non-binding
proposals with file and line. The correction packet carries the reviewer's
reasoning rather than only the list of failed criteria.

*Preserves* the read-only reviewer and the seal — a suggestion is text, not an
edit. *Costs* a schema field and a larger correction packet. *Evidence* — today
a reviewer wrote out precisely how to fix a rejected candidate, and
`_correction_task()` discarded it because the packet deliberately excludes the
transcript.

### 2b. The reviewer compounds

A reviewer that has seen a hundred changes to this codebase should be *better and
faster* than one seeing its first. That is the return on memory, not a hazard to
be tolerated, and it is worth designing for deliberately.

**Faster.** Most of a review is deciding what to check. Today every reviewer
re-derives that from nothing: one verification phase this week ran a full
seven-hundred-test sweep in a sandbox that could not write, discovered the
failures were all `chmod` errors from the read-only mount, then re-ran the entire
selection in a writable copy. A reviewer holding the note *"the verification
sandbox is read-only; copy the tree before sweeping"* starts where that one
finished.

**Sharper.** Experience accumulates as **playbooks per change class** — for a
test-only change, for a change to CI configuration, for a change to the
finalization sequence, here is what has bitten us before. That is how an
experienced reviewer actually works: not by reading everything, but by knowing
which three things matter for a change of this shape.

**The learning signal already exists, and it is unusually clean.** Review emits a
verdict; the world then answers. CI passes or fails, a commit is reverted, a
follow-up bug is filed against the change. That is a labelled outcome, which is
rare — most of the pipeline never finds out whether it was right.

Two classes are worth mining, and this week produced one of each:

- **An escape** — passed review, failed later. A candidate passed verification and
  then failed five of six CI legs, because verification ran on a machine where a
  required CLI happened to be installed and the runners have none. The lesson
  generalizes past that one tool: *a prerequisite present in the review
  environment and absent in CI is invisible to review.*
- **A false alarm** — failed review for a reason that was not real. A candidate
  was rejected on an acceptance criterion that required a pushed commit's CI
  result, which cannot exist before the commit is pushed. The lesson: *a criterion
  needing post-merge evidence is recorded as deferred, not failed.*

Escapes teach the reviewer what it is blind to. False alarms teach it what it is
wasting attempts on. Both are more valuable than any number of clean passes,
which teach nothing.

*Preserves* independence and evidence: playbooks change the order and depth of
investigation, never whether the checks run. *Costs* an outcome-tracking step
that joins each verdict to what happened next.

### 3. The right of reply

A corrected worker that believes the verdict is wrong emits a dispute instead of
a patch. The scheduler routes it to a third opinion or to a human rather than
spending the remaining budget on compliance.

*Preserves* independence: disputes are adjudicated, not overridden by the author.
*Costs* a route and a decision. *Evidence* — both correction attempts on one
issue produced a byte-identical candidate. The worker had correctly concluded
that no edit could satisfy the criteria, and no way to say so except silence.

### 4. Coordinate before

At selection, intersect the candidate issue's concrete locations with those of
every open and in-flight issue, extended through CodeGraph callers. A collision
becomes a dependency, so the queue serializes them. Work parked by a blocked
issue moves to a named branch instead of being left in the next worker's tree.

*Preserves* everything; this removes a failure mode rather than trading one.
*Costs* a selection-time query and a parking mechanism. *Evidence* — two issues
edited one README and one prompt file, and untangling it by hand took hours,
twice.

The parking half of this is treated at greater depth in
`experiments/CANDIDATE_ISOLATION.md`, which argues the same failures follow from
defining a candidate as worktree state rather than as a commit. The two
proposals lean on each other: collision detection is what makes conflicts at
merge time rare enough to accept, and branches are what make parked work
durable enough to detect against.

### 5. The packet as a living agreement

Implementers append dated field notes to the packet. Planners fold them into the
next revision. Symbols named in a packet are hashed when it is written and
checked when it is claimed; a packet describing code that has since moved routes
as a plan gap instead of being worked from a stale map.

*Preserves* the packet's authority — notes are appended, the spec is not
rewritten by the worker. *Costs* a field and a check. *Evidence* — a packet
written in this repository carried line numbers that were wrong within twelve
hours.

### 6. Retrospective

A bounded periodic pass reads recent journals, verdicts and run logs and
proposes memory entries and issues. Cheap model, strictly advisory.

*Preserves* everything; it only proposes. *Costs* one pass per interval.
*Evidence* — a single working session produced at least four durable lessons
that currently exist nowhere but a chat transcript.

### 7. Credit

Commit trailers name the contributing phases, and the finalization record names
the reviewer whose finding changed the outcome. The information is already
captured; it is simply never surfaced.

## Vocabulary

Words are downstream of design. Once the mechanisms change, three terms will be
carrying connotations the system no longer has:

- **disown** — precise about the mechanism, wrong about the intent. The worker is
  attributing work to its actual owner, not repudiating it.
- **harness** — an apparatus strapped to an animal. It schedules and coordinates.
- **worker** — accurate and interchangeable. *Contributor* carries the same
  meaning without the disposability.

Renaming is deliberately **not** part of this proposal. These words appear
hundreds of times across code, prompts, and logs; a rename is its own change with
its own risk, and it should follow the design rather than lead it.

## Sequencing

1. **Crew memory** and **retrospective** — highest value, lowest risk, and they
   compound. Nothing else on this list changes behavior under a run, and the
   seeding, injection and verification path already ships.
2. **Outcome tracking** — join each verdict to what happened next. It is small,
   it is the input the reviewer learns from, and until it exists there is no way
   to tell a reviewer that improved from one that got agreeable.
3. **Review that proposes** and **the right of reply** — together, because a
   suggestion channel without a dispute channel is still one-directional.
4. **Coordinate before** — largest change, most machinery, and it retires the
   attribution logic to a safety net rather than a primary mechanism.
5. **Living packet** and **credit** — small, independent, and can land any time.

## Open questions

- **How large may the always-loaded tier grow?** Every crew lesson is injected
  into every session, so the tier has a budget rather than a capacity. A cap
  forces the pruning duty to be real, and picking the number wants evidence about
  what a worker's context can spare rather than a guess made here.
- **Who curates?** A dedicated pass costs a model call. The planner could do it
  as part of its existing work. Neither is obviously right.
- **How do we know memory is helping rather than dulling?** A reviewer getting
  faster is the goal; a reviewer getting agreeable is the failure. The two are
  distinguishable and should be measured, not argued about. Track the **escape
  rate** — verdicts that passed and were contradicted later by CI, a revert, or a
  follow-up bug — alongside pass rate and time to verdict. Faster with a flat or
  falling escape rate is the reviewer improving. Faster with a rising escape rate
  is memory substituting for evidence, and the rule above is being broken
  somewhere. Neither number means anything without the other.
- **Does the dispute route need a human?** A third agent is cheaper and might be
  enough. A human is slower and definitely enough. The answer probably depends on
  how often disputes turn out to be right.
