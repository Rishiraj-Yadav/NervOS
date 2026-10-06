"""Owner-bound single-use account OAuth state with encrypted PKCE secret references."""

import sqlalchemy as sa
from alembic import op

revision = "0022_stage_h_account_oauth"
down_revision = "0021_stage_h_approval_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "account_connections",
        sa.Column("refresh_revision", sa.Integer, nullable=False, server_default="0"),
    )
    op.add_column(
        "account_connections", sa.Column("refresh_started_at", sa.DateTime, nullable=True)
    )
    op.add_column(
        "account_connections", sa.Column("revocation_outcome", sa.String(24), nullable=True)
    )
    op.create_table(
        "account_oauth_requests",
        sa.Column("state_hash", sa.String(64), primary_key=True),
        sa.Column("owner_user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("secret_id", sa.Integer, sa.ForeignKey("secrets.id"), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("scopes_json", sa.Text, nullable=False),
        sa.Column("expires_at", sa.DateTime, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("used_at", sa.DateTime, nullable=True),
        sa.CheckConstraint(
            "length(state_hash) = 64 AND state_hash NOT GLOB '*[^0-9a-f]*'", name="state_hash_shape"
        ),
        sa.CheckConstraint("expires_at > created_at", name="expiry_after_creation"),
    )
    op.create_index(
        "ix_account_oauth_requests_owner_expiry",
        "account_oauth_requests",
        ["owner_user_id", "expires_at"],
    )


def downgrade() -> None:
    raise RuntimeError(
        "downgrade is refused: it would remove account authorization replay evidence"
    )
