"""Application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import VERSION
from app.config import get_settings
from app.core.crypto import CryptoError, build_crypto_service
from app.core.db import create_engine, create_session_factory
from app.core.log import configure_logging, get_logger
from app.core.middleware import install_middleware
from app.core.migrations import migrate_database
from app.core.observability import configure_otel, init_sentry
from app.services.auth_policy import AuthPolicy
from app.services.key_check import ensure_key_check
from app.web.auth import router as auth_router
from app.web.deployment_form import router as deployment_form_router
from app.web.deployments import router as deployments_router
from app.web.dist_report import router as dist_report_router
from app.web.editor import router as editor_router
from app.web.environment_form import router as environment_form_router
from app.web.errors import http_error_handler, unhandled_error_handler
from app.web.history import router as history_router
from app.web.project_detail import router as project_detail_router
from app.web.projects import router as projects_router
from app.web.service_detail import router as service_detail_router
from app.web.service_form import router as service_form_router

log = get_logger("main")

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Check the master key, back up and migrate the database, then serve.

    A missing, malformed or mismatching master key stops startup before anything is served.
    """
    settings = get_settings()
    try:
        crypto = build_crypto_service(settings.key, settings.key_file)
    except CryptoError as error:
        log.error("startup refused: %s", error)
        raise
    settings.data_path.mkdir(parents=True, exist_ok=True)
    engine = create_engine(settings.database_path)
    try:
        await migrate_database(engine, settings.database_path, settings.backup_path)
        session_factory = create_session_factory(engine)
        async with session_factory() as session:
            try:
                created = await ensure_key_check(session, crypto)
            except CryptoError as error:
                log.error("startup refused: %s", error)
                raise
        if created:
            log.info("key check record created for a new database")
        application.state.crypto = crypto
        application.state.auth_policy = AuthPolicy()
        application.state.engine = engine
        application.state.session_factory = session_factory
        yield
    finally:
        await engine.dispose()


def create_app() -> FastAPI:
    """Build the FastAPI application."""
    settings = get_settings()
    configure_logging(settings.log_level)
    init_sentry(settings)
    configure_otel(settings)
    application = FastAPI(
        title="ENV Manager",
        version=VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    install_middleware(application, settings)
    application.add_exception_handler(StarletteHTTPException, http_error_handler)
    application.add_exception_handler(Exception, unhandled_error_handler)
    application.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    application.include_router(auth_router)
    application.include_router(projects_router)
    application.include_router(project_detail_router)
    application.include_router(deployments_router)
    application.include_router(deployment_form_router)
    application.include_router(service_detail_router)
    application.include_router(service_form_router)
    application.include_router(environment_form_router)
    application.include_router(editor_router)
    application.include_router(history_router)
    application.include_router(dist_report_router)

    @application.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        """Liveness probe, unauthenticated and free of secrets."""
        return {"status": "ok"}

    log.info("application created for environment %s", settings.environment)
    return application


app = create_app()
