"""I5 local HTTP install and real package-host/Worker execution acceptance helper."""

# pyright: basic

import asyncio
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

import uvicorn
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from nervos_api.api.dependencies import utc_now
from nervos_api.app import create_app
from nervos_api.config import Settings
from nervos_core.infrastructure.database.packages import SqlAlchemyPackageRegistryPersistence
from nervos_worker.package_execution import PackageExecutionAdapter

from apps.worker.tests.support import RecordingCompletion, build_worker, run_until_stopped

ROOT = Path(__file__).resolve().parents[4]
ORIGIN = {"Origin": "http://localhost:5173"}


def local_runtime_journey(hosted_app, tmp_path, monkeypatch, publish_v2, change_distribution):
    database = tmp_path / "local-runtime.db"
    store = tmp_path / "local-packages"
    monkeypatch.setenv("NERVOS_ENVIRONMENT", "test")
    monkeypatch.setenv("NERVOS_DATABASE_PATH", str(database))
    command.upgrade(Config(str(ROOT / "apps/api/alembic.ini")), "head")
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/prepare_runtime_artifacts.py"),
            "--destination",
            str(store / "runtime"),
        ],
        cwd=ROOT,
        check=True,
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(hosted_app, lifespan="off", log_level="warning"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started:
            if time.monotonic() > deadline:
                raise AssertionError("Hosted acceptance HTTP server failed startup")
            time.sleep(0.02)
        try:
            app = create_app(
                Settings(
                    environment="test",
                    database_path=database,
                    package_store=store,
                    marketplace_origin=origin,
                )
            )
            with TestClient(app) as client:
                setup = client.post(
                    "/api/v1/setup",
                    json={"username": "owner", "password": "Synthetic-acceptance-password-123"},
                    headers=ORIGIN,
                )
                assert setup.status_code == 201, setup.text
                assert (
                    client.get("/api/v1/marketplace/packages").json()["items"][0]["package_id"]
                    == "com.acme.journey"
                )

                def install(version):
                    ticket = client.post(
                        "/api/v1/marketplace/install-requests",
                        json={
                            "package_id": "com.acme.journey",
                            "package_version": version,
                        },
                        headers=ORIGIN,
                    )
                    assert ticket.status_code == 200, ticket.text
                    path = f"/api/v1/marketplace/install-requests/{ticket.json()['id']}"
                    ready = client.post(path + "/prepare", headers=ORIGIN)
                    assert ready.status_code == 200, ready.text
                    installed = client.post(path + "/install", headers=ORIGIN)
                    assert installed.status_code == 200, installed.text
                    assert installed.json()["state"] == "installed"

                install("1.0.0")
                instance = client.post(
                    "/api/v1/agent-instances",
                    json={
                        "agent_key": "com.acme.journey",
                        "agent_definition_version": "1.0.0",
                        "display_name": "I5 Runtime",
                        "model_provider": "anthropic",
                        "model_name": "offline-fixture",
                        "package_config": {},
                    },
                    headers=ORIGIN,
                )
                assert instance.status_code == 201, instance.text
                identifier = instance.json()["id"]
                engine = app.state.database_engine

                def execute(expected):
                    response = client.post(
                        f"/api/v1/agent-instances/{identifier}/runs",
                        json={"input": "acceptance"},
                        headers=ORIGIN,
                    )
                    assert response.status_code == 202, response.text
                    run_id = response.json()["id"]
                    adapter = PackageExecutionAdapter(
                        store, packages=SqlAlchemyPackageRegistryPersistence(engine)
                    )
                    worker = build_worker(
                        engine,
                        {"anthropic": RecordingCompletion()},
                        worker_id="i5-" + uuid4().hex,
                        clock=utc_now,
                        package_execution=adapter,
                    )
                    asyncio.run(run_until_stopped(worker, engine))
                    result = client.get(f"/api/v1/runs/{run_id}")
                    assert result.json()["status"] == "succeeded", result.text
                    assert result.json()["output_text"] == f"stage-i-runtime:{expected}", (
                        result.text
                    )
                    return result.json()

                first = execute("1.0.0")
                publish_v2()
                install("2.0.0")
                # Installing v2 leaves the existing instance and historical Run bound to v1.
                assert (
                    client.get(f"/api/v1/agent-instances/{identifier}").json()[
                        "agent_definition_version"
                    ]
                    == "1.0.0"
                )
                rebound = client.post(
                    f"/api/v1/agent-instances/{identifier}/rebind",
                    json={
                        "target_package_version": "2.0.0",
                        "expected_config_revision": 1,
                    },
                    headers=ORIGIN,
                )
                assert rebound.status_code == 200, rebound.text
                execute("2.0.0")
                assert client.get(f"/api/v1/runs/{first['id']}").json() == first
                change_distribution("1.0.0", "yanked")
                change_distribution("2.0.0", "revoked")
                for version, state in (("1.0.0", "yanked"), ("2.0.0", "revoked")):
                    observation = client.get(
                        f"/api/v1/marketplace/packages/com.acme.journey/versions/{version}"
                    )
                    assert observation.status_code == 200, observation.text
                    assert observation.json()["distribution_state"] == state
                assert all(
                    item["status"] == "active"
                    for item in client.get("/api/v1/packages").json()["items"]
                )
                execute("2.0.0")
                # Stop the actual distribution server and prove execution has no dependency on it.
                server.should_exit = True
                thread.join(timeout=10)
                assert not thread.is_alive()
                assert client.get("/api/v1/marketplace/packages").status_code == 503
                execute("2.0.0")
                assert len(client.get("/api/v1/packages").json()["items"]) == 2
        finally:
            server.should_exit = True
            thread.join(timeout=10)
