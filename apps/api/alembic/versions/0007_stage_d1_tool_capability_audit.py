# ruff: noqa: E501
"""stage d1 durable tool, capability, and audit schema

Revision ID: 0007_stage_d1_tool_capability_audit
Revises: 0006_stage_c6_queue_partitions
Create Date: 2026-09-17 09:00:00.000000

One responsibility, and no others: make the Stage D tool, capability and tool-audit model durable.
It creates four tables and extends three, and **nothing in it executes, discovers, authorizes or
dispatches a tool**. The permission decision, the MCP client, the registry and the Think-Act-Observe
loop all belong to later milestones; this migration only ensures that when they arrive, the states
they need are the states the database already permits, and the states they must never reach are
states it refuses to hold.

Four tables, each with a single job.

`mcp_connections` is the durable *definition* of an approved tool source -- configuration, never a
live client. The MCP session is ephemeral and owned by the Worker, so no column here describes a
socket, a negotiated protocol revision, or a cached catalog. **No column in this table can hold a
credential value.** `credential_ref` is an opaque, operator-declared alias whose shape is
CHECK-constrained to lowercase, which is what makes an environment-variable name -- the credential
passthrough ADR 0016 forbids by name -- unrepresentable rather than merely discouraged. A stdio
server is referenced by its operator-declared key, and there is deliberately no column for a
command, an argument vector, or a working directory: an arbitrary user-supplied stdio command is
arbitrary code execution with the Worker's privileges, so no migration may add a field that accepts
one.

`tool_definitions` describes one callable operation. Its identity is
`(source_kind, source_id, upstream_name)`, and it is enforced by **two partial unique indexes plus a
pairing CHECK rather than one three-column UNIQUE**. That is not a style choice: SQLite does not
enforce uniqueness over a NULL, so a plain three-column UNIQUE would accept unlimited duplicate
built-ins -- exactly the case where `source_id IS NULL`. This is the single most load-bearing
constraint in the migration, and the D1 tests prove the built-in case specifically. The four
`hint_*` columns store the source's own annotation claims because they are part of the fingerprint
a user reviewed; they are untrusted presentation metadata and may never grant or remove authority.
Their defaults are the conservative reading the MCP specification itself prescribes, so an
unannotated tool is stored as possibly destructive and possibly open-world.

`agent_tool_grants` is the authority that exists *now*. **The row's existence is the authority**:
there is no DENY row, so "never granted" and "revoked" are the same observable state and revocation
is a delete. `id` is an `INTEGER PRIMARY KEY AUTOINCREMENT` because it carries two roles at once --
the grant's durable identity, and the monotonic primitive `runs.tool_grant_cutoff_id` snapshots at
submission. Ids are never reused, so a capability can never be silently acquired by a Run whose
cutoff predates the review. **No timestamp decides authority anywhere in this model**, which is why
`created_at` is display metadata only.

`tool_invocations` is the durable audit of calls that actually happened. It cannot hold a secret: no
column exists for an argument, a result, a credential, a token, or a raw provider payload, and
arguments and results are represented only by a digest, a byte count, and -- for arguments -- a
bounded key/type skeleton carrying no values. A digest is content evidence, **never identity**: it
deduplicates nothing, keys nothing, and permits no replay. Identity is `id`; ordering is
`tool_sequence`. `started_at` is the ambiguity boundary, committed immediately before dispatch, and
the lifecycle CHECK keeps the non-dispatched states permanently incapable of claiming a start.

Three existing tables are extended, additively.

`runs` gains five Stage D columns. Four are the immutable loop limits; the fifth,
`tool_grant_cutoff_id`, is the durable monotonic cutoff, not a clock. Every default is the
legacy-compatible value, so a Run migrated from an earlier schema -- and every Run C2 still submits
-- carries `max_tool_calls = 0` and a cutoff of 0 and therefore cannot reach a tool even though the
columns now exist. **No grant is ever inferred for a historical Run.** The accepted C1
`limits_positive` clause keeps its exact meaning; the one Stage D limit that is legitimately zero
sits in a separate named constraint rather than widening an accepted one.

`job_attempts` gains three nullable usage columns for multi-turn, crash-durable accounting. They are
NULL for every Stage C Attempt, because a one-call Attempt reports its usage on the Run at
terminalization; nothing writes them until the tool loop exists, and no Run-level usage semantics
change.

`run_events` gains a nullable `tool_invocation_id` and six tool event types. The thirteen accepted
Stage C event types remain a byte-for-byte prefix of the accepted set. `tool.cancelled` is
deliberately **not** among the new types: ADR 0017 keeps `run.cancelled` as the cancellation truth.

The downgrade refuses rather than destroys.

Dropping populated Stage D state would destroy durable evidence -- including the per-Attempt token
accounting, which has no pre-0007 representation at all -- so the downgrade preflights **every**
condition before any destructive DDL and refuses if any Stage D-only state is present. It never
partially downgrades, never deletes history to succeed, and never rewrites a Run Event or a
ToolInvocation to fabricate compatibility. This follows the rule the earlier lifecycle migrations
established: preserve the truth rather than manufacture a downgrade.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_stage_d1_tool_capability_audit"
down_revision: str | None = "0006_stage_c6_queue_partitions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# ---------------------------------------------------------------------------------------------
# Vocabulary. The tool event types are appended rather than interleaved so that the thirteen
# accepted Stage C types remain an exact prefix of the accepted set.
# ---------------------------------------------------------------------------------------------

C1_EVENT_TYPES = (
    "'run.created','run.queued','attempt.claimed','attempt.started','attempt.failed',"
    "'attempt.expired','retry.scheduled','cancellation.requested','run.cancelled',"
    "'run.succeeded','run.failed','recovery.pre_start','recovery.ambiguous'"
)
TOOL_EVENT_TYPES = (
    "'tool.requested','tool.started','tool.succeeded','tool.failed','tool.denied','tool.ambiguous'"
)
CONNECTION_STATUSES = "'connected','unavailable','needs_refresh','definition_changed','disabled'"
DEFINITION_STATUSES = "'available','unavailable','unsupported_schema'"
INVOCATION_STATUSES = "'requested','denied','cancelled','started','succeeded','failed','ambiguous'"
INVOCATION_DISPATCHED_STATUSES = "'started','succeeded','failed','ambiguous'"

# An operator-declared name: a credential alias or a stdio server key. Lowercase kebab is frozen so
# that an environment-variable name cannot be stored in either column. A SQLite CHECK passes on
# NULL, so the expression is vacuously satisfied for a connection that declares neither.
CREDENTIAL_REF_SHAPE = (
    "credential_ref = trim(credential_ref) AND length(credential_ref) BETWEEN 1 AND 128"
    " AND credential_ref NOT GLOB '*[^a-z0-9-]*' AND credential_ref GLOB '[a-z0-9]*'"
)
SERVER_KEY_SHAPE = (
    "server_key = trim(server_key) AND length(server_key) BETWEEN 1 AND 128"
    " AND server_key NOT GLOB '*[^a-z0-9-]*' AND server_key GLOB '[a-z0-9]*'"
)

# The model-facing name is the stricter of the two provider length bounds and the intersection of
# their alphabets, so one persisted name is valid for either provider with no per-provider branch.
MODEL_NAME_SHAPE = (
    "model_name = lower(model_name) AND length(model_name) BETWEEN 1 AND 64"
    " AND model_name NOT GLOB '*[^a-z0-9_-]*' AND model_name GLOB '[a-z0-9]*'"
)

# `(source_kind = 'builtin') = (source_id IS NULL)` is what keeps the two partial unique indexes
# total. Without it a `builtin` row carrying a non-null `source_id` would fall outside both indexes
# and escape uniqueness entirely.
SOURCE_PAIRING = (
    "(source_kind = 'builtin' AND source_id IS NULL)"
    " OR (source_kind = 'mcp' AND source_id IS NOT NULL)"
)

FINGERPRINT_SHAPE = "length(fingerprint) = 64 AND fingerprint NOT GLOB '*[^0-9a-f]*'"
REVIEWED_FINGERPRINT_SHAPE = (
    "length(reviewed_fingerprint) = 64 AND reviewed_fingerprint NOT GLOB '*[^0-9a-f]*'"
)
DEFINITION_FINGERPRINT_SHAPE = (
    "length(definition_fingerprint) = 64 AND definition_fingerprint NOT GLOB '*[^0-9a-f]*'"
)
ARGUMENTS_DIGEST_SHAPE = "length(arguments_digest) = 64 AND arguments_digest NOT GLOB '*[^0-9a-f]*'"
RESULT_DIGEST_SHAPE = "length(result_digest) = 64 AND result_digest NOT GLOB '*[^0-9a-f]*'"

# ---------------------------------------------------------------------------------------------
# The two `runs` shapes this migration moves between, and the two `run_events` shapes. Every
# accepted expression is copied verbatim from the merged schema: the rebuild must add the Stage D
# columns without weakening the C1/C5 lifecycle, the C4/C5 finished-order rule, or the event
# vocabulary that C7's public timeline reads.
# ---------------------------------------------------------------------------------------------

BLANK_TEXT = (
    "char(9,10,11,12,13,32,133,160,5760,8192,8193,8194,8195,8196,8197,8198,8199,"
    "8200,8201,8202,8232,8233,8239,8287,12288)"
)
EXHAUSTED = "'worker_recovery_exhausted'"

RUN_STATUSES = "'created','running','succeeded','failed','cancelled'"

RUN_FINISHED_ORDER = (
    "finished_at IS NULL OR (started_at IS NOT NULL AND finished_at >= started_at)"
    f" OR (started_at IS NULL AND finished_at >= created_at AND (error_code = {EXHAUSTED}"
    " OR status = 'cancelled'))"
)

RUN_LIFECYCLE_SHAPE = (
    "(status='created' AND started_at IS NULL AND finished_at IS NULL AND output_text IS NULL"
    " AND finish_reason IS NULL AND error_code IS NULL AND error_message IS NULL"
    " AND input_tokens IS NULL AND output_tokens IS NULL AND total_tokens IS NULL"
    " AND elapsed_ms IS NULL)"
    " OR (status='running' AND started_at IS NOT NULL AND finished_at IS NULL"
    " AND output_text IS NULL AND finish_reason IS NULL AND error_code IS NULL"
    " AND error_message IS NULL AND input_tokens IS NULL AND output_tokens IS NULL"
    " AND total_tokens IS NULL AND elapsed_ms IS NULL)"
    " OR (status='succeeded' AND started_at IS NOT NULL AND finished_at IS NOT NULL"
    " AND output_text IS NOT NULL"
    f" AND length(trim(output_text, {BLANK_TEXT})) > 0"
    " AND error_code IS NULL AND error_message IS NULL AND elapsed_ms IS NOT NULL)"
    " OR (status='failed' AND started_at IS NOT NULL AND finished_at IS NOT NULL"
    " AND output_text IS NULL AND finish_reason IS NULL AND error_code IS NOT NULL"
    f" AND error_code <> {EXHAUSTED} AND error_message IS NOT NULL"
    f" AND length(trim(error_message, {BLANK_TEXT})) > 0 AND elapsed_ms IS NOT NULL)"
    " OR (status='failed' AND started_at IS NULL AND finished_at IS NOT NULL"
    " AND output_text IS NULL AND finish_reason IS NULL"
    f" AND error_code = {EXHAUSTED} AND error_message IS NOT NULL"
    f" AND length(trim(error_message, {BLANK_TEXT})) > 0"
    " AND input_tokens IS NULL AND output_tokens IS NULL AND total_tokens IS NULL"
    " AND elapsed_ms IS NULL)"
    " OR (status='cancelled' AND started_at IS NOT NULL AND finished_at IS NOT NULL"
    " AND output_text IS NULL AND finish_reason IS NULL AND error_code IS NULL"
    " AND error_message IS NULL AND input_tokens IS NULL AND output_tokens IS NULL"
    " AND total_tokens IS NULL AND elapsed_ms IS NOT NULL)"
    " OR (status='cancelled' AND started_at IS NULL AND finished_at IS NOT NULL"
    " AND output_text IS NULL AND finish_reason IS NULL AND error_code IS NULL"
    " AND error_message IS NULL AND input_tokens IS NULL AND output_tokens IS NULL"
    " AND total_tokens IS NULL AND elapsed_ms IS NULL)"
)

RUN_LIMITS_POSITIVE = (
    "input_max_bytes > 0 AND input_max_code_points > 0 AND output_max_bytes > 0"
    " AND output_max_code_points > 0 AND provider_timeout_ms > 0 AND max_output_tokens > 0"
    " AND max_model_calls > 0"
)

# The Stage D loop limits. `max_tool_calls` is the one Run limit that is legitimately zero, so it
# cannot live in `limits_positive`; `tool_grant_cutoff_id` is a monotonic grant id, not a clock,
# and 0 means "admits no grant at all".
TOOL_LIMITS_BOUNDS = (
    "max_tool_calls BETWEEN 0 AND 16 AND tool_timeout_ms BETWEEN 1000 AND 300000"
    " AND tool_result_max_bytes BETWEEN 1024 AND 1048576"
    " AND max_consecutive_tool_failures > 0 AND tool_grant_cutoff_id >= 0"
)

# The columns both shapes share. The five Stage D columns are deliberately absent: on upgrade they
# are filled from their defaults, and on downgrade they are dropped.
SHARED_RUN_COLUMNS = (
    "id, agent_instance_id, status, agent_key, agent_definition_version, model_provider,"
    " model_name, input_text, input_max_bytes, input_max_code_points, output_max_bytes,"
    " output_max_code_points, provider_timeout_ms, max_output_tokens, max_model_calls,"
    " output_text, finish_reason, error_code, error_message, input_tokens, output_tokens,"
    " total_tokens, elapsed_ms, created_at, started_at, finished_at"
)

# The exact legacy-compatible value of each Stage D Run column. A downgrade may only proceed when
# every Run still holds these, because they are the only values the pre-0007 schema can represent.
LEGACY_RUN_DEFAULTS = (
    ("max_tool_calls", "max_tool_calls <> 0", "0"),
    ("tool_timeout_ms", "tool_timeout_ms <> 30000", "30000"),
    ("tool_result_max_bytes", "tool_result_max_bytes <> 65536", "65536"),
    ("max_consecutive_tool_failures", "max_consecutive_tool_failures <> 3", "3"),
    ("tool_grant_cutoff_id", "tool_grant_cutoff_id <> 0", "0"),
)

# Every condition the downgrade must check, paired with the reason it is unforgeable. All of these
# are evaluated before the first destructive statement.
DOWNGRADE_PREFLIGHT = (
    ("mcp_connections holds connection definitions", "SELECT count(*) FROM mcp_connections"),
    ("tool_definitions holds discovered definitions", "SELECT count(*) FROM tool_definitions"),
    ("agent_tool_grants holds capability grants", "SELECT count(*) FROM agent_tool_grants"),
    ("tool_invocations holds tool-call audit rows", "SELECT count(*) FROM tool_invocations"),
    (
        "run_events holds Stage D tool events",
        f"SELECT count(*) FROM run_events WHERE event_type IN ({TOOL_EVENT_TYPES})",
    ),
    (
        "run_events references a tool invocation",
        "SELECT count(*) FROM run_events WHERE tool_invocation_id IS NOT NULL",
    ),
    *(
        (
            f"runs.{column} differs from its legacy value {value}",
            f"SELECT count(*) FROM runs WHERE {predicate}",
        )
        for column, predicate, value in LEGACY_RUN_DEFAULTS
    ),
    (
        "job_attempts holds Stage D token accounting",
        "SELECT count(*) FROM job_attempts WHERE input_tokens IS NOT NULL"
        " OR output_tokens IS NOT NULL OR total_tokens IS NOT NULL",
    ),
)


def runs_ddl(table: str, *, tool_limits: bool) -> str:
    """Build the `runs` DDL for `table`, with or without the Stage D columns.

    Constraint names are hardcoded to the `ck_runs_*` set so they survive the rename and keep
    matching the ORM's naming convention. The Stage D columns are placed where the ORM declares
    them rather than appended, so the rebuilt table's column order is the reviewed one.
    """
    tool_columns = (
        """
    max_tool_calls INTEGER NOT NULL DEFAULT '0',
    tool_timeout_ms INTEGER NOT NULL DEFAULT '30000',
    tool_result_max_bytes INTEGER NOT NULL DEFAULT '65536',
    max_consecutive_tool_failures INTEGER NOT NULL DEFAULT '3',
    tool_grant_cutoff_id INTEGER NOT NULL DEFAULT '0',"""
        if tool_limits
        else ""
    )
    tool_constraint = (
        f"\n    CONSTRAINT ck_runs_tool_limits_bounds CHECK ({TOOL_LIMITS_BOUNDS}),"
        if tool_limits
        else ""
    )
    return f"""
