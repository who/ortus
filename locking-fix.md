# Ortus dolt-locking architecture

> **Status:** current as of 2026-05-07. Replaces an earlier draft of this
> document that recommended a `bd-locked` flock wrapper as the primary fix —
> that approach was abandoned because it deadlocks under the OS sandbox
> (see `sandbox-bd-pipe-todo.md` § "Part 2"). Today's architecture takes
> the opposite tack: a single long-lived dolt server owned by ralph.sh.

## TL;DR

Projects generated from ortus run `ralph.sh` on the host; each iteration
spawns `claude -p ...` inside Claude Code's bwrap sandbox. With **bd in
`sandbox.excludedCommands`**, every bd call inside the sandbox runs *on the
host* and connects to **one long-lived dolt sql-server** that ralph.sh
starts at the top of the loop and stops via `trap` on exit. That single
server is the only writer to `.beads/dolt/.dolt/noms/LOCK`, so contention
is impossible by construction.

## The pile-up bug we fixed

In bubbles on 2026-05-07 we observed **49 orphan `dolt sql-server` processes**
holding noms/LOCK contention. Root cause:

1. **Multi-ralph race.** Six concurrent `ralph.sh` instances all hit bd's
   per-call auto-start at iteration 1; first wins the noms/LOCK, five
   spawn dolts that fail with `database "dolt" is locked by another dolt
   process`, leaving orphan processes alive.
2. **Auto-start retry storm.** When `waitForReady` times out (sandbox
   cold-start of dolt is slow), bd reports "auto-start failed" but the
   dolt process keeps running. The next bd call in the same iteration
   sees no `.beads/dolt-server.port`, spawns *another* dolt — repeat for
   every bd call (~30 per iteration) → unbounded growth.
3. **Sandbox netns reparenting.** When the inner claude exits, dolts
   spawned inside its bwrap PID namespace get reparented to host init.
   They're invisible to the next iteration's sandbox (different netns),
   but their on-disk noms/LOCK persists.

## Current architecture (in `ortus/ralph.sh` and `template/ortus/ralph.sh`)

Three pieces, all sandbox-friendly:

### 1. Single-instance flock guard

Refuses to start a second ralph against the same repo. Kernel auto-releases
the lock when the holding process exits, so a crashed prior ralph doesn't
block new starts.

```bash
exec 9>".beads/ralph.flock"
if ! flock -n 9; then
  echo "Another ralph is already running against this repo. Exiting." >&2
  exit 0
fi
```

### 2. Ralph-owned dolt lifecycle

