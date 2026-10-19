"""The access grid: the tree of items against the admin and viewer accounts, and cell toggles.

Rows are the tree Project > Deployment > Service > Environment, columns are active users. A
cell is a direct grant (editable at project, deployment and environment rows), an inherited
grant (not editable, with the item it comes from) or no access. A service row shows the result
for the user, because services are not a grant level. Superadmins reach everything and have no
column; disabled accounts have none either, their grants stay stored and are edited on the
user's own page.

The page holds the rows of one project only, the one a filter names or else the first by name;
every other project is its project row, and its rows are loaded when it is expanded. Loading
takes a fixed number of queries whatever the size of the tree or the number of users: the list
of projects, the tree of the open project, the users and their grants are read once and every
cell is computed from them.
"""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from urllib.parse import urlencode

from sqlalchemy import ColumnElement, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AccessGrant,
    Deployment,
    Environment,
    GrantLevel,
    Project,
    Service,
    User,
    UserGroup,
)
from app.services.audit import AuditActor
from app.services.grants import (
    MAX_ID_DIGITS,
    GrantError,
    GrantProject,
    GrantTarget,
    StaleGrantsError,
    list_grants,
    load_grant_tree,
    target_of,
    toggle_user_grant,
)

GRID_GROUPS = (UserGroup.ADMIN, UserGroup.VIEWER)
STALE_NOTICE = "This cell changed since the page was loaded. It shows the current state."
GONE_NOTICE = "This item no longer exists. Reload the page."


class NodeKind(StrEnum):
    """The level of a row of the grid."""

    PROJECT = "project"
    DEPLOYMENT = "deployment"
    SERVICE = "service"
    ENVIRONMENT = "environment"


class CellKind(StrEnum):
    """What a cell shows: a grant held directly, inherited access, none, or a service result."""

    DIRECT = "direct"
    INHERITED = "inherited"
    NONE = "none"
    RESULT = "result"


@dataclass(frozen=True)
class GridFilters:
    """The group of the columns and the project of the rows; None shows all."""

    group: UserGroup | None = None
    project_id: int | None = None

    @classmethod
    def parse(cls, params: Mapping[str, str]) -> GridFilters:
        """Filters from query parameters; a value that does not parse is ignored."""
        group = next((item for item in GRID_GROUPS if item.value == params.get("group")), None)
        raw_project = params.get("project", "")
        project_id = None
        if raw_project.isascii() and raw_project.isdigit() and len(raw_project) <= MAX_ID_DIGITS:
            project_id = int(raw_project) or None
        return cls(group, project_id)

    def query_string(self) -> str:
        """The filters as a query string, empty when nothing is filtered."""
        pairs: list[tuple[str, str]] = []
        if self.group is not None:
            pairs.append(("group", self.group.value))
        if self.project_id is not None:
            pairs.append(("project", str(self.project_id)))
        return urlencode(pairs)


@dataclass(frozen=True)
class GridSource:
    """A grantable item above a row, which a grant on it passes down."""

    target: GrantTarget
    name: str


@dataclass(frozen=True)
class GridNode:
    """One row of the grid.

    ``sources`` are the grantable items above the row, widest first. ``environments`` are the
    grant targets of a service row's environments, which decide its result.
    """

    id: str
    kind: NodeKind
    name: str
    parent_id: str | None
    depth: int
    target: GrantTarget | None
    sources: tuple[GridSource, ...]
    environments: tuple[GrantTarget, ...]
    expandable: bool

    @property
    def future_items(self) -> bool:
        """Whether a grant on this row includes items added later."""
        return self.kind in (NodeKind.PROJECT, NodeKind.DEPLOYMENT)


@dataclass(frozen=True)
class GridProject:
    """A project of the grid with its rows in tree order, the project row first.

    A project that is not ``loaded`` has its project row only; the rows below it are read when
    the person expands it.
    """

    id: int
    name: str
    nodes: tuple[GridNode, ...]
    loaded: bool = True


@dataclass(frozen=True)
class GridUser:
    """A column of the grid. The email tells apart users that share a name."""

    id: int
    name: str
    email: str
    group: UserGroup


@dataclass(frozen=True)
class GridCell:
    """What one user has at one row.

    ``source`` names the item an inherited cell comes from; ``also`` names the item that covers
    a direct grant as well, so a redundant grant can still be removed. A service result has the
    environments the user sees (``seen``) of those it has (``total``) and ``reaches`` says
    whether the user has access to the service at all.
    """

    kind: CellKind
    source: str | None = None
    also: str | None = None
    seen: int = 0
    total: int = 0
    reaches: bool = False


