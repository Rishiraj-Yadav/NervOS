# Graph Report - Nervos-self-hosting  (2026-09-09)

## Corpus Check
- 24 files · ~23,303 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 888 nodes · 1269 edges · 100 communities (47 shown, 42 thin omitted)
- Extraction: 92% EXTRACTED · 8% INFERRED · 0% AMBIGUOUS · INFERRED: 103 edges (avg confidence: 0.92)
- Token cost: 125 input · 0 output

## Community Hubs (Navigation)
- Frontend Dependencies
- Alembic Database Setup
- API Test Fixtures
- Session Cookie Routing
- Authentication Persistence
- Frontend TypeScript Config
- Web Package Tooling
- Migration Integration Tests
- Foundation Security Concepts
- Settings Validation Tests
- Repository Tooling Tests
- Node TypeScript Config
- API Composition
- Authentication Errors
- Security Token Adapters
- Authentication Routes
- Database Models
- Authentication Protocols
- API Dependencies
- Safe Error Responses
- Authentication Service Tests
- Workspace Commands
- UTC Datetime Tests
- UTC Database Types
- Authentication Persistence Port
- Bootstrap Tooling
- Product Roadmap
- Authentication Session Issuance
- Setup Concurrency
- Architecture Boundary Tests
- Quality Check Runner
- Development API Launcher
- Authentication Request Middleware
- Database Path Validation
- Security Boundary Tests
- Authentication API Tests
- Core Architecture Concepts
- Password Input Policy
- Setup API Route
- Setup API Tests
- Architecture Review Workflow
- Scoped Memory Model
- Runtime Execution Pipeline
- Current User Dependency
- Domain Entity Distinctions
- Initial Schema Migration
- Security Review Workflow
- Claude Code Hooks
- Agent Domain Model
- Production Origin Validation
- Permission Approval Flow
- Browser Authentication Session
- PNPM Workspace
- TypeScript Project References
- Modular Monolith Foundation
- Post Edit Hook
- Tool Guard Hook
- Sensitive Read Hook
- Secret Scan Hook
- Tool Permission Gateway
- Marketplace Package Flow
- Capability Grant Model
- Testing Review
- Session Context Hook
- Alembic Migration Workflow
- Run Audit Trail
- Single Node Product
- Core Application Package
- Core Domain Package
- API Core Dependency
- API Routes Package
- React Entry Point
- Generated Files Check
- Secrets Check
- TanStack Server State
- Agent Addition Workflow
- Security Review Skill
- Tool Concept
- Roadmap Document
- Core Root Package
- Workspace Package
- Community 92
- Community 93
- Community 94
- Community 95
- Community 96
- Community 97
- Community 98
- Community 99

## God Nodes (most connected - your core abstractions)
1. `Settings` - 29 edges
2. `SqlAlchemyAuthenticationPersistence` - 22 edges
3. `compilerOptions` - 19 edges
4. `create_app()` - 18 edges
5. `AuthenticationService` - 18 edges
6. `create_sqlite_engine()` - 15 edges
7. `migrated_app()` - 15 edges
8. `UTCDateTime` - 14 edges
9. `compilerOptions` - 13 edges
10. `PublicUser` - 13 edges

## Surprising Connections (you probably didn't know these)
- `test_development_api_migrates_before_starting_server()` --uses--> `Settings`  [INFERRED]
  tests/test_tooling.py → apps/api/src/nervos_api/config.py
- `test_development_api_stops_when_migration_fails()` --uses--> `Settings`  [INFERRED]
  tests/test_tooling.py → apps/api/src/nervos_api/config.py
- `get_authentication_service()` --uses--> `AuthenticationService`  [INFERRED]
  apps/api/src/nervos_api/api/dependencies.py → packages/nervos-core/src/nervos_core/application/authentication.py
- `migrated_app()` --uses--> `AuthenticationService`  [INFERRED]
  apps/api/tests/conftest.py → packages/nervos-core/src/nervos_core/application/authentication.py
