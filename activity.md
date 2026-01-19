# Activity Log

This file tracks work completed by agents and humans. Add new entries at the top.

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
