"""Independent immutable hosted catalog foundation."""

from alembic import op

revision = "mp0001_catalog_foundation"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # This first revision owns a fixed four-table foundation. No application startup DDL.
    op.execute("""

CREATE TABLE artifacts (
    archive_sha256 VARCHAR(64) NOT NULL,
    size_bytes BIGINT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT pk_artifacts PRIMARY KEY (archive_sha256),
    CONSTRAINT ck_artifacts_digest CHECK (archive_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_artifacts_size_bound CHECK (size_bytes BETWEEN 1 AND 268435456)
)


    """)
    op.execute("""

CREATE TABLE package_projects (
    id UUID NOT NULL,
    package_id VARCHAR(128) COLLATE "C" NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT pk_package_projects PRIMARY KEY (id),
        CONSTRAINT ck_package_projects_bounded_identity CHECK (length(package_id) BETWEEN 1 AND
128 AND package_id NOT LIKE 'nervos.%'),
    CONSTRAINT uq_package_projects_package_id UNIQUE (package_id)
)


    """)
    op.execute("""

CREATE TABLE package_listings (
    project_id UUID NOT NULL,
    display_name TEXT NOT NULL,
    summary TEXT NOT NULL,
    description TEXT NOT NULL,
    revision INTEGER NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
        search_vector TSVECTOR GENERATED ALWAYS AS (setweight(to_tsvector('simple', display_name),
'A') || setweight(to_tsvector('simple', summary), 'B') || setweight(to_tsvector('simple',
description), 'C')) STORED NOT NULL,
    CONSTRAINT pk_package_listings PRIMARY KEY (project_id),
        CONSTRAINT ck_package_listings_display_bound CHECK (length(display_name) BETWEEN 1 AND 256
AND octet_length(display_name) <= 1024),
    CONSTRAINT ck_package_listings_summary_bound CHECK (octet_length(summary) <= 4096),
    CONSTRAINT ck_package_listings_description_bound CHECK (octet_length(description) <= 32768),
    CONSTRAINT ck_package_listings_revision_positive CHECK (revision > 0),
        CONSTRAINT fk_package_listings_project_id_package_projects FOREIGN KEY(project_id)
REFERENCES package_projects (id) ON DELETE RESTRICT
)


    """)
    op.execute("""

CREATE TABLE package_releases (
    id UUID NOT NULL,
    project_id UUID NOT NULL,
    exact_version VARCHAR(64) COLLATE "C" NOT NULL,
    archive_sha256 VARCHAR(64) NOT NULL,
    content_digest VARCHAR(64) NOT NULL,
    signer_fingerprint VARCHAR(64) NOT NULL,
    manifest_version INTEGER NOT NULL,
    manifest_bytes BYTEA NOT NULL,
    runtime_language VARCHAR(16) NOT NULL,
    runtime_python VARCHAR(64) NOT NULL,
    nervos_min_version VARCHAR(64) NOT NULL,
    nervos_max_version VARCHAR(64) NOT NULL,
    semver_key BYTEA NOT NULL,
    is_prerelease BOOLEAN NOT NULL,
    publication_state VARCHAR(16) NOT NULL,
    distribution_state VARCHAR(16) NOT NULL,
    status_revision INTEGER NOT NULL,
    published_at TIMESTAMP WITH TIME ZONE,
    status_updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT pk_package_releases PRIMARY KEY (id),
    CONSTRAINT uq_package_releases_project_id UNIQUE (project_id, exact_version),
    CONSTRAINT ck_package_releases_version_bound CHECK (length(exact_version) BETWEEN 1 AND 64),
        CONSTRAINT ck_package_releases_evidence_digests CHECK (content_digest ~ '^[0-9a-f]{64}$'
AND signer_fingerprint ~ '^[0-9a-f]{64}$'),
        CONSTRAINT ck_package_releases_manifest_bound CHECK (manifest_version = 1 AND
octet_length(manifest_bytes) BETWEEN 1 AND 1048576),
    CONSTRAINT ck_package_releases_order_bound CHECK
        (octet_length(semver_key) BETWEEN 193 AND 2304),
    CONSTRAINT ck_package_releases_publication CHECK (publication_state IN ('ready','published')),
        CONSTRAINT ck_package_releases_distribution CHECK (distribution_state IN
('available','yanked','revoked')),
    CONSTRAINT ck_package_releases_revision_positive CHECK (status_revision > 0),
        CONSTRAINT ck_package_releases_publication_timestamp CHECK ((publication_state =
'published') = (published_at IS NOT NULL)),
        CONSTRAINT fk_package_releases_project_id_package_projects FOREIGN KEY(project_id)
REFERENCES package_projects (id) ON DELETE RESTRICT,
    CONSTRAINT uq_package_releases_archive_sha256 UNIQUE (archive_sha256),
        CONSTRAINT fk_package_releases_archive_sha256_artifacts FOREIGN KEY(archive_sha256)
REFERENCES artifacts (archive_sha256) ON DELETE RESTRICT
)


    """)
    op.execute("""
CREATE INDEX ix_listing_search ON package_listings USING gin (search_vector)
    """)
    op.execute("""
CREATE INDEX ix_release_published_order ON package_releases (project_id, semver_key,
exact_version) WHERE publication_state = 'published'
    """)
    op.execute("""
CREATE INDEX ix_release_stable_order ON package_releases (project_id, semver_key, exact_version)
WHERE publication_state = 'published' AND distribution_state = 'available' AND is_prerelease =
false
    """)
    op.execute("""
    CREATE FUNCTION mp_guard_release() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN
        IF OLD.publication_state = 'published' THEN
          RAISE EXCEPTION 'published history is immutable';
        END IF;
        RETURN OLD;
      END IF;
      -- Serialize publication with project identity changes (future I2 writers).
      PERFORM id FROM package_projects WHERE id = NEW.project_id FOR UPDATE;
      IF TG_OP = 'UPDATE' THEN
        IF NEW.status_revision < OLD.status_revision OR
           NEW.status_updated_at < OLD.status_updated_at THEN
          RAISE EXCEPTION 'status evidence cannot move backwards';
        END IF;
        IF OLD.publication_state = 'published' AND
          (to_jsonb(OLD) - ARRAY['distribution_state','status_revision','status_updated_at'])
          IS DISTINCT FROM
          (to_jsonb(NEW) - ARRAY['distribution_state','status_revision','status_updated_at']) THEN
          RAISE EXCEPTION 'published evidence is immutable';
        END IF;
        IF OLD.distribution_state = 'revoked' AND NEW.distribution_state <> 'revoked' THEN
          RAISE EXCEPTION 'revocation is monotonic';
        END IF;
        IF OLD.distribution_state <> NEW.distribution_state AND
          (NEW.status_revision <> OLD.status_revision + 1 OR
           NEW.status_updated_at < OLD.status_updated_at) THEN
          RAISE EXCEPTION 'status revision must advance';
        END IF;
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER mp_release_guard BEFORE INSERT OR UPDATE OR DELETE ON package_releases
      FOR EACH ROW EXECUTE FUNCTION mp_guard_release();
    CREATE FUNCTION mp_guard_project() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF EXISTS(SELECT 1 FROM package_releases
        WHERE project_id = OLD.id AND publication_state = 'published') THEN
        IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'published project is retained'; END IF;
        IF NEW.id <> OLD.id OR NEW.package_id <> OLD.package_id THEN
          RAISE EXCEPTION 'published identity is immutable';
        END IF;
      END IF;
      IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER mp_project_guard BEFORE UPDATE OR DELETE ON package_projects
      FOR EACH ROW EXECUTE FUNCTION mp_guard_project();
    CREATE FUNCTION mp_guard_artifact() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'UPDATE' AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'artifact evidence is immutable';
      END IF;
      IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER mp_artifact_guard BEFORE UPDATE ON artifacts
      FOR EACH ROW EXECUTE FUNCTION mp_guard_artifact();
    """)


def downgrade() -> None:
    # Destructive: only disposable test databases or explicitly reviewed deployments.
    op.execute("""
DROP TABLE package_releases, package_listings, artifacts, package_projects
    """)
    for name in ("mp_guard_release", "mp_guard_project", "mp_guard_artifact"):
        op.execute(f"DROP FUNCTION {name}()")