- `get_current_user()` --uses--> `AuthenticationRequired`  [INFERRED]
  apps/api/src/nervos_api/api/dependencies.py → packages/nervos-core/src/nervos_core/application/authentication.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Explicit Memory Scope Model** — docs_memory_session_memory, docs_memory_agent_private_memory, docs_memory_user_profile_memory, docs_memory_shared_workspace_memory [EXTRACTED 1.00]
- **NervOS Execution Domain Distinctions** — docs_overview_agentpackage, docs_overview_agentinstance, claude_rules_architecture_run, claude_rules_architecture_session [EXTRACTED 1.00]
- **Permission-mediated Tool Flow** — docs_permissions_permission_engine, docs_permissions_tool_mcp_gateway, docs_permissions_waiting_approval [EXTRACTED 1.00]
- **Runtime Execution Pipeline** — docs_runtime_trigger, docs_runtime_job, docs_runtime_persistent_queue, docs_runtime_worker, docs_runtime_run_coordinator, docs_runtime_runcontext, docs_runtime_agent_implementation [EXTRACTED 1.00]
- **Target Execution Architecture** — docs_adr_0004_job_driven_runtime_job_runtime, docs_architecture_execution_plane, docs_permissions_permission_engine [EXTRACTED 1.00]

## Communities (100 total, 42 thin omitted)

### Community 0 - "Frontend Dependencies"
Cohesion: 0.05
Nodes (55): clear_session_cookie(), cookie_is_secure(), datetime, Response, Settings, HTTP cookie policy for opaque authentication sessions., Derive Secure only from trusted validated configuration., Set the host-only opaque-session cookie. (+47 more)

### Community 1 - "Alembic Database Setup"
Cohesion: 0.07
Nodes (50): Alembic environment using the application's database configuration., Render migrations without opening a database connection., Run migrations on an application-configured connection., Run migrations with the same engine configuration as the application., run_migrations(), run_migrations_offline(), run_migrations_online(), alembic_config() (+42 more)

### Community 2 - "API Test Fixtures"
Cohesion: 0.07
Nodes (40): authentication_error_handler(), error_response(), InvalidOrigin, Exception, Safe public error handling for authentication endpoints., Raised when an unsafe request lacks the exact configured Origin., Build a stable non-cacheable public error response., Return a generic production-safe response for unexpected failures. (+32 more)

### Community 3 - "Session Cookie Routing"
Cohesion: 0.07
Nodes (39): get_settings(), Path, Typed process configuration for the NervOS API., Validated API configuration loaded only from process environment., Reject an empty environment value before Path coercion., Expand and resolve a database path without creating it., Require one exact HTTP(S) origin without URL suffixes., Reject insecure external origins in production. (+31 more)

### Community 4 - "Authentication Persistence"
Cohesion: 0.07
Nodes (33): Argon2PasswordHasher, fixture, MonkeyPatch, AuthenticationService, Clock, PasswordHasher, Return the current timezone-aware UTC instant., Coordinate setup, credentials, and opaque server-side sessions. (+25 more)

### Community 5 - "Frontend TypeScript Config"
Cohesion: 0.04
Nodes (45): devDependencies, eslint, @eslint/js, eslint-plugin-react-hooks, eslint-plugin-react-refresh, globals, jsdom, msw (+37 more)

### Community 6 - "Web Package Tooling"
Cohesion: 0.06
Nodes (28): clear_settings_cache(), client(), migrated_app(), FastAPI, fixture, MonkeyPatch, Path, TestClient (+20 more)

### Community 7 - "Migration Integration Tests"
Cohesion: 0.07
Nodes (27): Documentation Rules, Approved Milestone Scope, Documentation Workflow, Migration-First Database Workflow, Testing Pyramid, Thin Route Handlers, A3 Acceptance Criteria, A3 Current Work (+19 more)

### Community 8 - "Foundation Security Concepts"
Cohesion: 0.08
Nodes (24): compilerOptions, allowJs, allowSyntheticDefaultImports, esModuleInterop, forceConsistentCasingInFileNames, isolatedModules, jsx, lib (+16 more)

