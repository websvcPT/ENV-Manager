"""Application factory."""

from fastapi import FastAPI

from app.config import get_settings
from app.core.log import configure_logging, get_logger

log = get_logger("main")


def create_app() -> FastAPI:
    """Build the FastAPI application."""
    settings = get_settings()
    configure_logging(settings.log_level)
    application = FastAPI(title="ENV Manager", docs_url=None, redoc_url=None, openapi_url=None)

    @application.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        """Liveness probe, unauthenticated and free of secrets."""
        return {"status": "ok"}

    log.info("application created for environment %s", settings.environment)
    return application


app = create_app()
