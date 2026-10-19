"""The audit entries of one scripted run of audited flows, compared row by row.

The flows run through the services on the seeded database, one after another, and every entry
they leave is compared with the rows written down here: who is named as the actor, the address,
the target and its scope, the details and the versions involved. A change to how entries are
written must leave every row as it is.
"""

import contextlib
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.crypto import CryptoService
from app.models import (
    ApiToken,
    AuditEntry,
    AuditResult,
    Deployment,
    Environment,
    EnvironmentVersion,
    GrantLevel,
    Project,
    Service,
    User,
    UserGroup,
)
from app.seeds import DEV_PASSWORD, SeedData
from app.services import users as user_service
from app.services.account_sessions import revoke_all_other_sessions
from app.services.audit import AuditActor, record_actor_audit
from app.services.auth_policy import AuthPolicy
from app.services.dist_check import run_dist_check
from app.services.editor import (
    KeyValueSubmission,
    load_editor_header,
    reveal_value,
    save_version,
)
from app.services.environments import (
    CleanEnvironment,
    ContentSource,
    create_environment,
)
from app.services.grants import GrantTarget
from app.services.login import authenticate
from app.services.permissions import load_user_scope
from app.services.projects import (
    ProjectInUseError,
    create_project,
    delete_project,
    rename_project,
)
from app.services.sessions import create_session
from app.services.version_history import restore_version, reveal_diff
from app.tests.api_helpers import ApiWorld, deploy, last, make_token
from app.tests.dist_helpers import relocate_deployments

ADDRESS = "203.0.113.7"
CLI = AuditActor.cli()

Row = tuple[object, ...]


async def describe_entries(db: AsyncSession, after: int) -> list[Row]:
    """The audit entries newer than an id, oldest first, with ids replaced by their names."""
    names: dict[str, dict[int, str]] = {
        "user": dict((await db.execute(select(User.id, User.name))).all()),
        "token": dict((await db.execute(select(ApiToken.id, ApiToken.name))).all()),
        "project": dict((await db.execute(select(Project.id, Project.name))).all()),
        "deployment": dict((await db.execute(select(Deployment.id, Deployment.name))).all()),
        "service": dict((await db.execute(select(Service.id, Service.name))).all()),
        "environment": dict((await db.execute(select(Environment.id, Environment.name))).all()),
    }
    versions = {
        version_id: f"{names['environment'][environment_id]}:{number}"
        for version_id, environment_id, number in (
            await db.execute(
                select(
                    EnvironmentVersion.id,
                    EnvironmentVersion.environment_id,
                    EnvironmentVersion.number,
                )
            )
        ).all()
    }

    def named(kind: str, identifier: int | None) -> str | None:
        if identifier is None:
            return None
        return names[kind].get(identifier, f"gone:{identifier}")

    entries = (
        await db.execute(select(AuditEntry).where(AuditEntry.id > after).order_by(AuditEntry.id))
    ).scalars()
    return [
        (
            entry.action,
            entry.result.value,
            entry.actor_type.value,
            named("user", entry.actor_user_id),
            named("token", entry.actor_token_id),
            entry.actor_name,
            entry.ip_address,
            entry.target_type,
            named(entry.target_type, entry.target_id) if entry.target_type in names else None,
            entry.target_name,
            (
                named("project", entry.project_id),
                named("deployment", entry.deployment_id),
                named("service", entry.service_id),
                named("environment", entry.environment_id),
            ),
            entry.details,
            [versions.get(version_id, f"gone:{version_id}") for version_id in entry.version_ids],
        )
        for entry in entries
    ]


async def run_user_flows(db: AsyncSession, seed_data: SeedData) -> None:
    """Create, reset, disable and move users as the command line and as a superadmin."""
    users = seed_data.users
    root, admin = users.superadmin, users.admin
    as_root = AuditActor.for_user(root, ADDRESS)
    await user_service.create_user(
        db, name="newhire", email="newhire@example.test", group=UserGroup.VIEWER, actor=CLI
    )
    await user_service.create_user(
        db,
        name="lead",
        email="lead@example.test",
        group=UserGroup.ADMIN,
        actor=as_root,
        grants=[GrantTarget(GrantLevel.PROJECT, seed_data.projects.fancy.id)],
    )
    newhire = await user_service.get_user_by_name(db, "newhire")
    await user_service.reset_password(db, newhire, actor=as_root)
    await user_service.reset_two_factor(db, users.totp_user, actor=CLI)
    await user_service.set_user_active(db, admin, False, actor=as_root)
    await user_service.set_user_active(db, admin, True, actor=CLI)
    with contextlib.suppress(user_service.LastSuperadminError):
        await user_service.set_user_active(db, root, False, actor=CLI)
    await user_service.set_user_group(db, users.project_admin, UserGroup.VIEWER, actor=as_root)
    with contextlib.suppress(user_service.LastSuperadminError):
        await user_service.set_user_group(db, root, UserGroup.ADMIN, actor=CLI)


