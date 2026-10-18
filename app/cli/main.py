"""Typer commands for bootstrapping and recovering user access and for deploying a service."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated

import typer
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.crypto import build_crypto_service
from app.core.db import create_engine, create_session_factory
from app.core.migrations import migrate_database
from app.models import (
    AuditActorType,
    Deployment,
    DeployStatus,
    Environment,
    LoginCounter,
    Project,
    Service,
    User,
    UserGroup,
)
from app.seeds import (
    DEMO_STACK_FOLDER,
    DEV_PASSWORD,
    TOTP_ALT_USER_NAME,
    TOTP_USER_NAME,
    SeedError,
    ensure_activity,
    ensure_audit_figures,
    ensure_demo_project,
    ensure_deployment_history,
    ensure_dev_host_paths,
    ensure_editor_figures,
    ensure_service_figures,
    ensure_totp_user,
)
from app.seeds import seed as seed_data
from app.services import users as user_service
from app.services.audit import AuditActor
from app.services.deploy import DeployEngine
from app.services.deploy_recovery import recover_interrupted_runs
from app.services.deploy_types import DeployOutcome, DeployRequest, DeployRequestError
from app.services.docker_cli import DockerCli
from app.services.hook_runner import DockerHookRunner
from app.services.permissions import cli_scope

app = typer.Typer(help="ENV Manager administration.", no_args_is_help=True, add_completion=False)
user_app = typer.Typer(help="Manage users.", no_args_is_help=True)
app.add_typer(user_app, name="user")
dev_app = typer.Typer(help="Development helpers.", no_args_is_help=True)
app.add_typer(dev_app, name="dev")
SEED_ENVIRONMENTS = {"development", "testing"}


@asynccontextmanager
async def open_session() -> AsyncIterator[AsyncSession]:
    """A database session on the application database, migrated to the current schema."""
    settings = get_settings()
    settings.data_path.mkdir(parents=True, exist_ok=True)
    engine = create_engine(settings.database_path)
    try:
        await migrate_database(engine, settings.database_path, settings.backup_path)
        async with create_session_factory(engine)() as db:
            yield db
    finally:
        await engine.dispose()


def run_command[T](operation: Callable[[AsyncSession], Awaitable[T]]) -> T:
    """Run an operation on a fresh session, turning refused operations into exit code 1."""

    async def _run() -> T:
        async with open_session() as db:
            return await operation(db)

    try:
        return asyncio.run(_run())
    except user_service.UserError as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(code=1) from error


def _format_time(moment: datetime | None) -> str:
    """A UTC timestamp for the user list, or ``never``."""
    return moment.strftime("%Y-%m-%d %H:%M:%S UTC") if moment else "never"


@user_app.command("create")
def create_user(
    name: Annotated[str, typer.Argument(help="Sign-in name.")],
    email: Annotated[str, typer.Argument(help="Email address.")],
    group: Annotated[UserGroup, typer.Option(help="Permission group.")],
) -> None:
    """Create a user with a random 16 character password, printed once."""

    async def operation(db: AsyncSession) -> str:
        _, password = await user_service.create_user(
            db, name=name, email=email, group=group, actor_type=AuditActorType.CLI
        )
        return password

    password = run_command(operation)
    typer.echo(f"created user {name.strip()} ({group.value})")
    typer.echo(f"password: {password}")
    typer.echo("This password is shown once and cannot be recovered.")


@user_app.command("list")
def list_users() -> None:
    """List users with group, status and last login."""

    async def operation(db: AsyncSession) -> list[tuple[str, ...]]:
        return [
            (
                user.name,
                user.group.value,
                "active" if user.is_active else "disabled",
                _format_time(user.last_login_at),
            )
            for user in await user_service.list_users(db)
        ]

    rows = run_command(operation)
    header = ("NAME", "GROUP", "STATUS", "LAST LOGIN")
    widths = [max(len(row[column]) for row in [header, *rows]) for column in range(len(header))]
    for row in [header, *rows]:
        cells = (value.ljust(width) for value, width in zip(row, widths, strict=True))
        typer.echo("  ".join(cells).rstrip())


@user_app.command("disable")
def disable_user(name: Annotated[str, typer.Argument(help="Sign-in name.")]) -> None:
    """Disable a user and end their sessions."""

    async def operation(db: AsyncSession) -> int:
        user = await user_service.get_user_by_name(db, name)
        return await user_service.set_user_active(db, user, False, actor_type=AuditActorType.CLI)

    ended = run_command(operation)
    typer.echo(f"disabled user {name.strip()}, {ended} session(s) ended")


@user_app.command("enable")
def enable_user(name: Annotated[str, typer.Argument(help="Sign-in name.")]) -> None:
    """Enable a disabled user."""

    async def operation(db: AsyncSession) -> int:
        user = await user_service.get_user_by_name(db, name)
        return await user_service.set_user_active(db, user, True, actor_type=AuditActorType.CLI)

    run_command(operation)
    typer.echo(f"enabled user {name.strip()}")


@user_app.command("passwd")
def reset_user_password(name: Annotated[str, typer.Argument(help="Sign-in name.")]) -> None:
    """Set a new random 16 character password, printed once, and end the user's sessions."""

    async def operation(db: AsyncSession) -> str:
        user = await user_service.get_user_by_name(db, name)
        return await user_service.reset_password(db, user, actor_type=AuditActorType.CLI)

    password = run_command(operation)
    typer.echo(f"password reset for {name.strip()}")
    typer.echo(f"password: {password}")
    typer.echo("This password is shown once and cannot be recovered.")


