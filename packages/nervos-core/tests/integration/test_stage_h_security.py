"""Stage-H persistence and gate tests: secrets, connections, approvals, publisher trust.

Each test asserts durable state through the same public ports the API and the Worker use. No
test reads a secret value except the broker, and the broker is only ever asked the question the
dispatch path asks it.
"""

from __future__ import annotations

import base64
import os
from datetime import timedelta
from pathlib import Path

import pytest
from execution_support import NOW, TEST_CAPABILITY, migrate
from nervos_core.application.account_connections import (
    AccountConnectionService,
    BrokerDecision,
    CredentialBroker,
)
from nervos_core.application.approvals import (
    CONSUMED,
    ApprovalAdmission,
    ApprovalRequest,
    ApprovalService,
    requires_approval,
    safe_preview,
)
from nervos_core.application.publisher_trust import (
    PublisherRevoked,
    PublisherTrustService,
    ensure_execution_admissible,
    ensure_installable,
)
from nervos_core.application.secrets import (
    SecretManager,
    SecretNotFound,
    SecretReferenced,
    SecretResolver,
    SecretStoreUnavailable,
    SecretWrite,
)
from nervos_core.domain.tools import ToolDescriptor, ToolSourceKind
from nervos_core.infrastructure.database.account_connections import (
    SqlAlchemyAccountConnectionPersistence,
)
from nervos_core.infrastructure.database.approvals import (
    SqlAlchemyApprovalGate,
    SqlAlchemyApprovalPersistence,
)
from nervos_core.infrastructure.database.publisher_trust import (
    SqlAlchemyPublisherTrustPersistence,
)
from nervos_core.infrastructure.database.secrets import SqlAlchemySecretPersistence
from nervos_core.infrastructure.security.secret_keys import FileMasterKeyResolver
from sqlalchemy import Engine, text

SECRET_VALUE = "ghp-example-value-do-not-use"
FINGERPRINT = "b" * 64
OTHER_FINGERPRINT = "c" * 64


def _key_file(tmp_path: Path) -> Path:
    key_file = tmp_path / "master.key"
    key_file.write_text(base64.b64encode(os.urandom(32)).decode("ascii"), encoding="utf-8")
    # The resolver enforces owner-only access on POSIX, so the fixture provisions it that way.
    os.chmod(key_file, 0o600)
    return key_file


def _manager(engine: Engine, key_file: Path) -> SecretManager:
    return SecretManager(
        SqlAlchemySecretPersistence(engine), FileMasterKeyResolver(key_file, 1), lambda: NOW
    )


# -- H1: encrypted secret store -------------------------------------------------------------


def test_a_secret_is_stored_encrypted_and_never_returned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "secrets.db", monkeypatch)
    manager = _manager(engine, _key_file(tmp_path))

    metadata = manager.create(1, SecretWrite("github", SECRET_VALUE, "github"))

    with engine.connect() as connection:
        stored = (
            connection.execute(
                text("SELECT ciphertext, nonce FROM secrets WHERE id = :id"), {"id": metadata.id}
            )
            .mappings()
            .one()
        )
    assert SECRET_VALUE.encode("utf-8") not in bytes(stored["ciphertext"])
    assert bytes(stored["ciphertext"])
    # The API-shaped view carries no value field at all, by construction.
    assert not hasattr(metadata, "value")
    assert metadata.status == "active"
    assert manager.list(1)[0].name == "github"


def test_the_broker_resolves_only_an_owned_active_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "secrets.db", monkeypatch)
    key_file = _key_file(tmp_path)
    manager = _manager(engine, key_file)
    resolver = SecretResolver(
        SqlAlchemySecretPersistence(engine), FileMasterKeyResolver(key_file, 1)
    )
    metadata = manager.create(1, SecretWrite("github", SECRET_VALUE))

    assert resolver.resolve_active_value(1, metadata.id) == SECRET_VALUE
    with pytest.raises(SecretNotFound):
        resolver.resolve_active_value(2, metadata.id)  # a different owner
    manager.set_status(1, metadata.id, "disabled")
    with pytest.raises(SecretNotFound):
        resolver.resolve_active_value(1, metadata.id)


