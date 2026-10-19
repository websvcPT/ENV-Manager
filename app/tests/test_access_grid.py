"""The access grid read model and cell toggles on the seeded database."""

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.db import create_session_factory
from app.models import (
    AccessGrant,
    User,
    UserGroup,
)
from app.seeds import SeedData
from app.services.access_grid import (
    GONE_NOTICE,
    STALE_NOTICE,
    AccessGrid,
    CellKind,
    CellUpdate,
    GridCell,
    GridFilters,
    GridNode,
    load_access_grid,
    load_grid_project,
    toggle_cell,
)
from app.services.grants import SUPERADMIN_REFUSAL, GrantTarget, list_grants, set_user_grants
from app.tests.audit_helpers import audit_entries
from app.tests.grant_helpers import (
    DEPLOYMENT,
    ENVIRONMENT,
    PROJECT,
    actor_of,
    count_statements,
    grow,
)


async def fancy_grid(db: AsyncSession, seed_data: SeedData) -> AccessGrid:
    """The grid with the rows of the Fancy project, which is not the one open by default."""
    return await load_access_grid(db, GridFilters(project_id=seed_data.projects.fancy.id))


# The page reads the project list, the tree of the open project (four), the users and the grants.
PAGE_STATEMENTS = 7
# The rows of one project read its tree (four), the users and the grants inside it.
ROWS_STATEMENTS = 6


def node_of(grid: AccessGrid, node_id: str) -> GridNode:
    """A row of the grid by its dom id."""
    return next(node for project in grid.projects for node in project.nodes if node.id == node_id)


def cell_of(grid: AccessGrid, user: User, node_id: str) -> GridCell:
    """The cell of a seeded user at a row."""
    column = next(item for item in grid.users if item.id == user.id)
    return grid.cell(node_of(grid, node_id), column)


async def grants_of(db: AsyncSession, user: User) -> set[GrantTarget]:
    """The stored direct grants of a user."""
    return set(await list_grants(db, user.id))


async def test_columns_are_the_active_admins_and_viewers_by_name(
    db: AsyncSession, seed_data: SeedData
) -> None:
    grid = await load_access_grid(db, GridFilters())
    names = [user.name for user in grid.users]
    assert names == sorted(names, key=str.lower)
    assert "root" not in names
    assert {"admin", "viewer", "ungranted_admin", "totp_user"} <= set(names)
    assert all(user.group in (UserGroup.ADMIN, UserGroup.VIEWER) for user in grid.users)
    viewer_id = seed_data.users.viewer.id
    await db.execute(update(User).where(User.id == viewer_id).values(is_active=False))
    await db.commit()
    after = await load_access_grid(db, GridFilters())
    assert "viewer" not in [user.name for user in after.users]


async def test_the_group_filter_keeps_only_that_group(db: AsyncSession) -> None:
    viewers = await load_access_grid(db, GridFilters(group=UserGroup.VIEWER))
    assert viewers.users
    assert {user.group for user in viewers.users} == {UserGroup.VIEWER}
    admins = await load_access_grid(db, GridFilters(group=UserGroup.ADMIN))
    assert {user.group for user in admins.users} == {UserGroup.ADMIN}
    assert not {user.id for user in viewers.users} & {user.id for user in admins.users}


async def test_the_project_filter_narrows_the_rows_and_keeps_every_user(
    db: AsyncSession, seed_data: SeedData
) -> None:
    everything = await load_access_grid(db, GridFilters())
    billing = await load_access_grid(db, GridFilters(project_id=seed_data.projects.billing.id))
    assert [project.name for project in billing.projects] == ["billing"]
    assert len(everything.projects) == 2
    assert billing.users == everything.users
    assert billing.project_options == everything.project_options
    missing = await load_access_grid(db, GridFilters(project_id=999_999))
    assert missing.filters.project_id is None
    assert len(missing.projects) == 2


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({}, GridFilters()),
        ({"group": "admin", "project": "4"}, GridFilters(UserGroup.ADMIN, 4)),
        ({"group": "superadmin"}, GridFilters()),
        ({"group": "nobody", "project": "x"}, GridFilters()),
        ({"project": "0"}, GridFilters()),
        ({"project": "-3"}, GridFilters()),
        ({"project": "٣"}, GridFilters()),
        ({"project": "1234567890123456"}, GridFilters()),
    ],
)
def test_filters_ignore_values_that_do_not_parse(
    params: dict[str, str], expected: GridFilters
) -> None:
    assert GridFilters.parse(params) == expected


def test_filters_round_trip_through_a_query_string() -> None:
    assert GridFilters().query_string() == ""
    assert GridFilters(UserGroup.VIEWER, 7).query_string() == "group=viewer&project=7"
    assert GridFilters(project_id=7).query_string() == "project=7"


