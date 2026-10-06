"""Real ciphertext rotation with isolated keys and databases."""

from __future__ import annotations

import base64
import os
from pathlib import Path

import pytest
from execution_support import NOW, migrate
from nervos_core.application.secrets import (
    SecretManager,
    SecretReferenced,
    SecretResolver,
    SecretStoreUnavailable,
    SecretWrite,
)
from nervos_core.infrastructure.database.secrets import SqlAlchemySecretPersistence
from nervos_core.infrastructure.security.secret_keys import FileMasterKeyResolver


def _write_key(path: Path, material: bytes) -> None:
    """Provision a key file the way the resolver requires: owner-only on POSIX."""
    path.write_bytes(material)
    os.chmod(path, 0o600)


def test_old_ciphertext_survives_rotation_and_retirement_is_fenced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = migrate(tmp_path / "rotation.db", monkeypatch)
    key_file = tmp_path / "master.key"
    old_key = base64.b64encode(os.urandom(32))
    _write_key(key_file, old_key)
    store = SqlAlchemySecretPersistence(engine)
    old = SecretManager(store, FileMasterKeyResolver(key_file, 1), lambda: NOW)
    old.rotate_active_key()
    a = old.create(1, SecretWrite("first", "synthetic first value"))
    b = old.create(1, SecretWrite("second", "synthetic second value"))
    old.set_status(1, b.id, "disabled")
    _write_key(key_file.with_name("master.key.v1"), old_key)
    _write_key(key_file, base64.b64encode(os.urandom(32)))
    keys = FileMasterKeyResolver(key_file, 2)
    manager = SecretManager(store, keys, lambda: NOW)
    resolver = SecretResolver(store, keys)
    manager.rotate_active_key()
    assert resolver.resolve_active_value(1, a.id) == "synthetic first value"
    with pytest.raises(SecretReferenced):
        manager.retire_key_version(1)
    assert manager.reencrypt_batch(1) == 1
    # Interruption between batches preserves both old/new ciphertext readability.
    assert resolver.resolve_active_value(1, a.id) == "synthetic first value"
    assert manager.reencrypt_batch(1) == 1
    assert manager.reencrypt_batch(1) == 0
    manager.retire_key_version(1)
    key_file.with_name("master.key.v1").unlink()
    manager.set_status(1, b.id, "active")
    assert resolver.resolve_active_value(1, b.id) == "synthetic second value"
    with pytest.raises(SecretReferenced):
        manager.retire_key_version(2)
    assert {item.key_version for item in manager.list(1)} == {2}


def test_missing_old_key_rolls_back_the_entire_maintenance_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = migrate(tmp_path / "rotation.db", monkeypatch)
    key_file = tmp_path / "master.key"
    _write_key(key_file, base64.b64encode(os.urandom(32)))
    store = SqlAlchemySecretPersistence(engine)
    old = SecretManager(store, FileMasterKeyResolver(key_file, 1), lambda: NOW)
    secret = old.create(1, SecretWrite("first", "synthetic value"))
    _write_key(key_file, base64.b64encode(os.urandom(32)))
    manager = SecretManager(store, FileMasterKeyResolver(key_file, 2), lambda: NOW)
    with pytest.raises(SecretStoreUnavailable):
        manager.reencrypt_batch()
    assert manager.inspect(1, secret.id).key_version == 1
    with pytest.raises(SecretStoreUnavailable):
        SecretResolver(store, FileMasterKeyResolver(key_file, 2)).resolve_active_value(1, secret.id)


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes are not representable on Windows")
def test_a_key_file_reachable_by_other_users_is_refused(tmp_path: Path) -> None:
    """Owner-only access is enforced, not merely documented, wherever the OS can express it."""
    key_file = tmp_path / "master.key"
    material = base64.b64encode(os.urandom(32))
    _write_key(key_file, material)
    resolver = FileMasterKeyResolver(key_file, 1)
    assert resolver.resolve() == (1, base64.b64decode(material))

    os.chmod(key_file, 0o644)
    with pytest.raises(SecretStoreUnavailable):
        FileMasterKeyResolver(key_file, 1).resolve()

    # Rotation material is gated by the same rule: a readable `.vN` file is refused too.
    historical = key_file.with_name("master.key.v2")
    _write_key(historical, material)
    assert FileMasterKeyResolver(key_file, 1).resolve_version(2) == base64.b64decode(material)
    os.chmod(historical, 0o640)
    with pytest.raises(SecretStoreUnavailable):
        FileMasterKeyResolver(key_file, 1).resolve_version(2)
