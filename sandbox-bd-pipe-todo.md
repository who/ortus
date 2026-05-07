# TODO: Ralph + sandbox — drop the bd|xargs pipeline AND the flock wrapper

> **STATUS: BOTH PARTS COMPLETE (2026-05-07).** Preserved as a forensic
> record of how we got here. Today's authoritative architecture doc is
> `locking-fix.md`.
>
> - **Part 1 (bd|xargs pipeline):** Fixed in both `ortus/prompts/ralph-prompt.md`
>   and `template/ortus/prompts/ralph-prompt.md.jinja`. Step 1 of "Your Task"
>   now uses `bd show --long <id1> <id2> <id3>` (positional args, no pipe)
>   and includes the durable "Never pipe bd into other commands" guidance.
> - **Part 2 (`ortus/bd` flock wrapper):** Removed. Wrapper artifacts
>   (`ortus/bd`, `ortus/bd-locked`, `template/ortus/bd`,
>   `template/ortus/bd-locked`) no longer exist. ralph.sh has a comment
>   block at the former `export PATH="$PWD/ortus:$PATH"` location explaining
>   the removal.
>
> The original analysis below remains accurate as a description of the bugs
> as they manifested; only the verbs are wrong now ("we should drop X" →
> "we dropped X"). Nothing here is actionable.

## TL;DR

Two related bugs surface together when running Ralph under the OS sandbox
with bd 1.0.3 + Dolt:

1. **The Orient step's bd|xargs pipeline** hangs because the sandbox's bd
   exemption only applies to directly-invoked bd, not bd-as-child-of-xargs.
2. **The `ortus/bd` flock wrapper** turns every transient sandbox-blocked
   bd call into a permanent project-wide deadlock — flock is held forever
   by the stuck bd, and `flock -w 60` doesn't reliably time out under the
   conditions that produce the stuck bd.

Either bug alone is bad. Together, the first reliably triggers the second
on the very first iteration of every Ralph run.

---

# Part 1: ralph-prompt — drop bd|xargs pipeline

## Problem

The Orient step in the canonical Ralph prompt tells the agent to run:

    bd list --sort updated --all --limit 3 --json | jq -r '.[].id' | xargs bd show --json

Under the OS sandbox (Tier 1, bubblewrap on Linux/WSL2 or Seatbelt on macOS), bd
must be added to `sandbox.excludedCommands` in `.claude/settings.json` so it can
reach its dolt sql-server over loopback TCP. The exclusion only applies when
bd is the directly-invoked binary on the bash command line. Inside a pipeline
where `xargs` invokes `bd show ...`, the bd subprocess is a child of `xargs`
(which is sandboxed), inherits the network namespace, and hangs on the
dolt connection until it times out (~60s per call).

Net effect on a project scaffolded from ortus: every Ralph iteration begins
with a bd|xargs|bd command that hangs at step 1, so the loop makes zero
forward progress until the user kills it.

## Fix

Replace the piped pattern with two direct bd calls. `bd show --long <id>...`
accepts multiple ids and includes a `COMMENTS` section, so a list call followed
by a multi-id show call is equivalent and pipe-free.

Suggested replacement:

    1. **Orient**: Run `bd list --status=closed --sort closed --limit 3 --json`
       to find recently completed issues. Then run `bd show --long <id1> <id2>
       <id3>` (space-separated) to read their full details and comments. **Never
       pipe bd into other commands or use `xargs bd`** — the sandbox exemption
       only applies when bd is the directly-invoked binary; piping makes bd a
       child of another process and it hangs on dolt.

The "never pipe bd" guidance is durable — it generalizes the rule so future
prompt edits don't reintroduce the same class of bug.

## Files to update

- `template/ortus/prompts/ralph-prompt.md.jinja` — copier template, source of
  truth for new projects.
- `ortus/prompts/ralph-prompt.md` — working copy in this repo, kept in sync.

Both currently contain the broken line at section "## Your Task" → step 1.

## Related (optional, lower priority)

- `bd prime` SessionStart hook returns exit 1 with empty output under sandbox
  in the same setup — hooks appear to bypass `excludedCommands`. Cosmetic
  (the loop still progresses without hook output) but worth investigating
  whether to drop the hook or document the limitation.
