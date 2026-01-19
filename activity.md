# Activity Log

This file tracks work completed by agents and humans. Add new entries at the top.

---

## 2026-01-19T12:15:00-08:00 - Delete old PRD scripts from template/

**Task**: ortus-aqz - Delete old PRD scripts from template/
**Status**: Completed
**Changes**:
- Deleted 5 old PRD scripts: generate-prd.sh, generate-interview.sh, collect-interview.sh, generate-prd-from-interview.sh, prd-pipeline.sh
- Updated copier.yaml _tasks chmod list to only include ralph.sh, lisa.sh, tail.sh
- Updated copier.yaml _message_after_copy to reference lisa.sh workflow
- Updated README.md to document lisa.sh usage instead of generate-prd.sh
- Updated README.md file structure to show lisa.sh instead of generate-prd.sh
- Fixed reference in template/prd/PRD-INTERVIEW-PROMPT.md from prd-pipeline.sh to lisa.sh

**Verification**: git status shows clean file removals, no broken references outside .beads/

---

## 2026-01-19T11:45:00-08:00 - Implement lisa.sh approval and ralph handoff

**Task**: ortus-nxe - Implement lisa.sh approval and ralph handoff
**Status**: Completed
**Changes**:
- Added `generate_tasks_from_prd()` function that calls Claude to break PRD into implementation tasks
- Claude prompt asks for 3-10 atomic tasks with priorities and dependencies
- XML parsing extracts title, priority, depends_on, and description for each task
- Tasks created with `--assignee=ralph` using `bd create`
- Dependencies between tasks handled via task numbering system
- Implemented `handle_approved()` function as main entry point
- Detects 'approved' label and reads the PRD file (prd/PRD-<slug>.md)
- After task creation, removes 'prd:ready' and 'approved' labels
- Closes the idea with summary: "PRD complete. Created X tasks for ralph."
- Log output shows task creation details and provides `bd list --assignee ralph` command

**Verification**: Syntax check passes, help output correct. Full integration requires Claude API.

---

## 2026-01-19T11:15:00-08:00 - Implement lisa.sh PRD document generation

**Task**: ortus-9pm - Implement lisa.sh PRD document generation
**Status**: Completed
**Changes**:
- Added `slugify()` helper function to create URL-safe filenames from titles
- Implemented `generate_prd_document()` function that uses Claude to create comprehensive PRDs
- PRD prompt includes idea details, interview answers, and standard PRD structure
- PRD saved to `prd/PRD-<slugified-title>.md`
- Validates Claude output starts with `# PRD:` to ensure valid document
- Updated `handle_interviewing()` to call PRD generation after collecting answers
- Updated `handle_ready()` to check PRD file exists and provide status/instructions
- PRD structure includes: Metadata, Overview, Requirements, Architecture, Milestones, Epic Breakdown

**Verification**: Syntax check passes, help output correct. Full integration requires Claude API.

---

## 2026-01-19T10:45:00-08:00 - Implement lisa.sh interview completion detection

**Task**: ortus-om6 - Implement lisa.sh interview completion detection
**Status**: Completed
**Changes**:
- Implemented `handle_interviewing()` function to detect when all question beads are closed
- Uses `bd show --json` to get dependencies and checks their status
- Counts total dependencies vs open dependencies to determine completion
- Implemented `collect_answers()` function to extract answers from question bead comments
- Iterates through dependency IDs and collects comment text from each
- Formats answers as markdown with question titles as headers
- Answers stored in `logs/.lisa-answers-<idea-id>.tmp` for PRD generation phase
- Label transitions: removes `prd:interviewing`, adds `prd:ready`
- Handles edge cases: no questions (skips to ready), questions without answers (noted)

**Verification**: Syntax check passes, help output correct. Full integration requires Claude API.

---

## 2026-01-19T10:15:00-08:00 - Implement lisa.sh interview generation

**Task**: ortus-n75 - Implement lisa.sh interview generation
**Status**: Completed
**Changes**:
- Implemented `generate_interview_questions()` function that calls Claude to analyze ideas
- Claude generates 3-7 discovery questions in XML format for reliable parsing
- Questions cover: problem space, users, scope, success criteria, constraints
- Each question created as a bead assigned to 'human' with instructions on how to answer
- Blocking dependencies added: idea depends on all question beads
- `prd:interviewing` label added to idea after questions created
- Implemented `handle_new_idea()` state handler that orchestrates the flow
- Dual parsing approach: tries grep -P first, falls back to line-by-line sed parsing

