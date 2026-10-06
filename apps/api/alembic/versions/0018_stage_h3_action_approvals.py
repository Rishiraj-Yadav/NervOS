"""Stage H3: durable per-action approvals."""

import sqlalchemy as sa
from alembic import op

revision = "0018_stage_h3_action_approvals"
down_revision = "0017_stage_h2_account_connections"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "action_approvals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "agent_instance_id",
            sa.Integer(),
            sa.ForeignKey("agent_instances.id"),
            nullable=False,
        ),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("attempt_id", sa.Integer(), sa.ForeignKey("job_attempts.id"), nullable=False),
        sa.Column("tool_sequence", sa.Integer(), nullable=False),
        sa.Column(
            "tool_definition_id", sa.Integer(), sa.ForeignKey("tool_definitions.id"), nullable=False
        ),
        sa.Column("upstream_name", sa.String(128), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("input_digest", sa.String(64), nullable=False),
        sa.Column("preview_json", sa.Text(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("requested_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "state IN ('pending','approved','consumed','denied','expired','cancelled','revoked')",
            name="state_value",
        ),
        sa.CheckConstraint(
            "length(fingerprint) = 64 AND fingerprint NOT GLOB '*[^0-9a-f]*'",
            name="fingerprint_shape",
        ),
        sa.CheckConstraint(
            "length(input_digest) = 64 AND input_digest NOT GLOB '*[^0-9a-f]*'",
            name="input_digest_shape",
        ),
        sa.CheckConstraint("expires_at > requested_at", name="expiry_after_request"),
        sa.CheckConstraint(
            "(state = 'pending' AND decided_at IS NULL AND consumed_at IS NULL)"
            " OR (state = 'approved' AND decided_at IS NOT NULL AND consumed_at IS NULL)"
            " OR (state = 'consumed' AND decided_at IS NOT NULL AND consumed_at IS NOT NULL)"
            " OR (state IN ('denied','expired','cancelled','revoked') AND consumed_at IS NULL)",
            name="lifecycle_shape",
        ),
        sa.UniqueConstraint("attempt_id", "tool_sequence", name="attempt_sequence"),
    )
    op.create_index(
        "ix_action_approvals_owner_state", "action_approvals", ["owner_user_id", "state", "id"]
    )


def downgrade() -> None:
    raise RuntimeError(
        "downgrade of 0018_stage_h3_action_approvals is refused: it would destroy the durable "
        "audit evidence of per-action approvals."
    )