- README/setup docs in ortus could call out the required
  `sandbox.excludedCommands: ["bd", "bd *"]` entry in `.claude/settings.json`
  for projects that ship the OS sandbox config. Today users only discover
  this when ralph mysteriously hangs.

---

# Part 2: drop the ortus/bd flock wrapper

## Problem

`ortus/ralph.sh` prepends `$PWD/ortus` to PATH so `bd` resolves to the
flock wrapper at `ortus/bd` (a symlink to `ortus/bd-locked`). The wrapper
runs every bd invocation through `flock -w 60 .beads/dolt.flock bd ...`
to serialize concurrent dolt sql-server auto-starts (bubbles-m51.1).

Failure mode under the OS sandbox:

1. Ralph's child Claude issues a bd command (e.g. the SessionStart hook's
   `bd prime`).
2. PATH resolves it to `ortus/bd` → flock acquires the lock → execs real bd.
3. Real bd tries to connect to its dolt sql-server on localhost. The
   sandbox's network namespace drops loopback. bd hangs indefinitely.
4. The hung bd holds the flock forever. `flock -w 60` on subsequent
   waiters does NOT reliably expire — observed processes stuck in
   `flock` state for 30+ minutes.
5. Every future bd call (other Ralph iterations, manual shell, hooks)
   queues behind the hung holder. Project-wide deadlock.

Net: a single sandbox-blocked bd freezes all bd activity until the user
manually `pkill -9`s the holder.

## Why drop instead of repair

- The wrapper protected against multiple bd processes racing to auto-start
  dolt servers. Within a single Ralph loop, Claude already serializes
  Bash calls — there is no race to defend against.
- The race only surfaces with multiple Ralphs running concurrently against
  the same project. That isn't a typical workflow.
- bd 1.0.3 has substantially better dolt lifecycle handling than the
  version the wrapper was written against — auto-start/port-detection
  handle most of m51's pain upstream.
- The wrapper's narrow benefit (multi-Ralph concurrency) is not worth
  global deadlock risk under sandboxed runs.

## Fix

Edit `template/ortus/ralph.sh.jinja` (and the working `ortus/ralph.sh`)
to remove:

    export PATH="$PWD/ortus:$PATH"

and the surrounding bd-serialization comment block. Replace with a short
explanatory comment so future readers understand the history. Optionally
remove the `bd_retry()` helper function (defined inline in ralph.sh) —
its purpose was defense-in-depth around the wrapper. If kept, it's
harmless dead code.

Optionally also delete the wrapper artifacts:

- `template/ortus/bd` (symlink) and `template/ortus/bd-locked` (script)
- `ortus/bd` and `ortus/bd-locked` working copies
- Any `bd_retry.sh` if extracted to its own file in downstream projects

(bubbles externalized `bd_retry` to `ortus/bd_retry.sh`; ortus keeps it
inline. Both are obsolete after the wrapper is gone.)

## Files to update

- `template/ortus/ralph.sh.jinja` — copier template (lines around
  `export PATH="$PWD/ortus:$PATH"` and the `bd_retry()` definition).
- `ortus/ralph.sh` — working copy.
- `template/ortus/bd`, `template/ortus/bd-locked` — delete.
- `ortus/bd`, `ortus/bd-locked` — delete.

## Marking m51 obsolete

Any beads issues in downstream projects under the `m51` prefix (e.g.
`bubbles-m51.*`) covered the original lock-contention symptom that the
wrapper was meant to fix. Once the wrapper is removed, those issues
should be closed as superseded — the underlying class of bug is now
handled by bd's own retry/auto-start logic at the bd 1.0.3 level.

---

## Discovered in

bubbles project, 2026-05-07. Initial slowdown was traced to sandbox
network namespace blocking bd→dolt loopback connections inside xargs
subprocesses (Part 1). Once the prompt was fixed, a second hang
appeared: the SessionStart hook's `bd prime` got stuck holding the
project-wide flock, freezing every subsequent bd call (Part 2).
Patched bubbles' working copies to confirm the fix; upstream ortus
template still ships both broken pieces.
