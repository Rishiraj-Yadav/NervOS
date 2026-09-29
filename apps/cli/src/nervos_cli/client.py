"""HTTP API client for the NervOS CLI."""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Any, cast

import httpx

DEFAULT_API_URL = "http://localhost:8000"
DEFAULT_APP_ORIGIN = "http://localhost:5173"
DEFAULT_SESSION_FILE = Path("~/.nervos/session.json").expanduser()


class NervosClientError(Exception):
    def __init__(self, code: str, message: str, status_code: int | None = None) -> None:
        super().__init__(f"[{code}] {message}" if code else message)
        self.code = code
        self.message = message
        self.status_code = status_code


class NervosClient:
    """Thin HTTP client talking to the NervOS control-plane API."""

    def __init__(
        self,
        base_url: str | None = None,
        origin: str | None = None,
        session_file: Path | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = (base_url or os.environ.get("NERVOS_API_URL", DEFAULT_API_URL)).rstrip("/")
        self.origin = origin or os.environ.get("NERVOS_APP_ORIGIN", DEFAULT_APP_ORIGIN)
        self.session_file = session_file or DEFAULT_SESSION_FILE
        self._custom_client = client

    def _get_client(self) -> httpx.Client:
        if self._custom_client is not None:
            return self._custom_client
        token = self.get_session_token()
        cookies: dict[str, str] = {}
        if token:
            cookies["nervos_session"] = token
        headers = {
            "Origin": self.origin,
            "User-Agent": "nervos-cli/0.1.0",
        }
        return httpx.Client(base_url=self.base_url, headers=headers, cookies=cookies, timeout=60.0)

    def get_session_token(self) -> str | None:
        env_token = os.environ.get("NERVOS_SESSION_TOKEN")
        if env_token:
            return env_token
        if self.session_file.exists():
            try:
                raw_json: object = json.loads(self.session_file.read_text(encoding="utf-8"))
                if isinstance(raw_json, dict):
                    raw_dict = cast("dict[str, object]", raw_json)
                    tok = raw_dict.get("token")
                    if isinstance(tok, str):
                        return tok
                    if tok is not None:
                        return str(tok)
            except (json.JSONDecodeError, OSError):
                pass
        return None

    def save_session_token(self, token: str, user_info: dict[str, Any] | None = None) -> None:
        self.session_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {"token": token, "user": user_info or {}}
        self.session_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(self.session_file, 0o600)

    def clear_session_token(self) -> None:
        if self.session_file.exists():
            with contextlib.suppress(OSError):
                self.session_file.unlink()

    def _handle_response(self, response: httpx.Response) -> Any:
        if response.status_code == 204:
            return None
        try:
            data = response.json()
        except json.JSONDecodeError as error:
            if response.is_error:
                msg = f"HTTP {response.status_code}: {response.text}"
                raise NervosClientError("http_error", msg, response.status_code) from error
            return response.text

        if response.is_error:
            code = "unknown_error"
            msg = f"HTTP {response.status_code}"
            if isinstance(data, dict):
                dict_obj = cast("dict[str, object]", data)
                error_obj = dict_obj.get("error")
                if isinstance(error_obj, dict):
                    err_dict = cast("dict[str, object]", error_obj)
                    c = err_dict.get("code")
                    m = err_dict.get("message")
                    if isinstance(c, str):
                        code = c
                    if isinstance(m, str):
                        msg = m
            raise NervosClientError(code, msg, response.status_code)

        return data

    def login(self, username: str, password: str) -> dict[str, Any]:
        with httpx.Client(base_url=self.base_url, headers={"Origin": self.origin}) as client:
            resp = client.post(
                "/api/v1/auth/login", json={"username": username, "password": password}
            )
            data = self._handle_response(resp)
            token = resp.cookies.get("nervos_session")
            if token:
                self.save_session_token(token, data)
            return data

    def logout(self) -> None:
        with self._get_client() as client:
            resp = client.post("/api/v1/auth/logout")
            self._handle_response(resp)
        self.clear_session_token()

    def me(self) -> dict[str, Any]:
        with self._get_client() as client:
            resp = client.get("/api/v1/auth/me")
            return self._handle_response(resp)

    def inspect_package(self, file_path: Path) -> dict[str, Any]:
        with file_path.open("rb") as handle:
            files = {"file": (file_path.name, handle, "application/octet-stream")}
            with self._get_client() as client:
                resp = client.post("/api/v1/packages/inspect", files=files)
                return self._handle_response(resp)

    def install_package(
        self,
        file_path: Path,
        package_id: str,
        package_version: str,
        content_digest: str,
        signer_fingerprint: str,
        archive_digest: str | None = None,
    ) -> dict[str, Any]:
        with file_path.open("rb") as handle:
            files = {"file": (file_path.name, handle, "application/octet-stream")}
            data = {
                "package_id": package_id,
                "package_version": package_version,
                "content_digest": content_digest,
                "signer_fingerprint": signer_fingerprint,
            }
            if archive_digest is not None:
                data["archive_digest"] = archive_digest
            with self._get_client() as client:
                resp = client.post("/api/v1/packages/install", files=files, data=data)
                return self._handle_response(resp)

    def list_packages(self, status: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, str] = {}
        if status:
            params["status"] = status
        with self._get_client() as client:
            resp = client.get("/api/v1/packages", params=params)
            data = self._handle_response(resp)
            if isinstance(data, dict):
                dict_obj = cast("dict[str, object]", data)
                items = dict_obj.get("items")
                if isinstance(items, list):
                    raw_list = cast("list[object]", items)
                    return [cast("dict[str, Any]", x) for x in raw_list if isinstance(x, dict)]
            return []

    def get_package(self, package_id: str, package_version: str) -> dict[str, Any]:
        with self._get_client() as client:
            resp = client.get(f"/api/v1/packages/{package_id}/versions/{package_version}")
            return self._handle_response(resp)

    def get_removal_plan(self, package_id: str, package_version: str) -> dict[str, Any]:
        with self._get_client() as client:
            path = f"/api/v1/packages/{package_id}/versions/{package_version}/removal-plan"
            resp = client.get(path)
            return self._handle_response(resp)

    def uninstall_package(self, package_id: str, package_version: str) -> dict[str, Any]:
        with self._get_client() as client:
            resp = client.delete(f"/api/v1/packages/{package_id}/versions/{package_version}")
            return self._handle_response(resp)

    def create_agent(
        self,
        display_name: str,
        package_id: str,
        package_version: str,
        model_provider: str,
        model_name: str,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "display_name": display_name,
            "agent_key": package_id,
            "agent_definition_version": package_version,
            "model_provider": model_provider,
            "model_name": model_name,
            "package_config": config or {},
        }
        with self._get_client() as client:
            resp = client.post("/api/v1/agent-instances", json=payload)
            return self._handle_response(resp)

    def patch_config(
        self, agent_instance_id: int, config: dict[str, Any], expected_config_revision: int
    ) -> dict[str, Any]:
        payload = {
            "config": config,
            "expected_config_revision": expected_config_revision,
        }
        with self._get_client() as client:
            path = f"/api/v1/agent-instances/{agent_instance_id}/config"
            resp = client.patch(path, json=payload)
            return self._handle_response(resp)

    def rebind_agent(
        self,
        agent_instance_id: int,
        target_package_version: str,
        config: dict[str, Any] | None,
        expected_config_revision: int,
    ) -> dict[str, Any]:
        payload = {
            "target_package_version": target_package_version,
            "config": config,
            "expected_config_revision": expected_config_revision,
        }
        with self._get_client() as client:
            path = f"/api/v1/agent-instances/{agent_instance_id}/rebind"
            resp = client.post(path, json=payload)
            return self._handle_response(resp)
