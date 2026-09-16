"""stage c6 queue partitions for durable least-recently-served fairness

Revision ID: 0006_stage_c6_queue_partitions
Revises: 0005_stage_c5_run_cancellation
Create Date: 2026-09-16 09:00:00.000000

One responsibility, and no others: add the minimal durable fairness metadata that lets every
Worker choose between independent Agent-Instance backlogs identically.

Why durable state is required at all. C6's fairness guarantee is that a continuously-eligible
Agent partition is served within a bounded number of claims. A Worker's *eligible* set is not the
same as another Worker's: a Worker that supports only `openai` cannot see an `anthropic`-only
partition. Any cursor derived from the most recent global Attempt therefore orders a Worker
against a partition it cannot run, and a continuously-eligible partition can starve -- the
external review produced exactly that counterexample (Worker X eligible={55}, Worker Y
eligible={50,60}: Y always computes "the first partition after 55" and never reaches 50). The
ordering fact must instead be carried per Agent partition, in the database, so that all Workers
read the same history.

So this migration creates exactly one table holding exactly one scheduling fact. It is not a
queue, not a copy of Job state, not execution authority, and not an active/pending count:
`jobs` remains the only durable execution obligation, and the Job lease plus Attempt token remain
the only authority to execute. No counter is stored, because a counter can drift and this cannot.

`last_served_attempt_id` stores the monotonically increasing `job_attempts.id` of the most recent
committed claim for that partition, and is NULL for a partition that has never been served. It
carries no foreign key deliberately: it is a sequence marker that is only compared, never
dereferenced, so correctness does not depend on the referenced Attempt still being meaningful,
and an FK would add a RESTRICT edge that made attempt-history pruning impossible for no scheduling
benefit.

Backfill is deterministic and touches no execution row: one Agent partition row is created for
every Agent Instance that already has Jobs, with the marker taken from that Instance's highest
existing Attempt id, or NULL when it has never been claimed. Partitions with no Jobs are not
created; selection treats an absent row as never served.

No execution authority, Job state, Attempt state, Run state, or Run Event vocabulary changes, and
`jobs`, `job_attempts`, `run_events`, `runs`, and `workers` are untouched. Downgrade drops this
table and rewrites no history: the metadata is derived, so a later re-upgrade reconstructs it
from the same deterministic backfill.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_stage_c6_queue_partitions"
down_revision: str | None = "0005_stage_c5_run_cancellation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# One row per Agent Instance partition that has ever held work. The marker is this Instance's
# highest committed Attempt id, or NULL when it has never been served.
BACKFILL = """
INSERT INTO queue_partitions (agent_instance_id, last_served_attempt_id)
SELECT jobs.agent_instance_id, MAX(job_attempts.id)
FROM jobs
LEFT JOIN job_attempts ON job_attempts.job_id = jobs.id
GROUP BY jobs.agent_instance_id
"""


def upgrade() -> None:
    op.create_table(
        "queue_partitions",
        sa.Column("agent_instance_id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("last_served_attempt_id", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "agent_instance_id > 0", name=op.f("ck_queue_partitions_agent_instance_positive")
        ),
        sa.CheckConstraint(
            "last_served_attempt_id IS NULL OR last_served_attempt_id > 0",
            name=op.f("ck_queue_partitions_last_served_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["agent_instance_id"],
            ["agent_instances.id"],
            name=op.f("fk_queue_partitions_agent_instance_id_agent_instances"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("agent_instance_id", name=op.f("pk_queue_partitions")),
    )
    op.execute(BACKFILL)


def downgrade() -> None:
    # Safe to drop without refusal, unlike 0004/0005: this is derived scheduling metadata, so
    # removing it rewrites no execution history. Re-upgrading reconstructs it from BACKFILL,
    # which reads only Attempt and Job rows that this migration never modifies.
    op.drop_table("queue_partitions")
