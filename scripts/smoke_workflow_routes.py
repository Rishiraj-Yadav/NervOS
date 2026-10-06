"""Live smoke test for the W5b workflow control plane.

Boots the *real* API server process (not TestClient), drives the workflow routes over real
HTTP with a real owner session, and asserts the delivered behaviour end to end. Everything
runs against a throwaway migrated database.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "http://localhost:5173"
PASSWORD = "smoke test password 2026!"
failures: list[str] = []

Json = dict[str, Any]


def call(
    url: str, method: str = "GET", body: Json | None = None, cookie: str = ""
) -> tuple[int, Json]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Origin", ORIGIN)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if cookie:
        request.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def check(label: str, actual: object, expected: object) -> None:
    ok = actual == expected
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: {actual!r}")
    if not ok:
        failures.append(f"{label}: expected {expected!r}, got {actual!r}")


def main() -> int:
    # `ignore_cleanup_errors` because a terminated uvicorn process can still hold the SQLite
    # file open for a moment on Windows. The database is a throwaway in the OS temp
    # directory, so a leftover handle must not fail an otherwise-clean run.
    with tempfile.TemporaryDirectory(
        prefix="nervos-workflow-smoke-", ignore_cleanup_errors=True
    ) as directory:
        env: dict[str, str] = {
            **os.environ,
            "NERVOS_ENVIRONMENT": "test",
            "NERVOS_DATABASE_PATH": str(Path(directory) / "smoke.db"),
        }
        subprocess.run(
            ["uv", "run", "alembic", "-c", "apps/api/alembic.ini", "upgrade", "head"],
            cwd=ROOT,
            env=env,
            check=True,
            capture_output=True,
        )
        server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "--factory",
                "nervos_api.app:create_app",
                "--host",
                "127.0.0.1",
                "--port",
                "8129",
                "--log-level",
                "warning",
            ],
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        base = "http://127.0.0.1:8129"
        try:
            for _ in range(60):
                try:
                    if call(f"{base}/api/v1/workflows")[0] in (401, 200):
                        break
                except Exception:
                    time.sleep(0.5)
            else:
                print("  [FAIL] API did not become ready")
                return 1

            print("Authentication and origin")
            status, body = call(f"{base}/api/v1/workflows")
            check("unauthenticated list is 401", status, 401)
            check("error code", body["error"]["code"], "authentication_required")

            status, body = call(
                f"{base}/api/v1/setup",
                "POST",
                {"username": "smoke-owner", "password": PASSWORD},
            )
            check("setup", status, 201)

            # Log in explicitly so the session cookie is captured from a real response
            # header rather than assumed.
            opener = urllib.request.build_opener()
            login = urllib.request.Request(
                f"{base}/api/v1/auth/login",
                data=json.dumps({"username": "smoke-owner", "password": PASSWORD}).encode(),
                method="POST",
            )
            login.add_header("Origin", ORIGIN)
            login.add_header("Content-Type", "application/json")
            with opener.open(login, timeout=20) as response:
                cookie = next(
                    value for key, value in response.headers.items() if key.lower() == "set-cookie"
                ).split(";")[0]
            session = f"nervos_session={cookie.split('=', 1)[1]}"

            print("Workflow control plane")
            status, agent = call(
                f"{base}/api/v1/agent-instances",
                "POST",
                {
                    "agent_key": "nervos.chat",
                    "agent_definition_version": "1",
                    "display_name": "Researcher",
                    "model_provider": "anthropic",
                    "model_name": "opaque/model",
                },
                cookie=session,
            )
            check("create agent instance", status, 201)

            status, created = call(
                f"{base}/api/v1/workflows",
                "POST",
                {
                    "agent_instance_id": agent["id"],
                    "submission_key": "smoke-live-1",
                    "input_text": "Research durable workflows",
                    "workflow_kind": "research",
                },
                cookie=session,
            )
            check("create workflow", status, 201)
            check("status", created["status"], "running")
            check("step_count", created["step_count"], 1)

            status, replay = call(
                f"{base}/api/v1/workflows",
                "POST",
                {
                    "agent_instance_id": agent["id"],
                    "submission_key": "smoke-live-1",
                    "input_text": "Research durable workflows",
                    "workflow_kind": "research",
                },
                cookie=session,
            )
            check("idempotent replay", replay["id"], created["id"])

            status, conflict = call(
                f"{base}/api/v1/workflows",
                "POST",
                {
                    "agent_instance_id": agent["id"],
                    "submission_key": "smoke-live-1",
                    "input_text": "different body",
                    "workflow_kind": "research",
                },
                cookie=session,
            )
            check("changed body under same key is 409", status, 409)
            check("conflict code", conflict["error"]["code"], "workflow_conflict")

            status, listed = call(f"{base}/api/v1/workflows", cookie=session)
            check("list", status, 200)
            check("one workflow listed", len(listed["workflows"]), 1)
            check(
                "list carries no checkpoint content",
                [k for k in listed["workflows"][0] if k in ("state", "checkpoints", "steps")],
                [],
            )

            status, detail = call(f"{base}/api/v1/workflows/{created['id']}", cookie=session)
            check("detail", status, 200)
            check("one step", len(detail["steps"]), 1)

            status, paused = call(
                f"{base}/api/v1/workflows/{created['id']}/pause",
                "POST",
                {"paused": True},
                cookie=session,
            )
            check("pause", paused["paused"], True)
            check("pause is not termination", paused["status"], "running")

            status, resumed = call(
                f"{base}/api/v1/workflows/{created['id']}/pause",
                "POST",
                {"paused": False},
                cookie=session,
            )
            check("resume", resumed["paused"], False)

            status, cancelled = call(
                f"{base}/api/v1/workflows/{created['id']}/cancel",
                "POST",
                None,
                cookie=session,
            )
            check("cancel", cancelled["status"], "cancelled")

            status, refused = call(
                f"{base}/api/v1/workflows/{created['id']}/pause",
                "POST",
                {"paused": True},
                cookie=session,
            )
            check("terminal pause is 409", status, 409)
            check(
                "transition code",
                refused["error"]["code"],
                "workflow_transition_conflict",
            )

            status, recovery = call(
                f"{base}/api/v1/workflows/{created['id']}/needs-review", cookie=session
            )
            check("needs-review", status, 200)
            check("no attention for a clean finish", recovery["needs_attention"], False)
        finally:
            server.terminate()
            server.wait(timeout=20)

    print()
    if failures:
        print(f"SMOKE FAILED ({len(failures)}):")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("SMOKE PASSED: live workflow control plane verified over real HTTP")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
