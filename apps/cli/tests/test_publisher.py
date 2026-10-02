"""Hosted publisher credentials never fall back to plaintext or local runtime auth."""

import json
import time

import httpx
import pytest
from nervos_cli.publisher import PublisherClient, origin


@pytest.mark.parametrize(
    "value",
    [
        "http://marketplace.example",
        "https://user@host",
        "https://host/path",
        "https://host/?token=x",
    ],
)
def test_publisher_requires_https_origin(value: str) -> None:
    with pytest.raises(ValueError):
        origin(value)


def test_publisher_rejects_file_credential_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    import keyring

    class FileStore:
        pass

    monkeypatch.setattr(keyring, "get_keyring", lambda: FileStore())
    client = PublisherClient("https://marketplace.example")
    try:
        with pytest.raises(ValueError, match="OS credential store"):
            client.request("POST", "/publishers")
    finally:
        client.close()


def test_publisher_uses_origin_scoped_os_token_without_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import keyring

    backend = type("SecureStore", (), {"__module__": "keyring.backends.Windows"})
    monkeypatch.setattr(keyring, "get_keyring", lambda: backend())

    def credential(service: str, username: str) -> str | None:
        return (
            json.dumps(
                {
                    "token": "synthetic-cli-token",
                    "expires_at": time.time() + 60,
                }
            )
            if service == "nervos-marketplace:https://marketplace.example"
            else None
        )

    monkeypatch.setattr(keyring, "get_password", credential)
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"items": []})

    client = PublisherClient("https://marketplace.example")
    client.http.close()
    client.http = httpx.Client(
        base_url=client.origin, transport=httpx.MockTransport(respond), trust_env=False
    )
    try:
        assert client.request("GET", "/publishers") == {"items": []}
        assert requests[0].headers["Authorization"] == "Bearer synthetic-cli-token"
        assert str(requests[0].url) == "https://marketplace.example/marketplace/v1/publishers"
    finally:
        client.close()
