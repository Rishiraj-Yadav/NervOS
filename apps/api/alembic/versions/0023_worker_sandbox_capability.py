"""Worker-observed package-sandbox capability projection (ADR 0037/0038)."""

import sqlalchemy as sa
from alembic import op

revision = "0023_worker_sandbox_capability"
down_revision = "0022_stage_h_account_oauth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("workers") as batch:
        batch.add_column(sa.Column("platform", sa.String(32), nullable=True))
        batch.add_column(sa.Column("sandbox_backend", sa.String(64), nullable=True))
        batch.add_column(
            sa.Column(
                "package_execution_supported",
                sa.Boolean,
                nullable=False,
                server_default=sa.text("0"),
            )
        )


def downgrade() -> None:
    raise RuntimeError("downgrade is refused: it would drop observed Worker capability evidence")