**Verification**: Syntax check passes, help output correct. Full integration requires Claude API.

---

## 2026-01-19T09:25:00-08:00 - Create lisa.sh core loop structure

**Task**: ortus-oet - Create lisa.sh core loop structure
**Status**: Completed
**Changes**:
- Created `template/lisa.sh` with continuous polling loop (similar to ralph.sh)
- Configurable `--poll-interval` and `--idle-sleep` options
- Timestamped logging to `logs/lisa-<timestamp>.log`
- State routing based on labels: prd:interviewing, prd:ready, prd:approved
- Processes ideas assigned to 'lisa' via `bd ready --assignee lisa`
- Graceful handling of empty queue (sleeps and retries)
- Stubbed state handlers for: new ideas, interviewing, ready, approved
- Fixed epic dependency structure: removed subtask→epic dependencies that were blocking work

**Verification**: Syntax check passes, help output correct, brief loop test shows correct polling and logging behavior.

---

## 2026-01-19T08:40:00-08:00 - Refactor test-ralph.sh into tests/ folder with external task definitions

**Task**: ortus-yfp - Refactor test-ralph.sh into tests/ folder with external task definitions
**Status**: Completed
**Changes**:
- Created `tests/` folder structure with `fixtures/` subfolder
- Created `tests/fixtures/calculator-tasks.md` with 3 greeting-based tasks (greet, greet_person, farewell)
- Moved and refactored `test-ralph.sh` to `tests/test-ralph.sh`
- Added `create_tasks_from_fixture()` function to parse markdown task definitions
- Uses `--body-file -` to properly pass task descriptions from parsed markdown
- Tasks are created with titles, descriptions, and acceptance criteria from the fixture file
- Dependencies are set up automatically between sequential tasks
- Removed old `test-ralph.sh` from repository root

**Usage**:
- `./tests/test-ralph.sh --dry-run` - Set up test project, show manual instructions
- `./tests/test-ralph.sh` - Full test (requires Claude API)
- `./tests/test-ralph.sh --keep` - Full test, preserve test project after completion

**Verification**: Dry-run mode passes - creates 3 tasks with full descriptions and acceptance criteria from fixture file, sets up correct dependency chain, shows 1 ready task.

---

## 2026-01-19T08:15:00-08:00 - Create test suite for ralph.sh with deterministic project

**Task**: ortus-1im - Create test suite for ralph.sh with deterministic project
**Status**: Completed
**Changes**:
- Created `test-ralph.sh` in repo root (not included in template)
- Test generates fresh project from template with Python/uv defaults
- Creates 3 deterministic calculator tasks (add, subtract, multiply functions)
- Tasks have chained dependencies to ensure proper ordering
- Test 1 verifies `--tasks 1` completes exactly 1 task
- Test 2 verifies unlimited ralph completes all remaining tasks
- Added `--dry-run` mode for setup inspection without running Claude
- Added `--keep` flag to preserve test project for debugging
- Added `--help` flag with usage information

**Usage**:
- `./test-ralph.sh --dry-run` - Set up test project, show manual instructions
- `./test-ralph.sh` - Full test (requires Claude API, may have some flakiness)
- `./test-ralph.sh --keep` - Full test, preserve test project after completion

**Verification**: Dry-run mode tested successfully - generates project, creates 3 tasks with correct dependencies (1 ready due to blocking), shows correct task count.

---

## 2026-01-19T08:30:00-08:00 - Merge ralph.sh and mega-ralph.sh with task limit option

**Task**: ortus-2wr - Epic: Merge ralph.sh and mega-ralph.sh with task limit option
**Status**: Completed
**Changes**:
- Merged `mega-ralph.sh` logic into `ralph.sh` - runs until queue empty by default (mega mode)
- Added `--tasks N` option to limit number of tasks to complete
- Added `--iterations N` option (replaces positional arg, with legacy support)
- Added `--idle-sleep N` option for configuring wait time when no work available
- Added `-h|--help` flag for showing usage information
- Removed `mega-ralph.sh` from both template/ and repository root
- Updated `copier.yaml` _tasks to remove mega-ralph.sh from chmod list
- Updated `README.md` with new usage examples and file structure