async def test_a_project_grant_is_direct_and_passes_down_as_inherited(
    db: AsyncSession, seed_data: SeedData
) -> None:
    grid = await fancy_grid(db, seed_data)
    viewer = seed_data.users.viewer
    project = f"project-{seed_data.projects.fancy.id}"
    deployment = f"deployment-{seed_data.deployments.fancy_prod.id}"
    service = f"service-{seed_data.services.mysql.id}"
    environment = f"environment-{seed_data.envs.db.id}"
    assert cell_of(grid, viewer, project) == GridCell(CellKind.DIRECT)
    assert node_of(grid, project).future_items
    assert cell_of(grid, viewer, deployment) == GridCell(
        CellKind.INHERITED, source="Fancy Proj. Name"
    )
    assert cell_of(grid, viewer, environment) == GridCell(
        CellKind.INHERITED, source="Fancy Proj. Name"
    )
    result = cell_of(grid, viewer, service)
    assert result.kind is CellKind.RESULT
    assert result.reaches
    assert result.seen == result.total == len(node_of(grid, service).environments) > 1
    grid = await load_access_grid(db, GridFilters(project_id=seed_data.projects.billing.id))
    billing = f"project-{seed_data.projects.billing.id}"
    api = f"environment-{seed_data.envs.api.id}"
    assert cell_of(grid, viewer, billing) == GridCell(CellKind.NONE)
    assert cell_of(grid, viewer, api) == GridCell(CellKind.NONE)
    assert not cell_of(grid, viewer, f"service-{seed_data.services.api.id}").reaches


async def test_a_deployment_grant_passes_down_with_the_deployment_as_source(
    db: AsyncSession, seed_data: SeedData
) -> None:
    grid = await fancy_grid(db, seed_data)
    user = seed_data.users.deployment_admin
    prod = f"deployment-{seed_data.deployments.fancy_prod.id}"
    staging = f"deployment-{seed_data.deployments.fancy_staging.id}"
    assert cell_of(grid, user, prod) == GridCell(CellKind.DIRECT)
    assert node_of(grid, prod).future_items
    assert cell_of(grid, user, f"project-{seed_data.projects.fancy.id}").kind is CellKind.NONE
    assert cell_of(grid, user, f"environment-{seed_data.envs.web.id}") == GridCell(
        CellKind.INHERITED, source="prod"
    )
    assert cell_of(grid, user, staging).kind is CellKind.NONE
    assert not cell_of(grid, user, f"service-{seed_data.services.staging_web.id}").reaches


async def test_an_environment_grant_shows_a_partial_service_result(
    db: AsyncSession, seed_data: SeedData
) -> None:
    grid = await fancy_grid(db, seed_data)
    user = seed_data.users.env_scoped_admin
    db_file = f"environment-{seed_data.envs.db.id}"
    assert cell_of(grid, user, db_file) == GridCell(CellKind.DIRECT)
    assert not node_of(grid, db_file).future_items
    assert cell_of(grid, user, f"environment-{seed_data.envs.db_secrets.id}") == GridCell(
        CellKind.NONE
    )
    mysql = cell_of(grid, user, f"service-{seed_data.services.mysql.id}")
    assert (mysql.seen, mysql.reaches) == (1, True)
    assert mysql.total == len(node_of(grid, f"service-{seed_data.services.mysql.id}").environments)
    assert mysql.total > 1
    assert cell_of(grid, user, f"deployment-{seed_data.deployments.fancy_prod.id}").kind is (
        CellKind.NONE
    )


async def test_the_widest_item_is_the_source_and_a_redundant_grant_stays_direct(
    db: AsyncSession, seed_data: SeedData
) -> None:
    user = seed_data.users.viewer
    prod = GrantTarget(DEPLOYMENT, seed_data.deployments.fancy_prod.id)
    db_file = GrantTarget(ENVIRONMENT, seed_data.envs.db.id)
    project = GrantTarget(PROJECT, seed_data.projects.fancy.id)
    await set_user_grants(db, user, {project, prod, db_file}, actor=actor_of(seed_data))
    grid = await fancy_grid(db, seed_data)
    assert cell_of(grid, user, f"deployment-{prod.item_id}") == GridCell(
        CellKind.DIRECT, also="Fancy Proj. Name"
    )
    assert cell_of(grid, user, f"environment-{db_file.item_id}") == GridCell(
        CellKind.DIRECT, also="Fancy Proj. Name"
    )
    assert cell_of(grid, user, f"environment-{seed_data.envs.web.id}") == GridCell(
        CellKind.INHERITED, source="Fancy Proj. Name"
    )


