"""Stage I4 retained exact Marketplace install requests.

The request records the remote observation and local approval boundary. It does
not replace the Stage-G package registry or installer.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_stage_i4_marketplace_install_requests"
down_revision = "0013_stage_g3_package_registry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "marketplace_install_requests",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("marketplace_origin", sa.String(512), nullable=False),
        sa.Column("package_id", sa.String(128), nullable=False),
        sa.Column("package_version", sa.String(64), nullable=False),
        sa.Column("expected_size_bytes", sa.Integer(), nullable=False),
        sa.Column("expected_archive_sha256", sa.String(64), nullable=False),
        sa.Column("expected_content_digest", sa.String(64), nullable=False),
        sa.Column("expected_signer_fingerprint", sa.String(64), nullable=False),
        sa.Column("observed_status_revision", sa.Integer(), nullable=False),
        sa.Column("observed_distribution_state", sa.String(16), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="created"),
        sa.Column("artifact_path", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=False), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=False), nullable=False),
        sa.CheckConstraint(
            "state IN ('created','downloaded','approved','installed','failed')",
            name="state_value",
        ),
        sa.CheckConstraint("length(package_id) BETWEEN 1 AND 128", name="package_id_bound"),
        sa.CheckConstraint("length(package_version) BETWEEN 1 AND 64", name="version_bound"),
        sa.CheckConstraint("observed_status_revision > 0", name="revision_positive"),
        sa.CheckConstraint("expected_size_bytes >= 0", name="size_nonnegative"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], name="owner_user"),
    )
    op.create_index(
        "ix_marketplace_install_requests_owner",
        "marketplace_install_requests",
        ["owner_user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_marketplace_install_requests_owner", table_name="marketplace_install_requests"
    )
    op.drop_table("marketplace_install_requests")