async def run_project_flows(db: AsyncSession, seed_data: SeedData) -> None:
    """Create, rename and delete projects, one delete refused by a restricted token."""
    root = seed_data.users.superadmin
    project = await create_project(db, name="Parity Project", actor=root, ip_address=ADDRESS)
    await rename_project(db, project, name="Parity Project 2", actor=root, ip_address=ADDRESS)
    with contextlib.suppress(ProjectInUseError):
        await delete_project(db, seed_data.projects.billing, actor=root, ip_address=ADDRESS)
    await delete_project(db, project, actor=root, ip_address=ADDRESS)


async def run_sign_in_flows(db: AsyncSession, crypto: CryptoService, seed_data: SeedData) -> None:
    """Failed and locked sign-ins of a known and an unknown name, a success and the sessions."""
    policy = AuthPolicy(user_max_failures=2, ip_max_failures=50)
    for identifier, password in (
        ("nobody", "wrong"),
        ("viewer", "wrong"),
        ("viewer", "wrong again"),
        ("viewer", "wrong a third time"),
    ):
        await authenticate(
            db, crypto, policy, identifier=identifier, password=password, ip_address=ADDRESS
        )
    await authenticate(
        db, crypto, policy, identifier="admin", password=DEV_PASSWORD, ip_address=ADDRESS
    )
    admin = seed_data.users.admin
    await create_session(db, admin, policy, ip_address=ADDRESS)
    await db.commit()
    await revoke_all_other_sessions(db, admin, 0, ip_address=ADDRESS)


async def run_environment_flows(
    db: AsyncSession, crypto: CryptoService, seed_data: SeedData, tmp_path: Path
) -> None:
    """Reveal, save, compare and restore a file, create another and check one against its dist."""
    users = seed_data.users
    admin, root = users.admin, users.superadmin
    await relocate_deployments(db, tmp_path / "host")
    admin_scope = await load_user_scope(db, admin)
    header = await load_editor_header(db, admin_scope, seed_data.editor.id)
    assert header is not None
    await reveal_value(db, crypto, header, "MYSQL_HOST", actor=admin, ip_address=ADDRESS)
    await save_version(
        db,
        crypto,
        header,
        actor=admin,
        ip_address=ADDRESS,
        base_number=3,
        comment="parity",
        key_values=KeyValueSubmission(added=[("PARITY_KEY", "parity-value")]),
    )
    header = await load_editor_header(db, admin_scope, seed_data.editor.id)
    assert header is not None
    await reveal_diff(db, crypto, header, 3, 4, actor=admin, ip_address=ADDRESS)
    await restore_version(db, crypto, header, 3, base_number=4, actor=admin, ip_address=ADDRESS)
    deployment = await db.get(Deployment, seed_data.deployments.fancy_prod.id)
    assert deployment is not None
    await create_environment(
        db,
        crypto,
        deployment,
        CleanEnvironment(
            service_id=None,
            new_service_name="redis",
            name="redis.conf",
            target_path="environments/redis.conf",
            dist_path=None,
            file_mode=None,
            file_owner=None,
            file_group=None,
            source=ContentSource.PASTE,
            content="PARITY=1\n",
        ),
        actor=root,
        ip_address=ADDRESS,
    )
    await run_dist_check(db, crypto, seed_data.envs.db.id, actor=admin, ip_address=ADDRESS)


async def run_actor_kinds(db: AsyncSession, seed_data: SeedData) -> None:
    """One entry for each kind of actor, and a sign-in of an unknown name."""
    token = (await db.execute(select(ApiToken))).scalars().first()
    assert token is not None
    owner = await db.get(User, token.owner_id)
    assert owner is not None
    for actor in (
        AuditActor.for_user(seed_data.users.admin, ADDRESS),
        AuditActor.for_token(token, owner, ADDRESS),
        AuditActor.cli(),
        AuditActor.system("scheduler"),
    ):
        await record_actor_audit(
            db,
            actor,
            action="parity.actor",
            result=AuditResult.SUCCESS,
            target_type="service",
            target_id=seed_data.services.mysql.id,
            target_name="mysql",
            details={"kind": actor.type.value},
        )
    await db.commit()