@dataclass(frozen=True)
class AccessGrid:
    """Everything the grid page draws for a set of filters."""

    users: tuple[GridUser, ...]
    projects: tuple[GridProject, ...]
    project_options: tuple[tuple[int, str], ...]
    grants: Mapping[int, frozenset[GrantTarget]]
    filters: GridFilters

    def cell(self, node: GridNode, user: GridUser) -> GridCell:
        """The cell of a user at a row."""
        return cell_for(node, self.grants.get(user.id, frozenset()))

    def rows_href(self, project_id: int) -> str:
        """The address of the rows of a project, under the same columns as this grid."""
        query = GridFilters(self.filters.group).query_string()
        return f"/access/grid/project/{project_id}" + (f"?{query}" if query else "")


@dataclass(frozen=True)
class CellUpdate:
    """The answer to a toggle: the cells to redraw and why the toggle was refused, if it was.

    ``cells`` always holds the cell that was clicked (unless its item is gone) and, after a
    change, every other cell of the user in that project whose state changed. After a refusal
    it holds the user's whole column of that project as stored now. ``missing`` is the dom id
    of the clicked item when it no longer exists.
    """

    user: GridUser
    cells: tuple[tuple[GridNode, GridCell], ...]
    clicked: str
    notice: str | None
    changed: bool
    missing: bool = False


def node_dom_id(target: GrantTarget) -> str:
    """The row id of a grantable item, such as ``environment-7``."""
    return f"{target.level.value}-{target.item_id}"


def project_node(project_id: int, name: str, *, expandable: bool) -> GridNode:
    """The row of a project, the top of its tree."""
    target = GrantTarget(GrantLevel.PROJECT, project_id)
    node_id = node_dom_id(target)
    return GridNode(node_id, NodeKind.PROJECT, name, None, 0, target, (), (), expandable)


def unloaded_project(project_id: int, name: str, *, has_deployments: bool) -> GridProject:
    """A project drawn as its row alone, to be expanded later."""
    node = project_node(project_id, name, expandable=has_deployments)
    return GridProject(project_id, name, (node,), loaded=False)


def build_projects(tree: tuple[GrantProject, ...]) -> tuple[GridProject, ...]:
    """The rows of every project of the grant tree in tree order."""
    projects: list[GridProject] = []
    for project in tree:
        project_target = GrantTarget(GrantLevel.PROJECT, project.id)
        project_source = GridSource(project_target, project.name)
        project_id = node_dom_id(project_target)
        nodes = [project_node(project.id, project.name, expandable=bool(project.deployments))]
        for deployment in project.deployments:
            deployment_target = GrantTarget(GrantLevel.DEPLOYMENT, deployment.id)
            deployment_source = GridSource(deployment_target, deployment.name)
            deployment_id = node_dom_id(deployment_target)
            above = (project_source, deployment_source)
            nodes.append(
                GridNode(
                    deployment_id,
                    NodeKind.DEPLOYMENT,
                    deployment.name,
                    project_id,
                    1,
                    deployment_target,
                    (project_source,),
                    (),
                    bool(deployment.services),
                )
            )
            for service in deployment.services:
                service_id = f"service-{service.id}"
                targets = tuple(
                    GrantTarget(GrantLevel.ENVIRONMENT, item.id) for item in service.environments
                )
                nodes.append(
                    GridNode(
                        service_id,
                        NodeKind.SERVICE,
                        service.name,
                        deployment_id,
                        2,
                        None,
                        above,
                        targets,
                        bool(targets),
                    )
                )
                for environment, target in zip(service.environments, targets, strict=True):
                    nodes.append(
                        GridNode(
                            node_dom_id(target),
                            NodeKind.ENVIRONMENT,
                            environment.name,
                            service_id,
                            3,
                            target,
                            above,
                            (),
                            False,
                        )
                    )
        projects.append(GridProject(project.id, project.name, tuple(nodes)))
    return tuple(projects)


def covering_source(node: GridNode, grants: frozenset[GrantTarget]) -> GridSource | None:
    """The widest item above a row that the user holds a grant on, or None."""
    return next((item for item in node.sources if item.target in grants), None)


def inherited_source(node: GridNode, grants: frozenset[GrantTarget]) -> str | None:
    """The name of the widest item above a row that the user holds a grant on, or None."""
    source = covering_source(node, grants)
    return source.name if source is not None else None