### Community 9 - "Settings Validation Tests"
Cohesion: 0.12
Nodes (22): AuthenticationError, AuthenticationRequired, InvalidCredentials, InvalidPassword, InvalidUsername, PasswordWorkLimit, PersistenceUnavailable, Exception (+14 more)

### Community 10 - "Repository Tooling Tests"
Cohesion: 0.13
Nodes (17): Dialect, datetime, SQLAlchemy types shared by NervOS persistence models., Persist UTC datetimes in SQLite and return timezone-aware values., Normalize an aware value to the naïve UTC SQLite representation., Restore the UTC timezone discarded by SQLite storage., UTCDateTime, IneffectiveTimezone (+9 more)

### Community 11 - "Node TypeScript Config"
Cohesion: 0.09
Nodes (23): Architecture Reviewer, Job-Driven Execution, Model Provider Interface, Run, Session, Model Provider Adapter, .nervos Agent Package, Runtime Feature Workflow (+15 more)

### Community 12 - "API Composition"
Cohesion: 0.09
Nodes (21): dependencies, react, react-dom, react-router-dom, @tanstack/react-query, engines, node, name (+13 more)

### Community 13 - "Authentication Errors"
Cohesion: 0.15
Nodes (13): IntegrityError, NoReturn, datetime, Create a fresh session and optionally replace an obsolete hash., Resolve an unexpired, unrevoked session tied to an active user., Idempotently revoke the current matching session., Persist users and authentication sessions with explicit transactions., Return a non-authoritative fast setup-complete hint. (+5 more)

### Community 14 - "Security Token Adapters"
Cohesion: 0.16
Nodes (17): canonicalize_username(), IssuedSession, Create the first administrator and its initial session atomically., Authenticate credentials and create a fresh absolute-expiry session., Normalize a username into the only accepted persisted form., Enforce bounded password input without normalizing or truncating it., A committed session and its one-time raw cookie token., validate_password() (+9 more)

### Community 15 - "Authentication Routes"
Cohesion: 0.16
Nodes (18): CompletedProcess, ModuleType, load_development_module(), MonkeyPatch, Path, Tests for the Stage A1 repository command facade., Load the repository development script without requiring it as a package., Run a repository script with the active test interpreter. (+10 more)

### Community 16 - "Database Models"
Cohesion: 0.15
Nodes (17): get_authentication_service(), get_current_user(), get_settings(), alias, Cookie, datetime, Request, SESSION_COOKIE_NAME (+9 more)

### Community 17 - "Authentication Protocols"
Cohesion: 0.11
Nodes (17): compilerOptions, allowImportingTsExtensions, lib, module, moduleDetection, moduleResolution, noEmit, noUncheckedSideEffectImports (+9 more)

### Community 18 - "API Dependencies"
Cohesion: 0.26
Nodes (14): FastAPI, Path, Settings, TestClient, Adversarial HTTP-boundary tests for A3 authentication., test_api_responses_include_security_headers(), test_auth_mutations_require_exact_origin(), test_credentials_require_json_content_type() (+6 more)

### Community 19 - "Safe Error Responses"
Cohesion: 0.20
Nodes (11): DeclarativeBase, Base, Declarative metadata for NervOS persistence models., Base class for SQLAlchemy persistence records., AuthSessionRecord, SQLAlchemy persistence records for the Stage A schema., Persistence record for an opaque dashboard authentication session., Metadata tests for the Stage A persistence records. (+3 more)

### Community 20 - "Authentication Service Tests"
Cohesion: 0.20
Nodes (14): Atomic First-Run Setup, First User Is Admin, General Users Table, Local Authentication Security Boundary, Exact Password Input Policy, Setup Status Contract, Canonical Username Policy, Authentication Error Contract (+6 more)

### Community 21 - "Workspace Commands"
Cohesion: 0.14
Nodes (13): engines, node, pnpm, name, packageManager, private, scripts, build (+5 more)

### Community 22 - "UTC Datetime Tests"
Cohesion: 0.23
Nodes (12): Hashed Session Tokens, Absolute Session Expiry, Opaque Cookie Sessions, POST /api/v1/auth/logout, GET /api/v1/auth/me, Session Lifetime Contract, Session Token Contract, No Browser Authentication Storage (+4 more)

