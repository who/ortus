# Bash-era sunset notes (v0.x-final-bash)

> **Status:** draft (created by ortus-q075.8, Phase 1 pre-work). Consumed by Phase 5 task ortus-sod1.1, which performs the actual tag.

This document is the pre-written announcement for the **final pure-bash release of ortus**, tagged `v0.x-final-bash` at the head of `main` immediately before the Phase 5 deletions land. Phase 5 (ortus-sod1) removes `template/`, `copier.yaml`, `Makefile` parity targets, `scripts/check-ortus-parity.sh`, `ortus/*.sh`, `ortus/lib/*.sh`, and `ortus/prompts/*.md` — i.e., everything that made ortus a Copier-vendored bash toolkit. After Phase 5, the canonical repo ships only the Python CLI under `src/ortus/`.

The pre-tag exists so that users who prefer the bash workflow can pin to a known-good commit and keep using `copier copy gh:who/ortus@v0.x-final-bash` indefinitely.

---

## CHANGELOG.md entry (copy-paste at tag time)

```markdown
## v0.x-final-bash — YYYY-MM-DD

**Final pure-bash release of ortus.** The next release ships as a global Python CLI (`uv tool install ortus`) per PRD-ortus-global-cli; all `template/`, `copier.yaml`, `Makefile` parity targets, and `ortus/*.sh` scripts are removed in the immediately following commit (Phase 5, ortus-sod1).

This tag is the supported pinning point for anyone who wants to keep using the Copier-vendored bash workflow. There is no maintenance commitment beyond CVE-grade fixes; new feature work happens on the Python CLI.

### What this tag contains
- The complete copier template under `template/`
- The bash orchestrators: `ortus/goal.sh`, `ortus/ralph.sh` (shim), `ortus/idea.sh`, `ortus/interview.sh`, `ortus/triage.sh`, `ortus/human.sh`, `ortus/tail.sh`
- Bash helpers under `ortus/lib/` (sandbox.sh, cache.sh)
- Bundled prompts under `ortus/prompts/`
- `make parity` and `scripts/check-ortus-parity.sh`

### What changes on the next release
- All paths listed above are deleted
- Distribution switches to PyPI (`uv tool install ortus`) + a single `install.sh` released as a GitHub release asset
- Eight verbs land under one umbrella command: `ortus init|plan|grind|interview|tail|triage|human|check`
- `make parity` ceases to exist; `pyproject.toml` becomes the only build-system declaration
```

> When tagging, replace `YYYY-MM-DD` with the actual tag date. The tag should be **annotated** (`git tag -a v0.x-final-bash -m "..."`) with the message above truncated to a short summary plus a pointer to this file in `main`.

---

## Rollback / pinning instructions (for users)

Users who want to stay on the bash workflow can pin Copier to this tag. The bash workflow keeps working as-is — it just stops receiving updates.

```bash
# Generate a new project from the final bash-era ortus
copier copy gh:who/ortus@v0.x-final-bash ./my-project
cd my-project

# Or, for an existing copier-managed project, force-update to the pinned tag
copier update --vcs-ref v0.x-final-bash --defaults
```

Existing projects that were generated from earlier bash-era commits do **not** need to do anything — they already have their own vendored copy of `ortus/` and will continue to run. Pin to the tag only if you want to regenerate from a known-good state.

### Installing the bash-era ortus from a fresh clone

```bash
git clone --branch v0.x-final-bash https://github.com/who/ortus.git
cd ortus
# Use the canonical bash workflow directly, no copier needed:
./ortus/goal.sh        # primary orchestrator
./ortus/idea.sh        # PRD intake / interview / decomposition
./ortus/tail.sh        # log watcher
```

---

## What Phase 5 (ortus-sod1) will tag, in order

Phase 5's task ordering, for reviewer reference:

1. **ortus-sod1.1 — Tag `v0.x-final-bash`** (annotated, on the commit immediately before any deletions). Uses this file as the source of the annotation message and the announcement copy.
2. **ortus-sod1.2 — Delete copier scaffold** (`template/`, `copier.yaml`, `Makefile` parity targets, `scripts/check-ortus-parity.sh`).
3. **ortus-sod1.3 — Delete bash sources** (`ortus/*.sh`, `ortus/lib/*.sh`, `ortus/prompts/*.md`). The `scripts/` directory keeps `replay-reduce.sh`, `analyze-goal-logs.sh`, `eval-cost.sh` (preserved per Testing Strategy + PRD-goal-directive §E5).
4. **ortus-sod1.4 — Rewrite root docs** (`README.md`, `CLAUDE.md`, `AGENTS.md`) to reference `ortus <verb>` exclusively. `grep -rE 'template/|copier|make parity|ortus/.*\.sh'` across these docs must return zero hits.
5. **ortus-sod1.5 — Final smoke**: fresh clone → `uv tool install --editable .` → run the full `ortus init` → `ortus plan` → `ortus grind` → `ortus human` → `ortus tail` flow against a fresh fixture.

Reading this file cold, a reviewer should understand: **the bash era is being archived under a single annotated tag, not deprecated in place; the Python CLI is a hard cut-over with no in-repo shim; users who want the old shape pin to this tag.**