def test_revocation_destroys_the_ciphertext(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "secrets.db", monkeypatch)
    key_file = _key_file(tmp_path)
    manager = _manager(engine, key_file)
    metadata = manager.create(1, SecretWrite("github", SECRET_VALUE))

    revoked = manager.set_status(1, metadata.id, "revoked")

    assert revoked.status == "revoked"
    with engine.connect() as connection:
        row = (
            connection.execute(
                text("SELECT ciphertext FROM secrets WHERE id = :id"), {"id": metadata.id}
            )
            .mappings()
            .one()
        )
    assert bytes(row["ciphertext"]) == b""
    resolver = SecretResolver(
        SqlAlchemySecretPersistence(engine), FileMasterKeyResolver(key_file, 1)
    )
    with pytest.raises(SecretNotFound):
        resolver.resolve_active_value(1, metadata.id)


def test_an_unusable_key_file_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = migrate(tmp_path / "secrets.db", monkeypatch)
    manager = SecretManager(
        SqlAlchemySecretPersistence(engine),
        FileMasterKeyResolver(tmp_path / "absent.key", 1),
        lambda: NOW,
    )
    with pytest.raises(SecretStoreUnavailable):
        manager.create(1, SecretWrite("github", SECRET_VALUE))
    with engine.connect() as connection:
        assert int(connection.scalar(text("SELECT count(*) FROM secrets"))) == 0


# -- H2: account connections and the credential broker --------------------------------------


def test_the_broker_refuses_every_unproven_predicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "connections.db", monkeypatch)
    key_file = _key_file(tmp_path)
    manager = _manager(engine, key_file)
    connections = SqlAlchemyAccountConnectionPersistence(engine)
    service = AccountConnectionService(connections, lambda: NOW)
    broker = CredentialBroker(
        connections,
        SecretResolver(SqlAlchemySecretPersistence(engine), FileMasterKeyResolver(key_file, 1)),
        lambda: NOW,
    )
    secret = manager.create(1, SecretWrite("github", SECRET_VALUE))
    connection = service.connect(
        1,
        provider="github",
        display_name="GitHub",
        secret_id=secret.id,
        scopes=("repo:read",),
    )

    assert broker.check(
        owner_user_id=1, connection_id=connection.id, required_scope="repo:read"
    ).allowed
    for decision in (
        broker.check(owner_user_id=2, connection_id=connection.id, required_scope="repo:read"),
        broker.check(owner_user_id=1, connection_id=connection.id, required_scope="repo:write"),
    ):
        assert decision.allowed is False
        assert decision.reason_code is not None

    service.disconnect(1, connection.id)
    assert broker.check(
        owner_user_id=1, connection_id=connection.id, required_scope="repo:read"
    ) == BrokerDecision(False, "connection_not_connected")


def test_a_referenced_secret_cannot_be_revoked_or_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "connections.db", monkeypatch)
    manager = _manager(engine, _key_file(tmp_path))
    secret = manager.create(1, SecretWrite("github", SECRET_VALUE))
    service = AccountConnectionService(SqlAlchemyAccountConnectionPersistence(engine), lambda: NOW)
    service.connect(
        1,
        provider="github",
        display_name="GitHub",
        secret_id=secret.id,
        scopes=("repo:read",),
    )

    with pytest.raises(SecretReferenced):
        manager.set_status(1, secret.id, "revoked")
    with pytest.raises(SecretReferenced):
        manager.delete(1, secret.id)


# -- H3: durable per-action approvals --------------------------------------------------------


