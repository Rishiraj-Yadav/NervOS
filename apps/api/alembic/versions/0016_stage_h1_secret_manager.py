"""Stage H1: encrypted secret manager tables."""

import sqlalchemy as sa
from alembic import op

revision = "0016_stage_h1_secret_manager"
down_revision = "0015_runtime_integration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "secrets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("provider_hint", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(16), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("rotation_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("owner_user_id", "name", name="owner_secret_name"),
        sa.CheckConstraint("status IN ('active','disabled','revoked')", name="status_value"),
        sa.CheckConstraint("length(name) BETWEEN 1 AND 128", name="name_bound"),
        sa.CheckConstraint("key_version >= 1", name="key_version_positive"),
        sa.CheckConstraint("rotation_count >= 0", name="rotation_nonnegative"),
        sa.CheckConstraint(
            "(status = 'revoked' AND length(CAST(ciphertext AS BLOB)) = 0)"
            " OR (status != 'revoked' AND length(CAST(ciphertext AS BLOB)) > 0)",
            name="revocation_destroys_value",
        ),
    )
    op.create_index("ix_secrets_owner_id", "secrets", ["owner_user_id", "id"])
    op.create_table(
        "secret_keys",
        sa.Column("key_version", sa.Integer(), primary_key=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    # Stage H refuses a downgrade that would destroy security evidence: dropping the secret
    # tables discards every stored secret irrecoverably (and would strand any account
    # connection that references one). Refuse rather than destroy, per the H1 closeout rule.
    raise RuntimeError(
        "downgrade of 0016_stage_h1_secret_manager is refused: it would irreversibly destroy "
        "encrypted secrets. Restore from backup instead."
    )