NO_SCOPE = (None, None, None, None)
FANCY = "Fancy Proj. Name"
DB_APP_SCOPE = (FANCY, "prod", "mysql", ".env.db.app")
CREATED_PROJECT_SCOPE = ("gone:3", None, None, None)

EXPECTED: list[Row] = [
    (
        "project.create",
        "success",
        "user",
        "root",
        None,
        "root",
        ADDRESS,
        "project",
        "gone:3",
        "Parity Project",
        CREATED_PROJECT_SCOPE,
        {},
        [],
    ),
    (
        "project.rename",
        "success",
        "user",
        "root",
        None,
        "root",
        ADDRESS,
        "project",
        "gone:3",
        "Parity Project 2",
        CREATED_PROJECT_SCOPE,
        {"previous_name": "Parity Project"},
        [],
    ),
    (
        "project.delete",
        "blocked",
        "user",
        "root",
        None,
        "root",
        ADDRESS,
        "project",
        "billing",
        "billing",
        ("billing", None, None, None),
        {"reason": "restricted_tokens", "tokens": ["billing-ci"]},
        [],
    ),
    (
        "project.delete",
        "success",
        "user",
        "root",
        None,
        "root",
        ADDRESS,
        "project",
        "gone:3",
        "Parity Project 2",
        CREATED_PROJECT_SCOPE,
        {"deployments": 0},
        [],
    ),
    (
        "user.create",
        "success",
        "cli",
        None,
        None,
        "cli",
        None,
        "user",
        "newhire",
        "newhire",
        NO_SCOPE,
        {"group": "viewer", "grants": 0},
        [],
    ),
    (
        "user.create",
        "success",
        "user",
        "root",
        None,
        "root",
        ADDRESS,
        "user",
        "lead",
        "lead",
        NO_SCOPE,
        {"group": "admin", "grants": 1},
        [],
    ),
    (
        "grant.add",
        "success",
        "user",
        "root",
        None,
        "root",
        ADDRESS,
        "user",
        "lead",
        "lead",
        NO_SCOPE,
        {"level": "project", "item": FANCY},
        [],
    ),
    (
        "user.password_reset",
        "success",
        "user",
        "root",
        None,
        "root",
        ADDRESS,
        "user",
        "newhire",
        "newhire",
        NO_SCOPE,
        {"sessions_ended": 0},
        [],
    ),
    (
        "user.totp_reset",
        "success",
        "cli",
        None,
        None,
        "cli",
        None,
        "user",
        "totp_user",
        "totp_user",
        NO_SCOPE,
        {"sessions_ended": 0, "pending_logins_ended": 0},
        [],
    ),
    (
        "token.revoke",
        "success",
        "user",
        "root",
        None,
        "root",
        ADDRESS,
        "token",
        "billing-ci",
        "billing-ci",
        NO_SCOPE,
        {"owner": "admin", "reason": "user_disabled"},
        [],
    ),
    (
        "user.disable",
        "success",
        "user",
        "root",
        None,
        "root",
        ADDRESS,
        "user",
        "admin",
        "admin",
        NO_SCOPE,
        {"sessions_ended": 0, "tokens_revoked": 1},
        [],
    ),
    (
        "user.enable",
        "success",
        "cli",
        None,
        None,
        "cli",
        None,
        "user",
        "admin",
        "admin",
        NO_SCOPE,
        {"sessions_ended": 0},
        [],
    ),
    (
        "user.disable",
        "blocked",
        "cli",
        None,
        None,
        "cli",
        None,
        "user",
        "root",
        "root",
        NO_SCOPE,
        {"reason": "last_active_superadmin"},
        [],
    ),
    (
        "user.group_change",
        "success",
        "user",
        "root",
        None,
        "root",
        ADDRESS,
        "user",
        "project_admin",
        "project_admin",
        NO_SCOPE,
        {"from": "admin", "to": "viewer", "tokens_revoked": 0},
        [],
    ),
    (
        "user.group_change",
        "blocked",
        "cli",
        None,
        None,
        "cli",
        None,
        "user",
        "root",
        "root",
        NO_SCOPE,
        {"reason": "last_active_superadmin", "from": "superadmin", "to": "admin"},
        [],
    ),
    (
        "login.failure",
        "failure",
        "user",
        None,
        None,
        None,
        ADDRESS,
        None,
        None,
        None,
        NO_SCOPE,
        {"reason": "invalid_credentials"},
        [],
    ),
    (
        "login.failure",
        "failure",
        "user",
        "viewer",
        None,
        "viewer",
        ADDRESS,
        None,
        None,
        None,
        NO_SCOPE,
        {"reason": "invalid_credentials"},
        [],
    ),
    (
        "login.failure",
        "failure",
        "user",
        "viewer",
        None,
        "viewer",
        ADDRESS,
        None,
        None,
        None,
        NO_SCOPE,
        {"reason": "invalid_credentials"},
        [],
    ),
    (
        "login.lockout",
        "blocked",
        "user",
        "viewer",
        None,
        "viewer",
        ADDRESS,
        None,
        None,
        None,
        NO_SCOPE,
        {"scope": "user", "minutes": 15.0},
        [],
    ),
    (
        "login.failure",
        "blocked",
        "user",
        "viewer",
        None,
        "viewer",
        ADDRESS,
        None,
        None,
        None,
        NO_SCOPE,
        {"reason": "locked"},
        [],
    ),
    (
        "login.success",
        "success",
        "user",
        "admin",
        None,
        "admin",
        ADDRESS,
        None,
        None,
        None,
        NO_SCOPE,
        {},
        [],
    ),
    (
        "account.sessions_revoke_others",
        "success",
        "user",
        "admin",
        None,
        "admin",
        ADDRESS,
        "user",
        "admin",
        "admin",
        NO_SCOPE,
        {"sessions_ended": 2},
        [],
    ),
    (
        "environment.reveal",
        "success",
        "user",
        "admin",
        None,
        "admin",
        ADDRESS,
        "environment",
        ".env.db.app",
        ".env.db.app",
        DB_APP_SCOPE,
        {"version": 3, "key": "MYSQL_HOST"},
        [".env.db.app:3"],
    ),
    (
        "environment.save",
        "success",
        "user",
        "admin",
        None,
        "admin",
        ADDRESS,
        "environment",
        ".env.db.app",
        ".env.db.app",
        DB_APP_SCOPE,
        {"number": 4, "mode": "key_value", "keys": ["PARITY_KEY"]},
        [".env.db.app:4"],
    ),
    (
        "environment.reveal",
        "success",
        "user",
        "admin",
        None,
        "admin",
        ADDRESS,
        "environment",
        ".env.db.app",
        ".env.db.app",
        DB_APP_SCOPE,
        {"version": 4, "scope": "diff", "compared_with": 3, "keys": ["PARITY_KEY"]},
        [".env.db.app:4"],
    ),
    (
        "environment.restore",
        "success",
        "user",
        "admin",
        None,
        "admin",
        ADDRESS,
        "environment",
        ".env.db.app",
        ".env.db.app",
        DB_APP_SCOPE,
        {"number": 5, "restored": 3},
        [".env.db.app:3", ".env.db.app:5"],
    ),
    (
        "service.create",
        "success",
        "user",
        "root",
        None,
        "root",
        ADDRESS,
        "service",
        "redis",
        "redis",
        (FANCY, "prod", "redis", None),
        {},
        [],
    ),
    (
        "environment.create",
        "success",
        "user",
        "root",
        None,
        "root",
        ADDRESS,
        "environment",
        "redis.conf",
        "redis.conf",
        (FANCY, "prod", "redis", "redis.conf"),
        {
            "target_path": "environments/redis.conf",
            "dist_path": None,
            "file_mode": None,
            "file_owner": None,
            "file_group": None,
            "source": "paste",
            "service_created": True,
        },
        ["redis.conf:1"],
    ),
    (
        "environment.dist_check",
        "failure",
        "user",
        "admin",
        None,
        "admin",
        ADDRESS,
        "environment",
        ".env.db",
        ".env.db",
        (FANCY, "prod", "mysql", ".env.db"),
        {"outcome": "failed", "policy": "block", "version": 2, "problem": "not_a_file"},
        [".env.db:2"],
    ),
    (
        "parity.actor",
        "success",
        "user",
        "admin",
        None,
        "admin",
        ADDRESS,
        "service",
        "mysql",
        "mysql",
        NO_SCOPE,
        {"kind": "user"},
        [],
    ),
    (
        "parity.actor",
        "success",
        "api",
        "admin",
        "billing-ci",
        "token billing-ci",
        ADDRESS,
        "service",
        "mysql",
        "mysql",
        NO_SCOPE,
        {"kind": "api"},
        [],
    ),
    (
        "parity.actor",
        "success",
        "cli",
        None,
        None,
        "cli",
        None,
        "service",
        "mysql",
        "mysql",
        NO_SCOPE,
        {"kind": "cli"},
        [],
    ),
    (
        "parity.actor",
        "success",
        "system",
        None,
        None,
        "scheduler",
        None,
        "service",
        "mysql",
        "mysql",
        NO_SCOPE,
        {"kind": "system"},
        [],
    ),
]


