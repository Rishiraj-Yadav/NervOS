"""Tests for the NervOS CLI commands and parser."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from nervos_cli.client import NervosClient, NervosClientError
from nervos_cli.main import build_parser, main


def test_cli_parser_help() -> None:
    parser = build_parser()
    assert parser.prog == "nervos"


def test_cli_auth_status_authenticated(capsys: pytest.CaptureFixture[str]) -> None:
    mock_client = MagicMock(spec=NervosClient)
    mock_client.me.return_value = {"id": 1, "username": "admin", "role": "administrator"}

    with patch("nervos_cli.main.NervosClient", return_value=mock_client):
        code = main(["auth", "status"])
        assert code == 0
        captured = capsys.readouterr()
        assert "Authenticated as admin" in captured.out


def test_cli_auth_status_unauthenticated(capsys: pytest.CaptureFixture[str]) -> None:
    mock_client = MagicMock(spec=NervosClient)
    mock_client.me.side_effect = NervosClientError("unauthenticated", "Auth required", 401)

    with patch("nervos_cli.main.NervosClient", return_value=mock_client):
        code = main(["auth", "status"])
        assert code == 1
        captured = capsys.readouterr()
        assert "Not authenticated." in captured.err


def test_cli_package_inspect(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    dummy_pkg = tmp_path / "test.nervos"
    dummy_pkg.write_bytes(b"dummy")

    mock_client = MagicMock(spec=NervosClient)
    mock_client.inspect_package.return_value = {
        "package_id": "com.example.agent",
        "package_version": "1.0.0",
        "display_name": "Test Agent",
        "signer_fingerprint": "abc123def456",
        "content_digest": "sha256:789",
        "archive_digest": "sha256:012",
        "is_compatible": True,
        "entrypoint_module": "agent",
        "entrypoint_object": "Agent",
        "tools_required": ["invoices.read"],
    }

    with patch("nervos_cli.main.NervosClient", return_value=mock_client):
        code = main(["package", "inspect", str(dummy_pkg)])
        assert code == 0
        captured = capsys.readouterr()
        assert "com.example.agent" in captured.out
        assert "abc123def456" in captured.out


def test_cli_package_install_yes_alone_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dummy_pkg = tmp_path / "test.nervos"
    dummy_pkg.write_bytes(b"dummy")

    mock_client = MagicMock(spec=NervosClient)
    mock_client.inspect_package.return_value = {
        "package_id": "com.example.agent",
        "package_version": "1.0.0",
        "content_digest": "content_sha",
        "signer_fingerprint": "signer_fp_123",
        "archive_digest": "archive_sha",
        "is_compatible": True,
    }

    with patch("nervos_cli.main.NervosClient", return_value=mock_client):
        code = main(["package", "install", str(dummy_pkg), "--yes"])
        assert code == 1
        captured = capsys.readouterr()
        assert "Non-interactive installation requires both" in captured.err


def test_cli_package_install_signer_only_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dummy_pkg = tmp_path / "test.nervos"
    dummy_pkg.write_bytes(b"dummy")

    mock_client = MagicMock(spec=NervosClient)
    mock_client.inspect_package.return_value = {
        "package_id": "com.example.agent",
        "package_version": "1.0.0",
        "content_digest": "content_sha",
        "signer_fingerprint": "signer_fp_123",
        "archive_digest": "archive_sha",
        "is_compatible": True,
    }

    with patch("nervos_cli.main.NervosClient", return_value=mock_client):
        code = main(
            [
                "package",
                "install",
                str(dummy_pkg),
                "--approve-signer",
                "signer_fp_123",
                "--yes",
            ]
        )
        assert code == 1
        captured = capsys.readouterr()
        assert "Non-interactive installation requires both" in captured.err


def test_cli_package_install_content_digest_only_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dummy_pkg = tmp_path / "test.nervos"
    dummy_pkg.write_bytes(b"dummy")

    mock_client = MagicMock(spec=NervosClient)
    mock_client.inspect_package.return_value = {
        "package_id": "com.example.agent",
        "package_version": "1.0.0",
        "content_digest": "content_sha",
        "signer_fingerprint": "signer_fp_123",
        "archive_digest": "archive_sha",
        "is_compatible": True,
    }

    with patch("nervos_cli.main.NervosClient", return_value=mock_client):
        code = main(
            [
                "package",
                "install",
                str(dummy_pkg),
                "--approve-content-digest",
                "content_sha",
                "--yes",
            ]
        )
        assert code == 1
        captured = capsys.readouterr()
        assert "Non-interactive installation requires both" in captured.err


def test_cli_package_install_non_interactive_approval(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dummy_pkg = tmp_path / "test.nervos"
    dummy_pkg.write_bytes(b"dummy")

    mock_client = MagicMock(spec=NervosClient)
    mock_client.inspect_package.return_value = {
        "package_id": "com.example.agent",
        "package_version": "1.0.0",
        "content_digest": "content_sha",
        "signer_fingerprint": "signer_fp_123",
        "archive_digest": "archive_sha",
        "is_compatible": True,
    }
    mock_client.install_package.return_value = {
        "package_id": "com.example.agent",
        "package_version": "1.0.0",
        "status": "active",
    }

    with patch("nervos_cli.main.NervosClient", return_value=mock_client):
        code = main(
            [
                "package",
                "install",
                str(dummy_pkg),
                "--approve-signer",
                "signer_fp_123",
                "--approve-content-digest",
                "content_sha",
                "--yes",
            ]
        )
        assert code == 0
        captured = capsys.readouterr()
        assert "Successfully installed com.example.agent@1.0.0" in captured.out


def test_cli_package_install_fingerprint_mismatch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dummy_pkg = tmp_path / "test.nervos"
    dummy_pkg.write_bytes(b"dummy")

    mock_client = MagicMock(spec=NervosClient)
    mock_client.inspect_package.return_value = {
        "package_id": "com.example.agent",
        "package_version": "1.0.0",
        "content_digest": "content_sha",
        "signer_fingerprint": "actual_fp",
        "archive_digest": "archive_sha",
        "is_compatible": True,
    }

    with patch("nervos_cli.main.NervosClient", return_value=mock_client):
        code = main(
            [
                "package",
                "install",
                str(dummy_pkg),
                "--approve-signer",
                "wrong_fp",
                "--approve-content-digest",
                "content_sha",
            ]
        )
        assert code == 1
        captured = capsys.readouterr()
        assert "Signer fingerprint mismatch" in captured.err


def test_cli_package_install_content_digest_mismatch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dummy_pkg = tmp_path / "test.nervos"
    dummy_pkg.write_bytes(b"dummy")

    mock_client = MagicMock(spec=NervosClient)
    mock_client.inspect_package.return_value = {
        "package_id": "com.example.agent",
        "package_version": "1.0.0",
        "content_digest": "actual_content_sha",
        "signer_fingerprint": "actual_fp",
        "archive_digest": "archive_sha",
        "is_compatible": True,
    }

    with patch("nervos_cli.main.NervosClient", return_value=mock_client):
        code = main(
            [
                "package",
                "install",
                str(dummy_pkg),
                "--approve-signer",
                "actual_fp",
                "--approve-content-digest",
                "wrong_content_sha",
            ]
        )
        assert code == 1
        captured = capsys.readouterr()
        assert "Content digest mismatch" in captured.err


def test_cli_package_list_json(capsys: pytest.CaptureFixture[str]) -> None:
    mock_client = MagicMock(spec=NervosClient)
    mock_client.list_packages.return_value = [
        {"package_id": "com.example.agent", "package_version": "1.0.0", "status": "active"}
    ]

    with patch("nervos_cli.main.NervosClient", return_value=mock_client):
        code = main(["package", "list", "--json"])
        assert code == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert len(data) == 1
        assert data[0]["package_id"] == "com.example.agent"


def test_cli_agent_create(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_file = tmp_path / "cfg.json"
    config_file.write_text('{"greeting": "Hello"}', encoding="utf-8")

    mock_client = MagicMock(spec=NervosClient)
    mock_client.create_agent.return_value = {
        "id": 10,
        "display_name": "My Agent",
        "agent_key": "com.example.agent",
        "agent_definition_version": "1.0.0",
    }

    with patch("nervos_cli.main.NervosClient", return_value=mock_client):
        code = main(
            [
                "agent",
                "create",
                "--name",
                "My Agent",
                "--package",
                "com.example.agent@1.0.0",
                "--provider",
                "anthropic",
                "--model",
                "claude-3-5-sonnet",
                "--config",
                str(config_file),
            ]
        )
        assert code == 0
        captured = capsys.readouterr()
        assert "Created agent instance 10: My Agent" in captured.out
