"""Helpers shared by the tests of the access grid and the Access tabs: query counting and growth."""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import event
from sqlalchemy.engine import Connection
from sqlalchemy.engine.interfaces import DBAPICursor, ExecutionContext
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models import Deployment, Environment, Project, Service, User, UserGroup


@contextmanager
def count_statements(engine: AsyncEngine) -> Iterator[list[str]]:
    """Collect every SQL statement the engine runs while the block executes."""
    statements: list[str] = []

    def record(
        _connection: Connection,
        _cursor: DBAPICursor,
        statement: str,
        _parameters: object,
        _context: ExecutionContext | None,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)


async def grow(db: AsyncSession, projects: int, users: int) -> None:
    """Add projects with deployments, services and environments, and viewers, in bulk."""
    for number in range(projects):
        project = Project(name=f"big-{number}")
        db.add(project)
        await db.flush()
        for deployment_number in range(2):
            deployment = Deployment(
                project_id=project.id, name=f"dep-{deployment_number}", root_path="/srv/big"
            )
            db.add(deployment)
            await db.flush()
            for service_number in range(2):
                service = Service(deployment_id=deployment.id, name=f"svc-{service_number}")
                db.add(service)
                await db.flush()
                db.add_all(
                    Environment(service_id=service.id, name=f".env.{index}", target_path="/x")
                    for index in range(3)
                )
    for number in range(users):
        db.add(
            User(
                name=f"bulk-{number}",
                email=f"bulk-{number}@example.test",
                password_hash="x",
                group=UserGroup.VIEWER,
            )
        )
    await db.commit()
