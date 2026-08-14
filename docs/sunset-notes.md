# Bash-era sunset notes (v0.x-final-bash)

> **Status:** done. Tag `v0.x-final-bash` points at `e492e07`, the last commit
> that still shipped the Copier-vendored bash toolkit. The following commit
> removed that tree from `main`.

This document is the announcement for the **final commit that still contained
the Copier template**. After that tag, the canonical repo ships only the Python
CLI under `src/ortus/`.

Users who prefer the bash workflow can pin to the tag and keep using
`copier copy gh:who/ortus@v0.x-final-bash`. There is no maintenance commitment
beyond CVE-grade fixes; new feature work happens on the Python CLI.

---

## What the tag contains

- The complete Copier template under `template/`
- The bash orchestrators copied into that template (`goal.sh`, `ralph.sh`,
  `idea.sh`, `interview.sh`, `triage.sh`, `human.sh`, `tail.sh`)
- Bash helpers under `template/ortus/lib/`
- Bundled prompts under `template/ortus/prompts/`
- `make parity` and `scripts/check-ortus-parity.sh`

Root `ortus/*.sh` had already been removed on `main` before the tag; the
template tree was the remaining copy.

## What `main` no longer contains

- `template/`, `copier.yaml`, `Makefile`, `scripts/check-ortus-parity.sh`,
  `scripts/check-structural-parity.sh`, `extensions/`
- Archived Copier/bash test modules that only ran against that tree
- The `copier` and `copier-template-extensions` dev extras

Distribution is the Python CLI (`uv tool install ortus`). Eight verbs live
under one umbrella: `ortus init|plan|grind|interview|tail|triage|human|check`.

---

## Pinning instructions (for users who want the bash workflow)

```bash
# Generate a new project from the final bash-era ortus
copier copy gh:who/ortus@v0.x-final-bash ./my-project
cd my-project

# Or, for an existing copier-managed project, force-update to the pinned tag
copier update --vcs-ref v0.x-final-bash --defaults
```

Existing projects that were generated from earlier bash-era commits do **not**
need to do anything — they already have their own vendored copy of `ortus/`
and will continue to run.

### Installing the bash-era ortus from a fresh clone

```bash
git clone --branch v0.x-final-bash https://github.com/who/ortus.git
cd ortus
# Use the canonical bash workflow directly from the template tree:
./template/ortus/goal.sh
```
