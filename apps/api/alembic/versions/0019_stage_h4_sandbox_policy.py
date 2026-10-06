"""Stage H4: sandbox adoption marker (resource policy is code-level, mirroring C6)."""

revision = "0019_stage_h4_sandbox_policy"
down_revision = "0018_stage_h3_action_approvals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # H4's resource limits are a code-level policy (ADR 0035), deliberately not environment
    # settings and not schema: each Worker that could diverge would raise effective
    # containment. The migration exists so the schema-revision guard pins a Worker that
    # actually carries the containment adapter, exactly as C6 pinned the concurrency kernel.
    pass


def downgrade() -> None:
    # A no-op marker may be safely reversed; no durable state exists to destroy.
    pass
