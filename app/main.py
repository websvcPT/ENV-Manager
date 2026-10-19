"""Application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import VERSION
from app.api.errors import refusal_handler
from app.api.v1 import router as api_router
from app.config import get_settings
from app.core.crypto import CryptoError, build_crypto_service
from app.core.db import create_engine, create_session_factory
from app.core.log import configure_logging, get_logger
from app.core.middleware import install_middleware
from app.core.migrations import migrate_database
from app.core.observability import configure_otel, init_sentry
from app.services.api_errors import ApiRefusal
from app.services.backup_run import BackupLocations, adopt_backups
from app.services.backup_schedule import BackupScheduler
from app.services.deploy_recovery import recover_interrupted_runs
from app.services.docker_cli import DockerCli
from app.services.key_check import ensure_key_check
from app.services.mailer import FastapiMailTransport
from app.services.notifier import Notifications
from app.web.access_grid import router as access_grid_router
from app.web.account import router as account_router
from app.web.audit import router as audit_router
from app.web.auth import router as auth_router
from app.web.deploy_dialog import router as deploy_dialog_router
from app.web.deployment_form import router as deployment_form_router
from app.web.deployments import router as deployments_router
from app.web.dist_report import router as dist_report_router
from app.web.drift import router as drift_router
from app.web.editor import router as editor_router
from app.web.environment_form import router as environment_form_router
from app.web.errors import http_error_handler, unhandled_error_handler, validation_error_handler
from app.web.history import router as history_router
from app.web.project_detail import router as project_detail_router
from app.web.projects import router as projects_router
from app.web.service_detail import router as service_detail_router
from app.web.service_form import router as service_form_router
from app.web.settings import router as settings_router
from app.web.settings_backup import router as settings_backup_router
from app.web.settings_notifications import router as settings_notifications_router
from app.web.settings_paths import router as settings_paths_router
from app.web.settings_smtp import router as settings_smtp_router
from app.web.tokens import router as tokens_router
from app.web.users import router as users_router

log = get_logger("main")

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Check the master key, back up and migrate the database, start the scheduler, then serve.

    A missing, malformed or mismatching master key stops startup before anything is served.
    Deploy runs left running by a process that is gone are closed before the first request.
    The scheduler runs inside this process, which is assumed to be the only worker.
    """
    settings = get_settings()
    try:
        crypto = build_crypto_service(settings.key, settings.key_file)
    except CryptoError as error:
        log.error("startup refused: %s", error)
        raise
    settings.data_path.mkdir(parents=True, exist_ok=True)
    engine = create_engine(settings.database_path)
    scheduler: BackupScheduler | None = None
    notifier: Notifications | None = None
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
        async with session_factory() as session:
            await recover_interrupted_runs(session, settings.lock_path)
        application.state.crypto = crypto
        application.state.login_ip_max_failures = settings.login_ip_max_failures
        transport = FastapiMailTransport()
        notifier = Notifications(
            session_factory, crypto, base_url=settings.base_url, transport=transport
        )
        application.state.mail_transport = transport
        application.state.notifier = notifier
        application.state.settings_notifier = notifier
        application.state.docker_cli = DockerCli(socket_path=settings.docker_socket)
        application.state.lock_dir = settings.lock_path
        application.state.engine = engine
        application.state.session_factory = session_factory
        locations = BackupLocations.from_settings(settings)
        application.state.backup_locations = locations
        application.state.backup_notifier = notifier
        await adopt_backups(session_factory, locations)
        scheduler = BackupScheduler(
            session_factory, locations, lambda: application.state.backup_notifier
        )
        application.state.backup_scheduler = scheduler
        scheduler.start()
        await scheduler.reload()
        yield
    finally:
        if scheduler is not None:
            scheduler.shutdown()
        if notifier is not None:
            await notifier.close()
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
    application.add_exception_handler(ApiRefusal, refusal_handler)
    application.add_exception_handler(RequestValidationError, validation_error_handler)
    application.add_exception_handler(StarletteHTTPException, http_error_handler)
    application.add_exception_handler(Exception, unhandled_error_handler)
    application.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    application.include_router(auth_router)
    application.include_router(projects_router)
    application.include_router(project_detail_router)
    application.include_router(deployments_router)
    application.include_router(deployment_form_router)
    application.include_router(service_detail_router)
    application.include_router(deploy_dialog_router)
    application.include_router(service_form_router)
    application.include_router(environment_form_router)
    application.include_router(editor_router)
    application.include_router(history_router)
    application.include_router(drift_router)
    application.include_router(dist_report_router)
    application.include_router(audit_router)
    application.include_router(users_router)
    application.include_router(access_grid_router)
    application.include_router(tokens_router)
    application.include_router(account_router)
    application.include_router(settings_paths_router)
    application.include_router(settings_backup_router)
    application.include_router(settings_smtp_router)
    application.include_router(settings_notifications_router)
    application.include_router(settings_router)
    application.include_router(api_router)

    @application.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        """Liveness probe, unauthenticated and free of secrets."""
        return {"status": "ok"}

    log.info("application created for environment %s", settings.environment)
    return application


app = create_app()