def _mcp_descriptor() -> ToolDescriptor:
    return ToolDescriptor(
        tool_definition_id=7,
        upstream_name="search",
        model_name="claude",
        source_kind=ToolSourceKind.MCP,
        source_id=3,
        display_name="Search",
        description="Search the index.",
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
        output_schema=None,
        risk_hints=_risk_hints(),
        fingerprint="d" * 64,
    )


def _risk_hints():
    from nervos_core.domain.tools import RiskHints

    return RiskHints()


def _submit_run(
    engine: Engine, *, upstream_name: str = "search", worker_id: str = "worker-h"
) -> tuple[int, int, int, int]:
    """Submit one Run, claim one Attempt, and define one tool.

    Returns ``(run_id, job_id, attempt_id, tool_definition_id)``.

    The approval row carries real foreign keys to the Run, the Job, the Attempt, and the Tool
    Definition, so a test that fakes any of them would prove nothing about the gate: the
    identity the approval binds must be durable identity.
    """
    from nervos_core.domain.runs import RunLimits
    from nervos_core.infrastructure.database.jobs import (
        SqlAlchemyJobExecutionPersistence,
        SqlAlchemyJobPersistence,
    )

    run = SqlAlchemyJobPersistence(engine).submit(
        owner_user_id=1, agent_instance_id=1, input_text="go", limits=RunLimits(), now=NOW
    )
    with engine.begin() as connection:
        tool_id = int(
            connection.execute(
                text(
                    "INSERT INTO tool_definitions"
                    "(source_kind,upstream_name,model_name,display_name,description,input_schema,"
                    "fingerprint,status,created_at,updated_at)"
                    " VALUES('builtin',:upstream,:model,:display,'Search the index.','{}',"
                    ":f,'available',:n,:n) RETURNING id"
                ),
                {
                    "f": "d" * 64,
                    "n": NOW,
                    "upstream": upstream_name,
                    "model": f"nervos__builtin__{upstream_name}",
                    "display": upstream_name.title(),
                },
            ).scalar_one()
        )
    store = SqlAlchemyJobExecutionPersistence(engine)
    moment = NOW + timedelta(seconds=1)
    store.register_worker(worker_id=worker_id, capability=TEST_CAPABILITY, now=moment)
    claim = store.claim_next(
        worker_id=worker_id,
        provider_ids=("anthropic",),
        max_active=4,
        now=moment,
        lease_duration=timedelta(seconds=60),
    )
    assert claim is not None
    assert store.start_attempt(claim, now=moment)
    return run.id, claim.job_id, claim.attempt_id, tool_id


def test_an_unapproved_external_action_is_refused_and_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "approvals.db", monkeypatch)
    run_id, job_id, attempt_id, tool_id = _submit_run(engine)
    descriptor = _mcp_descriptor()
    gate = SqlAlchemyApprovalGate(engine)
    request = ApprovalRequest(
        agent_instance_id=1,
        run_id=run_id,
        job_id=job_id,
        attempt_id=attempt_id,
        tool_sequence=1,
        tool_definition_id=tool_id,
        upstream_name=descriptor.upstream_name,
        fingerprint=descriptor.fingerprint,
        arguments={"q": "annual report"},
    )

    assert requires_approval(descriptor) is True
    assert gate.admit(request, now=NOW) is ApprovalAdmission.REQUESTED
    # A second attempt before a decision re-finds the same pending request rather than adding one.
    assert gate.admit(request, now=NOW) is ApprovalAdmission.REQUESTED
    with engine.connect() as connection:
        assert int(connection.scalar(text("SELECT count(*) FROM action_approvals"))) == 1

    pending = SqlAlchemyApprovalPersistence(engine).list_pending(owner_user_id=1)
    assert len(pending) == 1
    # The owner-facing preview is redacted, and the model never sees the raw argument.
    assert pending[0].preview["q"] == "<string:13 bytes>"
    assert "annual report" not in str(pending[0].preview)


