"""Stage H2: account connections for credential brokering."""

import sqlalchemy as sa
from alembic import op

revision = "0017_stage_h2_account_connections"
down_revision = "0016_stage_h1_secret_manager"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "account_connections",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("secret_id", sa.Integer(), sa.ForeignKey("secrets.id"), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("scopes_json", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("refresh_failed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "state IN ('connected','needs_refresh','disconnected','revoked')",
            name="state_value",
        ),
        sa.CheckConstraint("length(provider) BETWEEN 1 AND 64", name="provider_bound"),
        sa.CheckConstraint("length(scopes_json) <= 4096", name="scopes_bound"),
        sa.CheckConstraint("length(display_name) BETWEEN 1 AND 128", name="display_name_bound"),
    )
    op.create_index(
        "ix_account_connections_owner_id", "account_connections", ["owner_user_id", "id"]
    )


def downgrade() -> None:
    raise RuntimeError(
        "downgrade of 0017_stage_h2_account_connections is refused: it would destroy "
        "account-connection evidence and strand stored credentials."
    )
