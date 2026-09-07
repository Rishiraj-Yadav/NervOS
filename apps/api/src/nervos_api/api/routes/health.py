"""Public process-health route."""

from fastapi import APIRouter

from nervos_api.api.schemas import HealthResponse

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Report process liveness without querying the database."""
    return HealthResponse()
