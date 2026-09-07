"""Run implemented NervOS development services."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from nervos_api.config import Settings

ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_CONFIG = ROOT / "apps" / "api" / "alembic.ini"
SERVICES = ("api", "web")


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


def main() -> int:
    """Run the selected development service when its milestone exists."""
    args = parse_args()
    if args.service == "api":
        return run_api()

    print(
        "The web development server is not implemented in A2. It becomes available in milestone A4."
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
