# Video Pipeline Ralph Prompt

Read @AGENTS.md for session rules and landing-the-plane protocol.

You are invoked in a bash loop. Each invocation = one task. The loop restarts you with fresh context after you exit. Do ONE thing, then stop.

## Your Task

1. **Config**: Read `MODEL.md` for provider config (provider, model, api_key_env, resolution, poll settings)
2. **Orient**: Run `bd activity --limit 10 --json | jq -r '.[].issue_id' | sort -u | xargs -I{} sh -c 'echo "=== {} ===" && bd comments {} 2>/dev/null'` to see what happened in previous loops
3. **Select**: Run `bd ready --json` to get issues with no blockers. If empty, output `<promise>EMPTY</promise>` and stop immediately (do not output BLOCKED).
4. **Claim**: Run `bd update <id> --status=in_progress` for the first issue before doing anything else
5. **Read task**: Read the task description to extract the generation prompt, acceptance criteria, and output path
6. **Execute by task type**: Follow the appropriate handler below (scene, verify-continuity, stitch-final, or epic/feature)
7. **Log**: Add structured completion comment (see format below)
8. **Close**: Run `bd close <id> --reason="<brief summary>"`
9. **Commit & Push**: Stage manifest and beads, commit with issue ID in message, then `git pull --rebase && bd sync && git push`

```bash
# After closing a scene or assembly task:
git add clips-manifest.json
bd sync
git commit -m "<id>: <brief summary>"
git pull --rebase && git push
```

10. **Exit**: Output `<promise>COMPLETE</promise>` and stop. The loop will restart you for the next task.

If you cannot complete the claimed issue, add a comment explaining the blocker via `bd comments add <id> "..."`, then output `<promise>BLOCKED</promise>` and stop.

## Task Type: Scene Generation

Scene tasks generate a single video clip and verify it against acceptance criteria.

### Workflow

```
1. Extract from task description:
   - Generation prompt (combining scene description + STYLE.md rules)
   - Acceptance criteria (duration, shot_type, content, color_grade, audio)
   - Output path (e.g., output/clips/scene-001.mp4)

2. Generate the clip:
   python -m video.generate \
     --prompt "<generation prompt>" \
     --duration <seconds> \
     --output <output_path> \
     --config MODEL.md

3. Verify the clip:
   python -m video.verify.runner <output_path> '<criteria_json>'

   criteria_json example:
   {
     "duration": "5-7s",
     "shot_type": "wide",
     "color_grade": {"color_temp_range": [4000, 5500], "max_saturation": 120},
     "content": {"required_subjects": ["person", "highway"], "prohibited_elements": ["text", "watermark"]},
     "audio": {"ambient_present": true, "silence_floor_db": -60}
   }

4. If ALL checks PASS:
   - Update clips-manifest.json with clip metadata (path, provider, model, prompt, verification status)
   - Close the task and commit

5. If ANY check FAILS:
   - Read the verification report at output/reports/<clip>-verify.json
   - Log the failure as a bd comment with which criteria failed and why
   - Rewrite the generation prompt to address failures (add negative prompts, adjust shot description)
   - Log the revised prompt as a bd comment
   - Retry from step 2
```

### Retry Logic

- **Maximum 3 attempts** per scene task
- Track attempts via bd comments (each attempt = one comment with the prompt used and verification result)
- After 3 failures: run `bd update <id> --status=blocked`, add a comment with all failure details, output `<promise>BLOCKED</promise>` and stop
- The human reviews the blocked task and either adjusts the scene spec or re-opens it

### Manifest Update

After successful generation and verification, update `clips-manifest.json`:

```python
# Use the manifest module:
from video.manifest import load_manifest, update_clip, save_manifest

manifest = load_manifest()
update_clip(manifest, "scene-NNN", {
    "path": "output/clips/scene-NNN.mp4",
    "provider": "<from MODEL.md>",
    "model": "<from MODEL.md>",
    "prompt": "<generation prompt used>",
    "generated_at": "<UTC ISO timestamp>",
    "duration_seconds": <actual duration>,
    "resolution": "<from MODEL.md>",
    "verification": {
        "status": "pass",
        "report_path": "output/reports/scene-NNN-verify.json",
        "checked_at": "<UTC ISO timestamp>",
        "attempts": <attempt number>
    }
})
save_manifest(manifest)
```

Then commit the manifest: `git add clips-manifest.json`

## Task Type: verify-continuity

Runs cross-scene continuity checks after all scene tasks are closed.

### Workflow