@user_app.command("reset-2fa")
def reset_user_two_factor(name: Annotated[str, typer.Argument(help="Sign-in name.")]) -> None:
    """Turn off a user's two-factor sign-in (a lost authenticator) and end their sessions."""

    async def operation(db: AsyncSession) -> int:
        user = await user_service.get_user_by_name(db, name)
        return await user_service.reset_two_factor(db, user, actor_type=AuditActorType.CLI)

    ended = run_command(operation)
    typer.echo(f"two-factor sign-in reset for {name.strip()}, {ended} session(s) ended")


class DeployTargetError(Exception):
    """The service or file named on the deploy command line does not exist."""


async def _find_deploy_request(
    db: AsyncSession, target: str, file_names: list[str], dry_run: bool, force: bool
) -> DeployRequest:
    """The deploy request for ``project/deployment/service`` and the named files (default all)."""
    names = target.split("/")
    if len(names) != 3 or not all(names):
        raise DeployTargetError("name the service as project/deployment/service")
    service_id = await db.scalar(
        select(Service.id)
        .join(Deployment, Deployment.id == Service.deployment_id)
        .join(Project, Project.id == Deployment.project_id)
        .where(Project.name == names[0], Deployment.name == names[1], Service.name == names[2])
    )
    if service_id is None:
        raise DeployTargetError(f"no service {target}")
    environment_ids: tuple[int, ...] | None = None
    if file_names:
        rows = await db.execute(
            select(Environment.name, Environment.id).where(
                Environment.service_id == service_id, Environment.name.in_(file_names)
            )
        )
        found = dict(rows.all())
        unknown = sorted(set(file_names) - set(found))
        if unknown:
            raise DeployTargetError(f"no file {', '.join(unknown)} in {target}")
        environment_ids = tuple(found.values())
    return DeployRequest(
        service_id=service_id, environment_ids=environment_ids, dry_run=dry_run, force=force
    )


def _print_deploy(target: str, outcome: DeployOutcome) -> None:
    """Print the result of a deploy: one line per file, then the post-deploy outcome."""
    kind = "dry run" if outcome.dry_run else "deploy"
    run = f" (run {outcome.run_id})" if outcome.run_id is not None else ""
    typer.echo(f"{kind} of {target}: {outcome.status.value}{run}")
    if outcome.message:
        typer.echo(outcome.message)
    for environment in outcome.environments:
        version = f"v{environment.version_number}" if environment.version_number else "no version"
        typer.echo(
            f"  {environment.name} ({version}): {environment.status.value}. {environment.message}"
        )
    post_deploy = outcome.post_deploy
    if not outcome.dry_run and outcome.run_id is not None:
        reason = f" ({post_deploy.reason})" if post_deploy.reason else ""
        typer.echo(f"  post-deploy: {post_deploy.status.value}{reason}")
        if post_deploy.exit_code is not None:
            typer.echo(f"  post-deploy exit code: {post_deploy.exit_code}")
        if post_deploy.output:
            typer.echo("  post-deploy output (truncated):")
            for line in post_deploy.output.splitlines():
                typer.echo(f"    {line}")


