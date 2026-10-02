"""Hosted identity expansion. Existing projects/releases require explicit attribution."""

from alembic import op
from sqlalchemy import text

revision = "mp0002_publisher_identity"
down_revision = "mp0001_catalog_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE marketplace_accounts (
      id UUID PRIMARY KEY, state VARCHAR(16) NOT NULL CHECK(state IN
        ('active','disabled','tombstoned')), created_at TIMESTAMPTZ NOT NULL);
    CREATE TABLE external_identities (
      id UUID PRIMARY KEY, account_id UUID NOT NULL REFERENCES marketplace_accounts(id)
        ON DELETE RESTRICT, issuer TEXT COLLATE "C" NOT NULL CHECK(octet_length(issuer)<=2048),
      subject TEXT COLLATE "C" NOT NULL CHECK(octet_length(subject) BETWEEN 1 AND 512),
      created_at TIMESTAMPTZ NOT NULL, UNIQUE(issuer,subject));
    CREATE TABLE publishers (
      id UUID PRIMARY KEY, handle VARCHAR(63) COLLATE "C" UNIQUE NOT NULL
        CHECK(handle ~ '^[a-z][a-z0-9-]{1,61}[a-z0-9]$'
          AND handle NOT IN ('nervos','admin','system','operator','marketplace')),
      display_name TEXT NOT NULL CHECK(length(display_name) BETWEEN 1 AND 256
        AND octet_length(display_name)<=1024),
      kind VARCHAR(16) NOT NULL CHECK(kind IN ('individual','organization')),
      state VARCHAR(16) NOT NULL CHECK(state IN ('pending','active','suspended','tombstoned')),
      revision INTEGER NOT NULL CHECK(revision>0), created_at TIMESTAMPTZ NOT NULL,
      updated_at TIMESTAMPTZ NOT NULL);
    CREATE TABLE publisher_memberships (
      publisher_id UUID NOT NULL REFERENCES publishers(id) ON DELETE RESTRICT,
      account_id UUID NOT NULL REFERENCES marketplace_accounts(id) ON DELETE RESTRICT,
      role VARCHAR(16) NOT NULL CHECK(role IN ('owner','maintainer','publisher')),
      state VARCHAR(16) NOT NULL CHECK(state IN ('active','removed')),
      created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
      PRIMARY KEY(publisher_id,account_id));
    CREATE TABLE marketplace_operator_grants (
      account_id UUID PRIMARY KEY REFERENCES marketplace_accounts(id) ON DELETE RESTRICT,
      active BOOLEAN NOT NULL, created_at TIMESTAMPTZ NOT NULL);
    CREATE TABLE hosted_sessions (
      token_hash VARCHAR(64) PRIMARY KEY CHECK(token_hash ~ '^[0-9a-f]{64}$'),
      account_id UUID NOT NULL REFERENCES marketplace_accounts(id) ON DELETE RESTRICT,
      authenticated_at TIMESTAMPTZ NOT NULL, acr TEXT, amr JSONB NOT NULL
        CHECK(jsonb_typeof(amr)='array' AND octet_length(amr::text)<=1024),
      csrf_hash VARCHAR(64) NOT NULL, created_at TIMESTAMPTZ NOT NULL,
      expires_at TIMESTAMPTZ NOT NULL, idle_expires_at TIMESTAMPTZ NOT NULL,
      revoked BOOLEAN NOT NULL DEFAULT false);
    CREATE TABLE cli_credentials (
      token_hash VARCHAR(64) PRIMARY KEY CHECK(token_hash ~ '^[0-9a-f]{64}$'),
      account_id UUID NOT NULL REFERENCES marketplace_accounts(id) ON DELETE RESTRICT,
      authenticated_at TIMESTAMPTZ NOT NULL, acr TEXT, amr JSONB NOT NULL
        CHECK(jsonb_typeof(amr)='array' AND octet_length(amr::text)<=1024),
      scope VARCHAR(64) NOT NULL CHECK(scope IN ('publisher','operator')),
      publisher_id UUID REFERENCES publishers(id) ON DELETE RESTRICT,
      created_at TIMESTAMPTZ NOT NULL, expires_at TIMESTAMPTZ NOT NULL,
      revoked BOOLEAN NOT NULL DEFAULT false);
    CREATE TABLE auth_transactions (
      state_hash VARCHAR(64) PRIMARY KEY, browser_hash VARCHAR(64) NOT NULL,
      nonce_hash VARCHAR(64) NOT NULL, encrypted_verifier BYTEA NOT NULL,
      redirect_uri TEXT, cli_state VARCHAR(256), cli_challenge VARCHAR(43),
      scope VARCHAR(64) NOT NULL CHECK(scope IN ('publisher','operator')),
      publisher_id UUID REFERENCES publishers(id) ON DELETE RESTRICT,
      expires_at TIMESTAMPTZ NOT NULL, consumed BOOLEAN NOT NULL DEFAULT false,
      completed_account_id UUID REFERENCES marketplace_accounts(id) ON DELETE RESTRICT,
      consented BOOLEAN NOT NULL DEFAULT false);
    CREATE TABLE authorization_codes (
      code_hash VARCHAR(64) PRIMARY KEY,
      account_id UUID NOT NULL REFERENCES marketplace_accounts(id) ON DELETE RESTRICT,
      client_id VARCHAR(64) NOT NULL, redirect_uri TEXT NOT NULL,
      challenge VARCHAR(43) NOT NULL, scope VARCHAR(64) NOT NULL,
      publisher_id UUID REFERENCES publishers(id) ON DELETE RESTRICT,
      authenticated_at TIMESTAMPTZ NOT NULL, acr TEXT, amr JSONB NOT NULL,
      expires_at TIMESTAMPTZ NOT NULL, consumed BOOLEAN NOT NULL DEFAULT false);
    CREATE TABLE publisher_signing_keys (
      id UUID PRIMARY KEY, publisher_id UUID NOT NULL REFERENCES publishers(id) ON DELETE RESTRICT,
      public_key BYTEA NOT NULL CHECK(octet_length(public_key)=32),
      fingerprint VARCHAR(64) NOT NULL CHECK(fingerprint ~ '^[0-9a-f]{64}$'),
      state VARCHAR(16) NOT NULL CHECK(state IN ('active','retired','revoked')),
      created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
      UNIQUE(publisher_id,fingerprint));
    CREATE TABLE key_proof_challenges (
      id UUID PRIMARY KEY, publisher_id UUID NOT NULL REFERENCES publishers(id) ON DELETE RESTRICT,
      account_id UUID NOT NULL REFERENCES marketplace_accounts(id) ON DELETE RESTRICT,
      public_key BYTEA NOT NULL CHECK(octet_length(public_key)=32),
      payload BYTEA NOT NULL CHECK(octet_length(payload)<=4096),
      expires_at TIMESTAMPTZ NOT NULL, consumed BOOLEAN NOT NULL DEFAULT false);
    ALTER TABLE package_projects ADD COLUMN publisher_id UUID REFERENCES publishers(id)
      ON DELETE RESTRICT, ADD COLUMN ownership_revision INTEGER NOT NULL DEFAULT 1
      CHECK(ownership_revision>0), ADD COLUMN visibility VARCHAR(16) NOT NULL DEFAULT 'visible'
      CHECK(visibility IN ('visible','hidden'));
    ALTER TABLE package_releases ADD COLUMN original_publisher_id UUID REFERENCES publishers(id)
      ON DELETE RESTRICT;
    CREATE TABLE project_key_authorizations (
      project_id UUID NOT NULL REFERENCES package_projects(id) ON DELETE RESTRICT,
      key_id UUID NOT NULL REFERENCES publisher_signing_keys(id) ON DELETE RESTRICT,
      ownership_revision INTEGER NOT NULL CHECK(ownership_revision>0),
      state VARCHAR(16) NOT NULL CHECK(state IN ('active','revoked')),
      actor_id UUID NOT NULL REFERENCES marketplace_accounts(id) ON DELETE RESTRICT,
      created_at TIMESTAMPTZ NOT NULL, PRIMARY KEY(project_id,key_id));
    CREATE TABLE project_transfers (
      id UUID PRIMARY KEY, project_id UUID NOT NULL
        REFERENCES package_projects(id) ON DELETE RESTRICT,
      source_publisher_id UUID NOT NULL REFERENCES publishers(id) ON DELETE RESTRICT,
      destination_publisher_id UUID NOT NULL REFERENCES publishers(id) ON DELETE RESTRICT,
      ownership_revision INTEGER NOT NULL CHECK(ownership_revision>0),
      requester_id UUID NOT NULL REFERENCES marketplace_accounts(id) ON DELETE RESTRICT,
      accepting_id UUID REFERENCES marketplace_accounts(id) ON DELETE RESTRICT,
      state VARCHAR(16) NOT NULL CHECK(state IN ('pending','accepted','cancelled','expired')),
      created_at TIMESTAMPTZ NOT NULL, expires_at TIMESTAMPTZ NOT NULL,
      CHECK(source_publisher_id<>destination_publisher_id));
    CREATE TABLE publication_audit_events (
      id UUID PRIMARY KEY, actor_kind VARCHAR(16) NOT NULL CHECK(actor_kind IN ('human','system')),
      actor_id UUID REFERENCES marketplace_accounts(id) ON DELETE RESTRICT,
      action VARCHAR(64) NOT NULL, target VARCHAR(256) NOT NULL,
      request_id VARCHAR(64) NOT NULL, occurred_at TIMESTAMPTZ NOT NULL,
      evidence JSONB NOT NULL CHECK(octet_length(evidence::text)<=16384),
      CHECK((actor_kind='human')=(actor_id IS NOT NULL)));
    CREATE INDEX ix_identity_account ON external_identities(account_id);
    CREATE INDEX ix_membership_account ON publisher_memberships(account_id,publisher_id);
    CREATE INDEX ix_project_owner ON package_projects(publisher_id);
    CREATE INDEX ix_audit_target_time ON publication_audit_events(target,occurred_at,id);
    CREATE INDEX ix_audit_actor_action_time
      ON publication_audit_events(actor_id,action,occurred_at);
    CREATE INDEX ix_auth_expiry ON auth_transactions(expires_at);
    CREATE INDEX ix_code_expiry ON authorization_codes(expires_at);
    CREATE INDEX ix_challenge_expiry ON key_proof_challenges(expires_at);
    CREATE FUNCTION mp_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN RAISE EXCEPTION 'append-only history'; END $$;
    CREATE TRIGGER mp_audit_guard BEFORE UPDATE OR DELETE OR TRUNCATE ON publication_audit_events
      FOR EACH STATEMENT EXECUTE FUNCTION mp_append_only();
    CREATE FUNCTION mp_key_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF (to_jsonb(OLD)-ARRAY['state','updated_at']) IS DISTINCT FROM
         (to_jsonb(NEW)-ARRAY['state','updated_at']) OR
         (OLD.state='revoked' AND NEW.state<>'revoked') OR
         (OLD.state='retired' AND NEW.state='active') THEN
        RAISE EXCEPTION 'key evidence/lifecycle is immutable';
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER mp_key_guard BEFORE UPDATE ON publisher_signing_keys
      FOR EACH ROW EXECUTE FUNCTION mp_key_guard();
    CREATE FUNCTION mp_publisher_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.id<>OLD.id OR NEW.handle<>OLD.handle OR NEW.kind<>OLD.kind THEN
        RAISE EXCEPTION 'publisher identity is immutable'; END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER mp_publisher_guard BEFORE UPDATE ON publishers
      FOR EACH ROW EXECUTE FUNCTION mp_publisher_guard();
    CREATE OR REPLACE FUNCTION mp_guard_release() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP='DELETE' THEN
        IF OLD.publication_state='published' THEN
          RAISE EXCEPTION 'published history is immutable'; END IF;
        RETURN OLD;
      END IF;
      PERFORM id FROM package_projects WHERE id=NEW.project_id FOR UPDATE;
      IF TG_OP='UPDATE' THEN
        IF OLD.original_publisher_id IS NULL AND NEW.original_publisher_id IS NOT NULL THEN
          IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='mp_maintenance') THEN
            RAISE EXCEPTION 'explicit maintenance role required'; END IF;
          IF NOT pg_has_role(current_user,'mp_maintenance','member') OR
             (to_jsonb(OLD)-'original_publisher_id') IS DISTINCT FROM
             (to_jsonb(NEW)-'original_publisher_id') THEN
            RAISE EXCEPTION 'only explicit legacy attribution permitted'; END IF;
          RETURN NEW;
        END IF;
        IF OLD.publication_state='published' AND
          (to_jsonb(OLD)-ARRAY['distribution_state','status_revision','status_updated_at'])
          IS DISTINCT FROM
          (to_jsonb(NEW)-ARRAY['distribution_state','status_revision','status_updated_at']) THEN
          RAISE EXCEPTION 'published evidence is immutable'; END IF;
        IF NEW.status_revision<OLD.status_revision OR NEW.status_updated_at<OLD.status_updated_at OR
           (OLD.distribution_state='revoked' AND NEW.distribution_state<>'revoked') OR
           (OLD.distribution_state<>NEW.distribution_state
             AND NEW.status_revision<>OLD.status_revision+1)
           THEN RAISE EXCEPTION 'invalid distribution transition'; END IF;
      END IF;
      RETURN NEW;
    END $$;
    """)


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(
            text(
                "SELECT EXISTS(SELECT 1 FROM marketplace_accounts) OR "
                "EXISTS(SELECT 1 FROM publishers)"
            )
        )
        .scalar_one()
    ):
        raise RuntimeError("Identity downgrade requires an empty disposable database")
    op.execute("""
      DROP TABLE project_key_authorizations,project_transfers,key_proof_challenges,
        publisher_signing_keys,publication_audit_events,authorization_codes,auth_transactions,
        cli_credentials,hosted_sessions,marketplace_operator_grants,publisher_memberships;
      ALTER TABLE package_projects DROP COLUMN publisher_id, DROP COLUMN ownership_revision,
        DROP COLUMN visibility;
      ALTER TABLE package_releases DROP COLUMN original_publisher_id;
      DROP TABLE publishers,external_identities,marketplace_accounts;
      DROP FUNCTION mp_append_only(),mp_key_guard(),mp_publisher_guard();
    """)