async def test_loading_takes_the_same_queries_for_a_small_and_a_large_tree(
    seeded_database: AsyncEngine,
) -> None:
    async with create_session_factory(seeded_database)() as session:
        with count_statements(seeded_database) as small:
            await load_access_grid(session, GridFilters())
        await grow(session, projects=12, users=15)
        with count_statements(seeded_database) as large:
            grid = await load_access_grid(session, GridFilters())
    assert len(grid.projects) == 14
    assert len(grid.users) >= 15
    assert len(small) == len(large) == PAGE_STATEMENTS


async def test_only_the_open_project_has_its_rows(db: AsyncSession, seed_data: SeedData) -> None:
    """The first project by name is open; every other project is its project row alone."""
    grid = await load_access_grid(db, GridFilters())
    billing, fancy = grid.projects
    assert (billing.name, billing.loaded) == ("billing", True)
    assert len(billing.nodes) > 1
    assert (fancy.name, fancy.loaded) == ("Fancy Proj. Name", False)
    assert [node.id for node in fancy.nodes] == [f"project-{fancy.id}"]
    assert fancy.nodes[0].expandable
    assert grid.rows_href(fancy.id) == f"/access/grid/project/{fancy.id}"
    filtered = await load_access_grid(db, GridFilters(UserGroup.VIEWER, fancy.id))
    assert [(project.name, project.loaded) for project in filtered.projects] == [
        ("Fancy Proj. Name", True)
    ]
    assert filtered.rows_href(billing.id) == f"/access/grid/project/{billing.id}?group=viewer"


