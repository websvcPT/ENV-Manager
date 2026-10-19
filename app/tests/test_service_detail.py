"""Service detail service: header, environment status, post-deploy panel and history by scope."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Deployment,
    DeployResult,
    DeployResultStatus,
    DeployRun,
    DeployStatus,
    DeployTrigger,
    Environment,
    EnvironmentVersion,
    PostDeployStatus,
    Service,
    User,
)
from app.seeds import SeedData
from app.services.permissions import load_user_scope
from app.services.service_detail import (
    EnvironmentRow,
    ServiceHeader,
    _environment_status,
    load_environment_rows,
    load_post_deploy,
    load_service_header,
    load_service_history,
)


async def _staging_web(db: AsyncSession) -> Service:
    """The seeded staging web service, which carries one environment in every status."""
    return (
        await db.execute(
            select(Service)
            .join(Deployment, Deployment.id == Service.deployment_id)
            .where(Deployment.name == "staging", Service.name == "web")
        )
    ).scalar_one()


async def _header(db: AsyncSession, user: User, service_id: int) -> ServiceHeader | None:
    """The service as a user sees it."""
    return await load_service_header(db, await load_user_scope(db, user), service_id)


async def _rows(db: AsyncSession, user: User, service_id: int) -> tuple[EnvironmentRow, ...]:
    """The environment rows a user sees for a service."""
    scope = await load_user_scope(db, user)
    header = await load_service_header(db, scope, service_id)
    assert header is not None
    return await load_environment_rows(db, scope, header)


@pytest.mark.parametrize(
    ("current", "latest", "expected"),
    [
        (1, None, "never"),
        (3, (DeployResultStatus.WRITTEN, 3), "deployed"),
        (3, (DeployResultStatus.UNCHANGED, 3), "deployed"),
        (4, (DeployResultStatus.WRITTEN, 3), "pending"),
        (4, (DeployResultStatus.BLOCKED, 3), "pending"),
        (3, (DeployResultStatus.BLOCKED, 3), "blocked"),
        (3, (DeployResultStatus.HALTED, 3), "halted"),
        (3, (DeployResultStatus.FAILED, 3), "failed"),
        (3, (DeployResultStatus.WRITTEN, None), "deployed"),
        (None, (DeployResultStatus.FAILED, 2), "failed"),
    ],
)
def test_environment_status_follows_the_newest_version_and_attempt(
    current: int | None, latest: tuple[DeployResultStatus, int | None] | None, expected: str
) -> None:
    """A newer saved version is pending; otherwise the newest attempt decides."""
    assert _environment_status(current, latest) == expected


async def test_superadmin_sees_every_status_with_versions(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """The staging web service has deployed, blocked, pending and never deployed files."""
    service = await _staging_web(db)
    rows = await _rows(db, seed_data.users.superadmin, service.id)
    figures = {row.name: (row.current, row.deployed, row.status) for row in rows}
    assert figures == {
        ".env.web": (1, 1, "deployed"),
        ".env.web.replica": (1, None, "blocked"),
        ".env.web.tuning": (2, 1, "pending"),
        "web-ca.pem": (1, None, "never"),
    }
    assert [row.name for row in rows] == sorted(figures)
    assert all(row.can_edit and row.can_check_dist for row in rows)
    assert not any(row.dist_linked for row in rows)


async def test_failed_attempts_show_as_failed_with_nothing_deployed(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """The seeded prod mysql run failed on both files, so no version was written."""
    rows = await _rows(db, seed_data.users.superadmin, seed_data.services.mysql.id)
    assert [(row.name, row.current, row.deployed, row.status) for row in rows] == [
        (".env.db", 2, None, "failed"),
        (".env.db.app", 3, None, "never"),
        (".env.db.secrets", 1, None, "failed"),
    ]
    assert [row.dist_linked for row in rows] == [True, False, False]


async def test_dry_runs_do_not_change_status(db: AsyncSession, seed_data: SeedData) -> None:
    """The billing api only has a dry run, which leaves its file never deployed."""
    rows = await _rows(db, seed_data.users.superadmin, seed_data.services.api.id)
    assert [(row.name, row.deployed, row.status) for row in rows] == [(".env.api", None, "never")]


async def test_viewer_rows_cannot_edit_or_check_dist(db: AsyncSession, seed_data: SeedData) -> None:
    """A viewer reads the same rows with neither the edit nor the dist action."""
    rows = await _rows(db, seed_data.users.viewer, seed_data.services.mysql.id)
    assert len(rows) == 3
    assert not any(row.can_edit or row.can_check_dist for row in rows)


async def test_environment_grant_limits_rows_and_hides_the_path(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """The caller granted one file sees that file only, counts the rest and gets no root path."""
    header = await _header(db, seed_data.users.env_scoped_admin, seed_data.services.mysql.id)
    assert header is not None
    names = (header.name, header.deployment_name, header.project_name)
    assert names == ("mysql", "prod", "Fancy Proj. Name")
    assert header.root_path is None and not header.deployment_full
    assert header.hidden_environments == 2
    rows = await _rows(db, seed_data.users.env_scoped_admin, seed_data.services.mysql.id)
    assert [row.name for row in rows] == [".env.db"]
    assert rows[0].can_edit and rows[0].can_check_dist


async def test_deployment_grant_sees_the_path_and_every_file(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """A deployment grant covers the services below it, root path included."""
    header = await _header(db, seed_data.users.deployment_admin, seed_data.services.mysql.id)
    assert header is not None
    assert header.root_path == "/opt/fancy/prod" and header.hidden_environments == 0
    assert len(header.visible_environment_ids) == 3


async def test_service_outside_the_scope_is_not_loaded(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """Services the caller cannot reach, and ids that do not exist, give None."""
    assert await _header(db, seed_data.users.ungranted_admin, seed_data.services.mysql.id) is None
    assert await _header(db, seed_data.users.viewer, seed_data.services.api.id) is None
    assert await _header(db, seed_data.users.superadmin, 9999) is None


async def test_output_and_command_follow_the_group(db: AsyncSession, seed_data: SeedData) -> None:
    """Admins read the output, only the superadmin sees the stored command, viewers neither."""
    service = await _staging_web(db)
    panels = {}
    for name, user in (
        ("root", seed_data.users.superadmin),
        ("admin", seed_data.users.admin),
        ("viewer", seed_data.users.viewer),
    ):
        header = await _header(db, user, service.id)
        assert header is not None
        history = await load_service_history(db, header)
        panels[name] = await load_post_deploy(db, header, history)
    for name in ("root", "admin", "viewer"):
        last = panels[name].last
        assert last is not None and last.status is PostDeployStatus.RAN
        assert last.exit_code == 0 and last.who == "admin"
    assert panels["root"].last is not None and panels["root"].last.output == "web recreated"
    assert panels["admin"].last is not None and panels["admin"].last.output == "web recreated"
    assert panels["viewer"].last is not None and panels["viewer"].last.output is None
    root_hook = panels["root"].hook
    assert root_hook is not None
    assert root_hook.commands == "docker compose up -d --no-deps web"
    assert root_hook.timeout_seconds == 120 and root_hook.run_only_if_changed
    assert panels["admin"].hook is None and panels["viewer"].hook is None


async def test_environment_grant_still_reads_output_of_visible_runs(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """The group, not the service grant, decides output: an admin with one file sees it."""
    header = await _header(db, seed_data.users.env_scoped_admin, seed_data.services.mysql.id)
    assert header is not None and header.can_view_output and not header.can_view_command
    history = await load_service_history(db, header)
    assert [(row.service, row.status) for row in history] == [("mysql", DeployStatus.FAILED)]
    assert history[0].environments == ((".env.db", 2),)
    panel = await load_post_deploy(db, header, history)
    assert panel.last is not None and panel.last.status is PostDeployStatus.SKIPPED
    assert panel.last.reason == "deploy did not finish"


async def test_history_is_newest_first_and_hides_runs_outside_the_scope(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """Two staging runs list newest first; a run that only touched hidden files is left out."""
    service = await _staging_web(db)
    header = await _header(db, seed_data.users.superadmin, service.id)
    assert header is not None
    history = await load_service_history(db, header)
    assert [row.status for row in history] == [DeployStatus.SUCCESS, DeployStatus.BLOCKED]
    assert history[0].environments == ((".env.web", 1), (".env.web.tuning", 1))
    assert history[1].environments == ((".env.web.replica", 1),)

    scoped = await _header(db, seed_data.users.env_scoped_admin, seed_data.services.mysql.id)
    assert scoped is not None
    secrets = (
        await db.execute(select(Environment).where(Environment.name == ".env.db.secrets"))
    ).scalar_one()
    version = (
        await db.execute(
            select(EnvironmentVersion).where(EnvironmentVersion.environment_id == secrets.id)
        )
    ).scalar_one()
    run = DeployRun(
        service_id=seed_data.services.mysql.id,
        service_name="mysql",
        deployment_name="prod",
        triggered_by_user_id=seed_data.users.admin.id,
        trigger=DeployTrigger.UI,
        status=DeployStatus.SUCCESS,
    )
    db.add(run)
    await db.flush()
    db.add(
        DeployResult(
            run_id=run.id,
            environment_id=secrets.id,
            environment_name=secrets.name,
            version_id=version.id,
            target_path=secrets.target_path,
            status=DeployResultStatus.WRITTEN,
        )
    )
    await db.commit()
    assert len(await load_service_history(db, scoped)) == 1
    full = await _header(db, seed_data.users.superadmin, seed_data.services.mysql.id)
    assert full is not None
    assert len(await load_service_history(db, full)) == 2
