"""Stage-H repair: credential-free approval request and decision timeline events."""

from alembic import op

revision = "0021_stage_h_approval_events"
down_revision = "0020_stage_h5_publisher_trust"
branch_labels = None
depends_on = None

EVENTS = (
    "run.created",
    "run.queued",
    "attempt.claimed",
    "attempt.started",
    "attempt.failed",
    "attempt.expired",
    "retry.scheduled",
    "cancellation.requested",
    "run.cancelled",
    "run.succeeded",
    "run.failed",
    "recovery.pre_start",
    "recovery.ambiguous",
    "tool.requested",
    "tool.started",
    "tool.succeeded",
    "tool.failed",
    "tool.denied",
    "tool.ambiguous",
    "tool.approval_requested",
    "tool.approval_decided",
)


def upgrade() -> None:
    vocabulary = ",".join(f"'{event}'" for event in EVENTS)
    with op.batch_alter_table("run_events", recreate="always") as batch:
        batch.drop_constraint(op.f("ck_run_events_event_type_value"), type_="check")
        batch.create_check_constraint(
            op.f("ck_run_events_event_type_value"), f"event_type IN ({vocabulary})"
        )


def downgrade() -> None:
    raise RuntimeError("downgrade is refused: it would remove approval audit vocabulary")
