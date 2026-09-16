# ruff: noqa: E501
"""stage c5 run cancellation lifecycle

Revision ID: 0005_stage_c5_run_cancellation
Revises: 0004_stage_c3_worker_registry
Create Date: 2026-09-16 06:00:00.000000

One responsibility, and no others: teach the frozen `runs` constraints that `cancelled` is a
distinct terminal lifecycle.

C1 already reserved every other cancellation fact -- `jobs.status`, `job_attempts.status`, the
`cancellation.requested` / `run.cancelled` event vocabulary, and the dormant
`jobs.cancel_requested_at` column with its `cancelled requires a request` CHECK. The Run status
vocabulary was the one thing deliberately withheld, and it is not representable without a
migration. So this migration rebuilds `runs` and nothing else: `jobs`, `job_attempts`, and
`run_events` are untouched, and no queue-partition concept is introduced.

A cancelled Run is a terminal lifecycle, not a euphemism for `failed`. It carries no error code,
no error message, and no usage, because refusing to continue is not a provider outcome. Two
shapes are legal:

* cancelled before execution began -- `started_at IS NULL`, `elapsed_ms IS NULL`;
* cancelled after it began -- the real `started_at` preserved, and `elapsed_ms` describing the
  truthful execution lifetime up to the durable cancellation, using the accepted C3 elapsed
  semantics rather than a fabricated provider duration.

SQLite cannot alter a CHECK constraint in place, so `runs` must be rebuilt. The repository's
Alembic harness installs `PRAGMA foreign_keys=ON` on every connection and wraps migrations in
`context.begin_transaction()`, which makes an in-transaction `PRAGMA foreign_keys=OFF` a silent
no-op -- and `jobs.run_id` / `run_events.run_id` are both RESTRICT children of `runs`, so a
rebuild's `DROP TABLE runs` would fail on any populated database. `autocommit_block()` commits
the surrounding transaction and runs the rebuild in autocommit, where the pragma genuinely
applies; a `PRAGMA foreign_key_check` afterwards proves integrity was preserved.

No execution row is rewritten semantically: `runs` rows are copied value-for-value with their
ids, timestamps, outputs, and usage intact.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005_stage_c5_run_cancellation"
down_revision: str | None = "0004_stage_c3_worker_registry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The exact blank-character set the Stage B `runs` CHECKs use for their non-blank assertions.
BLANK = (
    "char(9,10,11,12,13,32,133,160,5760,8192,8193,8194,8195,8196,8197,8198,8199,"
    "8200,8201,8202,8232,8233,8239,8287,12288)"
)

# The one infrastructure-owned code that licenses a `failed` Run with no start boundary.
EXHAUSTED = "'worker_recovery_exhausted'"

STAGE_B_STATUS = "'created','running','succeeded','failed'"

# The frozen Stage B expressions, reproduced byte-for-byte from migration 0002 so downgrade
# restores exactly what was there before.
STAGE_B_FINISHED_ORDER = (
    "finished_at IS NULL OR (started_at IS NOT NULL AND finished_at >= started_at)"
)
STAGE_B_LIFECYCLE_SHAPE = (
    "(status='created' AND started_at IS NULL AND finished_at IS NULL AND output_text IS NULL"
    " AND finish_reason IS NULL AND error_code IS NULL AND error_message IS NULL"
    " AND input_tokens IS NULL AND output_tokens IS NULL AND total_tokens IS NULL"
    " AND elapsed_ms IS NULL)"
    " OR (status='running' AND started_at IS NOT NULL AND finished_at IS NULL"
    " AND output_text IS NULL AND finish_reason IS NULL AND error_code IS NULL"
    " AND error_message IS NULL AND input_tokens IS NULL AND output_tokens IS NULL"
    " AND total_tokens IS NULL AND elapsed_ms IS NULL)"
    " OR (status='succeeded' AND started_at IS NOT NULL AND finished_at IS NOT NULL"
    f" AND output_text IS NOT NULL AND length(trim(output_text, {BLANK})) > 0"
    " AND error_code IS NULL AND error_message IS NULL AND elapsed_ms IS NOT NULL)"
    " OR (status='failed' AND started_at IS NOT NULL AND finished_at IS NOT NULL"
    " AND output_text IS NULL AND finish_reason IS NULL AND error_code IS NOT NULL"
    f" AND error_message IS NOT NULL AND length(trim(error_message, {BLANK})) > 0"
    " AND elapsed_ms IS NOT NULL)"
)

# The C3 expressions, reproduced byte-for-byte from migration 0004 so downgrade restores the
# accepted C3 state exactly.
C3_FINISHED_ORDER = (
    "finished_at IS NULL"
    " OR (started_at IS NOT NULL AND finished_at >= started_at)"
    f" OR (started_at IS NULL AND error_code = {EXHAUSTED} AND finished_at >= created_at)"
)
C3_LIFECYCLE_SHAPE = (
    "(status='created' AND started_at IS NULL AND finished_at IS NULL AND output_text IS NULL"
    " AND finish_reason IS NULL AND error_code IS NULL AND error_message IS NULL"
    " AND input_tokens IS NULL AND output_tokens IS NULL AND total_tokens IS NULL"
    " AND elapsed_ms IS NULL)"
    " OR (status='running' AND started_at IS NOT NULL AND finished_at IS NULL"
    " AND output_text IS NULL AND finish_reason IS NULL AND error_code IS NULL"
    " AND error_message IS NULL AND input_tokens IS NULL AND output_tokens IS NULL"
    " AND total_tokens IS NULL AND elapsed_ms IS NULL)"
    " OR (status='succeeded' AND started_at IS NOT NULL AND finished_at IS NOT NULL"
    f" AND output_text IS NOT NULL AND length(trim(output_text, {BLANK})) > 0"
    " AND error_code IS NULL AND error_message IS NULL AND elapsed_ms IS NOT NULL)"
    " OR (status='failed' AND started_at IS NOT NULL AND finished_at IS NOT NULL"
    " AND output_text IS NULL AND finish_reason IS NULL AND error_code IS NOT NULL"
    f" AND error_code <> {EXHAUSTED} AND error_message IS NOT NULL"
    f" AND length(trim(error_message, {BLANK})) > 0 AND elapsed_ms IS NOT NULL)"
    " OR (status='failed' AND started_at IS NULL AND finished_at IS NOT NULL"
    " AND output_text IS NULL AND finish_reason IS NULL"
    f" AND error_code = {EXHAUSTED} AND error_message IS NOT NULL"
    f" AND length(trim(error_message, {BLANK})) > 0"
    " AND input_tokens IS NULL AND output_tokens IS NULL AND total_tokens IS NULL"
    " AND elapsed_ms IS NULL)"
)

C5_STATUS = "'created','running','succeeded','failed','cancelled'"

# The evolved expressions. The five C3 branches are unchanged; the finished-order gain admits a
# never-started Run that follows its creation, extended only far enough to cover cancellation.
C5_FINISHED_ORDER = (
    "finished_at IS NULL"
    " OR (started_at IS NOT NULL AND finished_at >= started_at)"
    " OR (started_at IS NULL AND finished_at >= created_at"
    f" AND (error_code = {EXHAUSTED} OR status = 'cancelled'))"
)
# Two new branches. A cancelled Run never carries output, a finish reason, a provider error, or
# usage -- and the pair of branches keeps `elapsed_ms` tied to whether a start boundary exists,
# in both directions, so a pre-start cancellation can never carry a duration and a post-start
# cancellation can never omit one.
C5_LIFECYCLE_SHAPE = (
    C3_LIFECYCLE_SHAPE
    + " OR (status='cancelled' AND started_at IS NOT NULL AND finished_at IS NOT NULL"
    " AND output_text IS NULL AND finish_reason IS NULL AND error_code IS NULL"
    " AND error_message IS NULL AND input_tokens IS NULL AND output_tokens IS NULL"
    " AND total_tokens IS NULL AND elapsed_ms IS NOT NULL)"
    " OR (status='cancelled' AND started_at IS NULL AND finished_at IS NOT NULL"
    " AND output_text IS NULL AND finish_reason IS NULL AND error_code IS NULL"
    " AND error_message IS NULL AND input_tokens IS NULL AND output_tokens IS NULL"
    " AND total_tokens IS NULL AND elapsed_ms IS NULL)"
)

# Explicit column list: the rebuild copies values, never re-derives them.
RUN_COLUMNS = (
    "id, agent_instance_id, status, agent_key, agent_definition_version, model_provider,"
    " model_name, input_text, input_max_bytes, input_max_code_points, output_max_bytes,"
    " output_max_code_points, provider_timeout_ms, max_output_tokens, max_model_calls,"
    " output_text, finish_reason, error_code, error_message, input_tokens, output_tokens,"
    " total_tokens, elapsed_ms, created_at, started_at, finished_at"
)


def runs_ddl(table: str, status: str, lifecycle_shape: str, finished_order: str) -> str:
    """Build the `runs` DDL for `table` with the given status list and lifecycle expressions.

    Constraint names are hardcoded to the `ck_runs_*` set so they survive the rename and keep
    matching the ORM's naming convention.
    """
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
    max_model_calls INTEGER NOT NULL,
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
    CONSTRAINT ck_runs_status_value CHECK (status IN ({status})),
    CONSTRAINT ck_runs_limits_positive CHECK (input_max_bytes > 0 AND input_max_code_points > 0 AND output_max_bytes > 0 AND output_max_code_points > 0 AND provider_timeout_ms > 0 AND max_output_tokens > 0 AND max_model_calls > 0),
    CONSTRAINT ck_runs_input_bounds CHECK (length(input_text) BETWEEN 1 AND input_max_code_points AND length(CAST(input_text AS BLOB)) <= input_max_bytes AND instr(input_text, char(0)) = 0 AND length(trim(input_text, {BLANK})) > 0),
    CONSTRAINT ck_runs_output_bounds CHECK (output_text IS NULL OR (length(output_text) BETWEEN 1 AND output_max_code_points AND length(CAST(output_text AS BLOB)) <= output_max_bytes AND instr(output_text, char(0)) = 0)),
    CONSTRAINT ck_runs_usage_nonnegative CHECK ((input_tokens IS NULL OR input_tokens >= 0) AND (output_tokens IS NULL OR output_tokens >= 0) AND (total_tokens IS NULL OR total_tokens >= 0)),
    CONSTRAINT ck_runs_elapsed_nonnegative CHECK (elapsed_ms IS NULL OR elapsed_ms >= 0),
    CONSTRAINT ck_runs_started_order CHECK (started_at IS NULL OR started_at >= created_at),
    CONSTRAINT ck_runs_finished_order CHECK ({finished_order}),
    CONSTRAINT ck_runs_lifecycle_shape CHECK ({lifecycle_shape}),
    CONSTRAINT fk_runs_agent_instance_id_agent_instances FOREIGN KEY(agent_instance_id) REFERENCES agent_instances (id) ON DELETE RESTRICT
)
"""


