"""ASGI entrypoint for running the service."""
from __future__ import annotations

import uvicorn

from .api import app
from .config import get_settings


def run() -> None:
    """Convenience wrapper used by ``python -m inventory_service``."""

    settings = get_settings()
    uvicorn.run(
        "inventory_service.api:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.environment == "development",
    )


if __name__ == "__main__":
    run()