def test_an_approval_authorizes_exactly_one_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "approvals.db", monkeypatch)
    run_id, job_id, attempt_id, tool_id = _submit_run(engine)
    descriptor = _mcp_descriptor()
    gate = SqlAlchemyApprovalGate(engine)
    store = SqlAlchemyApprovalPersistence(engine)
    service = ApprovalService(store, lambda: NOW)
    request = ApprovalRequest(
        agent_instance_id=1,
        run_id=run_id,
        job_id=job_id,
        attempt_id=attempt_id,
        tool_sequence=1,
        tool_definition_id=tool_id,
        upstream_name=descriptor.upstream_name,
        fingerprint=descriptor.fingerprint,
        arguments={"q": "annual report"},
    )
    assert gate.admit(request, now=NOW) is ApprovalAdmission.REQUESTED
    approval = service.list_pending(owner_user_id=1)[0]

    decided = service.decide(owner_user_id=1, approval_id=approval.id, approve=True)

    assert decided.state == "approved"
    assert gate.admit(request, now=NOW) is ApprovalAdmission.APPROVED
    first = gate.consume(request, consuming_attempt_id=attempt_id, now=NOW)
    assert first.kind == CONSUMED
    # The second dispatch finds nothing approved: one approval, one dispatch.
    assert gate.consume(request, consuming_attempt_id=attempt_id, now=NOW).kind != CONSUMED
    assert gate.admit(request, now=NOW) is ApprovalAdmission.REFUSED


def test_a_denial_and_an_expiry_are_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "approvals.db", monkeypatch)
    run_id, job_id, attempt_id, tool_id = _submit_run(engine)
    descriptor = _mcp_descriptor()
    gate = SqlAlchemyApprovalGate(engine)
    service = ApprovalService(SqlAlchemyApprovalPersistence(engine), lambda: NOW)
    denied = ApprovalRequest(
        agent_instance_id=1,
        run_id=run_id,
        job_id=job_id,
        attempt_id=attempt_id,
        tool_sequence=1,
        tool_definition_id=tool_id,
        upstream_name=descriptor.upstream_name,
        fingerprint=descriptor.fingerprint,
        arguments={"q": "one"},
    )
    gate.admit(denied, now=NOW)
    approval = service.list_pending(owner_user_id=1)[0]
    service.decide(owner_user_id=1, approval_id=approval.id, approve=False)
    assert gate.admit(denied, now=NOW) is ApprovalAdmission.REFUSED
    assert service.get(owner_user_id=1, approval_id=approval.id).state == "denied"

    expiring = ApprovalRequest(
        agent_instance_id=1,
        run_id=run_id,
        job_id=job_id,
        attempt_id=attempt_id,
        tool_sequence=2,
        tool_definition_id=tool_id,
        upstream_name=descriptor.upstream_name,
        fingerprint=descriptor.fingerprint,
        arguments={"q": "two"},
    )
    gate.admit(expiring, now=NOW)
    second = service.list_pending(owner_user_id=1)[0]
    service.decide(owner_user_id=1, approval_id=second.id, approve=True)
    later = NOW + timedelta(hours=2)
    assert gate.admit(expiring, now=later) is ApprovalAdmission.REFUSED
    assert service.get(owner_user_id=1, approval_id=second.id).state == "expired"