async def test_the_project_row_of_an_unloaded_project_still_shows_its_grants(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """A project grant is a cell of the project row, so it is drawn before the rows load."""
    grid = await load_access_grid(db, GridFilters())
    fancy = f"project-{seed_data.projects.fancy.id}"
    assert cell_of(grid, seed_data.users.viewer, fancy) == GridCell(CellKind.DIRECT)
    assert cell_of(grid, seed_data.users.ungranted_admin, fancy) == GridCell(CellKind.NONE)


async def test_the_rows_of_a_project_match_the_open_project_and_use_the_group_columns(
    db: AsyncSession, seed_data: SeedData
) -> None:
    fancy = seed_data.projects.fancy.id
    opened = await fancy_grid(db, seed_data)
    loaded = await load_grid_project(db, fancy, UserGroup.VIEWER)
    assert loaded is not None
    assert loaded.projects == opened.projects
    assert {user.group for user in loaded.users} == {UserGroup.VIEWER}
    viewer = seed_data.users.viewer
    deployment = f"deployment-{seed_data.deployments.fancy_prod.id}"
    assert cell_of(loaded, viewer, deployment) == GridCell(
        CellKind.INHERITED, source="Fancy Proj. Name"
    )
    assert await load_grid_project(db, 999_999, None) is None


async def test_the_rows_of_a_project_take_the_same_queries_for_a_small_and_a_large_tree(
    seeded_database: AsyncEngine, seed_data: SeedData
) -> None:
    fancy = seed_data.projects.fancy.id
    async with create_session_factory(seeded_database)() as session:
        with count_statements(seeded_database) as small:
            await load_grid_project(session, fancy, None)
        await grow(session, projects=12, users=15)
        with count_statements(seeded_database) as large:
            await load_grid_project(session, fancy, None)
    assert len(small) == len(large) == ROWS_STATEMENTS


async def test_a_toggle_takes_the_same_queries_for_a_small_and_a_large_tree(
    seeded_database: AsyncEngine, seed_data: SeedData
) -> None:
    actor = actor_of(seed_data)
    user = seed_data.users.ungranted_admin
    target = GrantTarget(PROJECT, seed_data.projects.billing.id)
    async with create_session_factory(seeded_database)() as session:
        with count_statements(seeded_database) as small:
            await toggle_cell(session, user, target, was=False, actor=actor)
        await toggle_cell(session, user, target, was=True, actor=actor)
        await grow(session, projects=12, users=15)
        with count_statements(seeded_database) as large:
            await toggle_cell(session, user, target, was=False, actor=actor)
    assert len(small) == len(large)


async def test_a_toggle_adds_and_removes_one_grant_with_one_audit_entry_each(
    db: AsyncSession, seed_data: SeedData
) -> None:
    user = seed_data.users.ungranted_admin
    target = GrantTarget(PROJECT, seed_data.projects.billing.id)
    added = await toggle_cell(db, user, target, was=False, actor=actor_of(seed_data))
    assert added.changed and added.notice is None
    assert await grants_of(db, user) == {target}
    [entry] = await audit_entries(db, "grant.add")
    assert entry.target_name == "ungranted_admin"
    assert entry.details == {"level": "project", "item": "billing"}
    removed = await toggle_cell(db, user, target, was=True, actor=actor_of(seed_data))
    assert removed.changed and removed.notice is None
    assert await grants_of(db, user) == set()
    assert len(await audit_entries(db, "grant.add")) == 1
    [gone] = await audit_entries(db, "grant.remove")
    assert gone.details == {"level": "project", "item": "billing"}


async def test_a_project_toggle_redraws_the_cells_below_and_only_those_that_changed(
    db: AsyncSession, seed_data: SeedData
) -> None:
    user = seed_data.users.ungranted_admin
    billing = seed_data.projects.billing
    update = await toggle_cell(
        db, user, GrantTarget(PROJECT, billing.id), was=False, actor=actor_of(seed_data)
    )
    grid = await load_access_grid(db, GridFilters(project_id=billing.id))
    sent = {node.id: cell for node, cell in update.cells}
    project_nodes = {node.id for node in grid.projects[0].nodes}
    assert set(sent) == project_nodes
    assert sent[f"project-{billing.id}"].kind is CellKind.DIRECT
    assert sent[f"environment-{seed_data.envs.api.id}"] == GridCell(
        CellKind.INHERITED, source="billing"
    )
    assert sent[f"service-{seed_data.services.api.id}"].reaches
    assert update.clicked == f"project-{billing.id}"


async def test_an_environment_toggle_redraws_its_cell_and_the_service_result_only(
    db: AsyncSession, seed_data: SeedData
) -> None:
    user = seed_data.users.ungranted_admin
    target = GrantTarget(ENVIRONMENT, seed_data.envs.db.id)
    update = await toggle_cell(db, user, target, was=False, actor=actor_of(seed_data))
    assert {node.id for node, _ in update.cells} == {
        f"environment-{seed_data.envs.db.id}",
        f"service-{seed_data.services.mysql.id}",
    }


async def test_a_cell_that_shows_an_old_state_is_refused_and_nothing_changes(
    db: AsyncSession, seed_data: SeedData
) -> None:
    user = seed_data.users.viewer
    project = GrantTarget(PROJECT, seed_data.projects.fancy.id)
    before = await audit_entries(db, "grant.remove")
    update = await toggle_cell(db, user, project, was=False, actor=actor_of(seed_data))
    assert update.notice == STALE_NOTICE
    assert not update.changed
    assert await grants_of(db, user) == {project}
    assert await audit_entries(db, "grant.remove") == before
    shown = {node.id: cell for node, cell in update.cells}
    assert shown[f"project-{project.item_id}"].kind is CellKind.DIRECT
    grid = await load_access_grid(db, GridFilters(project_id=project.item_id))
    assert len(shown) == len(grid.projects[0].nodes)


async def test_access_an_item_above_already_includes_cannot_be_granted_again(
    db: AsyncSession, seed_data: SeedData
) -> None:
    user = seed_data.users.viewer
    target = GrantTarget(ENVIRONMENT, seed_data.envs.db.id)
    update = await toggle_cell(db, user, target, was=False, actor=actor_of(seed_data))
    assert update.notice == "Access to this item is already included through Fancy Proj. Name."
    assert await grants_of(db, user) == {GrantTarget(PROJECT, seed_data.projects.fancy.id)}
    assert await audit_entries(db, "grant.add") == []


async def test_a_redundant_grant_can_be_removed(db: AsyncSession, seed_data: SeedData) -> None:
    user = seed_data.users.viewer
    target = GrantTarget(ENVIRONMENT, seed_data.envs.db.id)
    project = GrantTarget(PROJECT, seed_data.projects.fancy.id)
    await set_user_grants(db, user, {project, target}, actor=actor_of(seed_data))
    update = await toggle_cell(db, user, target, was=True, actor=actor_of(seed_data))
    assert update.changed and update.notice is None
    assert await grants_of(db, user) == {project}
    [entry] = await audit_entries(db, "grant.remove")
    assert entry.details is not None and entry.details["level"] == "environment"


async def test_a_deleted_item_is_reported_and_nothing_is_written(
    db: AsyncSession, seed_data: SeedData
) -> None:
    user = seed_data.users.ungranted_admin
    update: CellUpdate = await toggle_cell(
        db, user, GrantTarget(PROJECT, 424242), was=False, actor=actor_of(seed_data)
    )
    assert update.missing and update.notice == GONE_NOTICE
    assert update.cells == ()
    assert update.clicked == "project-424242"
    assert await db.scalar(select(func.count()).select_from(AccessGrant)) == 8


async def test_a_superadmin_cannot_be_given_a_grant(db: AsyncSession, seed_data: SeedData) -> None:
    update = await toggle_cell(
        db,
        seed_data.users.superadmin,
        GrantTarget(PROJECT, seed_data.projects.billing.id),
        was=False,
        actor=actor_of(seed_data),
    )
    assert update.notice == SUPERADMIN_REFUSAL
    assert not update.changed
    assert await grants_of(db, seed_data.users.superadmin) == set()
    assert await audit_entries(db, "grant.add") == []