**Behavior**:
- `./ralph.sh` - Run until all tasks complete (default mega mode)
- `./ralph.sh --tasks 1` - Complete exactly 1 task then exit
- `./ralph.sh --tasks 5` - Complete up to 5 tasks then exit
- `./ralph.sh 20` - Legacy: 20 iterations per task (still works)

**Verification**: Tested template generation - ralph.sh is present and functional, mega-ralph.sh is absent, --help flag works correctly.

---

## 2026-01-19T08:10:00-08:00 - Fix ralph.sh tail.sh usage to show no arguments needed

**Task**: ortus-owu - Update ralph.sh log tailing instructions to mention tail.sh
**Status**: Completed
**Changes**:
- Fixed header comment in `template/ralph.sh` to show `./tail.sh` without arguments
- Fixed runtime log output to show `./tail.sh` without arguments
- Added "(auto-follows all logs)" clarification to explain tail.sh behavior
- Human-readable option uses `./tail.sh` (no args needed, auto-watches logs/)
- Raw output option uses standard `tail -f` for a specific log file

**Verification**: Tested template generation - ralph.sh is copied to generated projects with correct instructions showing tail.sh requires no arguments.

---

## 2026-01-19T07:30:00-08:00 - Add tail.sh to template with terminal-agnostic colors

**Task**: ortus-510 - Add tail.sh to template with terminal-agnostic colors
**Status**: Completed
**Changes**:
- Created `template/tail.sh` with improved color handling using `tput` for terminal-aware colors
- Added `NO_COLOR` environment variable support (https://no-color.org/)
- Replaced hardcoded ANSI gray (90m) with `DIM` attribute that works across themes
- Added fallback to basic ANSI codes when `tput` is unavailable
- Added `tail.sh` to chmod task in `copier.yaml`
- Removed old `tail.sh` from repository root

**Verification**: Tested template generation - tail.sh is copied to generated projects, is executable, and supports NO_COLOR=1 for color-free output.

---

## 2026-01-19T07:10:00-08:00 - Run prerequisite checks without adding shell script to generated project

**Task**: ortus-v3j - Run prerequisite checks without adding shell script to generated project
**Status**: Completed
**Changes**:
- Created `extensions/prerequisites.py` with `PrerequisiteChecker` class that runs tool availability checks during template generation
- Updated `copier.yaml` to load the new extension
- Added `check-prerequisites.sh` to `_exclude` list so it's not copied to generated projects
- Removed `./check-prerequisites.sh` from `_tasks` (no longer needed)
- Removed `check-prerequisites.sh` from chmod task list
- Added deduplication logic to ensure checks only run once per generation

**Verification**: Tested template generation - prerequisite checks run once during generation, check-prerequisites.sh is not present in generated project.

---

## 2026-01-18T16:41:12-08:00 - Git config defaults for copier wizard

**Task**: ortus-qom - Use git config for default author_name and author_email in copier wizard
**Status**: Completed
**Changes**:
- Created `extensions/context.py` with `GitConfigContext` class that retrieves git config values
- Updated `copier.yaml` to load `copier_templates_extensions.TemplateExtensionLoader` and custom extension
- Changed `author_name` default from "Developer" to `{{ git_user_name }}`
- Changed `author_email` default from "dev@example.com" to `{{ git_user_email }}`
- Extension gracefully falls back to "Developer" / "dev@example.com" when git config is not set

**Verification**: Tested template generation with and without git config values set. Template generates successfully in both cases with correct defaults.

**Note**: Users must have `copier-template-extensions` installed alongside copier for this feature to work (e.g., `uv tool install copier --with copier-template-extensions`).

---

<!-- Example entry:
## 2024-01-15T14:30:00-05:00 - Task completed

**Task**: proj-a1b2c3 - Add user authentication
**Status**: Completed
**Changes**:
- Added auth middleware in src/middleware/auth.ts
- Created login/logout endpoints
- Added JWT token validation

**Verification**: All tests passing, manual login flow tested

---
-->
