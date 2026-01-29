# PRD: [Feature Title]

## Metadata
- **Feature ID**: [FEATURE_ID]
- **Project Type**: Roblox
- **Created**: [Date]
- **Author**: Claude (from interview)
- **Interview Confidence**: [High/Medium/Low]

## Overview

### Problem Statement
[One paragraph describing the problem this game/experience solves. What entertainment need exists? Who is the target audience? Why will players want to play?]

### Proposed Solution
[One paragraph describing how this game/experience addresses the problem. What core gameplay loop does it provide? What makes it engaging?]

### Success Metrics
- [Metric 1 - e.g., "D1 retention > 30%"]
- [Metric 2 - e.g., "Average session length > 15 minutes"]
- [Metric 3 - e.g., "Concurrent users > 100 during peak"]

## Background & Context
[Why this game now? What's the inspiration? What similar games exist on Roblox? What makes this different or better?]

## Players & Personas

### Primary Persona: [Name]
- **Age Range**: [Target age group]
- **Play Style**: [Casual / Competitive / Social / Explorer]
- **Session Length**: [How long they typically play]
- **Goals**: [What they want from the experience]
- **Frustrations**: [What makes them leave games]

### Secondary Persona: [Name]
- **Age Range**: [Target age group]
- **Play Style**: [Casual / Competitive / Social / Explorer]
- **Goals**: [What they want from the experience]

### Player Journeys

#### First-Time Player (FTUE)
1. Player joins the game
2. [Onboarding step 1]
3. [Onboarding step 2]
4. Player understands core loop

#### Returning Player
1. Player rejoins
2. [What they see/do first]
3. [Progression check]
4. [Core gameplay begins]

## Requirements

### Functional Requirements
[P0] FR-001: Players shall be able to [core gameplay mechanic]
[P0] FR-002: Players shall be able to [core gameplay mechanic]
[P0] FR-003: The game shall [essential system]
[P1] FR-004: Players shall be able to [important feature]
[P1] FR-005: The game shall [important feature]
[P2] FR-006: Players shall be able to [nice-to-have feature]

### Non-Functional Requirements
[P0] NFR-001: The game shall maintain 60 FPS on mid-tier devices
[P0] NFR-002: Player data shall persist across sessions (DataStore)
[P0] NFR-003: The game shall handle [X] concurrent players per server
[P1] NFR-004: The game shall load within [X] seconds
[P1] NFR-005: The game shall gracefully handle network latency
[P2] NFR-006: The game shall support mobile, PC, and console inputs

## Game Design

### Core Loop
```
[Entry Point]
     │
     ▼
┌─────────────┐
│   Action    │ ← Player performs primary action
└─────────────┘
     │
     ▼
┌─────────────┐
│   Reward    │ ← Immediate feedback/reward
└─────────────┘
     │
     ▼
┌─────────────┐
│ Progression │ ← Long-term advancement
└─────────────┘
     │
     └──────────→ [Return to Action]
```

### Game Mechanics

#### Primary Mechanic: [Name]
- **Description**: [What the player does]
- **Controls**: [Input required]
- **Feedback**: [Visual/audio response]

#### Secondary Mechanics
| Mechanic | Description | Unlocked |
|----------|-------------|----------|
| [Mechanic 1] | [Description] | [When available] |
| [Mechanic 2] | [Description] | [When available] |

### Progression System

#### Short-term (Session)
- [What players achieve in one session]
- [Rewards gained per session]

#### Long-term (Persistent)
- [Level/rank system]
- [Unlockables]
- [Achievements]

#### Progression Table
| Level | XP Required | Unlocks |
|-------|-------------|---------|
| 1 | 0 | [Starting items] |
| 2 | [X] | [Unlock 1] |
| 3 | [X] | [Unlock 2] |
| ... | ... | ... |

### Economy Design

#### Currencies
| Currency | Earned By | Spent On | Sink |
|----------|-----------|----------|------|
| [Soft currency] | [Gameplay] | [Items, upgrades] | [How it leaves] |
| [Premium currency] | [Robux purchase] | [Cosmetics, boosts] | [How it leaves] |

#### Pricing Strategy
- **Free items**: [What's free]
- **Soft currency items**: [Price ranges]
- **Premium items**: [Price ranges in Robux]

### Multiplayer Design

#### Server Architecture
- **Max Players**: [Per server]
- **Server Type**: [Public / Private / Reserved]
- **Matchmaking**: [How players are grouped]

#### Social Features
| Feature | Description | Priority |
|---------|-------------|----------|
| [Chat] | [In-game communication] | [P0/P1/P2] |
| [Parties] | [Group play] | [P0/P1/P2] |
| [Leaderboards] | [Competition] | [P0/P1/P2] |
| [Trading] | [Item exchange] | [P0/P1/P2] |

#### Sync Requirements
| Data | Authority | Sync Rate | Priority |
|------|-----------|-----------|----------|
| [Player position] | Server | [Hz] | High |
| [Combat actions] | Server | [Hz] | High |
| [UI state] | Client | None | Low |

## Architecture

### High-Level Architecture

```
┌─────────────────────────────────────────────────────────┐
│                     Client (LocalScript)                 │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐     │
│  │     UI      │  │   Input     │  │   Effects   │     │
│  │ Controllers │  │  Handling   │  │   & Audio   │     │
│  └─────────────┘  └─────────────┘  └─────────────┘     │
└─────────────────────────────────────────────────────────┘
              │ RemoteEvent / RemoteFunction │
              ▼                              ▼
┌─────────────────────────────────────────────────────────┐
│                    Server (Script)                       │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐     │
│  │    Game     │  │   Player    │  │    Data     │     │
│  │    Logic    │  │   Manager   │  │   Service   │     │
│  └─────────────┘  └─────────────┘  └─────────────┘     │
└─────────────────────────────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────────────────────┐
│                   Roblox Services                        │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐     │
│  │  DataStore  │  │ Messaging   │  │  Analytics  │     │
│  │   Service   │  │   Service   │  │   Service   │     │
│  └─────────────┘  └─────────────┘  └─────────────┘     │
└─────────────────────────────────────────────────────────┘
```

### Project Structure (Rojo)

```
src/
├── server/                 # Server-side scripts
│   ├── init.server.luau   # Entry point
│   ├── Services/          # Server services
│   │   ├── DataService.luau
│   │   ├── GameService.luau
│   │   └── PlayerService.luau
│   └── Components/        # Server-side components
├── client/                 # Client-side scripts
│   ├── init.client.luau   # Entry point
│   ├── Controllers/       # Client controllers
│   │   ├── UIController.luau
│   │   ├── InputController.luau
│   │   └── CameraController.luau
│   └── Components/        # Client-side components
├── shared/                 # Shared code
│   ├── init.luau
│   ├── Constants.luau     # Game constants
│   ├── Types.luau         # Type definitions
│   └── Utils/             # Utility modules
└── assets/                 # Non-code assets (if managed externally)
```

### Key Technologies

| Category | Choice | Rationale |
|----------|--------|-----------|
| Framework | [Knit / None] | [Why this framework] |
| State Management | [Replica / Custom] | [Why this approach] |
| UI Framework | [Roact / Fusion / Instance] | [Why this choice] |
| Networking | [RemoteEvent / Knit / Custom] | [Why this approach] |

### Data Model

#### Player Data Schema
```lua
type PlayerData = {
    -- Profile
    userId: number,
    joinDate: number,
    lastLogin: number,

    -- Progression
    level: number,
    experience: number,

    -- Economy
    coins: number,
    gems: number,

    -- Inventory
    items: {[string]: number},
    equipped: {[string]: string},

    -- Stats
    totalPlayTime: number,
    [statName]: number,
}
```

#### DataStore Strategy
| Store | Purpose | Type | Scope |
|-------|---------|------|-------|
| PlayerData | Player progression | OrderedDataStore | Per-user |
| GlobalLeaderboard | Rankings | OrderedDataStore | Global |
| ServerData | Shared game state | DataStore | Per-place |

### Client-Server Communication

#### RemoteEvents
| Event | Direction | Payload | Purpose |
|-------|-----------|---------|---------|
| [EventName] | Client → Server | [Data type] | [What it does] |
| [EventName] | Server → Client | [Data type] | [What it does] |
| [EventName] | Server → All | [Data type] | [What it does] |

#### RemoteFunctions
| Function | Payload | Returns | Purpose |
|----------|---------|---------|---------|
| [FunctionName] | [Request type] | [Response type] | [What it does] |

### Security Model

#### Server Authority
- [What the server controls]
- [What the server validates]

#### Client Trust Level
- **Trusted**: [What client can control]
- **Untrusted**: [What must be validated]

#### Anti-Cheat Measures
| Threat | Mitigation |
|--------|------------|
| Speed hacking | Server-side position validation |
| Currency manipulation | Server-authoritative economy |
| [Threat] | [Mitigation] |

## Monetization

### Game Pass Strategy

| Game Pass | Price (R$) | Benefit | Target Player |
|-----------|------------|---------|---------------|
| [VIP] | [X] | [Benefits] | [Who buys this] |
| [Double XP] | [X] | [Benefits] | [Who buys this] |
| [Name] | [X] | [Benefits] | [Who buys this] |

### Developer Product Strategy

| Product | Price (R$) | Quantity | Purpose |
|---------|------------|----------|---------|
| [Currency Pack] | [X] | [Amount] | Soft/hard currency |
| [Consumable] | [X] | [Effect] | Temporary boost |

### Pricing Guidelines
- Entry-level purchase: [X] Robux
- Average transaction: [X] Robux
- Premium tier: [X] Robux

## Moderation & Safety

### Chat Filtering
- All text filtered through Roblox TextService
- [Additional custom filters if any]

### Content Safety
| Risk | Mitigation |
|------|------------|
| Inappropriate UGC | [How handled] |
| Harassment | [Reporting system] |
| Scamming | [Trade restrictions] |

### Reporting System
- [How players report issues]
- [What data is collected]

## Milestones & Phases

### Phase 1: Core Experience
**Goal**: Playable core loop
**Deliverables**:
- Basic gameplay mechanics
- Player spawning and controls
- Core game loop functional
- Minimal UI

### Phase 2: Progression & Economy
**Goal**: Reason to keep playing
**Deliverables**:
- Data persistence
- Progression system
- Economy (currencies, shops)
- Inventory system

### Phase 3: Social & Multiplayer
**Goal**: Play with friends
**Deliverables**:
- Multiplayer synchronization
- Social features (parties, chat)
- Leaderboards
- Matchmaking (if applicable)

### Phase 4: Monetization & Polish
**Goal**: Ready for launch
**Deliverables**:
- Game passes and developer products
- Visual polish and effects
- Sound design
- Performance optimization

### Phase 5: Launch & Live Ops
**Goal**: Public release
**Deliverables**:
- Marketing materials (thumbnail, description)
- Launch event/content
- Analytics integration
- Update pipeline established

## Epic Breakdown

### Epic: Core Gameplay
- **Requirements Covered**: FR-001, FR-002
- **Tasks**:
  - [ ] Set up Rojo project structure
  - [ ] Implement player spawning
  - [ ] Create core mechanic
  - [ ] Add basic feedback/effects

### Epic: Data & Progression
- **Requirements Covered**: NFR-002, FR-003
- **Tasks**:
  - [ ] Set up DataStore service
  - [ ] Implement player data schema
  - [ ] Create progression system
  - [ ] Add save/load functionality

### Epic: Economy
- **Requirements Covered**: FR-004
- **Tasks**:
  - [ ] Create currency system
  - [ ] Implement shop UI
  - [ ] Add item/upgrade system
  - [ ] Balance economy

### Epic: Multiplayer
- **Requirements Covered**: NFR-003, FR-005
- **Tasks**:
  - [ ] Set up RemoteEvent communication
  - [ ] Implement server-authoritative logic
  - [ ] Add player synchronization
  - [ ] Create social features

### Epic: Monetization
- **Requirements Covered**: Revenue goals
- **Tasks**:
  - [ ] Create game passes
  - [ ] Implement developer products
  - [ ] Add purchase prompts
  - [ ] Implement premium benefits

### Epic: Polish
- **Requirements Covered**: NFR-001, NFR-004
- **Tasks**:
  - [ ] Optimize performance
  - [ ] Add visual effects
  - [ ] Implement sound design
  - [ ] Create loading screen
  - [ ] Mobile/console input support

## Open Questions
- [Question 1 that needs stakeholder input]
- [Question 2]

## Out of Scope
- [Explicitly what this PRD does NOT cover]
- [Feature deferred to future version]

## Appendix

### Glossary
- **Experience**: Roblox term for a game/place
- **DevProduct**: One-time purchasable item
- **GamePass**: Permanent unlockable purchase
- **DataStore**: Roblox persistent storage service
- **RemoteEvent**: Client-server communication channel
- **Rojo**: External editor sync tool

### Reference Links
- [Roblox Creator Documentation](https://create.roblox.com/docs)
- [Roblox DevForum](https://devforum.roblox.com)
- [Similar game 1]
- [Similar game 2]

### Comparable Games
- **[Game 1]**: [What to learn from it]
- **[Game 2]**: [What to learn from it]

### Interview Notes Summary
[Brief summary of key points from the requirements gathering process]
