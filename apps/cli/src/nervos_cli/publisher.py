"""Direct hosted publisher CLI with browser PKCE and OS credential storage."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import secrets
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import keyring
from keyring.errors import KeyringError


def origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Publisher origin must be an HTTPS origin")
    return value.rstrip("/")


class PublisherClient:
    def __init__(self, selected_origin: str) -> None:
        self.origin = origin(selected_origin)
        self.service = "nervos-marketplace:" + self.origin
        self.http = httpx.Client(
            base_url=self.origin, trust_env=False, follow_redirects=False, timeout=150
        )

    def _require_os_store(self) -> None:
        backend = type(keyring.get_keyring()).__module__
        if backend not in {
            "keyring.backends.Windows",
            "keyring.backends.macOS",
            "keyring.backends.SecretService",
            "keyring.backends.libsecret",
            "keyring.backends.kwallet",
        }:
            raise ValueError(
                "A supported OS credential store is required; no file fallback is allowed"
            )

    def close(self) -> None:
        self.http.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: object = None,
        artifact: Path | None = None,
        authenticated: bool = True,
    ) -> dict[str, Any]:
        headers: dict[str, str] = {}
        if authenticated:
            self._require_os_store()
            stored = keyring.get_password(self.service, "publisher")
            if stored is None:
                raise ValueError("Publisher login required")
            credential = json.loads(stored)
            if credential["expires_at"] <= time.time():
                raise ValueError("Publisher login expired; log in again")
            headers["Authorization"] = "Bearer " + credential["token"]
        if artifact is None:
            response = self.http.request(
                method, "/marketplace/v1" + path, json=payload, headers=headers
            )
        else:
            headers["Content-Length"] = str(artifact.stat().st_size)
            headers["Content-Type"] = "application/octet-stream"
            with artifact.open("rb") as source:
                response = self.http.request(
                    method, "/marketplace/v1" + path, content=source, headers=headers
                )
        if response.status_code != 200:
            raise ValueError(f"Marketplace request failed (HTTP {response.status_code})")
        value: object = response.json()
        if not isinstance(value, dict):
            raise ValueError("Invalid Marketplace response")
        return cast(dict[str, Any], value)

    def login(self) -> None:
        self._require_os_store()
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        received: dict[str, str] = {}

        class Callback(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass  # callback codes never appear in logs

            def do_GET(self) -> None:
                selected = urlsplit(self.path)
                values = parse_qs(selected.query)
                returned = values.get("state", [""])[0]
                code = values.get("code", [""])[0]
                if (
                    selected.path != "/callback"
                    or not hmac.compare_digest(returned, state)
                    or not code
                    or len(code) > 256
                ):
                    self.send_error(400, "Invalid authorization callback")
                    return
                received["code"] = code
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(b"Publisher authorization received. You may close this tab.")

        with HTTPServer(("127.0.0.1", 0), Callback) as server:
            server.timeout = 1
            callback = f"http://127.0.0.1:{server.server_port}/callback"
            params = urlencode(
                {
                    "client_id": "nervos-publisher-cli",
                    "callback": callback,
                    "cli_state": state,
                    "challenge": challenge,
                    "launch": "true",
                }
            )
            if not webbrowser.open(self.origin + "/marketplace/v1/auth/start?" + params):
                raise ValueError("Unable to open browser for publisher login")
            deadline = time.monotonic() + 300
            while not received and time.monotonic() < deadline:
                server.handle_request()
            if not received:
                raise ValueError("Publisher login timed out")
            credential = self.request(
                "POST",
                "/auth/token",
                authenticated=False,
                payload={
                    "code": received["code"],
                    "verifier": verifier,
                    "callback": callback,
                    "client_id": "nervos-publisher-cli",
                },
            )
            # No plaintext file fallback and no credential output.
            keyring.set_password(
                self.service,
                "publisher",
                json.dumps(
                    {
                        "token": credential["access_token"],
                        "expires_at": time.time() + credential["expires_in"],
                    }
                ),
            )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--origin", required=True)
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("login")
    commands.add_parser("logout")
    create = commands.add_parser("create-publisher")
    create.add_argument("handle")
    create.add_argument("display_name")
    claim = commands.add_parser("claim-project")
    claim.add_argument("publisher_id")
    claim.add_argument("package_id")
    upload = commands.add_parser("upload")
    upload.add_argument("project_id")
    upload.add_argument("file", type=Path)
    upload.add_argument("--ownership-revision", type=int, required=True)
    upload.add_argument("--idempotency-key", required=True)
    request = commands.add_parser(
        "request", help="Send a hosted operation with a JSON request file"
    )
    request.add_argument("path")
    request.add_argument("payload", type=Path)
    return result


def main() -> int:
    args = parser().parse_args()
    client: PublisherClient | None = None
    try:
        client = PublisherClient(args.origin)
        if args.command == "login":
            client.login()
            print("Publisher login complete; credential stored in the OS credential store.")
            return 0
        if args.command == "logout":
            client.request("POST", "/auth/logout")
            keyring.delete_password(client.service, "publisher")
            print("Publisher credential removed.")
            return 0
        if args.command == "create-publisher":
            value = client.request(
                "POST",
                "/publishers",
                payload={
                    "handle": args.handle,
                    "display_name": args.display_name,
                    "kind": "individual",
                },
            )
        elif args.command == "claim-project":
            value = client.request(
                "POST",
                "/projects",
                payload={
                    "publisher_id": args.publisher_id,
                    "package_id": args.package_id,
                },
            )
        elif args.command == "upload":
            size = args.file.stat().st_size
            if not 0 < size <= 256 * 1024**2:
                raise ValueError("Archive exceeds the hosted size bound")
            with args.file.open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
            operation = client.request(
                "POST",
                "/uploads",
                payload={
                    "project_id": args.project_id,
                    "archive_sha256": digest,
                    "size_bytes": size,
                    "ownership_revision": args.ownership_revision,
                    "idempotency_key": args.idempotency_key,
                },
            )
            path = f"/uploads/{operation['id']}"
            client.request("PUT", path + "/artifact", artifact=args.file)
            value = client.request("POST", path + "/verify", payload={})
        else:
            if not args.path.startswith("/") or "?" in args.path or "#" in args.path:
                raise ValueError("Invalid hosted operation path")
            if args.path.startswith("/auth/"):
                raise ValueError("Use the dedicated login and logout commands")
            value = client.request("POST", args.path, payload=json.loads(args.payload.read_bytes()))
        print(json.dumps(value, indent=2))
        return 0
    except (ValueError, OSError, httpx.HTTPError, KeyringError):
        print("Publisher operation failed. Check login, origin, request and OS credential store.")
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
