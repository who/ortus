Read `AGENTS.md` first. One context window, one issue, then exit. Do not pick a second issue. Grind starts a fresh process for the next issue; do not compact.

1. **Orient.** Ninety-second standup only: what is in flight, what just happened, what the tree looks like. Do not open work specs here. Each `bd` command is its own Bash call with `bd` as the first token. Never wrap `bd` in a pipe, `xargs`, `&&`, `;`, or `bash -c`. Git commands are their own Bash calls.
   - `bd list --status=in_progress --json --brief` — leftover claims.
   - `bd events tail --limit 20 --json` — recent closes, comments, claims, creates. Comments are the interesting lines (`op=comment`); closes are next. `bd events` starts at enable and does not backfill older comments.
   - `git log -5 --oneline` — what actually landed.
   - `git status --porcelain` — inherited dirty paths.
   Do not `bd show`, `bd show --long`, or `bd comments` in this step. After you pick an id in step 2, `bd show <id> --json` is the work spec and `bd comments <id> --json` is that ticket's thread.

2. **Continue or select.** If any issue is `in_progress`, continue that id. If more than one is `in_progress`, flag human, comment PLAN-GAP, and stop. Else run `bd ready --json`. If empty, exit with no sentinel. Claim the first non-epic issue with `bd update <id> --status=in_progress`. Then `bd show <id> --json` — that packet is the work spec.

3. **Investigate and implement** only that issue. Use CodeGraph when the injected CodeGraph contract requires it. Run the issue's criterion checks and `docs/testing.md`; the bounded hermetic default is `uv run pytest -m fast -n auto --test-timeout=30`. Never run `network` or `live_provider` by default. Fix failures. File leftover work as new beads; do not keep it on this id.

4. **Session-close** that id per `AGENTS.md`: completion comment, commit, `bd close`, `git pull --rebase --autostash`, `bd dolt push`, `git push`. Do not wait for an outer process to commit or close.

5. **Exit.** No sentinel. Do not start another issue. After session-close the goal is achieved: the issue is closed and HEAD is in sync with origin. The criterion-check commands from step 3 are the whole verification — do not run pytest or the repo test suite again. Answer with the id, close reason, HEAD sha, and those commands. Then stop. Do not re-read the implementation.