CREATE TABLE {table} (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    agent_instance_id INTEGER NOT NULL,
    status VARCHAR(16) NOT NULL,
    agent_key VARCHAR(128) NOT NULL,
    agent_definition_version VARCHAR(64) NOT NULL,
    model_provider VARCHAR(64) NOT NULL,
    model_name VARCHAR(256) NOT NULL,
    input_text TEXT NOT NULL,
    input_max_bytes INTEGER NOT NULL,
    input_max_code_points INTEGER NOT NULL,
    output_max_bytes INTEGER NOT NULL,
    output_max_code_points INTEGER NOT NULL,
    provider_timeout_ms INTEGER NOT NULL,
    max_output_tokens INTEGER NOT NULL,
    max_model_calls INTEGER NOT NULL,{tool_columns}
    output_text TEXT,
    finish_reason VARCHAR(64),
    error_code VARCHAR(64),
    error_message VARCHAR(512),
    input_tokens INTEGER,
    output_tokens INTEGER,
    total_tokens INTEGER,
    elapsed_ms INTEGER,
    created_at DATETIME NOT NULL,
    started_at DATETIME,
    finished_at DATETIME,
    CONSTRAINT ck_runs_agent_instance_positive CHECK (agent_instance_id > 0),
    CONSTRAINT ck_runs_status_value CHECK (status IN ({RUN_STATUSES})),
    CONSTRAINT ck_runs_limits_positive CHECK ({RUN_LIMITS_POSITIVE}),{tool_constraint}
    CONSTRAINT ck_runs_input_bounds CHECK (length(input_text) BETWEEN 1 AND input_max_code_points AND length(CAST(input_text AS BLOB)) <= input_max_bytes AND instr(input_text, char(0)) = 0 AND length(trim(input_text, {BLANK_TEXT})) > 0),
    CONSTRAINT ck_runs_output_bounds CHECK (output_text IS NULL OR (length(output_text) BETWEEN 1 AND output_max_code_points AND length(CAST(output_text AS BLOB)) <= output_max_bytes AND instr(output_text, char(0)) = 0)),
    CONSTRAINT ck_runs_usage_nonnegative CHECK ((input_tokens IS NULL OR input_tokens >= 0) AND (output_tokens IS NULL OR output_tokens >= 0) AND (total_tokens IS NULL OR total_tokens >= 0)),
    CONSTRAINT ck_runs_elapsed_nonnegative CHECK (elapsed_ms IS NULL OR elapsed_ms >= 0),
    CONSTRAINT ck_runs_started_order CHECK (started_at IS NULL OR started_at >= created_at),
    CONSTRAINT ck_runs_finished_order CHECK ({RUN_FINISHED_ORDER}),
    CONSTRAINT ck_runs_lifecycle_shape CHECK ({RUN_LIFECYCLE_SHAPE}),
    CONSTRAINT fk_runs_agent_instance_id_agent_instances FOREIGN KEY(agent_instance_id) REFERENCES agent_instances (id) ON DELETE RESTRICT
)
"""


def run_events_ddl(table: str, *, tool_invocation: bool) -> str:
    """Build the `run_events` DDL for `table`, with or without the Stage D additions.

    A tool event is *about* one invocation, so the nullable `tool_invocation_id` is how a tool's
    identity reaches the public timeline without stuffing a name into `message` -- a field whose
    contract is the safe error pair, and which a read side would have to string-parse.
    """
    column = "\n    tool_invocation_id INTEGER," if tool_invocation else ""
    foreign_key = (
        "\n    CONSTRAINT fk_run_events_tool_invocation_id_tool_invocations"
        " FOREIGN KEY(tool_invocation_id) REFERENCES tool_invocations (id) ON DELETE RESTRICT,"
        if tool_invocation
        else ""
    )
    vocabulary = C1_EVENT_TYPES + (f",{TOOL_EVENT_TYPES}" if tool_invocation else "")
    return f"""
