#!/usr/bin/env bash
# setup-video-beads.sh - Create bead hierarchy for a video film project
#
# Usage: ./ortus/setup-video-beads.sh
#
# Reads SCRIPT.md, STYLE.md, MODEL.md and creates the full bead hierarchy:
#   - One epic for the film
#   - One feature per act
#   - One task per scene with full prompt + acceptance criteria
#   - Assembly tasks (verify-continuity, stitch-final) with dependencies

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Validate required files exist
for file in SCRIPT.md STYLE.md MODEL.md; do
    if [[ ! -f "$PROJECT_DIR/$file" ]]; then
        echo "Error: $PROJECT_DIR/$file not found"
        echo "Required files: SCRIPT.md, STYLE.md, MODEL.md"
        exit 1
    fi
done

echo "Setting up video beads from:"
echo "  Script: $PROJECT_DIR/SCRIPT.md"
echo "  Style:  $PROJECT_DIR/STYLE.md"
echo "  Model:  $PROJECT_DIR/MODEL.md"
echo ""

cd "$PROJECT_DIR"

PROMPT=$(cat <<'PROMPT_EOF'
You are a film production planner. Read the provided files and create a complete bead issue hierarchy for the video generation pipeline.

## Instructions

1. **Read the files**: Read SCRIPT.md (scenes/acts), STYLE.md (visual style guide), MODEL.md (generation config)
2. **Create the hierarchy** using bd commands:

### Epic (1 total)
- Create one epic for the entire film with title from SCRIPT.md
- Type: epic, priority: 1

### Features (1 per act)
- Create one feature per act described in SCRIPT.md
- Title format: "Act N: <act title/description>"
- Type: feature, priority: 1
- Add dependency: each feature depends on the film epic

### Scene Tasks (1 per scene)
- Create one task per scene in each act
- Title format: "scene-NNN: <brief scene description>" (NNN = zero-padded scene number, sequential across all acts)
- Type: task, priority: 2
- Description MUST include:
  - The full generation prompt combining: scene description from SCRIPT.md + visual style from STYLE.md
  - Output path: clips/scene-NNN.mp4
  - Duration from SCRIPT.md (or default 5s if not specified)
- Acceptance criteria MUST include:
  - clips/scene-NNN.mp4 exists and is a valid video file
  - Video matches the scene description and style guide
  - Duration matches specification
  - Testing: `python -m video.generate --prompt "<prompt>" --duration <N> --output clips/scene-NNN.mp4`
- Add dependency: each scene task depends on its act feature

### Assembly Tasks (2 total)
- **verify-continuity**: Task to verify visual continuity across all clips
  - Type: task, priority: 2
  - Depends on ALL scene tasks
- **stitch-final**: Task to assemble all clips into the final video
  - Type: task, priority: 2
  - Depends on verify-continuity

3. **Execution**: Run bd create and bd dep add commands to build the full graph. Use parallel subagents where possible for efficiency.

4. **When done**: Print a summary of all created issues and their dependencies, then tell the user to type /exit.
PROMPT_EOF
)

echo "$PROMPT" | claude \
    --allowedTools "Read(SCRIPT.md),Read(STYLE.md),Read(MODEL.md),Bash(bd:*)" \
    --dangerously-skip-permissions

echo ""
echo "Setup complete. Next steps:"
echo "  bd ready          # See available work"
echo "  ./ortus/ralph.sh  # Start generating clips"
