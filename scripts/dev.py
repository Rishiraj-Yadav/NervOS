"""Run implemented NervOS development services."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from nervos_api.config import Settings
from nervos_worker.config import WorkerSettings

ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_CONFIG = ROOT / "apps" / "api" / "alembic.ini"
SERVICES = ("api", "web", "worker")


def parse_args() -> argparse.Namespace:
    """Parse the requested development service."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("service", choices=SERVICES)
    return parser.parse_args()


def _subprocess_environment(settings: Settings) -> dict[str, str]:
    """Propagate the validated settings to migration and server processes."""
    environment = os.environ.copy()
    environment.update(
        {
            "NERVOS_ENVIRONMENT": settings.environment,
            "NERVOS_DATABASE_PATH": str(settings.database_path),
            "NERVOS_APP_ORIGIN": settings.app_origin,
            "NERVOS_LOG_LEVEL": settings.log_level,
        }
    )
    return environment


def run_api(settings: Settings | None = None) -> int:
    """Migrate the configured database before starting the API server."""
    resolved_settings = settings or Settings()
    environment = _subprocess_environment(resolved_settings)
    migration = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(ALEMBIC_CONFIG),
            "upgrade",
            "head",
        ],
        cwd=ROOT,
        check=False,
        env=environment,
    )
    if migration.returncode != 0:
        return migration.returncode

    server = subprocess.run(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "nervos_api.main:app",
            "--reload",
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
            "--log-level",
            resolved_settings.log_level.lower(),
        ],
        cwd=ROOT,
        check=False,
        env=environment,
    )
    return server.returncode


def run_worker(settings: WorkerSettings | None = None) -> int:
    """Run the execution worker. The Worker never migrates the database itself."""
    resolved = settings or WorkerSettings()
    environment = os.environ.copy()
    environment.update(
        {
            "NERVOS_ENVIRONMENT": resolved.environment,
            "NERVOS_DATABASE_PATH": str(resolved.database_path),
            "NERVOS_LOG_LEVEL": resolved.log_level,
            "NERVOS_WORKER_CONCURRENCY": str(resolved.worker_concurrency),
            "NERVOS_MAX_ACTIVE_JOBS": str(resolved.max_active_jobs),
        }
    )
    server = subprocess.run(
        [
            sys.executable,
            "-m",
            "nervos_worker",
        ],
        cwd=ROOT,
        check=False,
        env=environment,
    )
    return server.returncode


def run_web() -> int:
    """Start the Vite development server through the root pnpm script."""
    pnpm = shutil.which("pnpm")
    if pnpm is None:
        raise SystemExit(
            "Required command 'pnpm' was not found. Run the bootstrap prerequisites first."
        )

    server = subprocess.run(
        [pnpm, "dev:web"],
        cwd=ROOT,
        check=False,
        shell=False,
    )
    return server.returncode


def main() -> int:
    """Run the selected development service."""
    args = parse_args()
    if args.service == "api":
        return run_api()
    if args.service == "worker":
        return run_worker()
    return run_web()


if __name__ == "__main__":
    raise SystemExit(main())
