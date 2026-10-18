"""The access grid: the tree of items against the admin and viewer accounts, and cell toggles.

Rows are the tree Project > Deployment > Service > Environment, columns are active users. A
cell is a direct grant (editable at project, deployment and environment rows), an inherited
grant (not editable, with the item it comes from) or no access. A service row shows the result
for the user, because services are not a grant level. Superadmins reach everything and have no
column; disabled accounts have none either, their grants stay stored and are edited on the
user's own page.

Loading takes a fixed number of queries whatever the size of the tree or the number of users:
the tree, the users and their grants are read once and every cell is computed from them.
"""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from urllib.parse import urlencode

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AccessGrant, GrantLevel, User, UserGroup
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
    """A project of the grid with its rows in tree order, the project row first."""

    id: int
    name: str
    nodes: tuple[GridNode, ...]


@dataclass(frozen=True)
class GridUser:
    """A column of the grid."""

    id: int
    name: str
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


def build_projects(tree: tuple[GrantProject, ...]) -> tuple[GridProject, ...]:
    """The rows of every project in tree order, from the grant tree."""
    projects: list[GridProject] = []
    for project in tree:
        project_target = GrantTarget(GrantLevel.PROJECT, project.id)
        project_source = GridSource(project_target, project.name)
        project_id = node_dom_id(project_target)
        nodes = [
            GridNode(
                project_id,
                NodeKind.PROJECT,
                project.name,
                None,
                0,
                project_target,
                (),
                (),
                bool(project.deployments),
            )
        ]
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


def inherited_source(node: GridNode, grants: frozenset[GrantTarget]) -> str | None:
    """The widest item above a row that the user holds a grant on, by name, or None."""
    return next((item.name for item in node.sources if item.target in grants), None)


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


async def load_access_grid(db: AsyncSession, filters: GridFilters) -> AccessGrid:
    """The grid for the filters, in a fixed number of queries.

    A project filter that names no existing project is ignored, like a value that does not
    parse, so what the selects show is what the grid shows.
    """
    projects = build_projects(await load_grant_tree(db))
    options = tuple((project.id, project.name) for project in projects)
    known = {project_id for project_id, _name in options}
    effective = replace(
        filters, project_id=filters.project_id if filters.project_id in known else None
    )
    shown = tuple(project for project in projects if effective.project_id in (None, project.id))
    groups = (effective.group,) if effective.group is not None else GRID_GROUPS
    column_filter = (User.is_active.is_(True), User.group.in_(groups))
    users = (
        await db.execute(
            select(User.id, User.name, User.group)
            .where(*column_filter)
            .order_by(func.lower(User.name), User.id)
        )
    ).all()
    stored = (
        (
            await db.execute(
                select(AccessGrant).join(User, User.id == AccessGrant.user_id).where(*column_filter)
            )
        )
        .scalars()
        .all()
    )
    held: dict[int, set[GrantTarget]] = {}
    for grant in stored:
        held.setdefault(grant.user_id, set()).add(target_of(grant))
    return AccessGrid(
        users=tuple(GridUser(user_id, name, group) for user_id, name, group in users),
        projects=shown,
        project_options=options,
        grants={user_id: frozenset(targets) for user_id, targets in held.items()},
        filters=effective,
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


async def toggle_cell(
    db: AsyncSession,
    user: User,
    target: GrantTarget,
    *,
    was: bool,
    actor: AuditActor,
) -> CellUpdate:
    """Flip one direct grant from the cell a person clicked and say which cells to redraw.

    Refused, with nothing written, when the item is gone, the stored grant differs from what
    the page showed, an item above already includes it (inherited cells are not editable) or
    the user is a superadmin. A refusal answers the user's whole column of the project as
    stored now, since the page that sent the request is out of date.
    """
    grid_user = GridUser(user.id, user.name, user.group)
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
    before = frozenset(await list_grants(db, user.id))
    notice: str | None = None
    changed = False
    covering = None if was else inherited_source(node, before)
    if covering is not None and target not in before:
        notice = f"Access to this item is already included through {covering}."
    else:
        try:
            change = await toggle_user_grant(db, user, target, was=was, actor=actor)
            changed = change.changed
        except StaleGrantsError:
            notice = STALE_NOTICE
        except GrantError as error:
            notice = str(error)
    after = frozenset(await list_grants(db, user.id))
    cells = _column(project, before, after, clicked, everything=notice is not None)
    return CellUpdate(grid_user, cells, clicked, notice, changed)