def cell_for(node: GridNode, grants: frozenset[GrantTarget]) -> GridCell:
    """The cell of a row for a user holding ``grants``.

    A direct grant shows as direct even when an item above covers it too, so it can be
    removed; the covering item is named in ``also``. Otherwise access passed down from the
    widest item above is inherited. A service row has no grant of its own and shows the
    environments the user sees.
    """
    covering = inherited_source(node, grants)
    if node.kind is NodeKind.SERVICE:
        total = len(node.environments)
        seen = total if covering else sum(1 for target in node.environments if target in grants)
        return GridCell(
            CellKind.RESULT, seen=seen, total=total, reaches=covering is not None or seen > 0
        )
    if node.target in grants:
        return GridCell(CellKind.DIRECT, also=covering)
    if covering is not None:
        return GridCell(CellKind.INHERITED, source=covering)
    return GridCell(CellKind.NONE)


async def load_grid_users(
    db: AsyncSession, groups: tuple[UserGroup, ...] = GRID_GROUPS
) -> tuple[GridUser, ...]:
    """The active users of the groups, by name: the accounts that can hold grants."""
    rows = (
        await db.execute(
            select(User.id, User.name, User.email, User.group)
            .where(User.is_active.is_(True), User.group.in_(groups))
            .order_by(func.lower(User.name), User.id)
        )
    ).all()
    return tuple(GridUser(user_id, name, email, group) for user_id, name, email, group in rows)


def _grant_in_project(project_id: int) -> ColumnElement[bool]:
    """Whether a grant lies on the project or on a deployment or environment inside it."""
    deployments = select(Deployment.id).where(Deployment.project_id == project_id)
    environments = (
        select(Environment.id)
        .join(Service, Service.id == Environment.service_id)
        .join(Deployment, Deployment.id == Service.deployment_id)
        .where(Deployment.project_id == project_id)
    )
    return or_(
        AccessGrant.project_id == project_id,
        AccessGrant.deployment_id.in_(deployments),
        AccessGrant.environment_id.in_(environments),
    )


async def load_user_grants(
    db: AsyncSession,
    groups: tuple[UserGroup, ...] = GRID_GROUPS,
    *,
    project_id: int | None = None,
) -> dict[int, frozenset[GrantTarget]]:
    """The direct grants of the active users of the groups, by user id, in one query.

    With ``project_id`` only the grants on that project and inside it are read.
    """
    query = (
        select(AccessGrant)
        .join(User, User.id == AccessGrant.user_id)
        .where(User.is_active.is_(True), User.group.in_(groups))
    )
    if project_id is not None:
        query = query.where(_grant_in_project(project_id))
    held: dict[int, set[GrantTarget]] = {}
    for grant in (await db.execute(query)).scalars():
        held.setdefault(grant.user_id, set()).add(target_of(grant))
    return {user_id: frozenset(targets) for user_id, targets in held.items()}


async def load_project_list(db: AsyncSession) -> tuple[tuple[int, str, bool], ...]:
    """Every project as id, name and whether it has deployments, ordered by name, in one query."""
    has_deployments = exists().where(Deployment.project_id == Project.id)
    rows = (await db.execute(select(Project.id, Project.name, has_deployments))).all()
    ordered = sorted(rows, key=lambda row: row[1].lower())
    return tuple((project_id, name, bool(found)) for project_id, name, found in ordered)


async def load_access_grid(db: AsyncSession, filters: GridFilters) -> AccessGrid:
    """The grid for the filters, in a fixed number of queries.

    The project the filter names, or else the first by name, comes with its rows; every other
    project is its row alone. A project filter that names no existing project is ignored, like
    a value that does not parse, so what the selects show is what the grid shows.
    """
    listing = await load_project_list(db)
    options = tuple((project_id, name) for project_id, name, _found in listing)
    known = {project_id for project_id, _name in options}
    effective = replace(
        filters, project_id=filters.project_id if filters.project_id in known else None
    )
    shown = tuple(item for item in listing if effective.project_id in (None, item[0]))
    tree = await load_grant_tree(db, shown[0][0]) if shown else ()
    opened = {project.id: project for project in build_projects(tree)}
    groups = (effective.group,) if effective.group is not None else GRID_GROUPS
    return AccessGrid(
        users=await load_grid_users(db, groups),
        projects=tuple(
            opened.get(project_id)
            or unloaded_project(project_id, name, has_deployments=has_deployments)
            for project_id, name, has_deployments in shown
        ),
        project_options=options,
        grants=await load_user_grants(db, groups),
        filters=effective,
    )