CREATE TABLE {table} (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    job_id INTEGER NOT NULL,
    attempt_id INTEGER,{column}
    sequence INTEGER NOT NULL,
    event_type VARCHAR(64) NOT NULL,
    code VARCHAR(64),
    message VARCHAR(512),
    attempt_number INTEGER,
    available_at DATETIME,
    created_at DATETIME NOT NULL,
    CONSTRAINT ck_run_events_sequence_positive CHECK (sequence > 0),
    CONSTRAINT ck_run_events_event_type_value CHECK (event_type IN ({vocabulary})),
    CONSTRAINT ck_run_events_message_pair CHECK ((code IS NULL) = (message IS NULL)),
    CONSTRAINT ck_run_events_message_bounds CHECK (code IS NULL OR (length(code) BETWEEN 1 AND 64 AND length(message) BETWEEN 1 AND 512)),
    CONSTRAINT ck_run_events_attempt_number_positive CHECK (attempt_number IS NULL OR attempt_number > 0),
    CONSTRAINT ck_run_events_retry_available CHECK (event_type != 'retry.scheduled' OR available_at IS NOT NULL),
    CONSTRAINT fk_run_events_attempt_id_job_attempts FOREIGN KEY(attempt_id) REFERENCES job_attempts (id) ON DELETE RESTRICT,{foreign_key}
    CONSTRAINT fk_run_events_job_id_jobs FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE RESTRICT,
    CONSTRAINT fk_run_events_run_id_runs FOREIGN KEY(run_id) REFERENCES runs (id) ON DELETE RESTRICT,
    CONSTRAINT uq_run_events_run_id UNIQUE (run_id, sequence)
)
"""


def rebuild_runs(*, tool_limits: bool) -> None:
    """Rebuild `runs` outside the harness transaction so the FK pragma actually applies.

    SQLite can neither add a CHECK to an existing table nor drop one, so the Stage D constraint
    requires a table rebuild. The rebuild must run in autocommit: the harness wraps migrations in
    `context.begin_transaction()`, and SQLite ignores `PRAGMA foreign_keys` inside a transaction,
    so an in-transaction disable is a silent no-op and `DROP TABLE runs` fails against its RESTRICT
    children (`jobs.run_id`, `run_events.run_id`).
    """
    with op.get_context().autocommit_block():
        op.execute("PRAGMA foreign_keys=OFF")
        op.execute(runs_ddl("runs_old", tool_limits=tool_limits))
        op.execute(
            f"INSERT INTO runs_old ({SHARED_RUN_COLUMNS}) SELECT {SHARED_RUN_COLUMNS} FROM runs"
        )
        op.execute("DROP TABLE runs")
        op.execute("ALTER TABLE runs_old RENAME TO runs")
        op.execute("CREATE INDEX ix_runs_agent_instance_id_id ON runs (agent_instance_id, id)")
        op.execute("PRAGMA foreign_keys=ON")
    violations = op.get_bind().exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError(
            f"0007 preserved no integrity: foreign_key_check reported {len(violations)} violation(s)"
        )


def rebuild_run_events(*, tool_invocation: bool) -> None:
    """Rebuild `run_events` for the same reason `runs` is rebuilt: its event-type CHECK changes.

    Nothing references `run_events`, so the rebuild needs no `PRAGMA foreign_keys` dance -- but the
    FK to `tool_invocations` is only meaningful on the upgrade, so `tool_invocations` must already
    exist when this runs.
    """
    with op.get_context().autocommit_block():
        op.execute(run_events_ddl("run_events_old", tool_invocation=tool_invocation))
        op.execute(
            "INSERT INTO run_events_old"
            " (id, run_id, job_id, attempt_id, sequence, event_type, code, message,"
            " attempt_number, available_at, created_at)"
            " SELECT id, run_id, job_id, attempt_id, sequence, event_type, code, message,"
            " attempt_number, available_at, created_at FROM run_events"
        )
        op.execute("DROP TABLE run_events")
        op.execute("ALTER TABLE run_events_old RENAME TO run_events")
        op.execute("CREATE INDEX ix_run_events_attempt_id_id ON run_events (attempt_id, id)")
    violations = op.get_bind().exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError(
            f"0007 preserved no integrity: foreign_key_check reported {len(violations)} violation(s)"
        )


def create_stage_d_tables() -> None:
    """Create the four durable Stage D tables in dependency order."""
    op.create_table(
        "mcp_connections",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("display_name", sa.String(length=100), nullable=False),
        sa.Column("transport", sa.String(length=16), nullable=False),
        sa.Column("endpoint", sa.String(length=512), nullable=True),
        sa.Column("server_key", sa.String(length=128), nullable=True),
        sa.Column("credential_ref", sa.String(length=128), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "catalog_status", sa.String(length=32), server_default="unavailable", nullable=False
        ),
        sa.Column("last_discovery_at", sa.DateTime(), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("last_error_message", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("owner_user_id > 0", name=op.f("ck_mcp_connections_owner_positive")),
        sa.CheckConstraint(
            "display_name = trim(display_name) AND length(display_name) BETWEEN 1 AND 100",
            name=op.f("ck_mcp_connections_display_name_shape"),
        ),
        sa.CheckConstraint(
            "length(CAST(display_name AS BLOB)) <= 400",
            name=op.f("ck_mcp_connections_display_name_bytes"),
        ),
        sa.CheckConstraint(
            "transport IN ('stdio','http')", name=op.f("ck_mcp_connections_transport_value")
        ),
        sa.CheckConstraint(
            "(transport = 'http' AND endpoint IS NOT NULL AND server_key IS NULL)"
            " OR (transport = 'stdio' AND endpoint IS NULL AND server_key IS NOT NULL)",
            name=op.f("ck_mcp_connections_target_shape"),
        ),
        sa.CheckConstraint(
            "endpoint IS NULL OR (length(endpoint) BETWEEN 1 AND 512"
            " AND instr(endpoint, char(0)) = 0)",
            name=op.f("ck_mcp_connections_endpoint_bounds"),
        ),
        sa.CheckConstraint(SERVER_KEY_SHAPE, name=op.f("ck_mcp_connections_server_key_shape")),
        sa.CheckConstraint(
            CREDENTIAL_REF_SHAPE, name=op.f("ck_mcp_connections_credential_ref_shape")
        ),
        sa.CheckConstraint("enabled IN (0, 1)", name=op.f("ck_mcp_connections_enabled_boolean")),
        sa.CheckConstraint(
            f"catalog_status IN ({CONNECTION_STATUSES})",
            name=op.f("ck_mcp_connections_status_value"),
        ),
        sa.CheckConstraint(
            "(last_error_code IS NULL) = (last_error_message IS NULL)",
            name=op.f("ck_mcp_connections_error_pair"),
        ),
        sa.CheckConstraint(
            "last_error_code IS NULL OR (length(last_error_code) BETWEEN 1 AND 64"
            " AND length(last_error_message) BETWEEN 1 AND 512)",
            name=op.f("ck_mcp_connections_error_bounds"),
        ),
        sa.CheckConstraint(
            "last_discovery_at IS NULL OR last_discovery_at >= created_at",
            name=op.f("ck_mcp_connections_discovery_order"),
        ),
        sa.CheckConstraint(
            "updated_at >= created_at", name=op.f("ck_mcp_connections_timestamp_order")
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name=op.f("fk_mcp_connections_owner_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mcp_connections")),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "ix_mcp_connections_owner_user_id_id", "mcp_connections", ["owner_user_id", "id"]
    )

    op.create_table(
        "tool_definitions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_kind", sa.String(length=16), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=True),
        sa.Column("upstream_name", sa.String(length=128), nullable=False),
        sa.Column("model_name", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("input_schema", sa.Text(), nullable=False),
        sa.Column("output_schema", sa.Text(), nullable=True),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("hint_read_only", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("hint_destructive", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("hint_idempotent", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("hint_open_world", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(SOURCE_PAIRING, name=op.f("ck_tool_definitions_source_shape")),
        sa.CheckConstraint(
            "source_kind IN ('builtin','mcp')", name=op.f("ck_tool_definitions_source_kind_value")
        ),
        sa.CheckConstraint(
            "length(upstream_name) BETWEEN 1 AND 128",
            name=op.f("ck_tool_definitions_upstream_name_shape"),
        ),
        sa.CheckConstraint(MODEL_NAME_SHAPE, name=op.f("ck_tool_definitions_model_name_shape")),
        sa.CheckConstraint(
            "display_name = trim(display_name) AND length(display_name) BETWEEN 1 AND 100",
            name=op.f("ck_tool_definitions_display_name_shape"),
        ),
        sa.CheckConstraint(
            "length(CAST(display_name AS BLOB)) <= 400",
            name=op.f("ck_tool_definitions_display_name_bytes"),
        ),
        sa.CheckConstraint(
            "length(CAST(description AS BLOB)) <= 65536",
            name=op.f("ck_tool_definitions_description_bounds"),
        ),
        sa.CheckConstraint(
            "length(CAST(input_schema AS BLOB)) BETWEEN 2 AND 65536",
            name=op.f("ck_tool_definitions_input_schema_bounds"),
        ),
        sa.CheckConstraint(
            "output_schema IS NULL OR length(CAST(output_schema AS BLOB)) BETWEEN 2 AND 65536",
            name=op.f("ck_tool_definitions_output_schema_bounds"),
        ),
        sa.CheckConstraint(FINGERPRINT_SHAPE, name=op.f("ck_tool_definitions_fingerprint_shape")),
        sa.CheckConstraint(
            f"status IN ({DEFINITION_STATUSES})", name=op.f("ck_tool_definitions_status_value")
        ),
        sa.CheckConstraint(
            "hint_read_only IN (0, 1) AND hint_destructive IN (0, 1)"
            " AND hint_idempotent IN (0, 1) AND hint_open_world IN (0, 1)",
            name=op.f("ck_tool_definitions_hint_boolean"),
        ),
        sa.CheckConstraint(
            "updated_at >= created_at", name=op.f("ck_tool_definitions_timestamp_order")
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["mcp_connections.id"],
            name=op.f("fk_tool_definitions_source_id_mcp_connections"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tool_definitions")),
        sqlite_autoincrement=True,
    )
    # Two partial unique indexes, not one three-column UNIQUE. SQLite does not enforce uniqueness
    # over a NULL, so `UNIQUE(source_kind, source_id, upstream_name)` would accept unlimited
    # duplicate built-ins -- the case where `source_id IS NULL`. These, plus the pairing CHECK,
    # are what make the built-in case exactly as strict as the MCP one.
    op.create_index(
        "uq_tool_definitions_builtin",
        "tool_definitions",
        ["upstream_name"],
        unique=True,
        sqlite_where=sa.text("source_kind = 'builtin'"),
    )
    op.create_index(
        "uq_tool_definitions_mcp",
        "tool_definitions",
        ["source_id", "upstream_name"],
        unique=True,
        sqlite_where=sa.text("source_kind = 'mcp'"),
    )
    op.create_index(
        "uq_tool_definitions_model_name", "tool_definitions", ["model_name"], unique=True
    )
    # Not redundant with the partial MCP index above. SQLite uses a partial index only when the
    # query's own WHERE clause implies the index's condition, so `WHERE source_id = ?` alone falls
    # outside `WHERE source_kind = 'mcp'` and would scan. Measured with EXPLAIN QUERY PLAN.
    op.create_index("ix_tool_definitions_source_id", "tool_definitions", ["source_id"])

    op.create_table(
        "agent_tool_grants",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("agent_instance_id", sa.Integer(), nullable=False),
        sa.Column("tool_definition_id", sa.Integer(), nullable=False),
        sa.Column("reviewed_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "agent_instance_id > 0", name=op.f("ck_agent_tool_grants_agent_instance_positive")
        ),
        sa.CheckConstraint(
            "tool_definition_id > 0", name=op.f("ck_agent_tool_grants_tool_definition_positive")
        ),
        sa.CheckConstraint(
            REVIEWED_FINGERPRINT_SHAPE,
            name=op.f("ck_agent_tool_grants_reviewed_fingerprint_shape"),
        ),
        sa.ForeignKeyConstraint(
            ["agent_instance_id"],
            ["agent_instances.id"],
            name=op.f("fk_agent_tool_grants_agent_instance_id_agent_instances"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tool_definition_id"],
            ["tool_definitions.id"],
            name=op.f("fk_agent_tool_grants_tool_definition_id_tool_definitions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_tool_grants")),
        sa.UniqueConstraint(
            "agent_instance_id",
            "tool_definition_id",
            name=op.f("uq_agent_tool_grants_agent_instance_id"),
        ),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "ix_agent_tool_grants_tool_definition_id", "agent_tool_grants", ["tool_definition_id"]
    )

    op.create_table(
        "tool_invocations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("attempt_id", sa.Integer(), nullable=False),
        sa.Column("tool_sequence", sa.Integer(), nullable=False),
        sa.Column("tool_definition_id", sa.Integer(), nullable=False),
        sa.Column("source_kind", sa.String(length=16), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=True),
        sa.Column("upstream_name", sa.String(length=128), nullable=False),
        sa.Column("model_name", sa.String(length=64), nullable=False),
        sa.Column("definition_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("permission_decision", sa.String(length=64), nullable=False),
        sa.Column("provider_call_id", sa.String(length=128), nullable=True),
        sa.Column("requested_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("arguments_digest", sa.String(length=64), nullable=True),
        sa.Column("arguments_shape", sa.Text(), nullable=True),
        sa.Column("result_digest", sa.String(length=64), nullable=True),
        sa.Column("result_bytes", sa.Integer(), nullable=True),
        sa.Column("result_truncated", sa.Boolean(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.String(length=512), nullable=True),
        sa.CheckConstraint(
            "tool_sequence > 0", name=op.f("ck_tool_invocations_tool_sequence_positive")
        ),
        sa.CheckConstraint(
            f"status IN ({INVOCATION_STATUSES})", name=op.f("ck_tool_invocations_status_value")
        ),
        sa.CheckConstraint(SOURCE_PAIRING, name=op.f("ck_tool_invocations_source_shape")),
        sa.CheckConstraint(
            "source_kind IN ('builtin','mcp')", name=op.f("ck_tool_invocations_source_kind_value")
        ),
        sa.CheckConstraint(
            "length(upstream_name) BETWEEN 1 AND 128",
            name=op.f("ck_tool_invocations_upstream_name_shape"),
        ),
        sa.CheckConstraint(MODEL_NAME_SHAPE, name=op.f("ck_tool_invocations_model_name_shape")),
        sa.CheckConstraint(
            DEFINITION_FINGERPRINT_SHAPE,
            name=op.f("ck_tool_invocations_definition_fingerprint_shape"),
        ),
        # The permission decision is durable: "was this call allowed?" is answerable after the
        # fact. The `denied_<reason>` vocabulary is left open because the evaluator that produces
        # the reasons belongs to a later milestone.
        sa.CheckConstraint(
            "permission_decision = 'allowed' OR permission_decision GLOB 'denied_*'",
            name=op.f("ck_tool_invocations_permission_decision_value"),
        ),
        sa.CheckConstraint(
            "length(permission_decision) BETWEEN 1 AND 64",
            name=op.f("ck_tool_invocations_permission_decision_bounds"),
        ),
        sa.CheckConstraint(
            "provider_call_id IS NULL OR length(provider_call_id) BETWEEN 1 AND 128",
            name=op.f("ck_tool_invocations_provider_call_id_bounds"),
        ),
        # The ambiguity boundary as a durable invariant: an invocation is dispatched exactly when
        # it has a start, and never otherwise.
        sa.CheckConstraint(
            f"(status IN ({INVOCATION_DISPATCHED_STATUSES})) = (started_at IS NOT NULL)",
            name=op.f("ck_tool_invocations_dispatched_shape"),
        ),
        sa.CheckConstraint(
            "(status = 'requested' AND started_at IS NULL AND finished_at IS NULL)"
            " OR (status IN ('denied','cancelled') AND started_at IS NULL"
            " AND finished_at IS NOT NULL)"
            " OR (status = 'started' AND started_at IS NOT NULL AND finished_at IS NULL)"
            " OR (status IN ('succeeded','failed','ambiguous') AND started_at IS NOT NULL"
            " AND finished_at IS NOT NULL)",
            name=op.f("ck_tool_invocations_lifecycle_shape"),
        ),
        sa.CheckConstraint(
            "started_at IS NULL OR started_at >= requested_at",
            name=op.f("ck_tool_invocations_start_order"),
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= COALESCE(started_at, requested_at)",
            name=op.f("ck_tool_invocations_finish_order"),
        ),
        sa.CheckConstraint(
            "(status = 'succeeded' AND result_digest IS NOT NULL AND result_bytes IS NOT NULL"
            " AND result_truncated IS NOT NULL)"
            " OR (status != 'succeeded' AND result_digest IS NULL AND result_bytes IS NULL"
            " AND result_truncated IS NULL)",
            name=op.f("ck_tool_invocations_result_shape"),
        ),
        sa.CheckConstraint(
            "result_bytes IS NULL OR result_bytes >= 0",
            name=op.f("ck_tool_invocations_result_nonnegative"),
        ),
        sa.CheckConstraint(
            "result_truncated IS NULL OR result_truncated IN (0, 1)",
            name=op.f("ck_tool_invocations_result_truncated_value"),
        ),
        sa.CheckConstraint(
            ARGUMENTS_DIGEST_SHAPE, name=op.f("ck_tool_invocations_arguments_digest_shape")
        ),
        sa.CheckConstraint(
            RESULT_DIGEST_SHAPE, name=op.f("ck_tool_invocations_result_digest_shape")
        ),
        sa.CheckConstraint(
            "arguments_shape IS NULL OR length(CAST(arguments_shape AS BLOB)) <= 4096",
            name=op.f("ck_tool_invocations_arguments_shape_bounds"),
        ),
        sa.CheckConstraint(
            "status != 'succeeded' OR error_code IS NULL",
            name=op.f("ck_tool_invocations_success_shape"),
        ),
        sa.CheckConstraint(
            "status NOT IN ('failed','ambiguous') OR error_code IS NOT NULL",
            name=op.f("ck_tool_invocations_failure_error"),
        ),
        sa.CheckConstraint(
            "(error_code IS NULL) = (error_message IS NULL)",
            name=op.f("ck_tool_invocations_error_pair"),
        ),
        sa.CheckConstraint(
            "error_code IS NULL OR (length(error_code) BETWEEN 1 AND 64"
            " AND length(error_message) BETWEEN 1 AND 512)",
            name=op.f("ck_tool_invocations_error_bounds"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_tool_invocations_run_id_runs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["jobs.id"],
            name=op.f("fk_tool_invocations_job_id_jobs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["job_attempts.id"],
            name=op.f("fk_tool_invocations_attempt_id_job_attempts"),
            ondelete="RESTRICT",
        ),
        # RESTRICT, so a definition with invocation history can never be cascaded away.
        sa.ForeignKeyConstraint(
            ["tool_definition_id"],
            ["tool_definitions.id"],
            name=op.f("fk_tool_invocations_tool_definition_id_tool_definitions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tool_invocations")),
        # `tool_sequence` is the ordering authority, not a timestamp. This unique constraint also
        # materialises the index the replay guard seeks, so no second index is added for it.
        sa.UniqueConstraint(
            "attempt_id", "tool_sequence", name=op.f("uq_tool_invocations_attempt_id")
        ),
        sqlite_autoincrement=True,
    )


def drop_stage_d_tables() -> None:
    """Drop the four Stage D tables, children first."""
    op.drop_table("tool_invocations")
    op.drop_table("agent_tool_grants")
    op.drop_table("tool_definitions")
    op.drop_table("mcp_connections")


def refuse_if_stage_d_state_exists() -> None:
    """Refuse the downgrade before any destructive statement when Stage D state is present.

    Dropping populated Stage D state would destroy durable evidence -- including the per-Attempt
    token accounting, which has no pre-0007 representation at all. Every condition is therefore
    evaluated here, before the first `DROP`/`ALTER`, so a refusal leaves both schema and data
    exactly as they were. Nothing is ever deleted, rewritten or zeroed to make a downgrade succeed.
    """
    connection = op.get_bind()
    blockers = [
        f"{reason} ({count} row(s))"
        for reason, statement in DOWNGRADE_PREFLIGHT
        if (count := connection.exec_driver_sql(statement).scalar())
    ]
    if blockers:
        raise RuntimeError(
            "cannot downgrade 0007: Stage D state has no pre-0007 representation, so the downgrade"
            " would destroy durable evidence. Resolve these first: " + "; ".join(blockers)
        )


def upgrade() -> None:
    """Make the Stage D tool, capability and audit model durable. No tool is executed."""
    create_stage_d_tables()
    rebuild_runs(tool_limits=True)
    op.execute("ALTER TABLE job_attempts ADD COLUMN input_tokens INTEGER")
    op.execute("ALTER TABLE job_attempts ADD COLUMN output_tokens INTEGER")
    op.execute("ALTER TABLE job_attempts ADD COLUMN total_tokens INTEGER")
    rebuild_run_events(tool_invocation=True)


def downgrade() -> None:
    """Restore the frozen 0006 schema, refusing to destroy Stage D state."""
    refuse_if_stage_d_state_exists()
    rebuild_run_events(tool_invocation=False)
    op.execute("ALTER TABLE job_attempts DROP COLUMN total_tokens")
    op.execute("ALTER TABLE job_attempts DROP COLUMN output_tokens")
    op.execute("ALTER TABLE job_attempts DROP COLUMN input_tokens")
    rebuild_runs(tool_limits=False)
    drop_stage_d_tables()
