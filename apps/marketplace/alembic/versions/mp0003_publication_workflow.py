"""Publication workflow and explicit ownership/attribution contraction."""

from alembic import op
from sqlalchemy import text

revision = "mp0003_publication_workflow"
down_revision = "mp0002_publisher_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    missing = (
        op.get_bind()
        .execute(
            text("""SELECT
      EXISTS(SELECT 1 FROM package_projects WHERE publisher_id IS NULL) OR
      EXISTS(SELECT 1 FROM package_releases WHERE original_publisher_id IS NULL)""")
        )
        .scalar_one()
    )
    if missing:
        raise RuntimeError(
            "Explicit project ownership AND historical release attribution mapping "
            "required: upgrade to mp0002, run maintenance reconcile-ownership, "
            "then upgrade to mp0003"
        )
    op.execute("""
      ALTER TABLE artifacts ADD COLUMN storage_version_id VARCHAR(1024),
        ADD CONSTRAINT ck_artifacts_storage_version CHECK(storage_version_id IS NULL OR
          (storage_version_id <> 'null' AND length(storage_version_id) BETWEEN 1 AND 1024
           AND storage_version_id ~ '^[A-Za-z0-9._~+/=-]+$'));
      ALTER TABLE package_projects ALTER COLUMN publisher_id SET NOT NULL;
      ALTER TABLE package_releases ALTER COLUMN original_publisher_id SET NOT NULL;
      CREATE TABLE upload_operations (
        id UUID PRIMARY KEY, actor_id UUID NOT NULL REFERENCES marketplace_accounts(id)
          ON DELETE RESTRICT, publisher_id UUID NOT NULL
          REFERENCES publishers(id) ON DELETE RESTRICT,
        project_id UUID NOT NULL REFERENCES package_projects(id) ON DELETE RESTRICT,
        ownership_revision INTEGER NOT NULL CHECK(ownership_revision>0),
        archive_sha256 VARCHAR(64) NOT NULL CHECK(archive_sha256 ~ '^[0-9a-f]{64}$'),
        size_bytes BIGINT NOT NULL CHECK(size_bytes BETWEEN 1 AND 268435456),
        quarantine_key VARCHAR(256) NOT NULL UNIQUE,
        state VARCHAR(16) NOT NULL CHECK(state IN
          ('uploading','uploaded','verifying','verified','rejected','failed','expired')),
        failure_code VARCHAR(64), attempt_count INTEGER NOT NULL DEFAULT 0
          CHECK(attempt_count BETWEEN 0 AND 3),
        generation INTEGER NOT NULL DEFAULT 0 CHECK(generation>=0), lease_token UUID,
        lease_expires_at TIMESTAMPTZ, upload_token UUID, upload_expires_at TIMESTAMPTZ,
        ready_release_id UUID REFERENCES package_releases(id) ON DELETE RESTRICT,
        created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
        expires_at TIMESTAMPTZ NOT NULL, credential_hash VARCHAR(64) NOT NULL
          REFERENCES cli_credentials(token_hash) ON DELETE RESTRICT);
      CREATE INDEX ix_upload_claim ON upload_operations(state,created_at,id);
      CREATE INDEX ix_upload_expiry ON upload_operations(expires_at);
      CREATE INDEX ix_upload_actor ON upload_operations(actor_id,created_at);
      CREATE TABLE verification_admission (
        id INTEGER PRIMARY KEY CHECK(id=1), operation_id UUID, generation INTEGER,
        expires_at TIMESTAMPTZ);
      INSERT INTO verification_admission(id) VALUES(1);
      CREATE TABLE publication_idempotency (
        actor_id UUID NOT NULL REFERENCES marketplace_accounts(id) ON DELETE RESTRICT,
        action VARCHAR(64) NOT NULL, target VARCHAR(256) NOT NULL, key VARCHAR(128) NOT NULL,
        request_digest VARCHAR(64) NOT NULL CHECK(request_digest ~ '^[0-9a-f]{64}$'),
        result_id UUID NOT NULL, expires_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY(actor_id,action,target,key));
      CREATE TABLE release_status_advisories (
        release_id UUID NOT NULL REFERENCES package_releases(id) ON DELETE RESTRICT,
        revision INTEGER NOT NULL CHECK(revision>0),
        state VARCHAR(16) NOT NULL CHECK(state IN ('available','yanked','revoked')),
        reason TEXT NOT NULL CHECK(octet_length(reason) BETWEEN 1 AND 4096),
        created_at TIMESTAMPTZ NOT NULL, PRIMARY KEY(release_id,revision));
      CREATE TRIGGER mp_advisory_guard BEFORE UPDATE OR DELETE OR TRUNCATE
        ON release_status_advisories FOR EACH STATEMENT EXECUTE FUNCTION mp_append_only();
      CREATE OR REPLACE FUNCTION mp_guard_release() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF TG_OP='DELETE' THEN RAISE EXCEPTION 'release reservation is permanent'; END IF;
        IF TG_OP='INSERT' OR (TG_OP='UPDATE' AND OLD.publication_state='ready'
                             AND NEW.publication_state='published') THEN
          IF NOT EXISTS(SELECT 1 FROM artifacts WHERE archive_sha256=NEW.archive_sha256
                        AND storage_version_id IS NOT NULL) THEN
            RAISE EXCEPTION 'new publication requires exact storage version'; END IF;
        END IF;
        PERFORM id FROM package_projects WHERE id=NEW.project_id FOR UPDATE;
        IF TG_OP='UPDATE' THEN
          IF (to_jsonb(OLD)-ARRAY['publication_state','published_at','distribution_state',
              'status_revision','status_updated_at']) IS DISTINCT FROM
             (to_jsonb(NEW)-ARRAY['publication_state','published_at','distribution_state',
              'status_revision','status_updated_at']) THEN
            RAISE EXCEPTION 'release evidence is immutable'; END IF;
          IF OLD.publication_state='published' AND
             (NEW.publication_state<>'published' OR NEW.published_at<>OLD.published_at) THEN
            RAISE EXCEPTION 'publication is permanent'; END IF;
          IF OLD.distribution_state='revoked' AND NEW.distribution_state<>'revoked' THEN
            RAISE EXCEPTION 'revocation is monotonic'; END IF;
          IF NEW.status_revision<OLD.status_revision OR NEW.status_updated_at<OLD.status_updated_at
            OR (OLD.distribution_state<>NEW.distribution_state AND
                NEW.status_revision<>OLD.status_revision+1) THEN
            RAISE EXCEPTION 'status evidence must advance'; END IF;
        END IF;
        RETURN NEW;
      END $$;
    """)


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(
            text(
                "SELECT EXISTS(SELECT 1 FROM upload_operations) OR "
                "EXISTS(SELECT 1 FROM package_releases)"
            )
        )
        .scalar_one()
    ):
        raise RuntimeError("Publication downgrade requires an empty disposable database")
    op.execute("""
      DROP TABLE release_status_advisories,publication_idempotency,verification_admission,
        upload_operations;
      ALTER TABLE package_projects ALTER COLUMN publisher_id DROP NOT NULL;
      ALTER TABLE package_releases ALTER COLUMN original_publisher_id DROP NOT NULL;
      ALTER TABLE artifacts DROP CONSTRAINT ck_artifacts_storage_version,
        DROP COLUMN storage_version_id;
    """)
    # Preserve the stronger reservation guard during an empty-database downgrade.
