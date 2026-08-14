Read `AGENTS.md` first. One context window, one issue, then exit. Do not pick a second issue. Grind starts a fresh process for the next issue; do not compact.

1. **Orient.** Run `bd list --status=closed --sort closed --limit 3 --json`. Then run `bd show --long` on those ids. Each `bd` command is its own Bash call with `bd` as the first token. Never wrap `bd` in a pipe, `xargs`, `&&`, `;`, or `bash -c`.

2. **Continue or select.** If any issue is `in_progress`, continue that id. If more than one is `in_progress`, flag human, comment PLAN-GAP, and stop. Else run `bd ready --json`. If empty, exit with no sentinel. Claim the first non-epic issue with `bd update <id> --status=in_progress`. Then `bd show <id> --json` — that packet is the work spec.

3. **Investigate and implement** only that issue. Use CodeGraph when the injected phase contract requires it. Run the issue's criterion checks and `docs/testing.md`. Fix failures. File leftover work as new beads; do not keep it on this id.

4. **Session-close** that id per `AGENTS.md`: completion comment, commit, `bd close`, `git pull --rebase --autostash`, `bd dolt push`, `git push`. Do not wait for an outer process to commit or close.

5. **Exit.** No sentinel. Do not start another issue.
