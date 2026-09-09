# Graph Report - Nervos-self-hosting  (2026-09-09)

## Corpus Check
- Corpus is ~21,757 words - fits in a single context window. You may not need a graph.

## Summary
- 785 nodes · 1158 edges · 92 communities (58 shown, 23 thin omitted)
- Extraction: 89% EXTRACTED · 11% INFERRED · 0% AMBIGUOUS · INFERRED: 125 edges (avg confidence: 0.93)
- Token cost: 11,799 input · 0 output

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
- Opaque Session Security
- Post Edit Hook
- Tool Guard Hook
- Sensitive Read Hook
- Secret Scan Hook
- Tool Permission Gateway
- Marketplace Package Flow
- Capability Grant Model
- Testing Review
- Alembic Migration Workflow
- Agent Runtime Isolation
- Memory Architecture
- Run Audit Trail
- Single Node Product
- Core Application Package
- Core Domain Package
- Core Infrastructure Package
- API Core Dependency
- Core Package
- TanStack Server State
- Agent Addition Workflow
- Security Review Skill
- Tool Concept
- Roadmap Document
- Workspace Package
- TypeScript Language Service

## God Nodes (most connected - your core abstractions)
1. `Settings` - 42 edges
2. `SqlAlchemyAuthenticationPersistence` - 24 edges
3. `create_app()` - 21 edges
4. `compilerOptions` - 19 edges
5. `create_sqlite_engine()` - 17 edges
6. `Argon2PasswordHasher` - 16 edges
7. `migrated_app()` - 15 edges
8. `AuthenticationService` - 15 edges
9. `UserRecord` - 15 edges
10. `AuthSessionRecord` - 15 edges

## Surprising Connections (you probably didn't know these)
- `get_authentication_service()` --uses--> `AuthenticationService`  [INFERRED]
  apps/api/src/nervos_api/api/dependencies.py → packages/nervos-core/src/nervos_core/application/authentication.py
- `get_current_user()` --uses--> `AuthenticationRequired`  [INFERRED]
  apps/api/src/nervos_api/api/dependencies.py → packages/nervos-core/src/nervos_core/application/authentication.py
- `get_current_user()` --uses--> `AuthenticationService`  [INFERRED]
  apps/api/src/nervos_api/api/dependencies.py → packages/nervos-core/src/nervos_core/application/authentication.py
- `get_current_user()` --uses--> `PublicUser`  [INFERRED]
  apps/api/src/nervos_api/api/dependencies.py → packages/nervos-core/src/nervos_core/application/authentication.py
- `create_app()` --uses--> `AuthenticationError`  [INFERRED]
  apps/api/src/nervos_api/app.py → packages/nervos-core/src/nervos_core/application/authentication.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **NervOS Execution Domain Distinctions** — claude_rules_architecture_agent_package, claude_rules_architecture_agent_instance, claude_rules_architecture_run, claude_rules_architecture_session [EXTRACTED 1.00]
- **Authentication Persistence** — docs_authentication_local_authentication, docs_database_users, docs_database_auth_sessions [EXTRACTED 1.00]
- **Target Execution Architecture** — docs_adr_0004_job_driven_runtime_job_runtime, docs_architecture_execution_plane, docs_architecture_permission_engine [EXTRACTED 1.00]
- **Explicit Memory Scope Model** — docs_memory_session_memory, docs_memory_agent_private_memory, docs_memory_user_profile_memory, docs_memory_shared_workspace_memory [EXTRACTED 1.00]
- **Runtime Execution Pipeline** — docs_runtime_trigger, docs_runtime_job, docs_runtime_persistent_queue, docs_runtime_worker, docs_runtime_run_coordinator, docs_runtime_runcontext, docs_runtime_agent_implementation [EXTRACTED 1.00]
- **Permission-mediated Tool Flow** — docs_permissions_permission_engine, docs_permissions_tool_mcp_gateway, docs_permissions_waiting_approval [EXTRACTED 1.00]

## Communities (92 total, 23 thin omitted)

### Community 0 - "Frontend Dependencies"
Cohesion: 0.04
Nodes (45): devDependencies, eslint, @eslint/js, eslint-plugin-react-hooks, eslint-plugin-react-refresh, globals, jsdom, msw (+37 more)

### Community 1 - "Alembic Database Setup"
Cohesion: 0.09
Nodes (30): Alembic environment using the application's database configuration., Render migrations without opening a database connection., Run migrations on an application-configured connection., Run migrations with the same engine configuration as the application., run_migrations(), run_migrations_offline(), run_migrations_online(), Connection (+22 more)

