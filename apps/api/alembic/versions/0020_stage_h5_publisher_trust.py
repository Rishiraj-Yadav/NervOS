"""Stage H4/H5: local publisher trust store and sandbox metadata."""

import sqlalchemy as sa
from alembic import op

revision = "0020_stage_h5_publisher_trust"
down_revision = "0019_stage_h4_sandbox_policy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # H4 ships its limits as code-level policy (the frozen Stage C/C6 precedent for
    # concurrency): divergent per-Worker configuration would raise effective containment.
    # This migration therefore carries only H5's durable trust decision.
    op.create_table(
        "publisher_trust",
        sa.Column("signer_fingerprint", sa.String(64), primary_key=True),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("decided_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("reason", sa.String(512), nullable=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("state IN ('trusted','untrusted','revoked')", name="state_value"),
        sa.CheckConstraint(
            "length(signer_fingerprint) = 64 AND signer_fingerprint NOT GLOB '*[^0-9a-f]*'",
            name="fingerprint_shape",
        ),
        sa.CheckConstraint("length(reason) <= 512", name="reason_bound"),
        sa.UniqueConstraint("signer_fingerprint", name="signer_identity"),
    )


def downgrade() -> None:
    raise RuntimeError(
        "downgrade of 0020_stage_h5_publisher_trust is refused: it would erase the durable "
        "record of publisher trust and revocation decisions."
    )