@app.command("deploy")
def deploy_service(
    target: Annotated[str, typer.Argument(help="The service as project/deployment/service.")],
    env: Annotated[
        list[str] | None,
        typer.Option("--env", help="File name to deploy, repeatable. Default: every file."),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Check and report without writing anything.")
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Override missing-keys blocks and drift halts.")
    ] = False,
) -> None:
    """Deploy a service as the CLI actor. Exits 1 unless every file was written or unchanged."""

    async def operation(db: AsyncSession) -> DeployOutcome:
        settings = get_settings()
        engine = DeployEngine(
            crypto=build_crypto_service(settings.key, settings.key_file),
            lock_dir=settings.lock_path,
            hook_runner=DockerHookRunner(DockerCli(socket_path=settings.docker_socket)),
        )
        await recover_interrupted_runs(db, settings.lock_path)
        request = await _find_deploy_request(db, target, env or [], dry_run, force)
        return await engine.run(db, cli_scope(), AuditActor.cli(), request)

    try:
        outcome = run_command(operation)
    except (DeployTargetError, DeployRequestError) as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(code=1) from error
    _print_deploy(target, outcome)
    if outcome.status is not DeployStatus.SUCCESS:
        raise typer.Exit(code=1)


def _require_development_environment(action: str) -> None:
    """Exit with an error unless the environment is one where dev helpers may run."""
    environment = get_settings().environment
    if environment not in SEED_ENVIRONMENTS:
        typer.echo(
            f"error: {action} is only allowed when ENVMANAGER_ENVIRONMENT is one of "
            f"{', '.join(sorted(SEED_ENVIRONMENTS))}, not {environment!r}",
            err=True,
        )
        raise typer.Exit(code=1)


@dev_app.command("seed")
def seed_dataset() -> None:
    """Create the development dataset with its fixed dev-only passwords.

    An already seeded database is left alone except for users, deploy runs and tokens that
    were added to the dataset later, and it always allows the development deployments directory.
    It also adds the audit history figures that the audit log screen needs (every kind of actor
    and scope, and a second page of entries), which the plain dataset leaves out so tests that
    read the audit log find only what they wrote.
    When ENVMANAGER_DEMO_ROOT is set the demo project rooted below it is created or completed too,
    on a new and on an already seeded database.
    """
    _require_development_environment("seeding")

    async def operation(db: AsyncSession) -> None:
        settings = get_settings()
        crypto = build_crypto_service(settings.key, settings.key_file)
        try:
            await seed_data(db, crypto, demo_root=settings.demo_root)
            await ensure_audit_figures(db)
            await ensure_dev_host_paths(db)
            await db.commit()
        except SeedError:
            for name in (TOTP_USER_NAME, TOTP_ALT_USER_NAME):
                await ensure_totp_user(db, crypto, name)
            await ensure_activity(db)
            await ensure_deployment_history(db)
            await ensure_service_figures(db, crypto)
            await ensure_editor_figures(db, crypto)
            await ensure_audit_figures(db)
            if settings.demo_root is not None:
                await ensure_demo_project(db, crypto, settings.demo_root)
            await ensure_dev_host_paths(db)
            await db.commit()
            raise

    try:
        run_command(operation)
    except SeedError as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo("seeded the development dataset")
    typer.echo(f"every seeded user signs in with the dev-only password {DEV_PASSWORD}")
    demo_root = get_settings().demo_root
    if demo_root is not None:
        typer.echo(f"demo project rooted at {demo_root / DEMO_STACK_FOLDER}")


@dev_app.command("reset-lockouts")
def reset_lockouts() -> None:
    """Clear failed login counters, lockouts and second factor replay marks for browser specs."""
    _require_development_environment("resetting lockouts")

    async def operation(db: AsyncSession) -> int:
        count = (await db.execute(select(func.count()).select_from(LoginCounter))).scalar_one()
        await db.execute(delete(LoginCounter))
        await db.execute(update(User).values(totp_last_step=None))
        await db.commit()
        return count

    count = run_command(operation)
    typer.echo(f"cleared {count} login counters")