### Community 2 - "API Test Fixtures"
Cohesion: 0.09
Nodes (24): clear_settings_cache(), client(), migrated_app(), FastAPI, fixture, MonkeyPatch, Path, TestClient (+16 more)

### Community 3 - "Session Cookie Routing"
Cohesion: 0.11
Nodes (24): clear_session_cookie(), cookie_is_secure(), datetime, Response, HTTP cookie policy for opaque authentication sessions., Derive Secure only from trusted validated configuration., Set the host-only opaque-session cookie., Clear the cookie with the same scope and security attributes. (+16 more)

### Community 4 - "Authentication Persistence"
Cohesion: 0.12
Nodes (15): IntegrityError, NoReturn, Authentication data retained below the HTTP boundary., StoredCredential, datetime, Session, sessionmaker, Create a fresh session and optionally replace an obsolete hash. (+7 more)

### Community 5 - "Frontend TypeScript Config"
Cohesion: 0.08
Nodes (24): compilerOptions, allowJs, allowSyntheticDefaultImports, esModuleInterop, forceConsistentCasingInFileNames, isolatedModules, jsx, lib (+16 more)

### Community 6 - "Web Package Tooling"
Cohesion: 0.09
Nodes (21): dependencies, react, react-dom, react-router-dom, @tanstack/react-query, engines, node, name (+13 more)

### Community 7 - "Migration Integration Tests"
Cohesion: 0.22
Nodes (19): alembic_config(), application_tables(), assert_integrity_error(), insert_row(), migrate_database(), Any, Engine, MonkeyPatch (+11 more)

### Community 8 - "Foundation Security Concepts"
Cohesion: 0.11
Nodes (20): Marketplace Placeholder, Stage A Bootstrap, Testing Pyramid, SQLite First, Server-Side Opaque Sessions, Exact-Origin CSRF Protection, First-Run Setup, Local Authentication (+12 more)

### Community 9 - "Settings Validation Tests"
Cohesion: 0.19
Nodes (18): Validated API configuration loaded only from process environment., Settings, clear_nervos_environment(), fixture, MonkeyPatch, parametrize, Path, Unit tests for process-environment configuration. (+10 more)

### Community 10 - "Repository Tooling Tests"
Cohesion: 0.16
Nodes (18): CompletedProcess, ModuleType, load_development_module(), MonkeyPatch, Path, Tests for the Stage A1 repository command facade., Load the repository development script without requiring it as a package., Run a repository script with the active test interpreter. (+10 more)

### Community 11 - "Node TypeScript Config"
Cohesion: 0.11
Nodes (17): compilerOptions, allowImportingTsExtensions, lib, module, moduleDetection, moduleResolution, noEmit, noUncheckedSideEffectImports (+9 more)

### Community 12 - "API Composition"
Cohesion: 0.17
Nodes (12): Pre-body security boundary for authentication mutations., create_app(), FastAPI, FastAPI application composition., Compose the API without connecting to or migrating the database., get_settings(), Typed process configuration for the NervOS API., Return the validated process settings for normal composition. (+4 more)

### Community 13 - "Authentication Errors"
Cohesion: 0.16
Nodes (16): AuthenticationError, AuthenticationRequired, InvalidPassword, InvalidUsername, PasswordWorkLimit, PersistenceUnavailable, Exception, Application contracts and policies for local authentication. (+8 more)

### Community 14 - "Security Token Adapters"
Cohesion: 0.14
Nodes (11): Authentication security adapters., Argon2id password hashing adapter with bounded expensive work., Opaque session-token generation and digesting., Generate 256-bit cookie-safe tokens and binary SHA-256 digests., Return a fresh URL-safe token with 256 bits of source entropy., Validate and hash a presented token for database lookup., SecureSessionTokens, parametrize (+3 more)

### Community 15 - "Authentication Routes"
Cohesion: 0.16
Nodes (16): login(), logout(), me(), alias, AuthenticationServiceDependency, Cookie, get, OriginDependency (+8 more)

### Community 16 - "Database Models"
Cohesion: 0.18
Nodes (13): DeclarativeBase, Base, Declarative metadata for NervOS persistence models., Base class for SQLAlchemy persistence records., AuthSessionRecord, SQLAlchemy persistence records for the Stage A schema., Persistence record for a NervOS user., Persistence record for an opaque dashboard authentication session. (+5 more)