async def load_grid_project(
    db: AsyncSession, project_id: int, group: UserGroup | None
) -> AccessGrid | None:
    """The rows of one project under the columns of ``group``, or None when it does not exist.

    Four queries for the tree, one for the users and one for the grants inside the project.
    """
    projects = build_projects(await load_grant_tree(db, project_id))
    if not projects:
        return None
    groups = (group,) if group is not None else GRID_GROUPS
    return AccessGrid(
        users=await load_grid_users(db, groups),
        projects=projects,
        project_options=(),
        grants=await load_user_grants(db, groups, project_id=project_id),
        filters=GridFilters(group),
    )


def _column(
    project: GridProject,
    before: frozenset[GrantTarget],
    after: frozenset[GrantTarget],
    clicked: str,
    *,
    everything: bool,
) -> tuple[tuple[GridNode, GridCell], ...]:
    """The cells of one user in a project to redraw: those that changed and the clicked one."""
    cells: list[tuple[GridNode, GridCell]] = []
    for node in project.nodes:
        now = cell_for(node, after)
        if everything or node.id == clicked or now != cell_for(node, before):
            cells.append((node, now))
    return tuple(cells)


class Refusal(StrEnum):
    """Why a toggle was refused: the page is out of date, an item above covers it, or the rest."""

    STALE = "stale"
    INCLUDED = "included"
    OTHER = "other"


@dataclass(frozen=True)
class ToggleOutcome:
    """What flipping one direct grant did: the user's grants before and after, and the refusal.

    ``covering`` names the item above that already includes the access (``Refusal.INCLUDED``)
    and ``message`` is the reason of any other refusal.
    """

    before: frozenset[GrantTarget]
    after: frozenset[GrantTarget]
    changed: bool
    refusal: Refusal | None = None
    covering: str | None = None
    message: str | None = None

    def notice(self, stale: str) -> str | None:
        """The words for the refusal, None when there was none; ``stale`` words an old page."""
        if self.refusal is Refusal.STALE:
            return stale
        if self.refusal is Refusal.INCLUDED:
            return f"Access to this item is already included through {self.covering}."
        return self.message


async def apply_toggle(
    db: AsyncSession,
    user: User,
    node: GridNode,
    target: GrantTarget,
    *,
    was: bool,
    actor: AuditActor,
) -> ToggleOutcome:
    """Flip one direct grant of a row from the state a page showed, or refuse with nothing written.

    ``was`` is whether the page showed the grant as held. Refused when the stored grant differs
    from it, when an item above already includes the access (inherited cells are not editable)
    and when the user is a superadmin. Shared by the grid cells and the Access tabs.
    """
    before = frozenset(await list_grants(db, user.id))
    covering = None if was else inherited_source(node, before)
    if covering is not None and target not in before:
        return ToggleOutcome(before, before, False, Refusal.INCLUDED, covering=covering)
    refusal: Refusal | None = None
    message: str | None = None
    changed = False
    try:
        changed = (
            await toggle_user_grant(db, user, target, was=was, actor=actor, held=before)
        ).changed
    except StaleGrantsError:
        refusal = Refusal.STALE
    except GrantError as error:
        refusal, message = Refusal.OTHER, str(error)
    if refusal is not None:
        after = frozenset(await list_grants(db, user.id))
    else:
        after = before ^ {target} if changed else before
    return ToggleOutcome(before, after, changed, refusal, message=message)


async def toggle_cell(
    db: AsyncSession,
    user: User,
    target: GrantTarget,
    *,
    was: bool,
    actor: AuditActor,
) -> CellUpdate:
    """Flip one direct grant from the cell a person clicked and say which cells to redraw.

    Refused, with nothing written, when the item is gone or ``apply_toggle`` refuses. A refusal
    answers the user's whole column of the project as stored now, since the page that sent the
    request is out of date.
    """
    grid_user = GridUser(user.id, user.name, user.email, user.group)
    clicked = node_dom_id(target)
    located = next(
        (
            (project, node)
            for project in build_projects(await load_grant_tree(db))
            for node in project.nodes
            if node.target == target
        ),
        None,
    )
    if located is None:
        return CellUpdate(grid_user, (), clicked, GONE_NOTICE, changed=False, missing=True)
    project, node = located
    outcome = await apply_toggle(db, user, node, target, was=was, actor=actor)
    notice = outcome.notice(STALE_NOTICE)
    cells = _column(project, outcome.before, outcome.after, clicked, everything=notice is not None)
    return CellUpdate(grid_user, cells, clicked, notice, outcome.changed)