def rebuild_runs(status: str, lifecycle_shape: str, finished_order: str) -> None:
    """Rebuild `runs` outside the harness transaction so the FK pragma actually applies.

    The rebuild must run in autocommit: the harness wraps migrations in
    `context.begin_transaction()`, and SQLite ignores `PRAGMA foreign_keys` inside a
    transaction, so an in-transaction disable is a silent no-op and `DROP TABLE runs` fails
    against its two RESTRICT children (`jobs.run_id`, `run_events.run_id`).
    """
    with op.get_context().autocommit_block():
        op.execute("PRAGMA foreign_keys=OFF")
        op.execute(runs_ddl("runs_old", status, lifecycle_shape, finished_order))
        op.execute(f"INSERT INTO runs_old ({RUN_COLUMNS}) SELECT {RUN_COLUMNS} FROM runs")
        op.execute("DROP TABLE runs")
        op.execute("ALTER TABLE runs_old RENAME TO runs")
        op.execute("CREATE INDEX ix_runs_agent_instance_id_id ON runs (agent_instance_id, id)")
        op.execute("PRAGMA foreign_keys=ON")
    violations = op.get_bind().exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError(
            "0005 preserved no integrity: foreign_key_check reported"
            f" {len(violations)} violation(s)"
        )


def upgrade() -> None:
    """Admit `cancelled` as a distinct terminal Run lifecycle."""
    rebuild_runs(C5_STATUS, C5_LIFECYCLE_SHAPE, C5_FINISHED_ORDER)


def downgrade() -> None:
    """Restore the frozen C3 `runs` constraints.

    A cancelled Run has no representation under the C3 CHECK, and rewriting one as `failed`
    would fabricate a provider error the user never had. The downgrade refuses instead of
    silently rewriting user history.
    """
    stranded = (
        op.get_bind().exec_driver_sql("SELECT count(*) FROM runs WHERE status='cancelled'").scalar()
    )
    if stranded:
        raise RuntimeError(
            f"cannot downgrade: {stranded} cancelled Run(s) have no C3 representation;"
            " resolve them before downgrading"
        )
    rebuild_runs(STAGE_B_STATUS, C3_LIFECYCLE_SHAPE, C3_FINISHED_ORDER)
