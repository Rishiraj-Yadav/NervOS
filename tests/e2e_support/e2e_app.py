"""Test-only ASGI factory for the offline C2 browser journey.

The API needs no provider at all now: it accepts Runs without a credential and executes none.
This module therefore reduces to the *production* application plus two negative controls —
the composition must hold no configured provider (proving the supervisor stripped both keys)
and must know exactly the two supported provider identifiers. Running the real API with an
unconfigured catalog is a genuine control: any accidental API-side `resolve()` would raise and
fail the journey loudly instead of silently making a call.
"""

from __future__ import annotations

from deterministic import ANTHROPIC_ID, OPENAI_ID
from fastapi import FastAPI
from nervos_api.app import create_app as create_production_app
from nervos_core.application.model_providers import ModelProviderCatalog


def create_app() -> FastAPI:
    """Compose the real API and prove it holds no execution capability at all."""
    app = create_production_app()
    catalog: ModelProviderCatalog = app.state.model_provider_catalog

    if catalog.configured_ids:
        raise RuntimeError("production composition had a configured provider credential")
    for identifier in (ANTHROPIC_ID, OPENAI_ID):
        if not catalog.is_known(identifier):
            raise RuntimeError(f"production composition does not know {identifier}")
    if "run_coordinator" in vars(app.state) or "run_executor" in vars(app.state):
        raise RuntimeError("the control plane composed an execution collaborator")
    return app