### Community 17 - "Authentication Protocols"
Cohesion: 0.14
Nodes (7): Clock, PasswordHasher, Return the current timezone-aware UTC instant., Small application boundary for password hashing., Small application boundary for opaque session tokens., SessionTokens, Protocol

### Community 18 - "API Dependencies"
Cohesion: 0.18
Nodes (14): get_authentication_service(), get_settings(), datetime, Request, FastAPI dependencies for authentication and request security., Return the current aware UTC instant., Return the settings composed into this FastAPI application., Return the concrete authentication service composed into the app. (+6 more)

### Community 19 - "Safe Error Responses"
Cohesion: 0.20
Nodes (14): authentication_error_handler(), error_response(), Request, Safe public error handling for authentication endpoints., Build a stable non-cacheable public error response., Map expected authentication failures without exposing internals., Return sanitized validation details without rejected input values., validation_error_handler() (+6 more)

### Community 20 - "Authentication Service Tests"
Cohesion: 0.22
Nodes (13): AuthenticationService, Coordinate setup, credentials, and opaque server-side sessions., Idempotently revoke the current session when a valid token is present., authentication(), fixture, MonkeyPatch, Path, Session (+5 more)

### Community 21 - "Workspace Commands"
Cohesion: 0.14
Nodes (13): engines, node, pnpm, name, packageManager, private, scripts, build (+5 more)

### Community 22 - "UTC Datetime Tests"
Cohesion: 0.22
Nodes (10): IneffectiveTimezone, datetime, parametrize, Unit tests for UTCDateTime normalization., Timezone whose offset is intentionally undefined., test_utc_datetime_normalizes_aware_values(), test_utc_datetime_passes_none_through(), test_utc_datetime_rejects_values_without_effective_offset() (+2 more)

### Community 23 - "UTC Database Types"
Cohesion: 0.23
Nodes (8): Dialect, SQLAlchemy database infrastructure., datetime, SQLAlchemy types shared by NervOS persistence models., Persist UTC datetimes in SQLite and return timezone-aware values., Normalize an aware value to the naïve UTC SQLite representation., Restore the UTC timezone discarded by SQLite storage., UTCDateTime

### Community 24 - "Authentication Persistence Port"
Cohesion: 0.24
Nodes (6): AuthenticationPersistence, PublicUser, datetime, Focused persistence operations required by authentication., Resolve one valid opaque token to its active persisted user., Safe authenticated-user data available above the application boundary.

### Community 25 - "Bootstrap Tooling"
Cohesion: 0.23
Nodes (11): main(), Install the project-local Python and JavaScript dependencies., Return a command path or exit with an actionable message., Read a dotted numeric version from a command., Run one bootstrap command from the repository root., Validate local runtimes without installing system packages., Install locked dependencies into project-local environments., read_version() (+3 more)

### Community 26 - "Product Roadmap"
Cohesion: 0.18
Nodes (11): Stage A Foundation, Stage B Runtime Proof, Stage C Persistent Execution Engine, Stage D Tool and MCP Layer, Stage E Scheduling and Events, Stage F Agent Sessions and Memory, Stage G Agent Package System, Stage H Security Isolation (+3 more)

### Community 27 - "Authentication Session Issuance"
Cohesion: 0.22
Nodes (9): canonicalize_username(), IssuedSession, Create the first administrator and its initial session atomically., Authenticate credentials and create a fresh absolute-expiry session., Return the fixed seven-day absolute session expiration., Normalize a username into the only accepted persisted form., A committed session and its one-time raw cookie token., _session_expiration() (+1 more)

### Community 28 - "Setup Concurrency"
Cohesion: 0.20
Nodes (9): InvalidCredentials, Raised when initial setup is already permanently unavailable., Raised for every public credential-authentication failure., SetupComplete, Focused SQLAlchemy persistence for local authentication., MonkeyPatch, Path, File-backed SQLite concurrency test for one-time setup. (+1 more)

### Community 29 - "Architecture Boundary Tests"
Cohesion: 0.33
Nodes (10): imported_modules(), Path, python_files(), Architecture regression tests for the Stage A package boundaries., Collect statically declared imports from a Python source file., Return repository Python files in deterministic order., test_application_has_no_fastapi_or_api_imports(), test_base_metadata_create_all_is_not_used() (+2 more)

