"""FastAPI application composition."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from nervos_core.infrastructure.database import create_session_factory, create_sqlite_engine

from nervos_api.api.router import api_router
from nervos_api.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Compose the A2 API without connecting to or migrating the database."""
    resolved_settings = settings or get_settings()
    engine = create_sqlite_engine(resolved_settings.database_path)
    session_factory = create_session_factory(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        del app
        yield
        engine.dispose()

    app = FastAPI(title="NervOS API", lifespan=lifespan)
    app.state.settings = resolved_settings
    app.state.database_engine = engine
    app.state.session_factory = session_factory
    app.include_router(api_router)
    return app