`start_dolt` runs `bd dolt start` once at the top of the script. `stop_dolt`
runs `bd dolt stop` via `trap` on EXIT/INT/TERM. Every iteration's bd calls
discover the running server through `.beads/dolt-server.port`. Stale
state-file cleanup is bounded to *bd-owned* files (`.beads/dolt-server.{lock,pid,port}`)
— **never `noms/LOCK`**, per upstream
[gastownhall/beads#2933](https://github.com/gastownhall/beads/issues/2933):
removing `noms/LOCK` while another process holds the flock causes silent
corruption (orphan-inode race).

### 3. bd in `sandbox.excludedCommands`

In `template/.claude/settings.json.jinja` (and the working copy in
`.claude/settings.json`):

```json
"sandbox": {
  "excludedCommands": ["bd", "bd *"],
  ...
}
```

Bash tool calls of `bd ...` from inside the bwrap sandbox bypass the netns
isolation and run on the host. This is what allows the inner Claude session
to reach ralph's host-level dolt server — which is otherwise impossible
([gastownhall/beads#3582](https://github.com/gastownhall/beads/issues/3582)
documents the constraint).

**Trade-off accepted:** any prompt-injected `bd` invocation runs unsandboxed.
bd's command surface is bounded (issue tracking), so the blast radius is
acceptable for our threat model.

## What this fix does NOT include

- **No `bd-locked` flock wrapper.** Earlier versions of ortus shipped one;
  it was removed (see ralph.sh § "bd flock wrapper removed" comment) because
  prepending `$PWD/ortus` to PATH defeats `excludedCommands`'s typed-token
  match. The wrapper would also hold a project-wide flock indefinitely if
  any wrapped bd hung on a sandboxed loopback.
- **No noms/LOCK cleanup.** Forbidden by upstream. The previous
  `cleanup_stale_dolt_locks` function in ralph.sh was both buggy (the
  pgrep predicate never matched dolt's argv) and dangerous (#2933).
- **No retry-on-lock-error wrappers.** With single-server architecture
  there is no lock contention to retry around.
- **No auto-start disable.** An earlier draft of this fix recommended
  `bd config set dolt.auto-start false` (or `BEADS_DOLT_AUTO_START=0`) as
  belt-and-suspenders against bd's `KillStaleServers` race
  ([gastownhall/beads#3392](https://github.com/gastownhall/beads/issues/3392)).
  **Don't.** It breaks bd in any non-ralph context: a parallel terminal or
  separate Claude session in the same repo can no longer file or update
  issues until ralph is running. The flock guard + ralph-owned lifecycle
  alone are sufficient to prevent the orphan pile-up; the marginal
  protection against KillStaleServers (a rare race that requires the
  dolt to be briefly unreachable) is not worth the parallel-use cost.

## Failure modes prevented

| Failure mode | Mechanism that prevents it |
|---|---|
| Multi-ralph race spawning N dolts | flock guard — refuses second instance |
| Per-iteration auto-start orphans | ralph owns lifecycle; one dolt for whole session |
| Stale crash state confusing next ralph | `start_dolt` clears bd-owned state when prior PID is dead |
| `noms/LOCK` corruption | never touched (upstream #2933) |
| Hook (`SessionStart: bd prime`) silently spawning dolt | empirically reaches host dolt via `excludedCommands` and connects |

## When pile-up still happens — recovery

If you hit the cascade despite the architecture above (most often when bd's
auto-start fails for unrelated reasons — e.g. `.beads/dolt-server.{pid,port}`
go missing while a server keeps running, see
[gastownhall/beads#3392](https://github.com/gastownhall/beads/issues/3392)),
run:

```bash
./ortus/recover-dolt.sh
```

The script SIGKILLs all dolt sql-server processes visible to your shell,
clears bd-owned state files (NOT `noms/LOCK` per
[gastownhall/beads#2933](https://github.com/gastownhall/beads/issues/2933)),
clears stale circuit breakers in `/tmp/beads-circuit/`, then runs
`bd dolt start` and verifies. It refuses to run if `ralph.sh` is currently
holding `.beads/ralph.flock` (ralph already manages dolt's lifecycle —
don't fight it).

`./ortus/recover-dolt.sh --dry-run` prints what it would do without making
changes.

## Verification

```bash
# Parity between dogfood and template:
./scripts/check-ortus-parity.sh

# After running ralph for a while, the count must stay at 1:
pgrep -af 'dolt sql-server' | grep -v 'pgrep' | wc -l   # → 1

# Lock contention errors must remain 0:
grep -c 'database .* is locked' .beads/dolt-server.log  # → 0

# `Starting server` count grows only on cold starts (one per ralph
# session), not per iteration:
grep -c 'Starting server' .beads/dolt-server.log
```

A 1-iteration smoke test:

```bash
./ortus/ralph.sh --iterations 1 --idle-sleep 5
```

Expected: flock acquired → `bd dolt start` succeeds → one iteration runs →
`trap` fires `bd dolt stop` on exit → `pgrep dolt sql-server` returns 0.

## References

- [gastownhall/beads#2933](https://github.com/gastownhall/beads/issues/2933)
  — removing internal Dolt LOCK files causes corruption
- [gastownhall/beads#3392](https://github.com/gastownhall/beads/issues/3392)
  — bd auto-start nondeterministic + stale-lock races
- [gastownhall/beads#3582](https://github.com/gastownhall/beads/issues/3582)
  — sandboxed agents can't reach host-level shared dolt server
  (the constraint that drove the `excludedCommands` decision)
- `sandbox-bd-pipe-todo.md` — companion forensic doc on the
  `bd|xargs|bd` pipe bug (Part 1 — fixed in both `ortus/prompts/ralph-prompt.md`
  and `template/ortus/prompts/ralph-prompt.md.jinja`) and the `bd-locked`
  wrapper removal (Part 2 — done in this commit's predecessors).
- bubbles forensic log: `~/code/bubbles/logs/ralph-20260507-073255.log`
  (the 49-dolt pile-up that drove this redesign).