```
1. Run continuity check on all clips in the manifest:
   python -m video.assemble.continuity --manifest clips-manifest.json

2. If ALL checks PASS:
   - Close the task

3. If ANY check FAILS:
   - Parse the output to identify offending scenes
   - Reopen the offending scene tasks:
     bd update <scene-id> --status=open
   - Add a comment to each reopened task explaining the continuity issue
     (e.g., "Color temperature delta of 1500K with adjacent scene-002 — regenerate with warmer grade")
   - Add a comment to the verify-continuity task listing what was reopened
   - Output <promise>BLOCKED</promise> and stop
     (The reopened scene tasks will be picked up by future loop iterations,
      and verify-continuity will become ready again once they close)
```

## Task Type: stitch-final

Assembles all approved clips into the final rendered video.

### Workflow

```
1. Run the stitch:
   python -m video.assemble.stitch --manifest clips-manifest.json

2. If successful (exit 0):
   - The stitch module updates clips-manifest.json with assembly.final_render automatically
   - Close the task
   - Commit the updated manifest

3. If failed (exit 1):
   - Log the error output as a bd comment
   - Output <promise>BLOCKED</promise> and stop
```

## Task Type: epic/feature (Milestone Check)

These are containers for related work — they do not involve direct implementation.

```
1. Run bd show <id> to see child issues
2. If ALL children are closed:
   - Close with bd close <id> --reason="All child issues complete"
3. If children remain open:
   - Output <promise>BLOCKED</promise> — the loop will retry later
```

## Subagent Strategy

**Three principles:**
1. **Main context = scheduler only** — never do leaf work in the main context
2. **Subagents = disposable memory** — they read, summarize, and return; main context stays clean
3. **Simplicity wins** — prefer many simple subagents over few complex ones

**Allocation table:**

| Category | Model | Effort | Parallelism | Examples |
|----------|-------|--------|-------------|----------|
| Reads | Sonnet | low | up to 500 parallel | read SCRIPT.md, STYLE.md, MODEL.md, manifest |
| Writes | Sonnet | high | 1 serial | update manifest, edit prompts |
| Generation | Sonnet | high | 1 serial | run video.generate CLI |
| Verification | Sonnet | medium | 1 serial | run video.verify.runner, video.assemble.continuity |
| Reasoning | Opus | max | 1 | prompt rewriting after verification failure |

**Why serial for generation and verification:** Video generation is a long-running external API call. Verification must follow generation. Keep these serial to get clear pass/fail signals and avoid wasted API spend on clips that will be discarded.

## Context Management

- Fresh ~200K token window per invocation
- 40-60% utilization is the "smart zone" — past 60% model quality degrades
- Never load large files into the main context — use subagents to read and summarize
- Prefer markdown over JSON for LLM communication — fewer tokens, same information
- One scene = one invocation = clean context

## Important Rules

- **One task per invocation** - Do not run `bd ready` a second time. Do not claim a second issue.
- **No partial work** - Either complete the issue fully or declare it BLOCKED
- **Max 3 retries** - After 3 failed generation+verification cycles, mark blocked for human review
- **Git tracks manifest, not clips** - Commit `clips-manifest.json` changes. Never commit `.mp4` files.
- **Read STYLE.md** - Always incorporate style guide rules when constructing generation prompts
- **Found bugs** - Never fix bugs inline. Always `bd create --type=bug` to track separately
- **Verify acceptance criteria** - Tasks MUST NOT be closed unless ALL acceptance criteria pass
- **Descriptive commits** - Include issue ID in commit message

## Completion Comment Format

```bash
bd comments add <id> "**Changes**:
- <what was generated or checked>
- <manifest updates>

**Verification**: <check results, attempt count, pass/fail details>"
```

**Scene example:**
```bash
bd comments add ortus-abc1 "**Changes**:
- Generated output/clips/scene-001.mp4 (attempt 2, prompt revised for wider framing)
- Updated clips-manifest.json with clip metadata and verification status

**Verification**: duration ✓ (6.2s, spec 5-7s), shot_type ✓ (wide), color_grade ✓ (CCT 5100K), content ✓ (person, highway visible), audio ✓ (ambient present)"
```

**Continuity example:**
```bash
bd comments add ortus-def2 "**Changes**:
- Ran continuity check across 8 scene clips

**Verification**: resolution_uniformity ✓ (all 1280x720), color_consistency ✓ (max delta 600K), subject_persistence ✓"
```

## Completion Signals

**EMPTY** — When `bd ready` returns no issues:
```
<promise>EMPTY</promise>
```

**COMPLETE** — When you have successfully completed ONE issue:
```
<promise>COMPLETE</promise>
```

**BLOCKED** — When the claimed issue cannot be completed (failed 3 retries, continuity failure reopened scenes, technical blocker). Add a comment first:
```
<promise>BLOCKED</promise>
```

After outputting any signal, stop immediately. Do not continue working.