def test_an_approval_never_authorizes_another_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same action in a different Run is a different action (ADR 0034).

    Identity is the full tuple -- owner, Run, Job, sequence, tool identity, and the digest of
    the exact input -- so an approval granted for one Run cannot be replayed by another Run that
    happens to issue an identical call at the same sequence.
    """
    engine = migrate(tmp_path / "approvals.db", monkeypatch)
    first_run, first_job, first_attempt, tool_id = _submit_run(engine)
    second_run, second_job, second_attempt, second_tool_id = _submit_run(
        engine, upstream_name="fetch", worker_id="worker-h2"
    )
    descriptor = _mcp_descriptor()
    gate = SqlAlchemyApprovalGate(engine)
    service = ApprovalService(SqlAlchemyApprovalPersistence(engine), lambda: NOW)

    def request(run_id: int, job_id: int, attempt_id: int, definition_id: int) -> ApprovalRequest:
        return ApprovalRequest(
            agent_instance_id=1,
            run_id=run_id,
            job_id=job_id,
            attempt_id=attempt_id,
            tool_sequence=1,
            tool_definition_id=definition_id,
            upstream_name=descriptor.upstream_name,
            fingerprint=descriptor.fingerprint,
            arguments={"q": "one"},
        )

    granted = request(first_run, first_job, first_attempt, tool_id)
    assert gate.admit(granted, now=NOW) is ApprovalAdmission.REQUESTED
    service.decide(
        owner_user_id=1,
        approval_id=service.list_pending(owner_user_id=1)[0].id,
        approve=True,
    )

    replayed = request(second_run, second_job, second_attempt, second_tool_id)
    assert gate.admit(replayed, now=NOW) is ApprovalAdmission.REQUESTED
    assert gate.consume(replayed, consuming_attempt_id=second_attempt, now=NOW).kind != CONSUMED
    # The granted Run still holds its own, single, unspent approval.
    assert gate.admit(granted, now=NOW) is ApprovalAdmission.APPROVED


def test_another_owner_can_never_read_or_decide_an_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "approvals.db", monkeypatch)
    run_id, job_id, attempt_id, tool_id = _submit_run(engine)
    descriptor = _mcp_descriptor()
    gate = SqlAlchemyApprovalGate(engine)
    service = ApprovalService(SqlAlchemyApprovalPersistence(engine), lambda: NOW)
    gate.admit(
        ApprovalRequest(
            agent_instance_id=1,
            run_id=run_id,
            job_id=job_id,
            attempt_id=attempt_id,
            tool_sequence=1,
            tool_definition_id=tool_id,
            upstream_name=descriptor.upstream_name,
            fingerprint=descriptor.fingerprint,
            arguments={"q": "one"},
        ),
        now=NOW,
    )
    approval = service.list_pending(owner_user_id=1)[0]

    assert service.list_pending(owner_user_id=2) == ()
    with pytest.raises(LookupError):
        service.get(owner_user_id=2, approval_id=approval.id)


def test_the_safe_preview_redacts_and_bounds() -> None:
    preview = safe_preview(
        {
            "long": "x" * 64,
            "short": "ok",
            "count": 3,
            "flag": True,
            "nested": {"a": 1},
            "items": [1, 2, 3],
        }
    )
    assert preview["long"] == "<string:64 bytes>"
    # Even a short string is redacted: a token or a path is short too.
    assert preview["short"] == "<string:2 bytes>"
    assert preview["count"] == 3
    assert preview["flag"] is True
    assert preview["nested"] == "<object:1 keys>"
    assert preview["items"] == "<array:3 items>"


# -- H5: publisher trust and local revocation ------------------------------------------------


def test_only_revocation_is_a_wall(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = migrate(tmp_path / "trust.db", monkeypatch)
    service = PublisherTrustService(SqlAlchemyPublisherTrustPersistence(engine), lambda: NOW)

    # An unknown signer installs: existing Stage-G authorization is still required.
    assert service.state_for(FINGERPRINT) == "untrusted"
    ensure_installable(service, FINGERPRINT)
    ensure_execution_admissible(service, FINGERPRINT)

    service.decide(signer_fingerprint=FINGERPRINT, state="trusted", decided_by=1)
    ensure_installable(service, FINGERPRINT)

    service.decide(signer_fingerprint=FINGERPRINT, state="revoked", decided_by=1, reason="key leak")
    with pytest.raises(PublisherRevoked):
        ensure_installable(service, FINGERPRINT)
    with pytest.raises(PublisherRevoked):
        ensure_execution_admissible(service, FINGERPRINT)
    # Another signer is untouched by one revocation.
    ensure_execution_admissible(service, OTHER_FINGERPRINT)
