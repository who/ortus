# Hello World

## Goal

Ship a minimal "hello world" program so a harness change can be compared
before and after on a fixture whose correct outcome is never in doubt. Keep
scope tiny on purpose.

## Product

A single Node.js CLI package in this repository that, when run with
`node bin/hello.js` (or the npm script `npm start`), prints exactly:

```text
Hello, world!
```

to stdout and exits 0.

## Non-goals

- No web UI, database, Docker, auth, CI matrix, or packaging beyond a basic
  package.json.
- No TypeScript unless the worker finds it strictly necessary; prefer plain JS.
- No extra features (args, i18n, colors, logging frameworks).

## Acceptance

1. `package.json` exists with a `"start"` script that runs the hello program.
2. `npm start` (or `node bin/hello.js`) prints `Hello, world!` followed by a
   newline and exits 0.
3. A small automated check exists (for example `node --test` or a one-line
   shell assertion in `npm test`) that fails if the output is wrong.
4. README.md states how to run it in one short paragraph.

## Decomposition guidance

Prefer 1 to 3 small beads total (bootstrap package + implement hello +
verify/README). Do not invent epics or infrastructure work. If something
outside this PRD is required, stop with PLAN-GAP rather than expanding scope.