### Community 30 - "Quality Check Runner"
Cohesion: 0.24
Nodes (10): main(), parse_args(), Namespace, Run the repository checks available through Stage A2., Resolve the executable without invoking a platform shell., Run every command in a named check group., Parse the requested check group., Run the selected repository checks. (+2 more)

### Community 31 - "Development API Launcher"
Cohesion: 0.24
Nodes (10): main(), parse_args(), Namespace, Run implemented NervOS development services., Parse the requested development service., Propagate the validated settings to migration and server processes., Migrate the configured database before starting the API server., Run the selected development service when its milestone exists. (+2 more)

### Community 32 - "Authentication Request Middleware"
Cohesion: 0.24
Nodes (7): AuthenticationBoundaryMiddleware, Reject unsafe authentication requests before buffering their bodies., test_credential_body_size_boundary_rejects_missing_and_oversized(), ASGIApp, Receive, Scope, Send

### Community 33 - "Database Path Validation"
Cohesion: 0.25
Nodes (5): Path, Reject an empty environment value before Path coercion., Expand and resolve a database path without creating it., Require one exact HTTP(S) origin without URL suffixes., field_validator

### Community 34 - "Security Boundary Tests"
Cohesion: 0.32
Nodes (7): Path, TestClient, Adversarial HTTP-boundary tests for A3 authentication., test_auth_mutations_require_exact_origin(), test_invalid_cookie_is_safe_and_health_remains_public(), test_oversized_credential_body_is_rejected_before_parsing(), test_secure_cookie_is_derived_from_https_and_production()

### Community 35 - "Authentication API Tests"
Cohesion: 0.54
Nodes (7): FastAPI, TestClient, Integration tests for login, current-user, and logout endpoints., setup_user(), test_inactive_user_cannot_login_or_use_existing_session(), test_login_uses_safe_generic_errors_and_stores_only_digest(), test_me_requires_valid_session_and_logout_revokes_current_session()

### Community 36 - "Core Architecture Concepts"
Cohesion: 0.25
Nodes (8): Modular Monolith, Job-Driven Runtime, Scoped Agent Memory, AgentInstance, AgentPackage, Control Plane, Execution Plane, Permission Engine

### Community 37 - "Password Input Policy"
Cohesion: 0.36
Nodes (7): Enforce bounded password input without normalizing or truncating it., validate_password(), parametrize, Tests for authentication input policies., test_invalid_username_is_rejected(), test_password_boundaries_are_accepted(), test_password_outside_boundaries_is_rejected()

### Community 38 - "Setup API Route"
Cohesion: 0.29
Nodes (7): AuthenticationServiceDependency, OriginDependency, post, Response, SettingsDependency, Create the sole initial administrator and log them in., setup()

### Community 39 - "Setup API Tests"
Cohesion: 0.43
Nodes (6): FastAPI, TestClient, Integration tests for first-run setup., test_setup_creates_admin_sets_cookie_and_locks_setup(), test_setup_rejects_invalid_or_missing_origin_without_mutation(), test_validation_response_never_echoes_password()

### Community 40 - "Architecture Review Workflow"
Cohesion: 0.29
Nodes (7): Architecture Reviewer, Control Plane, Execution Plane, Job-Driven Execution, Model Provider Interface, Model Provider Adapter, Runtime Feature Workflow

### Community 41 - "Scoped Memory Model"
Cohesion: 0.33
Nodes (7): Agent-private Memory, Selective Context Construction, Memory Private by Default, Session Memory, Shared Workspace Memory, User Profile Memory, Bounded Session Context Strategy

### Community 42 - "Runtime Execution Pipeline"
Cohesion: 0.29
Nodes (7): Agent Implementation, Job, Persistent Queue, Run Coordinator, RunContext, Trigger, Worker

### Community 43 - "Current User Dependency"
Cohesion: 0.33
Nodes (6): get_current_user(), alias, Cookie, SESSION_COOKIE_NAME, Resolve authenticated identity solely from the opaque session cookie., Depends

### Community 44 - "Domain Entity Distinctions"
Cohesion: 0.33
Nodes (6): AgentInstance, AgentPackage, Run, Session, Agent Conversation Session, Session and Run Distinction

### Community 45 - "Initial Schema Migration"
Cohesion: 0.40
Nodes (4): downgrade(), Create exactly the two Stage A application tables., Drop the destructive initial schema in dependency-safe order., upgrade()

