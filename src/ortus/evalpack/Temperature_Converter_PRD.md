# Temperature Converter CLI

## Goal

Ship a small Node.js command-line converter, so that a harness change can be
compared before and after on a fixture that is one step richer than Hello
World: several beads, two automated test files, and one real dependency
between beads. It must still finish in minutes, not hours.

## Product

A single Node.js package in this repository exposing `bin/tconv.js` and
runnable as `node bin/tconv.js <value><unit> --to <unit>`:

```text
$ node bin/tconv.js 100C --to F
212F
```

Units are `C`, `F` and `K`. Input units are case-insensitive; output units are
upper case. `--json` prints `{"value":212,"unit":"F"}` on one line instead of
the plain line. A value that does not parse as a number, a missing `--to`, or
a unit outside the three exits 1 with a one-line message on stderr and prints
nothing on stdout.

## Non-goals

- No web server, HTTP API, database, Docker, auth, or CI configuration.
- No units beyond C, F and K — no length, mass, or currency conversion.
- No runtime dependency outside the Node standard library; tests use
  `node --test`.
- No TypeScript unless the worker finds it strictly necessary; prefer plain JS.
- No interactive prompt, config file, or persisted history.

## Acceptance

1. `package.json` exists with `"start"` and `"test"` scripts, and declares no
   runtime dependencies.
2. `src/convert.js` exports `convert(value, from, to)` — pure, no I/O, exact
   for the three units, and throwing on an unknown unit.
3. `src/parse.js` exports `parseRequest(argv)`, returning the value, source
   unit, target unit, and whether `--json` was asked for, and throwing a
   one-line error for malformed input.
4. `bin/tconv.js` wires the parser to the converter, prints the plain line, and
   exits 0; a thrown parse or conversion error becomes a stderr line and
   exit 1.
5. `--json` prints the object form described above and exits 0.
6. Two test files under `test/` — one covering the conversion table (including
   a round trip through K) and one covering parse failures and CLI exit codes
   — both passing under `npm test`.
7. README.md states how to run and how to test it, in one short paragraph
   each.

## Decomposition guidance

Prefer 5 to 8 beads: bootstrap the package, the conversion module, the
argument parser, the CLI wiring, `--json` output, then tests plus README. The
CLI-wiring bead must depend on both the conversion-module bead and the
argument-parser bead; that dependency edge is part of what this fixture
exercises, so do not collapse those three into a single bead. Do not invent
epics or infrastructure work. If something outside this PRD is required, stop
with PLAN-GAP rather than expanding scope.
