"""Pydantic schemas exposed by the A2 API."""

from typing import Literal

from pydantic import BaseModel


class HealthResponse(BaseModel):
    """Public liveness response."""

    status: Literal["ok"] = "ok"