### Community 46 - "Security Review Workflow"
Cohesion: 0.40
Nodes (5): Security Reviewer, Thin Route Handlers, Server-Side Ownership Checks, API Endpoint Workflow, Release Readiness

### Community 47 - "Claude Code Hooks"
Cohesion: 0.40
Nodes (5): Claude Code Hooks, Post-Edit Validation Hook, Pre-Tool Guard Hook, Pre-Write Secret Scan Hook, Session Context Hook

### Community 48 - "Agent Domain Model"
Cohesion: 0.40
Nodes (5): AgentInstance, AgentPackage, Run, Session, .nervos Agent Package

### Community 49 - "Production Origin Validation"
Cohesion: 0.50
Nodes (3): Reject insecure external origins in production., model_validator, Self

### Community 50 - "Permission Approval Flow"
Cohesion: 0.50
Nodes (4): Permission Engine, Tool/MCP Gateway, waiting_approval Run State, Run State Machine

### Community 51 - "Browser Authentication Session"
Cohesion: 0.50
Nodes (4): Authentication Session, nervos_session Cookie, 256-bit Opaque Session Token, SHA-256 Token Digest

### Community 52 - "PNPM Workspace"
Cohesion: 0.50
Nodes (4): apps/web Workspace Package, esbuild Built Dependency, msw Ignored Built Dependency, pnpm Workspace

### Community 54 - "Modular Monolith Foundation"
Cohesion: 0.67
Nodes (3): Modular Monolith, NervOS Platform, Stage A

### Community 55 - "Opaque Session Security"
Cohesion: 0.67
Nodes (3): Server-Side Opaque Sessions, Argon2id Password Hashing, Hashed Session Tokens

### Community 60 - "Tool Permission Gateway"
Cohesion: 0.67
Nodes (3): Tool Permission Layer, Permission Engine Tool Flow, MCP Tool Capability

### Community 61 - "Marketplace Package Flow"
Cohesion: 0.67
Nodes (3): Agent Package, Local Package Installer, NervOS Marketplace

### Community 62 - "Capability Grant Model"
Cohesion: 0.67
Nodes (3): AgentInstance Capability Grant, Least Capability Principle, Tool Connection

## Knowledge Gaps
- **150 isolated node(s):** `nervos-api`, `name`, `version`, `private`, `type` (+145 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 389 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **23 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `Settings` connect `Settings Validation Tests` to `Authentication Request Middleware`, `Alembic Database Setup`, `Database Path Validation`, `Session Cookie Routing`, `API Test Fixtures`, `Security Boundary Tests`, `Authentication API Tests`, `Setup API Tests`, `Repository Tooling Tests`, `API Composition`, `Production Origin Validation`, `API Dependencies`, `Development API Launcher`?**
  _High betweenness centrality (0.098) - this node is a cross-community bridge._
- **Why does `create_app()` connect `API Composition` to `Authentication Request Middleware`, `Alembic Database Setup`, `API Test Fixtures`, `Security Boundary Tests`, `Authentication Persistence`, `Settings Validation Tests`, `Authentication Errors`, `Security Token Adapters`, `API Dependencies`, `Safe Error Responses`, `Authentication Service Tests`?**
  _High betweenness centrality (0.047) - this node is a cross-community bridge._
- **Why does `SqlAlchemyAuthenticationPersistence` connect `Authentication Persistence` to `API Test Fixtures`, `API Composition`, `Authentication Errors`, `Database Models`, `Authentication Service Tests`, `Authentication Persistence Port`, `Setup Concurrency`?**
  _High betweenness centrality (0.030) - this node is a cross-community bridge._
- **Are the 27 inferred relationships involving `Settings` (e.g. with `run_migrations_offline()` and `run_migrations_online()`) actually correct?**
  _`Settings` has 27 INFERRED edges - model-reasoned connections that need verification._
- **Are the 8 inferred relationships involving `SqlAlchemyAuthenticationPersistence` (e.g. with `InvalidCredentials` and `PersistenceUnavailable`) actually correct?**
  _`SqlAlchemyAuthenticationPersistence` has 8 INFERRED edges - model-reasoned connections that need verification._
- **Are the 12 inferred relationships involving `create_app()` (e.g. with `utc_now()` and `authentication_error_handler()`) actually correct?**
  _`create_app()` has 12 INFERRED edges - model-reasoned connections that need verification._
- **What connects `nervos-api`, `name`, `version` to the rest of the system?**
  _150 weakly-connected nodes found - possible documentation gaps or missing edges._