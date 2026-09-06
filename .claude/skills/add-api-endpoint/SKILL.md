---
name: add-api-endpoint
description: Add or modify a NervOS FastAPI endpoint while preserving route/service boundaries, Pydantic validation, authentication/authorization, error mapping, tests, and API consistency.
---

# Add a NervOS API Endpoint

Before coding:

1. read backend/security rules
2. inspect neighboring routers, schemas, dependencies, services, and tests
3. determine whether behavior belongs in core/application service code

## Route responsibilities

A route should normally:

- validate request data
- resolve dependencies/authenticated user
- call application service/domain behavior
- map expected errors to HTTP
- return a response schema

Do not place reusable business logic directly in the route.

## Security

For protected endpoints:

- derive identity server-side
- check ownership/permission server-side
- never trust request `user_id` as identity
- do not expose sensitive fields
- validate all external input

## Completion checklist

- request/response Pydantic schemas
- route registered correctly
- service/core behavior added if required
- expected error mapping
- success test
- validation test
- auth/authorization tests where applicable
- docs updated if public behavior changed
- focused tests, then normal backend quality checks