class AuditWrites:
    """The statements that wrote the audit table while this watched an engine."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.statements: list[str] = []
        self._engine = engine.sync_engine

    def _note(
        self,
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        """Keep a statement that inserts, updates or deletes audit entries."""
        text = " ".join(statement.split()).upper()
        if text.startswith(
            ("INSERT INTO AUDIT_ENTRIES", "UPDATE AUDIT_ENTRIES", "DELETE FROM AUDIT_ENTRIES")
        ):
            self.statements.append(text)

    def __enter__(self) -> AuditWrites:
        event.listen(self._engine, "before_cursor_execute", self._note)
        return self

    def __exit__(self, *exc: object) -> None:
        event.remove(self._engine, "before_cursor_execute", self._note)

    def of_kind(self, verb: str) -> list[str]:
        """The statements that start with a verb such as ``UPDATE``."""
        return [text for text in self.statements if text.startswith(verb)]


async def run_every_flow(
    db: AsyncSession, crypto: CryptoService, seed_data: SeedData, tmp_path: Path
) -> None:
    """All the scripted flows, in the order the rows below were recorded."""
    await run_project_flows(db, seed_data)
    await run_user_flows(db, seed_data)
    await run_sign_in_flows(db, crypto, seed_data)
    await run_environment_flows(db, crypto, seed_data, tmp_path)
    await run_actor_kinds(db, seed_data)


async def test_scripted_flows_leave_these_rows(
    db: AsyncSession, seed_data: SeedData, crypto: CryptoService, tmp_path: Path
) -> None:
    seeded = await db.scalar(select(func.max(AuditEntry.id))) or 0
    await run_every_flow(db, crypto, seed_data, tmp_path)
    assert await describe_entries(db, seeded) == EXPECTED


async def test_no_flow_changes_an_entry_it_has_written(
    db: AsyncSession,
    seeded_database: AsyncEngine,
    seed_data: SeedData,
    crypto: CryptoService,
    tmp_path: Path,
) -> None:
    """The table is append-only: every entry is one INSERT, with its time, versions and actor
    name, and nothing is updated or deleted afterwards."""
    seeded = await db.scalar(select(func.max(AuditEntry.id))) or 0
    with AuditWrites(seeded_database) as writes:
        await run_every_flow(db, crypto, seed_data, tmp_path)
    written = len(await describe_entries(db, seeded))
    assert written == len(EXPECTED)
    assert writes.of_kind("UPDATE") == [] and writes.of_kind("DELETE") == []
    assert len(writes.of_kind("INSERT")) == written


async def test_an_entry_keeps_the_time_it_was_written_with(
    db: AsyncSession, seeded_database: AsyncEngine, seed_data: SeedData
) -> None:
    """With no time given the entry has the time of its insert, and a given time is stored as it
    is, as are the versions and the name of a token actor."""
    moment = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
    token = (await db.execute(select(ApiToken))).scalars().first()
    assert token is not None
    owner = await db.get(User, token.owner_id)
    assert owner is not None
    owner_id, owner_name, token_id, token_name = owner.id, owner.name, token.id, token.name
    with AuditWrites(seeded_database) as writes:
        now = await record_actor_audit(
            db,
            AuditActor.for_user(owner, ADDRESS),
            action="time.now",
            result=AuditResult.SUCCESS,
            version_ids=[1, 2],
        )
        given = await record_actor_audit(
            db,
            AuditActor.for_token(token, owner, ADDRESS),
            action="time.given",
            result=AuditResult.SUCCESS,
            created_at=moment,
        )
        await db.commit()
    assert writes.of_kind("UPDATE") == []
    written, inserted_at = [now.id, given.id], now.created_at
    db.expire_all()
    stored_now, stored_given = (
        (
            await db.execute(
                select(AuditEntry).where(AuditEntry.id.in_(written)).order_by(AuditEntry.id)
            )
        )
        .scalars()
        .all()
    )
    assert stored_now.version_ids == [1, 2] and stored_now.actor_name == owner_name
    assert stored_now.created_at == inserted_at
    assert stored_given.created_at == moment
    assert stored_given.actor_name == f"token {token_name}"
    assert (stored_given.actor_token_id, stored_given.actor_user_id) == (token_id, owner_id)


TOKEN = ("admin", "ci", "token ci", "198.51.100.7")
MYSQL_SCOPE = (FANCY, "prod", "mysql", None)
ALL_VERSIONS = [".env.db:2", ".env.db.app:3", ".env.db.secrets:1"]


def dist_check_row(name: str, version: int) -> Row:
    """The entry of a dist check of a file of the production MySQL service made by the token."""
    return (
        "environment.dist_check",
        "success",
        "api",
        *TOKEN,
        "environment",
        name,
        name,
        (FANCY, "prod", "mysql", name),
        {"outcome": "skipped", "policy": "block", "version": version, "skipped": "no_dist_linked"},
        [f"{name}:{version}"],
    )


def deploy_details(run_id: int, dry_run: bool) -> dict[str, object]:
    """The details of the entry of a deploy of the three files, real or dry run."""
    return {
        "run_id": run_id,
        "dry_run": dry_run,
        "force": False,
        "status": "success",
        "post_deploy": "none" if dry_run else "ran",
        "post_deploy_exit_code": None if dry_run else 0,
        "post_deploy_output": None if dry_run else "ran compose up -d --no-deps mysql\n",
        "post_deploy_reason": None,
        "environments": [
            {
                "environment": name,
                "version": version,
                "status": "would_write" if dry_run else "written",
                "dist": "skipped",
                "drift": "missing",
                "forced": False,
            }
            for name, version in ((".env.db", 2), (".env.db.app", 3), (".env.db.secrets", 1))
        ],
    }


CHECKS = [
    dist_check_row(".env.db", 2),
    dist_check_row(".env.db.app", 3),
    dist_check_row(".env.db.secrets", 1),
]

EXPECTED_API: list[Row] = [
    (
        "token.auth_failure",
        "failure",
        "api",
        None,
        None,
        None,
        "198.51.100.7",
        None,
        None,
        None,
        NO_SCOPE,
        {"reason": "unknown"},
        [],
    ),
    (
        "service.deploy",
        "failure",
        "api",
        *TOKEN,
        "service",
        "gone:999999",
        None,
        NO_SCOPE,
        {"refused": "not_found"},
        [],
    ),
    *CHECKS,
    (
        "service.deploy_dry_run",
        "success",
        "api",
        *TOKEN,
        "service",
        "mysql",
        "mysql",
        MYSQL_SCOPE,
        deploy_details(6, dry_run=True),
        ALL_VERSIONS,
    ),
    *CHECKS,
    (
        "service.deploy",
        "success",
        "api",
        *TOKEN,
        "service",
        "mysql",
        "mysql",
        MYSQL_SCOPE,
        deploy_details(7, dry_run=False),
        ALL_VERSIONS,
    ),
    (
        "service.last_deploy_read",
        "success",
        "api",
        *TOKEN,
        "service",
        "mysql",
        "mysql",
        MYSQL_SCOPE,
        {"run_id": 7},
        [],
    ),
]


async def test_api_calls_leave_these_rows(api_world: ApiWorld) -> None:
    """A token's refused, dry run, real and read calls: the token and its owner are the actor."""
    world = api_world
    secret = await make_token(world, "admin")
    mysql = world.services["prod/mysql"]
    async with world.session() as db:
        seeded = await db.scalar(select(func.max(AuditEntry.id))) or 0
    with AuditWrites(world.app.state.engine) as writes:
        await deploy(world, "not-a-token", mysql)
        await deploy(world, secret, 999_999)
        await deploy(world, secret, mysql, {"dry_run": True})
        await deploy(world, secret, mysql)
        await last(world, secret, mysql)
    async with world.session() as db:
        rows = await describe_entries(db, seeded)
    assert rows == EXPECTED_API
    assert writes.of_kind("UPDATE") == [] and len(writes.of_kind("INSERT")) == len(rows)
