# PRD: [Feature Title]

## Metadata
- **Feature ID**: [FEATURE_ID]
- **Project Type**: API
- **Created**: [Date]
- **Author**: Claude (from interview)
- **Interview Confidence**: [High/Medium/Low]

## Overview

### Problem Statement
[One paragraph describing the problem this API solves. What pain point exists? Who experiences it? Why is it worth solving?]

### Proposed Solution
[One paragraph describing how this API addresses the problem. What does it enable? What approach does it take?]

### Success Metrics
- [Metric 1 - e.g., "API response time < 200ms at p95"]
- [Metric 2 - e.g., "Support 1000 requests/second"]
- [Metric 3 - e.g., "99.9% uptime SLA"]

## Background & Context
[Why this API now? What's the motivation? Are there existing APIs that fall short? What's the business driver?]

## Users & Personas

### API Consumers
[Who will call this API? Internal services? External developers? Mobile apps? Third-party integrations?]

### Consumer Goals
- [Goal 1 - What do they want to accomplish?]
- [Goal 2]

## Requirements

### Functional Requirements
[P0] FR-001: The API shall [core functionality requirement]
[P0] FR-002: The API shall [core functionality requirement]
[P1] FR-003: The API shall [important feature]
[P1] FR-004: The API shall [important feature]
[P2] FR-005: The API shall [nice-to-have feature]

### Non-Functional Requirements
[P0] NFR-001: The API shall respond within [X]ms at p95 under normal load
[P0] NFR-002: The API shall authenticate all requests using [method]
[P1] NFR-003: The API shall handle [X] requests per second
[P1] NFR-004: The API shall log all requests for audit purposes
[P2] NFR-005: The API shall support [caching strategy]

## API Design

### Base URL
```
[Production]: https://api.example.com/v1
[Staging]: https://api-staging.example.com/v1
```

### Endpoints

| Method | Path | Description | Auth Required |
|--------|------|-------------|---------------|
| GET | /resource | List all resources | Yes |
| GET | /resource/:id | Get a specific resource | Yes |
| POST | /resource | Create a new resource | Yes |
| PUT | /resource/:id | Update a resource | Yes |
| DELETE | /resource/:id | Delete a resource | Yes |

### Authentication & Authorization

**Authentication Method**: [API Key / OAuth 2.0 / JWT / etc.]

**How to Authenticate**:
```
Authorization: Bearer <token>
```

**Permission Model**:
- [Role/scope 1]: [What actions are allowed]
- [Role/scope 2]: [What actions are allowed]

### Rate Limiting

| Tier | Limit | Window | Notes |
|------|-------|--------|-------|
| Free | [X] requests | per minute | [Notes] |
| Pro | [Y] requests | per minute | [Notes] |
| Enterprise | [Z] requests | per minute | [Notes] |

**Rate Limit Headers**:
```
X-RateLimit-Limit: 100
X-RateLimit-Remaining: 95
X-RateLimit-Reset: 1609459200
```

### Request/Response Schemas

#### Resource Schema
```json
{
  "id": "string",
  "created_at": "ISO 8601 timestamp",
  "updated_at": "ISO 8601 timestamp",
  "field1": "type",
  "field2": "type"
}
```

#### Pagination
```json
{
  "data": [...],
  "pagination": {
    "total": 100,
    "page": 1,
    "per_page": 20,
    "total_pages": 5
  }
}
```

### Error Handling

**Standard Error Format**:
```json
{
  "error": {
    "code": "ERROR_CODE",
    "message": "Human-readable message",
    "details": {}
  }
}
```

**Error Codes**:

| HTTP Status | Code | Description |
|-------------|------|-------------|
| 400 | INVALID_REQUEST | Request body validation failed |
| 401 | UNAUTHORIZED | Missing or invalid authentication |
| 403 | FORBIDDEN | Authenticated but not authorized |
| 404 | NOT_FOUND | Resource does not exist |
| 429 | RATE_LIMITED | Too many requests |
| 500 | INTERNAL_ERROR | Unexpected server error |

### Versioning Strategy

**Approach**: [URL versioning / Header versioning / Query param]

**Current Version**: v1

**Deprecation Policy**:
- Major versions supported for [X] months after successor release
- Deprecation notices sent [X] weeks in advance
- Breaking changes only in major versions

## Data Models

### Entity Relationship
```
[Describe key entities and their relationships]
```

### Core Entities

#### [Entity 1]
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| id | string | Yes | Unique identifier |
| ... | ... | ... | ... |

## System Architecture

### Components
- **API Gateway**: [Description]
- **Application Layer**: [Description]
- **Data Layer**: [Description]
- **Cache Layer**: [Description]

### Infrastructure Requirements
- [Infrastructure requirement 1]
- [Infrastructure requirement 2]

## Milestones & Phases

### Phase 1: Foundation
**Goal**: Basic API structure and core endpoints
**Deliverables**:
- Authentication setup
- Core CRUD endpoints
- Error handling
- Basic documentation

### Phase 2: Core Features
**Goal**: Full feature implementation
**Deliverables**:
- All endpoints implemented
- Rate limiting
- Pagination
- Validation

### Phase 3: Production Readiness
**Goal**: Ready for production traffic
**Deliverables**:
- Performance optimization
- Monitoring and alerting
- API documentation
- SDK/client libraries (if applicable)

## Epic Breakdown

### Epic: Authentication & Authorization
- **Requirements Covered**: NFR-002
- **Tasks**:
  - [ ] Implement authentication middleware
  - [ ] Set up token validation
  - [ ] Create permission system

### Epic: Core Endpoints
- **Requirements Covered**: FR-001, FR-002, FR-003
- **Tasks**:
  - [ ] Implement GET /resource
  - [ ] Implement POST /resource
  - [ ] Implement PUT /resource/:id
  - [ ] Implement DELETE /resource/:id

### Epic: Rate Limiting & Quotas
- **Requirements Covered**: NFR-003
- **Tasks**:
  - [ ] Implement rate limiting middleware
  - [ ] Set up rate limit storage (Redis)
  - [ ] Add rate limit headers

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