### Community 23 - "UTC Database Types"
Cohesion: 0.24
Nodes (7): AuthenticationPersistence, PublicUser, datetime, Focused persistence operations required by authentication., Return the fixed seven-day absolute session expiration., Safe authenticated-user data available above the application boundary., _session_expiration()

### Community 24 - "Authentication Persistence Port"
Cohesion: 0.23
Nodes (11): main(), Install the project-local Python and JavaScript dependencies., Return a command path or exit with an actionable message., Read a dotted numeric version from a command., Run one bootstrap command from the repository root., Validate local runtimes without installing system packages., Install locked dependencies into project-local environments., read_version() (+3 more)

### Community 25 - "Bootstrap Tooling"
Cohesion: 0.33
Nodes (10): imported_modules(), Path, python_files(), Architecture regression tests for the Stage A package boundaries., Collect statically declared imports from a Python source file., Return repository Python files in deterministic order., test_application_has_no_fastapi_or_api_imports(), test_base_metadata_create_all_is_not_used() (+2 more)

### Community 26 - "Product Roadmap"
Cohesion: 0.24
Nodes (10): main(), parse_args(), Namespace, Run the repository checks available through Stage A2., Resolve the executable without invoking a platform shell., Run every command in a named check group., Parse the requested check group., Run the selected repository checks. (+2 more)

### Community 27 - "Authentication Session Issuance"
Cohesion: 0.47
Nodes (8): FastAPI, Settings, TestClient, Integration tests for login, current-user, and logout endpoints., setup_user(), test_inactive_user_cannot_login_or_use_existing_session(), test_login_uses_safe_generic_errors_and_stores_only_digest(), test_me_requires_valid_session_and_logout_revokes_current_session()

### Community 28 - "Setup Concurrency"
Cohesion: 0.25
Nodes (9): Server-Side Opaque Sessions, Argon2id Password Hashing, Password Work Semaphore, Argon2 Concurrency Bound, Dummy Hash Verification, Password Hash Upgrade, RFC 9106 Low-Memory Profile, A3 Reviewer Findings (+1 more)

### Community 29 - "Architecture Boundary Tests"
Cohesion: 0.36
Nodes (7): FastAPI, Settings, TestClient, Integration tests for first-run setup., test_setup_creates_admin_sets_cookie_and_locks_setup(), test_setup_rejects_invalid_or_missing_origin_without_mutation(), test_validation_response_never_echoes_password()

### Community 30 - "Quality Check Runner"
Cohesion: 0.38
Nodes (7): Exact Origin Boundary, JSON Credential Boundary, POST /api/v1/auth/login, No CORS Policy, POST /api/v1/setup, Unsafe API Origin Enforcement, A3 Authentication Development Rules

### Community 31 - "Development API Launcher"
Cohesion: 0.29
Nodes (7): Agent Implementation, Job, Persistent Queue, Run Coordinator, RunContext, Trigger, Worker

### Community 32 - "Authentication Request Middleware"
Cohesion: 0.33
Nodes (6): Authentication Session Separation, Agent Conversation Session, Explicit Agent Handoff Data, Future Conversation Context Strategy, Session Is Not Run, Two Distinct Session Types

### Community 33 - "Database Path Validation"
Cohesion: 0.33
Nodes (6): Response Security Policy, Authentication Cookie Policy, Non-Cacheable Authentication Responses, Cookie Security Derivation, NERVOS_APP_ORIGIN, Production HTTPS Requirement

### Community 34 - "Security Boundary Tests"
Cohesion: 0.40
Nodes (6): Agent-private Memory, Selective Context Construction, Memory Private by Default, Session Memory, Shared Workspace Memory, User Profile Memory

### Community 35 - "Authentication API Tests"
Cohesion: 0.40
Nodes (4): downgrade(), Create exactly the two Stage A application tables., Drop the destructive initial schema in dependency-safe order., upgrade()

### Community 36 - "Core Architecture Concepts"
Cohesion: 0.40
Nodes (5): Security Reviewer, Thin Route Handlers, Server-Side Ownership Checks, API Endpoint Workflow, Release Readiness

