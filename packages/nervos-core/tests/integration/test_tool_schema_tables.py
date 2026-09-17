"""D1 durable tool, capability and audit schema: the constraints that carry the design.

Every test here is about what the *database* refuses, not about what a later milestone will do
with these rows. Nothing in D1 discovers, authorizes or dispatches a tool, so a test that needed a
registry, an evaluator or an MCP client to pass would be a test of the wrong milestone. What is
asserted instead is the set of durable states the accepted Stage D architecture depends on being
representable -- and the set it depends on being impossible.

The load-bearing case throughout is the built-in: its identity has a NULL `source_id`, and SQLite
does not enforce uniqueness over a NULL, so a naive three-column UNIQUE would silently permit
unlimited duplicate built-ins. Several tests below exist specifically to prove the partial-index
construction closes that hole.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from execution_support import migrate
from nervos_core.infrastructure.database import models as database_models
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError

_ = database_models

NOW = datetime(2026, 9, 17, tzinfo=UTC)
# An unexpired lease: `lease_expires_at > claimed_at` is a C2 invariant, not an incidental value.
LEASE = NOW + timedelta(minutes=1)
FINGERPRINT = "a" * 64
OTHER_FINGERPRINT = "b" * 64

STAGE_D_TABLES = (
    "mcp_connections",
    "tool_definitions",
    "agent_tool_grants",
    "tool_invocations",
)

# Column names that would mean the durable model can hold a secret. D1's safety argument is
# structural -- there is no column that could hold one -- so it is asserted as a schema fact
# rather than left to a redaction layer a later edit could weaken.
FORBIDDEN_COLUMN_TOKENS = (
    "token",
    "secret",
    "password",
    "api_key",
    "authorization",
    "header",
    "credential_value",
)


@pytest.fixture
def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Engine:
    """One disposable migrated database holding a user and two Agent Instances."""
    return migrate(tmp_path / "tool-schema.db", monkeypatch, agents=2)


def insert(engine: Engine, table: str, values: dict[str, Any]) -> None:
    columns = ", ".join(values)
    parameters = ", ".join(f":{column}" for column in values)
    with engine.begin() as connection:
        connection.execute(text(f"INSERT INTO {table} ({columns}) VALUES ({parameters})"), values)


def rejects(engine: Engine, table: str, values: dict[str, Any]) -> None:
    with pytest.raises(IntegrityError):
        insert(engine, table, values)


def execute(engine: Engine, statement: str, **values: object) -> None:
    with engine.begin() as connection:
        connection.execute(text(statement), values)


def scalar(engine: Engine, statement: str, **values: object) -> Any:
    with engine.connect() as connection:
        return connection.scalar(text(statement), values)


def connection_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "owner_user_id": 1,
        "display_name": "GitHub",
        "transport": "http",
        "endpoint": "https://mcp.example.test/rpc",
        "catalog_status": "unavailable",
        "created_at": NOW,
        "updated_at": NOW,
    }
    row.update(overrides)
    return row


def definition_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "source_kind": "builtin",
        "source_id": None,
        "upstream_name": "current_time",
        "model_name": "nervos__builtin__current_time",
        "display_name": "Current time",
        "description": "Return the current time.",
        "input_schema": "{}",
        "fingerprint": FINGERPRINT,
        "status": "available",
        "created_at": NOW,
        "updated_at": NOW,
    }
    row.update(overrides)
    return row


def seed_connection(engine: Engine, *, display_name: str = "GitHub") -> int:
    insert(engine, "mcp_connections", connection_row(display_name=display_name))
    return int(scalar(engine, "SELECT max(id) FROM mcp_connections") or 0)


def seed_definition(engine: Engine, **overrides: object) -> int:
    insert(engine, "tool_definitions", definition_row(**overrides))
    return int(scalar(engine, "SELECT max(id) FROM tool_definitions") or 0)


def seed_parents(engine: Engine) -> tuple[int, int, int]:
    """Create one Run, its Job, and one `running` Attempt. Returns their identifiers."""
    insert(
        engine,
        "runs",
        {
            "agent_instance_id": 1,
            "status": "created",
            "agent_key": "nervos.chat",
            "agent_definition_version": "1",
            "model_provider": "anthropic",
            "model_name": "opaque/model",
            "input_text": "hello",
            "input_max_bytes": 8000,
            "input_max_code_points": 4000,
            "output_max_bytes": 32000,
            "output_max_code_points": 16000,
            "provider_timeout_ms": 60000,
            "max_output_tokens": 1024,
            "max_model_calls": 1,
            "created_at": NOW,
        },
    )
    run_id = int(scalar(engine, "SELECT max(id) FROM runs") or 0)
    insert(
        engine,
        "jobs",
        {
            "run_id": run_id,
            "agent_instance_id": 1,
            "model_provider": "anthropic",
            "status": "queued",
            "available_at": NOW,
            "attempt_count": 0,
            "max_attempts": 3,
            "created_at": NOW,
            "updated_at": NOW,
        },
    )
    job_id = int(scalar(engine, "SELECT max(id) FROM jobs") or 0)
    insert(
        engine,
        "job_attempts",
        {
            "job_id": job_id,
            "attempt_number": 1,
            "status": "running",
            "worker_id": "worker-1",
            "claim_token": b"a" * 32,
            "claimed_at": NOW,
            "execution_started_at": NOW,
            "lease_expires_at": LEASE,
            "last_heartbeat_at": NOW,
            "created_at": NOW,
        },
    )
    attempt_id = int(scalar(engine, "SELECT max(id) FROM job_attempts") or 0)
    return run_id, job_id, attempt_id


def invocation_row(
    run_id: int, job_id: int, attempt_id: int, **overrides: object
) -> dict[str, object]:
    row: dict[str, object] = {
        "run_id": run_id,
        "job_id": job_id,
        "attempt_id": attempt_id,
        "tool_sequence": 1,
        "tool_definition_id": 1,
        "source_kind": "builtin",
        "source_id": None,
        "upstream_name": "current_time",
        "model_name": "nervos__builtin__current_time",
        "definition_fingerprint": FINGERPRINT,
        "status": "requested",
        "permission_decision": "allowed",
        "requested_at": NOW,
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------------------
# The four tables exist, and only the reviewed columns do
# ---------------------------------------------------------------------------------------


def test_the_four_durable_tables_exist_with_exactly_the_reviewed_columns(engine: Engine) -> None:
    """The applied schema and the ORM declare the same columns, in the same order."""
    inspector = inspect(engine)
    for table in STAGE_D_TABLES:
        applied = [column["name"] for column in inspector.get_columns(table)]
        declared = [column.name for column in database_models.Base.metadata.tables[table].columns]
        assert applied == declared, table


def test_no_stage_d_table_has_a_column_that_could_hold_a_secret(engine: Engine) -> None:
    """The safety argument is structural: the safe model is the persisted model.

    A credential, an access token, an authorization header or a claim token has no column to live
    in -- so no later edit can start writing one without failing this test first. The single
    credential-bearing column is `mcp_connections.credential_ref`, and it holds an opaque alias
    whose shape is asserted below.
    """
    inspector = inspect(engine)
    allowed = {"credential_ref"}
    for table in STAGE_D_TABLES:
        for column in inspector.get_columns(table):
            name = str(column["name"])
            if name in allowed:
                continue
            for forbidden in FORBIDDEN_COLUMN_TOKENS:
                assert forbidden not in name, (table, name)


# ---------------------------------------------------------------------------------------
# mcp_connections -- configuration, never a live client
# ---------------------------------------------------------------------------------------


def test_a_connection_holds_only_the_transports_stage_d_supports(engine: Engine) -> None:
    insert(engine, "mcp_connections", connection_row())
    insert(
        engine,
        "mcp_connections",
        {
            "owner_user_id": 1,
            "display_name": "Local",
            "transport": "stdio",
            "server_key": "github-local",
            "catalog_status": "unavailable",
            "created_at": NOW,
            "updated_at": NOW,
        },
    )
    rejects(engine, "mcp_connections", connection_row(display_name="SSE", transport="sse"))


def test_a_connection_carries_exactly_the_target_its_transport_uses(engine: Engine) -> None:
    """A stdio server is referenced by key, and no column anywhere accepts a command."""
    rejects(
        engine,
        "mcp_connections",
        {
            "owner_user_id": 1,
            "display_name": "Both",
            "transport": "stdio",
            "server_key": "k",
            "endpoint": "https://mcp.example.test/rpc",
            "catalog_status": "unavailable",
            "created_at": NOW,
            "updated_at": NOW,
        },
    )
    rejects(
        engine,
        "mcp_connections",
        {
            "owner_user_id": 1,
            "display_name": "Neither",
            "transport": "http",
            "catalog_status": "unavailable",
            "created_at": NOW,
            "updated_at": NOW,
        },
    )
    columns = {column["name"] for column in inspect(engine).get_columns("mcp_connections")}
    for forbidden in ("command", "args", "arguments", "cwd", "working_directory", "env"):
        assert forbidden not in columns, forbidden


def test_a_credential_alias_can_never_be_an_environment_variable_name(engine: Engine) -> None:
    """ADR 0016 forbids an environment-variable name here, so the shape makes one unrepresentable.

    `credential_ref = OPENAI_API_KEY` is the exact token-passthrough the ADR forbids by name: it
    would let a connection forward the Worker's model-provider secret to an arbitrary server. The
    CHECK refuses it at the database, not merely at a route.
    """
    insert(engine, "mcp_connections", connection_row(credential_ref="github-prod"))
    for rejected in (
        "OPENAI_API_KEY",
        "openai_api_key",
        "GitHub-Prod",
        "-leading",
        "with space",
        "",
    ):
        rejects(engine, "mcp_connections", connection_row(credential_ref=rejected))
    # A stdio server key is the same kind of operator-declared name.
    rejects(
        engine,
        "mcp_connections",
        {
            "owner_user_id": 1,
            "display_name": "Local",
            "transport": "stdio",
            "server_key": "GITHUB_KEY",
            "catalog_status": "unavailable",
            "created_at": NOW,
            "updated_at": NOW,
        },
    )


def test_a_connection_status_is_one_of_the_five_the_ui_can_show(engine: Engine) -> None:
    for status in ("connected", "unavailable", "needs_refresh", "definition_changed", "disabled"):
        insert(engine, "mcp_connections", connection_row(catalog_status=status))
    rejects(engine, "mcp_connections", connection_row(catalog_status="unknown_state"))
    # A connection that has not proven itself connected offers no tools, so the default is the
    # fail-closed state rather than an optimistic one.
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO mcp_connections"
                "(owner_user_id,display_name,transport,endpoint,created_at,updated_at)"
                " VALUES(1,'Defaulted','http','https://mcp.example.test/rpc',:n,:n)"
            ),
            {"n": NOW},
        )
    assert scalar(
        engine, "SELECT catalog_status FROM mcp_connections WHERE display_name='Defaulted'"
    ) == ("unavailable")


def test_a_connection_error_pair_is_all_or_nothing(engine: Engine) -> None:
    rejects(engine, "mcp_connections", connection_row(last_error_code="mcp_unreachable"))
    rejects(engine, "mcp_connections", connection_row(last_error_message="unreachable"))
    insert(
        engine,
        "mcp_connections",
        connection_row(last_error_code="mcp_unreachable", last_error_message="unreachable"),
    )


# ---------------------------------------------------------------------------------------
# tool_definitions -- identity is the load-bearing constraint
# ---------------------------------------------------------------------------------------


def test_a_duplicate_builtin_identity_is_rejected(engine: Engine) -> None:
    """The case a plain `UNIQUE(source_kind, source_id, upstream_name)` would have missed.

    SQLite treats NULLs as distinct in a UNIQUE index, so `source_id IS NULL` on every built-in
    row made the naive constraint non-enforcing exactly where it was needed. The partial unique
    index over `upstream_name WHERE source_kind = 'builtin'` is what closes it.
    """
    seed_definition(engine)
    rejects(engine, "tool_definitions", definition_row(model_name="nervos__builtin__other"))


def test_a_builtin_and_an_mcp_tool_may_share_an_upstream_name(engine: Engine) -> None:
    """The two partial indexes are disjoint, so a shared name across kinds is legitimate."""
    source = seed_connection(engine)
    seed_definition(engine)
    insert(
        engine,
        "tool_definitions",
        definition_row(
            source_kind="mcp",
            source_id=source,
            model_name="nervos__c1__current_time",
        ),
    )


def test_source_kind_and_source_id_must_agree(engine: Engine) -> None:
    """The pairing CHECK is what keeps the two partial indexes total."""
    source = seed_connection(engine)
    rejects(engine, "tool_definitions", definition_row(source_id=source))
    rejects(
        engine,
        "tool_definitions",
        definition_row(source_kind="mcp", source_id=None, model_name="nervos__c1__x"),
    )
    rejects(engine, "tool_definitions", definition_row(source_kind="plugin"))


def test_a_duplicate_mcp_identity_on_one_connection_is_rejected(engine: Engine) -> None:
    source = seed_connection(engine)
    insert(
        engine,
        "tool_definitions",
        definition_row(source_kind="mcp", source_id=source, model_name="nervos__c1__read_file"),
    )
    rejects(
        engine,
        "tool_definitions",
        definition_row(source_kind="mcp", source_id=source, model_name="nervos__c1__read_file_2"),
    )


def test_the_same_upstream_name_on_a_different_connection_is_allowed(engine: Engine) -> None:
    """A shared connection is a shared source of tools; a different source is a different tool.

    Two servers may both expose `read_file`, and they are two durable identities -- which is the
    whole reason identity is not just a name.
    """
    first = seed_connection(engine)
    second = seed_connection(engine, display_name="GitLab")
    insert(
        engine,
        "tool_definitions",
        definition_row(source_kind="mcp", source_id=first, model_name="nervos__c1__read_file"),
    )
    insert(
        engine,
        "tool_definitions",
        definition_row(source_kind="mcp", source_id=second, model_name="nervos__c2__read_file"),
    )


def test_a_duplicate_model_facing_name_is_rejected(engine: Engine) -> None:
    """`UNIQUE(model_name)` is the final fail-closed collision guard."""
    source = seed_connection(engine)
    insert(
        engine,
        "tool_definitions",
        definition_row(source_kind="mcp", source_id=source, model_name="nervos__c1__read_file"),
    )
    rejects(
        engine,
        "tool_definitions",
        definition_row(
            source_kind="mcp",
            source_id=source,
            upstream_name="read_file_other",
            model_name="nervos__c1__read_file",
        ),
    )


def test_a_model_facing_name_is_bounded_and_provider_safe(engine: Engine) -> None:
    """64 characters, the stricter provider bound, over the intersection of their alphabets."""
    insert(engine, "tool_definitions", definition_row(model_name="a" * 64))
    rejects(engine, "tool_definitions", definition_row(model_name="b" * 65))
    rejects(engine, "tool_definitions", definition_row(model_name="Nervos__Builtin__X"))
    rejects(engine, "tool_definitions", definition_row(model_name="nervos.builtin.x"))
    rejects(engine, "tool_definitions", definition_row(model_name=""))
    # The display name carries the human label, so it may contain what the model name may not.
    insert(
        engine,
        "tool_definitions",
        definition_row(
            upstream_name="pretty",
            model_name="nervos__builtin__pretty",
            display_name="Read a file (any/path)",
        ),
    )


def test_a_definition_status_is_one_of_the_three_the_registry_can_publish(engine: Engine) -> None:
    for index, status in enumerate(("available", "unavailable", "unsupported_schema")):
        insert(
            engine,
            "tool_definitions",
            definition_row(
                status=status,
                upstream_name=f"status_{index}",
                model_name=f"nervos__builtin__status_{index}",
            ),
        )
    rejects(engine, "tool_definitions", definition_row(status="review_required"))


def test_an_annotation_hint_defaults_to_the_conservative_reading(engine: Engine) -> None:
    """An unannotated tool is stored as possibly destructive and possibly open-world.

    These are the MCP specification's own defaults, adopted so that "unknown implies worst case"
    is not a NervOS idiosyncrasy. They are presentation metadata: no hint may grant or remove
    authority, so nothing about a grant depends on them.
    """
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO tool_definitions"
                "(source_kind,upstream_name,model_name,display_name,description,input_schema,"
                "fingerprint,status,created_at,updated_at)"
                " VALUES('builtin','unannotated','nervos__builtin__unannotated','U','d','{}',"
                ":f,'available',:n,:n)"
            ),
            {"f": FINGERPRINT, "n": NOW},
        )
    row = (
        scalar(
            engine, "SELECT hint_read_only FROM tool_definitions WHERE upstream_name='unannotated'"
        ),
        scalar(
            engine,
            "SELECT hint_destructive FROM tool_definitions WHERE upstream_name='unannotated'",
        ),
        scalar(
            engine, "SELECT hint_idempotent FROM tool_definitions WHERE upstream_name='unannotated'"
        ),
        scalar(
            engine, "SELECT hint_open_world FROM tool_definitions WHERE upstream_name='unannotated'"
        ),
    )
    assert row == (0, 1, 0, 1)


def test_a_definition_rejects_a_fingerprint_that_is_not_a_sha256_digest(engine: Engine) -> None:
    rejects(engine, "tool_definitions", definition_row(fingerprint="a" * 63))
    rejects(engine, "tool_definitions", definition_row(fingerprint="A" * 64))
    rejects(engine, "tool_definitions", definition_row(fingerprint="not-a-digest"))


def test_a_definition_bounds_the_untrusted_text_it_stores(engine: Engine) -> None:
    rejects(engine, "tool_definitions", definition_row(description="d" * 65537))
    rejects(engine, "tool_definitions", definition_row(input_schema=" "))
    rejects(engine, "tool_definitions", definition_row(output_schema=" "))


def test_an_mcp_definition_must_reference_a_real_connection(engine: Engine) -> None:
    rejects(
        engine,
        "tool_definitions",
        definition_row(source_kind="mcp", source_id=999, model_name="nervos__c999__x"),
    )


# ---------------------------------------------------------------------------------------
# agent_tool_grants -- the authority that exists now
# ---------------------------------------------------------------------------------------


def test_one_grant_per_instance_and_definition(engine: Engine) -> None:
    definition = seed_definition(engine)
    insert(
        engine,
        "agent_tool_grants",
        {
            "agent_instance_id": 1,
            "tool_definition_id": definition,
            "reviewed_fingerprint": FINGERPRINT,
            "created_at": NOW,
        },
    )
    rejects(
        engine,
        "agent_tool_grants",
        {
            "agent_instance_id": 1,
            "tool_definition_id": definition,
            "reviewed_fingerprint": OTHER_FINGERPRINT,
            "created_at": NOW,
        },
    )
    # A second Instance may hold the same capability; authority is per Instance.
    insert(
        engine,
        "agent_tool_grants",
        {
            "agent_instance_id": 2,
            "tool_definition_id": definition,
            "reviewed_fingerprint": FINGERPRINT,
            "created_at": NOW,
        },
    )


def test_a_grant_id_is_never_reused(engine: Engine) -> None:
    """The cutoff depends on this: revoking and re-granting must mint a strictly greater id.

    If SQLite recycled the id, a re-granted capability would fall at or below an existing Run's
    `tool_grant_cutoff_id` and be silently acquired by work the user had already started -- the
    exact capability expansion the cutoff exists to prevent.
    """
    definition = seed_definition(engine)
    grant = {
        "agent_instance_id": 1,
        "tool_definition_id": definition,
        "reviewed_fingerprint": FINGERPRINT,
        "created_at": NOW,
    }
    insert(engine, "agent_tool_grants", grant)
    first = int(scalar(engine, "SELECT id FROM agent_tool_grants") or 0)
    execute(engine, "DELETE FROM agent_tool_grants")
    insert(engine, "agent_tool_grants", grant)
    second = int(scalar(engine, "SELECT id FROM agent_tool_grants") or 0)
    assert first == 1
    assert second > first, "a re-granted capability must be a strictly newer identity"
    # AUTOINCREMENT is the mechanism: the table keeps a sequence row of its own.
    assert (
        scalar(engine, "SELECT seq FROM sqlite_sequence WHERE name='agent_tool_grants'") == second
    )


def test_a_grant_cannot_exist_without_its_parents(engine: Engine) -> None:
    definition = seed_definition(engine)
    rejects(
        engine,
        "agent_tool_grants",
        {
            "agent_instance_id": 999,
            "tool_definition_id": definition,
            "reviewed_fingerprint": FINGERPRINT,
            "created_at": NOW,
        },
    )
    rejects(
        engine,
        "agent_tool_grants",
        {
            "agent_instance_id": 1,
            "tool_definition_id": 999,
            "reviewed_fingerprint": FINGERPRINT,
            "created_at": NOW,
        },
    )


def test_a_granted_definition_cannot_be_deleted_out_from_under_it(engine: Engine) -> None:
    """RESTRICT, so authority can never be orphaned by deleting the thing it points at."""
    definition = seed_definition(engine)
    insert(
        engine,
        "agent_tool_grants",
        {
            "agent_instance_id": 1,
            "tool_definition_id": definition,
            "reviewed_fingerprint": FINGERPRINT,
            "created_at": NOW,
        },
    )
    with pytest.raises(IntegrityError):
        execute(engine, "DELETE FROM tool_definitions WHERE id=:d", d=definition)
    with pytest.raises(IntegrityError):
        execute(engine, "DELETE FROM agent_instances WHERE id=1")


def test_a_grant_records_the_fingerprint_the_user_reviewed(engine: Engine) -> None:
    definition = seed_definition(engine)
    rejects(
        engine,
        "agent_tool_grants",
        {
            "agent_instance_id": 1,
            "tool_definition_id": definition,
            "reviewed_fingerprint": "c" * 63,
            "created_at": NOW,
        },
    )


# ---------------------------------------------------------------------------------------
# tool_invocations -- the ambiguity boundary, made durable
# ---------------------------------------------------------------------------------------


def test_a_tool_sequence_is_positive_and_unique_within_one_attempt(engine: Engine) -> None:
    """Ordering authority is the sequence, never a timestamp."""
    run_id, job_id, attempt_id = seed_parents(engine)
    definition = seed_definition(engine)
    base = invocation_row(run_id, job_id, attempt_id, tool_definition_id=definition)
    insert(engine, "tool_invocations", base)
    rejects(engine, "tool_invocations", base)
    rejects(engine, "tool_invocations", {**base, "tool_sequence": 0})
    insert(engine, "tool_invocations", {**base, "tool_sequence": 2})
    # The same sequence in a different Attempt is a different call. A second Attempt needs its own
    # Job: `uq_job_attempts_one_active` permits one live Attempt per Job, which is a C2 invariant.
    other_run, other_job, other_attempt = seed_parents(engine)
    insert(
        engine,
        "tool_invocations",
        {
            **base,
            "run_id": other_run,
            "job_id": other_job,
            "attempt_id": other_attempt,
            "tool_sequence": 1,
        },
    )


def test_the_non_dispatched_states_can_never_claim_a_start(engine: Engine) -> None:
    """`started_at` is the ambiguity boundary, so only a dispatched call may have one.

    A crash matrix is only trustworthy if "this call provably never left the process" is a durable
    fact. The dispatched-shape CHECK is what makes it one.
    """
    run_id, job_id, attempt_id = seed_parents(engine)
    definition = seed_definition(engine)
    base = invocation_row(run_id, job_id, attempt_id, tool_definition_id=definition)
    # A start is dispatch, and no non-dispatched state may carry one.
    for index, status in enumerate(("requested", "denied", "cancelled"), start=1):
        rejects(
            engine,
            "tool_invocations",
            {**base, "tool_sequence": index, "status": status, "started_at": NOW},
        )
    # And a dispatched state must carry one.
    reject_sequence = 10
    rejects(
        engine,
        "tool_invocations",
        {**base, "tool_sequence": reject_sequence, "status": "started"},
    )


def test_every_invocation_state_is_representable_and_no_other_is(engine: Engine) -> None:
    run_id, job_id, attempt_id = seed_parents(engine)
    definition = seed_definition(engine)
    base = invocation_row(run_id, job_id, attempt_id, tool_definition_id=definition)
    legal = (
        {"status": "requested"},
        {"status": "denied", "finished_at": NOW, "permission_decision": "denied_no_grant"},
        {"status": "cancelled", "finished_at": NOW},
        {"status": "started", "started_at": NOW},
        {
            "status": "succeeded",
            "started_at": NOW,
            "finished_at": NOW,
            "result_digest": FINGERPRINT,
            "result_bytes": 12,
            "result_truncated": False,
        },
        {
            "status": "failed",
            "started_at": NOW,
            "finished_at": NOW,
            "error_code": "tool_failed",
            "error_message": "the server returned an error",
        },
        {
            "status": "ambiguous",
            "started_at": NOW,
            "finished_at": NOW,
            "error_code": "tool_outcome_unknown",
            "error_message": "the response was lost",
        },
    )
    for index, shape in enumerate(legal, start=1):
        insert(engine, "tool_invocations", {**base, "tool_sequence": index, **shape})
    rejects(engine, "tool_invocations", {**base, "tool_sequence": 90, "status": "dispatched"})
    # A terminal dispatched failure must be explainable, and a success must not carry an error.
    rejects(
        engine,
        "tool_invocations",
        {**base, "tool_sequence": 91, "status": "failed", "started_at": NOW, "finished_at": NOW},
    )
    rejects(
        engine,
        "tool_invocations",
        {
            **base,
            "tool_sequence": 92,
            "status": "succeeded",
            "started_at": NOW,
            "finished_at": NOW,
            "result_digest": FINGERPRINT,
            "result_bytes": 1,
            "result_truncated": False,
            "error_code": "tool_failed",
            "error_message": "unexpected",
        },
    )


def test_a_result_belongs_only_to_a_success(engine: Engine) -> None:
    run_id, job_id, attempt_id = seed_parents(engine)
    definition = seed_definition(engine)
    base = invocation_row(run_id, job_id, attempt_id, tool_definition_id=definition)
    # A denied call never produced a result.
    rejects(
        engine,
        "tool_invocations",
        {
            **base,
            "status": "denied",
            "finished_at": NOW,
            "permission_decision": "denied_no_grant",
            "result_digest": FINGERPRINT,
            "result_bytes": 1,
            "result_truncated": False,
        },
    )
    # Neither does a success with no recorded size, and truncation must be recorded, not implied.
    rejects(
        engine,
        "tool_invocations",
        {**base, "status": "succeeded", "started_at": NOW, "finished_at": NOW},
    )


def test_a_permission_decision_is_allowed_or_a_named_denial(engine: Engine) -> None:
    run_id, job_id, attempt_id = seed_parents(engine)
    definition = seed_definition(engine)
    base = invocation_row(run_id, job_id, attempt_id, tool_definition_id=definition)
    insert(engine, "tool_invocations", {**base, "permission_decision": "denied_revoked"})
    rejects(
        engine,
        "tool_invocations",
        {**base, "tool_sequence": 2, "permission_decision": "maybe"},
    )


def test_an_invocation_cannot_outlive_the_rows_it_audits(engine: Engine) -> None:
    """History is never cascaded away: the definition, Run, Job and Attempt are all RESTRICT."""
    run_id, job_id, attempt_id = seed_parents(engine)
    definition = seed_definition(engine)
    base = invocation_row(run_id, job_id, attempt_id, tool_definition_id=definition)
    insert(engine, "tool_invocations", base)
    for statement, values in (
        ("DELETE FROM tool_definitions WHERE id=:v", {"v": definition}),
        ("DELETE FROM runs WHERE id=:v", {"v": run_id}),
        ("DELETE FROM jobs WHERE id=:v", {"v": job_id}),
        ("DELETE FROM job_attempts WHERE id=:v", {"v": attempt_id}),
    ):
        with pytest.raises(IntegrityError):
            execute(engine, statement, **values)


def test_the_identity_snapshot_survives_the_connection_it_came_from(engine: Engine) -> None:
    """`source_id` carries no foreign key, deliberately.

    The row must stay readable and self-describing after its definition or connection is gone,
    which is what makes deleting configuration safe while the audit survives it. An FK here would
    have made the connection permanent instead.
    """
    source = seed_connection(engine)
    definition = seed_definition(
        engine,
        source_kind="mcp",
        source_id=source,
        upstream_name="read_file",
        model_name="nervos__c1__read_file",
    )
    run_id, job_id, attempt_id = seed_parents(engine)
    insert(
        engine,
        "tool_invocations",
        invocation_row(
            run_id,
            job_id,
            attempt_id,
            tool_definition_id=definition,
            source_kind="mcp",
            source_id=source,
            upstream_name="read_file",
            model_name="nervos__c1__read_file",
        ),
    )
    foreign_keys = {item["name"] for item in inspect(engine).get_foreign_keys("tool_invocations")}
    assert "fk_tool_invocations_source_id_mcp_connections" not in foreign_keys
    # The snapshot is complete on its own.
    row = (
        scalar(engine, "SELECT source_kind FROM tool_invocations"),
        scalar(engine, "SELECT source_id FROM tool_invocations"),
        scalar(engine, "SELECT upstream_name FROM tool_invocations"),
        scalar(engine, "SELECT model_name FROM tool_invocations"),
    )
    assert row == ("mcp", source, "read_file", "nervos__c1__read_file")


# ---------------------------------------------------------------------------------------
# runs, job_attempts and run_events
# ---------------------------------------------------------------------------------------


def test_a_run_carries_the_five_stage_d_columns_at_their_legacy_values(engine: Engine) -> None:
    """A Run submitted without tool limits gets the legacy-compatible ones.

    `max_tool_calls = 0` and `tool_grant_cutoff_id = 0` together mean a migrated Run can never
    acquire a Stage D capability: there is no tool budget and no cutoff admits any grant.
    """
    run_id, _, _ = seed_parents(engine)
    row = (
        scalar(engine, "SELECT max_tool_calls FROM runs WHERE id=:r", r=run_id),
        scalar(engine, "SELECT tool_timeout_ms FROM runs WHERE id=:r", r=run_id),
        scalar(engine, "SELECT tool_result_max_bytes FROM runs WHERE id=:r", r=run_id),
        scalar(engine, "SELECT max_consecutive_tool_failures FROM runs WHERE id=:r", r=run_id),
        scalar(engine, "SELECT tool_grant_cutoff_id FROM runs WHERE id=:r", r=run_id),
    )
    assert row == (0, 30000, 65536, 3, 0)


def test_the_run_tool_limits_are_bounded(engine: Engine) -> None:
    """`max_tool_calls` may legitimately be zero; every other Stage D limit must be positive."""
    run_id, _, _ = seed_parents(engine)
    for statement, values in (
        ("UPDATE runs SET max_tool_calls=17 WHERE id=:r", {"r": run_id}),
        ("UPDATE runs SET max_tool_calls=-1 WHERE id=:r", {"r": run_id}),
        ("UPDATE runs SET tool_timeout_ms=999 WHERE id=:r", {"r": run_id}),
        ("UPDATE runs SET tool_timeout_ms=300001 WHERE id=:r", {"r": run_id}),
        ("UPDATE runs SET tool_result_max_bytes=1023 WHERE id=:r", {"r": run_id}),
        ("UPDATE runs SET tool_result_max_bytes=1048577 WHERE id=:r", {"r": run_id}),
        ("UPDATE runs SET max_consecutive_tool_failures=0 WHERE id=:r", {"r": run_id}),
        ("UPDATE runs SET tool_grant_cutoff_id=-1 WHERE id=:r", {"r": run_id}),
    ):
        with pytest.raises(IntegrityError):
            execute(engine, statement, **values)
    execute(
        engine,
        "UPDATE runs SET max_tool_calls=16, tool_timeout_ms=1000,"
        " tool_result_max_bytes=1024, tool_grant_cutoff_id=999 WHERE id=:r",
        r=run_id,
    )


def test_the_c1_run_limits_keep_their_exact_meaning(engine: Engine) -> None:
    """D1 adds a constraint beside `limits_positive`; it does not widen one."""
    run_id, _, _ = seed_parents(engine)
    with pytest.raises(IntegrityError):
        execute(engine, "UPDATE runs SET max_model_calls=0 WHERE id=:r", r=run_id)
    with pytest.raises(IntegrityError):
        execute(engine, "UPDATE runs SET provider_timeout_ms=0 WHERE id=:r", r=run_id)


def test_an_attempt_usage_column_is_nullable_and_starts_null(engine: Engine) -> None:
    """Stage C writes nothing here: a one-call Attempt reports usage on the Run."""
    _, _, attempt_id = seed_parents(engine)
    row = (
        scalar(engine, "SELECT input_tokens FROM job_attempts WHERE id=:a", a=attempt_id),
        scalar(engine, "SELECT output_tokens FROM job_attempts WHERE id=:a", a=attempt_id),
        scalar(engine, "SELECT total_tokens FROM job_attempts WHERE id=:a", a=attempt_id),
    )
    assert row == (None, None, None)
    execute(
        engine,
        "UPDATE job_attempts SET input_tokens=5, output_tokens=7, total_tokens=12 WHERE id=:a",
        a=attempt_id,
    )
    assert scalar(engine, "SELECT total_tokens FROM job_attempts WHERE id=:a", a=attempt_id) == 12


def test_the_run_event_vocabulary_gains_the_six_tool_types_and_keeps_the_thirteen(
    engine: Engine,
) -> None:
    """The Stage C vocabulary remains a byte-for-byte prefix of the accepted set.

    `tool.cancelled` is deliberately absent: ADR 0017 keeps `run.cancelled` as the only
    cancellation truth, and inventing a second one would let a tool event contradict it.
    """
    run_id, job_id, _ = seed_parents(engine)
    stage_c = (
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
    )
    tool = (
        "tool.requested",
        "tool.started",
        "tool.succeeded",
        "tool.failed",
        "tool.denied",
        "tool.ambiguous",
    )
    for index, event_type in enumerate((*stage_c, *tool), start=1):
        insert(
            engine,
            "run_events",
            {
                "run_id": run_id,
                "job_id": job_id,
                "sequence": index,
                "event_type": event_type,
                "available_at": NOW if event_type == "retry.scheduled" else None,
                "created_at": NOW,
            },
        )
    rejects(
        engine,
        "run_events",
        {
            "run_id": run_id,
            "job_id": job_id,
            "sequence": 99,
            "event_type": "tool.cancelled",
            "created_at": NOW,
        },
    )
    rejects(
        engine,
        "run_events",
        {
            "run_id": run_id,
            "job_id": job_id,
            "sequence": 98,
            "event_type": "run.paused",
            "created_at": NOW,
        },
    )


def test_a_tool_event_may_point_at_its_invocation_and_other_events_may_not(engine: Engine) -> None:
    """The pointer is nullable -- an unresolvable tool has no invocation -- and is a real FK."""
    run_id, job_id, attempt_id = seed_parents(engine)
    definition = seed_definition(engine)
    insert(
        engine,
        "tool_invocations",
        invocation_row(run_id, job_id, attempt_id, tool_definition_id=definition),
    )
    invocation_id = int(scalar(engine, "SELECT max(id) FROM tool_invocations") or 0)
    insert(
        engine,
        "run_events",
        {
            "run_id": run_id,
            "job_id": job_id,
            "sequence": 1,
            "event_type": "tool.requested",
            "tool_invocation_id": invocation_id,
            "created_at": NOW,
        },
    )
    assert (
        scalar(engine, "SELECT tool_invocation_id FROM run_events WHERE sequence=1")
        == invocation_id
    )
    # A lifecycle event simply does not carry one.
    insert(
        engine,
        "run_events",
        {
            "run_id": run_id,
            "job_id": job_id,
            "sequence": 2,
            "event_type": "run.queued",
            "created_at": NOW,
        },
    )
    assert scalar(engine, "SELECT tool_invocation_id FROM run_events WHERE sequence=2") is None
    # It is a real reference, and it is RESTRICT, so an invocation with a published event cannot
    # be deleted out from under the timeline.
    rejects(
        engine,
        "run_events",
        {
            "run_id": run_id,
            "job_id": job_id,
            "sequence": 3,
            "event_type": "tool.started",
            "tool_invocation_id": 999,
            "created_at": NOW,
        },
    )
    with pytest.raises(IntegrityError):
        execute(engine, "DELETE FROM tool_invocations WHERE id=:i", i=invocation_id)


# ---------------------------------------------------------------------------------------
# Indexes and query plans -- justified by the queries that exist, and no others
# ---------------------------------------------------------------------------------------


def test_d1_adds_no_index_to_any_existing_hot_table(engine: Engine) -> None:
    """The structural reason no Worker hot path can have changed cost.

    The claim path, the fairness path and the admission path all read `jobs`, `job_attempts` and
    `queue_partitions`. D1 adds no index to any of them, so no claim statement's plan can differ
    from the one C6 measured.
    """
    inspector = inspect(engine)
    assert {item["name"] for item in inspector.get_indexes("queue_partitions")} == set()
    assert "tool" not in inspect(engine).get_indexes("jobs").__str__()
    assert {item["name"] for item in inspector.get_indexes("run_events")} == {
        "ix_run_events_attempt_id_id"
    }


def test_the_stage_d_index_set_is_exactly_the_justified_one(engine: Engine) -> None:
    inspector = inspect(engine)
    assert {item["name"] for item in inspector.get_indexes("mcp_connections")} == {
        "ix_mcp_connections_owner_user_id_id"
    }
    assert {item["name"] for item in inspector.get_indexes("tool_definitions")} == {
        "uq_tool_definitions_builtin",
        "uq_tool_definitions_mcp",
        "uq_tool_definitions_model_name",
        "ix_tool_definitions_source_id",
    }
    assert {item["name"] for item in inspector.get_indexes("agent_tool_grants")} == {
        "ix_agent_tool_grants_tool_definition_id"
    }
    # `tool_invocations` adds no explicit index: `UNIQUE(attempt_id, tool_sequence)` already
    # materialises the index both the audit read and the replay predicate seek.
    assert {item["name"] for item in inspector.get_indexes("tool_invocations")} == set()


def test_the_stage_d_indexes_are_partial_and_are_used(engine: Engine) -> None:
    """Prove the partial indexes exist *and* serve the queries, by plan rather than by name."""
    with engine.connect() as connection:
        for table in STAGE_D_TABLES:
            connection.execute(text(f"SELECT count(*) FROM {table}"))
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM sqlite_master WHERE type='index'"
                    " AND name IN ('uq_tool_definitions_builtin','uq_tool_definitions_mcp')"
                    " AND sql LIKE '%WHERE%'"
                )
            )
            == 2
        )


def test_the_stage_d_queries_are_index_served(engine: Engine) -> None:
    """Every Stage D query seeks a named index; none scans a table that can grow.

    Asserted on the plan's *shape* rather than its text, so a SQLite version that words the plan
    differently cannot make this vacuous.
    """
    statements = (
        # owner-scoped connection list
        "SELECT id, display_name FROM mcp_connections WHERE owner_user_id = 1 ORDER BY id",
        # definitions of one connection
        "SELECT id FROM tool_definitions WHERE source_id = 1",
        # resolve a definition by identity
        "SELECT id FROM tool_definitions WHERE source_kind='mcp' AND source_id=1"
        " AND upstream_name='read_file'",
        # the exact call-time grant lookup
        "SELECT id FROM agent_tool_grants WHERE agent_instance_id=1 AND tool_definition_id=1",
        # the grants for one Agent's catalog
        "SELECT tool_definition_id FROM agent_tool_grants WHERE agent_instance_id=1",
        # the replay guard: dispatched invocations of one Attempt
        "SELECT id FROM tool_invocations WHERE attempt_id=1"
        " AND status IN ('started','succeeded','failed','ambiguous')",
    )
    with engine.connect() as connection:
        for statement in statements:
            plan = " | ".join(
                str(row[3]) for row in connection.exec_driver_sql(f"EXPLAIN QUERY PLAN {statement}")
            )
            assert "SEARCH" in plan, (statement, plan)
            for table in (*STAGE_D_TABLES, "runs", "jobs", "job_attempts"):
                assert f"SCAN {table}" not in plan, (statement, plan)


def test_the_sqlite_invariants_stage_c_fixed_still_hold(engine: Engine) -> None:
    with engine.connect() as connection:
        assert connection.scalar(text("PRAGMA journal_mode")) == "delete"
        assert connection.scalar(text("PRAGMA foreign_keys")) == 1
        assert connection.exec_driver_sql("PRAGMA integrity_check").scalar() == "ok"
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
