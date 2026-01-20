# PRD: [Feature Title]

## Metadata
- **Feature ID**: [FEATURE_ID]
- **Project Type**: Full-Stack
- **Created**: [Date]
- **Author**: Claude (from interview)
- **Interview Confidence**: [High/Medium/Low]

## Overview

### Problem Statement
[One paragraph describing the problem this application solves. What user need exists? Who has this problem? Why is a full-stack application the right solution?]

### Proposed Solution
[One paragraph describing how this application addresses the problem. What experience does it provide? What approach does it take?]

### Success Metrics
- [Metric 1 - e.g., "Users complete onboarding in < 2 minutes"]
- [Metric 2 - e.g., "Page load time < 1.5s on 3G"]
- [Metric 3 - e.g., "99.5% uptime for core workflows"]

## Background & Context
[Why this application now? What's the motivation? Are there existing solutions that fall short? What's the business or user need?]

## Users & Personas

### Primary Persona: [Name]
- **Role**: [Job title / role]
- **Goals**: [What they want to accomplish]
- **Pain Points**: [Current frustrations]
- **Technical Level**: [Beginner / Intermediate / Expert]

### Secondary Persona: [Name]
- **Role**: [Job title / role]
- **Goals**: [What they want to accomplish]

### User Journeys

#### Journey 1: [Primary Task]
1. User navigates to [page]
2. User [action]
3. System [response]
4. User [completes goal]

## Requirements

### Functional Requirements
[P0] FR-001: Users shall be able to [core functionality]
[P0] FR-002: Users shall be able to [core functionality]
[P0] FR-003: The system shall [core functionality]
[P1] FR-004: Users shall be able to [important feature]
[P1] FR-005: The system shall [important feature]
[P2] FR-006: Users shall be able to [nice-to-have feature]

### Non-Functional Requirements
[P0] NFR-001: Pages shall load within [X] seconds on 3G networks
[P0] NFR-002: The system shall protect user data with encryption at rest and in transit
[P1] NFR-003: The application shall be responsive on mobile devices
[P1] NFR-004: The system shall handle [X] concurrent users
[P2] NFR-005: The application shall achieve [X] score on accessibility audit

## Architecture

### High-Level Architecture

```
┌─────────────────────────────────────────────────────────┐
│                      Client Layer                        │
│  ┌─────────────────────────────────────────────────────┐│
│  │              Frontend Application                    ││
│  │         ([Framework]: React/Vue/etc.)               ││
│  └─────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────┘
                           │
                           │ HTTPS / WebSocket
                           ▼
┌─────────────────────────────────────────────────────────┐
│                     Server Layer                         │
│  ┌─────────────────────────────────────────────────────┐│
│  │              Backend Application                     ││
│  │         ([Framework]: Express/Django/etc.)          ││
│  └─────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────┐
│                      Data Layer                          │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  │
│  │   Database   │  │    Cache     │  │    Files     │  │
│  │  [Type]      │  │  [Redis]     │  │  [S3/etc.]   │  │
│  └──────────────┘  └──────────────┘  └──────────────┘  │
└─────────────────────────────────────────────────────────┘
```

### Frontend Architecture

**Framework**: [React / Vue / Svelte / etc.]

**Structure**:
```
src/
├── components/         # Reusable UI components
│   ├── common/        # Buttons, inputs, cards
│   └── [feature]/     # Feature-specific components
├── pages/             # Page components / routes
├── hooks/             # Custom hooks
├── services/          # API client, utilities
├── store/             # State management
└── styles/            # Global styles, themes
```

**Key Technologies**:
| Category | Choice | Rationale |
|----------|--------|-----------|
| Framework | [choice] | [reason] |
| State Management | [choice] | [reason] |
| Styling | [choice] | [reason] |
| Routing | [choice] | [reason] |

### Backend Architecture

**Framework**: [Express / Django / FastAPI / etc.]

**Structure**:
```
src/
├── routes/            # API route handlers
├── controllers/       # Business logic
├── models/            # Data models
├── middleware/        # Auth, logging, etc.
├── services/          # External service integrations
└── utils/             # Helper functions
```

**Key Technologies**:
| Category | Choice | Rationale |
|----------|--------|-----------|
| Framework | [choice] | [reason] |
| ORM | [choice] | [reason] |
| Validation | [choice] | [reason] |

### API Contract

**Base URL**: `/api/v1`

**Authentication**: [JWT / Session / OAuth]

**Endpoints**:

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| POST | /auth/login | User login | No |
| POST | /auth/logout | User logout | Yes |
| GET | /users/me | Get current user | Yes |
| GET | /[resource] | List resources | Yes |
| POST | /[resource] | Create resource | Yes |
| GET | /[resource]/:id | Get resource | Yes |
| PUT | /[resource]/:id | Update resource | Yes |
| DELETE | /[resource]/:id | Delete resource | Yes |

**Request/Response Format**:
```json
// Success Response
{
  "data": { ... },
  "meta": { ... }
}

// Error Response
{
  "error": {
    "code": "ERROR_CODE",
    "message": "Human-readable message"
  }
}
```

### State Management

**Approach**: [Redux / Vuex / Zustand / Context / etc.]