### Community 37 - "Password Input Policy"
Cohesion: 0.40
Nodes (5): Claude Code Hooks, Post-Edit Validation Hook, Pre-Tool Guard Hook, Pre-Write Secret Scan Hook, Session Context Hook

### Community 38 - "Setup API Route"
Cohesion: 0.40
Nodes (5): Trusted-Interface Bootstrap Requirement, First-Run Setup API, Setup Concurrency Contract, Authentication Database Test Isolation, A3 Operational Blockers

### Community 39 - "Setup API Tests"
Cohesion: 0.40
Nodes (4): MonkeyPatch, Path, File-backed SQLite concurrency test for one-time setup., test_two_concurrent_setup_attempts_create_exactly_one_user()

### Community 40 - "Architecture Review Workflow"
Cohesion: 0.50
Nodes (4): SQLite First, auth_sessions Table, Database Foundation, users Table

### Community 41 - "Scoped Memory Model"
Cohesion: 0.50
Nodes (4): Immutable API Settings, No Automatic Environment File Loading, Process Environment Configuration, Untracked Sensitive Local State

### Community 42 - "Runtime Execution Pipeline"
Cohesion: 0.50
Nodes (4): apps/web Workspace Package, esbuild Built Dependency, msw Ignored Built Dependency, pnpm Workspace

### Community 44 - "Domain Entity Distinctions"
Cohesion: 0.67
Nodes (3): Backend Reviewer, Frontend Reviewer, Review Workflow

### Community 45 - "Initial Schema Migration"
Cohesion: 0.67
Nodes (3): Modular Monolith, NervOS Platform, Stage A

### Community 50 - "Permission Approval Flow"
Cohesion: 0.67
Nodes (3): Tool Permission Layer, Permission Engine Tool Flow, MCP Tool Capability

### Community 51 - "Browser Authentication Session"
Cohesion: 0.67
Nodes (3): AgentInstance Capability Grant, Least Capability Principle, Tool Connection

## Knowledge Gaps
- **150 isolated node(s):** `eslint`, `@eslint/js`, `eslint-plugin-react-hooks`, `eslint-plugin-react-refresh`, `globals` (+145 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 416 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **42 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `Settings` connect `Session Cookie Routing` to `Alembic Database Setup`, `API Test Fixtures`, `Web Package Tooling`, `Authentication Routes`, `Database Models`?**
  _High betweenness centrality (0.067) - this node is a cross-community bridge._
- **Why does `migrated_app()` connect `Web Package Tooling` to `Alembic Database Setup`, `API Test Fixtures`, `Session Cookie Routing`, `Authentication Persistence`, `Authentication Errors`, `Database Models`?**
  _High betweenness centrality (0.056) - this node is a cross-community bridge._
- **Why does `SqlAlchemyAuthenticationPersistence` connect `Authentication Errors` to `Web Package Tooling`, `Setup API Tests`, `Settings Validation Tests`, `Safe Error Responses`, `PNPM Workspace`, `TypeScript Project References`, `UTC Database Types`?**
  _High betweenness centrality (0.029) - this node is a cross-community bridge._
- **Are the 18 inferred relationships involving `Settings` (e.g. with `run_migrations_offline()` and `run_migrations_online()`) actually correct?**
  _`Settings` has 18 INFERRED edges - model-reasoned connections that need verification._
- **Are the 8 inferred relationships involving `SqlAlchemyAuthenticationPersistence` (e.g. with `InvalidCredentials` and `PersistenceUnavailable`) actually correct?**
  _`SqlAlchemyAuthenticationPersistence` has 8 INFERRED edges - model-reasoned connections that need verification._
- **Are the 9 inferred relationships involving `create_app()` (e.g. with `authentication_error_handler()` and `InvalidOrigin`) actually correct?**
  _`create_app()` has 9 INFERRED edges - model-reasoned connections that need verification._
- **What connects `eslint`, `@eslint/js`, `eslint-plugin-react-hooks` to the rest of the system?**
  _150 weakly-connected nodes found - possible documentation gaps or missing edges._