# PRD: [Feature Title]

## Metadata
- **Feature ID**: [FEATURE_ID]
- **Project Type**: CLI
- **Created**: [Date]
- **Author**: Claude (from interview)
- **Interview Confidence**: [High/Medium/Low]

## Overview

### Problem Statement
[One paragraph describing the problem this CLI tool solves. What manual or tedious task exists? Who performs it? Why is automation valuable?]

### Proposed Solution
[One paragraph describing how this CLI tool addresses the problem. What workflow does it enable? What approach does it take?]

### Success Metrics
- [Metric 1 - e.g., "Reduce task time from 10 minutes to 30 seconds"]
- [Metric 2 - e.g., "Zero manual errors in batch operations"]
- [Metric 3 - e.g., "95% of users can complete task without documentation"]

## Background & Context
[Why this CLI tool now? What's the motivation? Are there existing tools that fall short? What drives the need?]

## Users & Personas

### Target Users
[Who will use this CLI? Developers? DevOps engineers? System administrators? End users?]

### User Goals
- [Goal 1 - What task do they want to accomplish?]
- [Goal 2 - What outcome do they need?]

### User Environment
- **Operating Systems**: [macOS, Linux, Windows, etc.]
- **Shell**: [bash, zsh, PowerShell, etc.]
- **Technical Level**: [Beginner / Intermediate / Expert]

## Requirements

### Functional Requirements
[P0] FR-001: The CLI shall [core command functionality]
[P0] FR-002: The CLI shall [core command functionality]
[P1] FR-003: The CLI shall [important feature]
[P1] FR-004: The CLI shall [important feature]
[P2] FR-005: The CLI shall [nice-to-have feature]

### Non-Functional Requirements
[P0] NFR-001: The CLI shall provide clear error messages for invalid input
[P0] NFR-002: The CLI shall complete common operations within [X] seconds
[P1] NFR-003: The CLI shall support non-interactive/scripting mode
[P1] NFR-004: The CLI shall respect NO_COLOR environment variable
[P2] NFR-005: The CLI shall generate shell completions for bash/zsh/fish

## CLI Design

### Command Structure

```
[cli-name] [command] [subcommand] [flags] [arguments]
```

### Global Options

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| --help | -h | bool | false | Show help information |
| --version | -v | bool | false | Show version number |
| --config | -c | string | ~/.config/[cli]/config.yaml | Config file path |
| --verbose | | bool | false | Enable verbose output |
| --quiet | -q | bool | false | Suppress non-error output |
| --output | -o | string | text | Output format (text, json, yaml) |

### Commands

#### `[command1]` - [Brief description]

**Usage**:
```bash
[cli-name] [command1] [arguments] [flags]
```

**Arguments**:
| Argument | Required | Description |
|----------|----------|-------------|
| [arg1] | Yes | [Description] |
| [arg2] | No | [Description] |

**Flags**:
| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| --flag1 | -f | string | "" | [Description] |
| --flag2 | | bool | false | [Description] |

**Examples**:
```bash
# Basic usage
[cli-name] [command1] arg1

# With flags
[cli-name] [command1] arg1 --flag1 value

# Piped input
echo "data" | [cli-name] [command1] -
```

#### `[command2]` - [Brief description]

[Repeat structure for each command]

### Input Sources

| Source | Support | Notes |
|--------|---------|-------|
| Command-line arguments | Yes | Primary input method |
| Standard input (stdin) | [Yes/No] | Use `-` as argument |
| File input | [Yes/No] | Use `--file` or `@filename` |
| Environment variables | [Yes/No] | `[CLI_NAME]_*` prefix |
| Config file | [Yes/No] | YAML/JSON/TOML format |

### Output Formats

#### Human-readable (default)
```
[Example of human-readable output]
```

#### JSON (`--output json`)
```json
{
  "result": "value",
  "metadata": {}
}
```

#### YAML (`--output yaml`)
```yaml
result: value
metadata: {}
```

### Exit Codes

| Code | Meaning | When |
|------|---------|------|
| 0 | Success | Operation completed successfully |
| 1 | General error | Unspecified error occurred |
| 2 | Usage error | Invalid arguments or flags |
| 3 | Config error | Configuration file issues |
| 4 | Input error | Invalid or missing input |
| 5 | Network error | Connection or API failures |
| 64-78 | Reserved | BSD sysexits.h compatibility |

### Error Messages

**Format**:
```
Error: [Brief description]

  [Detailed explanation if helpful]

Hint: [Suggestion for fixing]

For more information, try '[cli-name] help [command]'
```

**Guidelines**:
- Use lowercase for error messages (following Unix convention)
- Include actionable hints when possible
- Show relevant context (file path, line number, etc.)
- Support `--verbose` for additional debug info

### Shell Completion

**Supported Shells**:
- bash
- zsh
- fish
- PowerShell

**Installation**:
```bash
# Bash
[cli-name] completion bash > /etc/bash_completion.d/[cli-name]

# Zsh
[cli-name] completion zsh > "${fpath[1]}/_[cli-name]"

# Fish
[cli-name] completion fish > ~/.config/fish/completions/[cli-name].fish
```

### Configuration

**Config File Location**:
- Linux/macOS: `~/.config/[cli-name]/config.yaml`
- Windows: `%APPDATA%\[cli-name]\config.yaml`

**Config Schema**:
```yaml
# [cli-name] configuration
default_output: text
verbose: false
[section]:
  [key]: [value]
```

**Environment Variables**:
| Variable | Description | Example |
|----------|-------------|---------|
| [CLI_NAME]_CONFIG | Config file path | /path/to/config.yaml |
| [CLI_NAME]_VERBOSE | Enable verbose mode | true |

## System Architecture

### Components
- **CLI Parser**: [Framework/library used - e.g., cobra, clap, argparse]
- **Core Logic**: [Business logic description]
- **Output Formatter**: [How output is formatted]
- **Config Loader**: [How configuration is loaded]

### Dependencies
- [Runtime dependency 1]
- [Runtime dependency 2]

## Milestones & Phases

### Phase 1: Foundation
**Goal**: Basic CLI structure with core commands
**Deliverables**:
- Project setup and build configuration
- Argument parsing
- Help system
- Core command implementation

### Phase 2: Full Features
**Goal**: Complete command set
**Deliverables**:
- All commands implemented
- Input validation
- Output formatting
- Error handling

### Phase 3: Polish
**Goal**: Production-ready tool
**Deliverables**:
- Shell completions
- Configuration file support
- Documentation
- Installation instructions

## Epic Breakdown

### Epic: CLI Foundation
- **Requirements Covered**: FR-001, NFR-001
- **Tasks**:
  - [ ] Set up project structure
  - [ ] Implement argument parsing
  - [ ] Create help system
  - [ ] Add version flag

### Epic: Core Commands
- **Requirements Covered**: FR-002, FR-003
- **Tasks**:
  - [ ] Implement [command1]
  - [ ] Implement [command2]
  - [ ] Add input validation
  - [ ] Add output formatting

### Epic: User Experience
- **Requirements Covered**: NFR-004, NFR-005
- **Tasks**:
  - [ ] Generate shell completions
  - [ ] Support NO_COLOR
  - [ ] Add configuration file support
  - [ ] Create man pages / documentation

## Open Questions
- [Question 1 that needs stakeholder input]
- [Question 2]

## Out of Scope
- [Explicitly what this PRD does NOT cover]
- [Feature deferred to future version]

## Appendix

### Glossary
- **Term 1**: Definition
- **Term 2**: Definition

### Reference Links
- [Link 1]
- [Link 2]

### Similar Tools
- [Tool 1]: [How this differs]
- [Tool 2]: [How this differs]
