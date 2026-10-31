"""Process-kind package install (ADR 0046).

Revises: 0024_durable_workflows
Revised by: (none yet)

Adds ``runtime_kind`` to ``installed_package_versions``, makes the entrypoint
columns nullable under a kind/entrypoint pairing CHECK, and refuses to downgrade
while any process-kind row exists. Python-kind rows and rules are unchanged.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0025_process_kind_packages"
down_revision = "0024_durable_workflows"
branch_labels = None
depends_on = None

_PAIRING_CHECK = (
    "(runtime_kind = 'python' AND entrypoint_module IS NOT NULL AND "
    "entrypoint_object IS NOT NULL) OR "
    "(runtime_kind = 'process' AND entrypoint_module IS NULL AND "
    "entrypoint_object IS NULL)"
)


def _assert_no_process_rows() -> None:
    connection = op.get_bind()
    process_rows = connection.execute(
        sa.text("SELECT count(*) FROM installed_package_versions WHERE runtime_kind = 'process'")
    ).scalar_one()
    if process_rows:
        raise RuntimeError(
            "0025 downgrade refuses to run: "
            f"{process_rows} process-kind installed package row(s) exist and the "
            "pre-0025 schema cannot represent them. Remove them first."
        )


def upgrade() -> None:
    with op.batch_alter_table("installed_package_versions", recreate="always") as batch:
        batch.add_column(
            sa.Column(
                "runtime_kind",
                sa.String(8),
                nullable=False,
                server_default="python",
            )
        )
        batch.alter_column(
            "entrypoint_module",
            existing_type=sa.String(256),
            nullable=True,
        )
        batch.alter_column(
            "entrypoint_object",
            existing_type=sa.String(256),
            nullable=True,
        )
        batch.create_check_constraint(
            "runtime_kind_value",
            "runtime_kind IN ('python','process')",
        )
        batch.create_check_constraint(
            "runtime_entrypoint_pairing",
            _PAIRING_CHECK,
        )


def downgrade() -> None:
    _assert_no_process_rows()
    with op.batch_alter_table("installed_package_versions", recreate="always") as batch:
        batch.drop_constraint("runtime_entrypoint_pairing", type_="check")
        batch.drop_constraint("runtime_kind_value", type_="check")
        batch.alter_column(
            "entrypoint_module",
            existing_type=sa.String(256),
            nullable=False,
        )
        batch.alter_column(
            "entrypoint_object",
            existing_type=sa.String(256),
            nullable=False,
        )
        batch.drop_column("runtime_kind")
