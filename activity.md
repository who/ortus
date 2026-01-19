# Activity Log

This file tracks work completed by agents and humans. Add new entries at the top.

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