**Store Structure**:
```javascript
{
  auth: {
    user: User | null,
    isAuthenticated: boolean,
    loading: boolean
  },
  [feature]: {
    items: Item[],
    selected: Item | null,
    loading: boolean,
    error: Error | null
  }
}
```

**Data Flow**:
1. User action triggers dispatch
2. Action calls API service
3. Response updates store
4. Components re-render with new state

### Authentication Flow

**Method**: [JWT / Session / OAuth 2.0]

**Flow**:
```
1. User enters credentials
2. Frontend sends POST /auth/login
3. Backend validates credentials
4. Backend returns [JWT token / session cookie]
5. Frontend stores token in [localStorage / httpOnly cookie]
6. Subsequent requests include token in [Authorization header / cookie]
7. Backend validates token on protected routes
```

**Session Management**:
- Token expiry: [X] hours
- Refresh strategy: [Refresh token / Silent refresh / Re-login]
- Logout: [What happens on logout]

### Database Design

**Database Type**: [PostgreSQL / MongoDB / etc.]

**Core Entities**:

#### users
| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| id | UUID | PK | Unique identifier |
| email | VARCHAR(255) | UNIQUE, NOT NULL | User email |
| password_hash | VARCHAR(255) | NOT NULL | Hashed password |
| created_at | TIMESTAMP | NOT NULL | Creation time |
| updated_at | TIMESTAMP | NOT NULL | Last update time |

#### [entity2]
| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| id | UUID | PK | Unique identifier |
| user_id | UUID | FK -> users | Owner reference |
| ... | ... | ... | ... |

**Relationships**:
```
users 1───< [entity2] (one-to-many)
[entity2] >───< [entity3] (many-to-many via join table)
```

## Data Flow

### Create Flow
```
1. User fills form →
2. Frontend validates →
3. POST /api/resource →
4. Backend validates →
5. Save to database →
6. Return created resource →
7. Update frontend state →
8. Show success message
```

### Read Flow
```
1. Page loads →
2. GET /api/resource →
3. Backend queries database →
4. Return data →
5. Update frontend state →
6. Render components
```

## Deployment

### Environment Strategy

| Environment | Purpose | URL |
|-------------|---------|-----|
| Development | Local development | localhost:3000 |
| Staging | Testing before production | staging.example.com |
| Production | Live users | example.com |

### Deployment Architecture

**Option A: Monorepo Single Deployment**
```
[Vercel / Netlify / Railway]
├── Frontend (static)
└── Backend (serverless/container)
```

**Option B: Separate Deployments**
```
Frontend: [Vercel / CloudFlare Pages]
Backend: [Railway / Render / AWS ECS]
Database: [Managed service]
```

### Infrastructure Requirements

| Component | Service | Sizing |
|-----------|---------|--------|
| Frontend | [Service] | [Plan/tier] |
| Backend | [Service] | [Plan/tier] |
| Database | [Service] | [Plan/tier] |
| Cache | [Service] | [Plan/tier] |
| File Storage | [Service] | [Plan/tier] |

### Environment Variables

**Frontend**:
| Variable | Description | Example |
|----------|-------------|---------|
| NEXT_PUBLIC_API_URL | Backend API URL | https://api.example.com |

**Backend**:
| Variable | Description | Example |
|----------|-------------|---------|
| DATABASE_URL | Database connection string | postgresql://... |
| JWT_SECRET | Token signing secret | [secure random] |
| [SERVICE]_API_KEY | External service key | sk_... |

## Milestones & Phases

### Phase 1: Foundation
**Goal**: Basic application structure and authentication
**Deliverables**:
- Project setup (frontend + backend)
- Database schema and migrations
- User authentication (register, login, logout)
- Basic layout and navigation

### Phase 2: Core Features
**Goal**: Primary user workflows
**Deliverables**:
- [Core feature 1] implementation
- [Core feature 2] implementation
- API integration complete
- Basic error handling

### Phase 3: Polish & Launch
**Goal**: Production-ready application
**Deliverables**:
- Responsive design
- Loading states and error handling
- Performance optimization
- Deployment pipeline

## Epic Breakdown

### Epic: Project Setup
- **Requirements Covered**: Foundation
- **Tasks**:
  - [ ] Initialize frontend project
  - [ ] Initialize backend project
  - [ ] Set up database
  - [ ] Configure environment variables
  - [ ] Set up CI/CD

### Epic: Authentication
- **Requirements Covered**: FR-001, NFR-002
- **Tasks**:
  - [ ] Create user model and migration
  - [ ] Implement registration endpoint
  - [ ] Implement login endpoint
  - [ ] Add JWT/session middleware
  - [ ] Create login/register pages
  - [ ] Implement protected routes

### Epic: [Core Feature]
- **Requirements Covered**: FR-002, FR-003
- **Tasks**:
  - [ ] Create [entity] model
  - [ ] Implement CRUD endpoints
  - [ ] Create list view component
  - [ ] Create detail view component
  - [ ] Create form component
  - [ ] Add frontend state management

### Epic: Production Readiness
- **Requirements Covered**: NFR-001, NFR-003
- **Tasks**:
  - [ ] Optimize bundle size
  - [ ] Add loading states
  - [ ] Implement error boundaries
  - [ ] Add responsive styles
  - [ ] Set up monitoring

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

### Design Mockups
- [Link to Figma / design files if available]
