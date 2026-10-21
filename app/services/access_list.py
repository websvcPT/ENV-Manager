"""The Access tabs: who has access to one item, direct or inherited, and changes to it.

An Access tab lists, for a project, deployment, service or environment, the active admin and
viewer accounts that reach it: through a grant on the item itself (direct), through a grant on
an item above it (inherited, with that item as the source) or through grants on items inside it
(also shown as inherited, with those items as the source and, for a service, how many of its
environments the user sees). Superadmins reach everything and are not listed; disabled accounts
are not listed either, their grants stay stored and are edited on the user's page.

Everything is derived from the rows and cells of the access grid (``build_projects``,
``cell_for``), so a tab and the grid never disagree about who has access. Loading takes a fixed
number of queries whatever the size of the project or the number of users. Adding and removing
a direct grant goes through ``apply_toggle``, so the tabs and the grid share their refusals.
"""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import GrantLevel, User
from app.services.access_grid import (
    CellKind,
    GridNode,
    GridProject,
    GridSource,
    GridUser,
    NodeKind,
    apply_toggle,
    build_projects,
    cell_for,
    covering_source,
    load_grid_users,
    load_user_grants,
)
from app.services.audit import AuditActor
from app.services.grants import GrantTarget, load_grant_tree

SOURCES_SHOWN = 3
STALE_NOTICE = "This access changed since the page was loaded. The list shows the current state."
GONE_NOTICE = "This item no longer exists. Reload the page."
USER_GONE_NOTICE = "This user no longer exists. The list shows the current state."
DISABLED_NOTICE = (
    "This account is disabled, so it is not listed here. Its grants are edited on the user's page."
)
NOT_GRANTABLE_NOTICE = (
    "Services are not a grant level. Grant access on the deployment or an environment."
)

LEVEL_LABELS = {
    GrantLevel.PROJECT: "Project",
    GrantLevel.DEPLOYMENT: "Deployment",
    GrantLevel.ENVIRONMENT: "Environment",
}
_ACCESS_PATHS = {
    GrantLevel.PROJECT: "/projects",
    GrantLevel.DEPLOYMENT: "/deployments",
    GrantLevel.ENVIRONMENT: "/environments",
}


@dataclass(frozen=True)
class AccessSource:
    """An item whose grant gives a user access: its name, level and the path of its Access tab."""

    target: GrantTarget
    name: str

    @classmethod
    def of(cls, source: GridSource) -> AccessSource:
        """The source behind a grid source."""
        return cls(source.target, source.name)

    @property
    def level_label(self) -> str:
        """The level as a word: Project, Deployment or Environment."""
        return LEVEL_LABELS[self.target.level]

    @property
    def href(self) -> str:
        """Where the grant on this item is edited: its own Access tab."""
        return f"{_ACCESS_PATHS[self.target.level]}/{self.target.item_id}/access"


@dataclass(frozen=True)
class AccessEntry:
    """One user with access to the item.

    A ``direct`` entry holds a grant on the item itself and may also be covered by an item above
    (``also``), which is why the grant can be removed and the user stays listed. Otherwise the
    access is inherited from ``sources``: the widest item above that has a grant, or, when
    ``below`` is set, the items inside that do (the first ``SOURCES_SHOWN`` of them, ``more``
    counts the rest), which gives only part of the item. ``sees`` is the (seen, total)
    environments of a service reached in part.
    """

    user: GridUser
    direct: bool
    sources: tuple[AccessSource, ...] = ()
    more: int = 0
    also: AccessSource | None = None
    sees: tuple[int, int] | None = None
    below: bool = False

    @property
    def source_count(self) -> int:
        """How many items give the access, shown or not."""
        return len(self.sources) + self.more

    @property
    def remove_label(self) -> str:
        """The words of the Remove control: a grant has nothing else to change, so removing it is
        the one action, on the item itself or on the item that gives the access."""
        if self.direct:
            return "Remove access"
        if self.source_count == 1:
            source = self.sources[0]
            return f"Remove the grant on {source.level_label.lower()} {source.name}"
        return "Remove the grants that give this access"


@dataclass(frozen=True)
class AccessList:
    """The Access tab of one item: the users with access and the users that can be added.

    ``candidates`` are the active admins and viewers a grant on the item would give something
    new: nobody who holds it or is covered by an item above, and nobody for a service, which is
    not a grant level. ``item_name`` is the name of the item as the lists show it (a file is
    ``service/.env-name``).
    """

    node: GridNode
    item_name: str
    entries: tuple[AccessEntry, ...]
    candidates: tuple[GridUser, ...]

    @property
    def grantable(self) -> bool:
        """Whether a grant can be set on the item itself."""
        return self.node.target is not None

    @property
    def subtitle(self) -> str:
        """The item as a phrase, such as ``project billing``."""
        return f"{self.node.kind.value} {self.item_name}"

    @property
    def parents(self) -> tuple[AccessSource, ...]:
        """The grantable items above the item, widest first."""
        return tuple(AccessSource.of(source) for source in self.node.sources)

    def entry_of(self, user_id: int) -> AccessEntry | None:
        """The entry of a user, or None when the user is not listed."""
        return next((entry for entry in self.entries if entry.user.id == user_id), None)


@dataclass(frozen=True)
class AccessChange:
    """The answer to adding or removing a direct grant: whether it changed and why not.

    ``notice`` is the reason of a refusal, None when the change was made. ``user_name`` names
    the user when the account exists.
    """

    changed: bool
    notice: str | None
    user_name: str | None = None


def _locate(projects: tuple[GridProject, ...], node_id: str) -> tuple[GridProject, GridNode] | None:
    """The project and row with the id, or None when the item is not in the tree."""
    for project in projects:
        for node in project.nodes:
            if node.id == node_id:
                return project, node
    return None


def _grantable_below(project: GridProject, node: GridNode) -> tuple[GridNode, ...]:
    """The rows inside a row that can hold a grant (services are skipped), in tree order."""
    below: list[GridNode] = []
    inside = False
    for item in project.nodes:
        if item.id == node.id:
            inside = True
        elif inside and item.depth <= node.depth:
            break
        elif inside and item.target is not None:
            below.append(item)
    return tuple(below)


def _item_names(project: GridProject) -> dict[str, str]:
    """The name each row of the project goes by in the lists, by row id.

    A file is shown as ``service/.env-name`` so files of different services that share a name
    are told apart; every other item keeps its own name. The grid keeps the bare names, its
    rows already sit below their service.
    """
    services = {item.id: item.name for item in project.nodes if item.kind is NodeKind.SERVICE}
    return {
        item.id: (
            f"{services[item.parent_id]}/{item.name}"
            if item.kind is NodeKind.ENVIRONMENT and item.parent_id in services
            else item.name
        )
        for item in project.nodes
    }


def _entry(
    node: GridNode,
    below: tuple[GridNode, ...],
    names: dict[str, str],
    user: GridUser,
    grants: frozenset[GrantTarget],
) -> AccessEntry | None:
    """The entry of a user at a row, or None when the user has no access to it."""
    cell = cell_for(node, grants)
    covering = covering_source(node, grants)
    if cell.kind is CellKind.DIRECT:
        return AccessEntry(user, True, also=AccessSource.of(covering) if covering else None)
    if covering is not None:
        return AccessEntry(user, False, (AccessSource.of(covering),))
    held = [AccessSource(item.target, names[item.id]) for item in below if item.target in grants]
    if not held:
        return None
    sees = (cell.seen, cell.total) if node.kind is NodeKind.SERVICE else None
    return AccessEntry(
        user,
        False,
        tuple(held[:SOURCES_SHOWN]),
        more=max(len(held) - SOURCES_SHOWN, 0),
        sees=sees,
        below=True,
    )


async def load_access_list(db: AsyncSession, project_id: int, node_id: str) -> AccessList | None:
    """The Access tab of the item with the grid row id ``node_id`` inside a project.

    The tree of the project, the active admin and viewer accounts and their grants inside the
    project are read in six queries. Returns None when the item is not in the project.
    """
    located = _locate(build_projects(await load_grant_tree(db, project_id)), node_id)
    if located is None:
        return None
    project, node = located
    below = _grantable_below(project, node)
    names = _item_names(project)
    users = await load_grid_users(db)
    grants = await load_user_grants(db, project_id=project_id)
    entries: list[AccessEntry] = []
    candidates: list[GridUser] = []
    for user in users:
        held = grants.get(user.id, frozenset())
        entry = _entry(node, below, names, user, held)
        if entry is not None:
            entries.append(entry)
        if node.target is not None and (entry is None or entry.below):
            candidates.append(user)
    return AccessList(node, names[node.id], tuple(entries), tuple(candidates))


async def change_access(
    db: AsyncSession,
    project_id: int,
    node_id: str,
    user_id: int,
    *,
    grant: bool,
    actor: AuditActor,
) -> AccessChange:
    """Add (``grant``) or remove the direct grant of a user on an item, auditing the change.

    Refused, with nothing written, when the item or the user is gone, the user is disabled or a
    superadmin, a service is named, or ``apply_toggle`` refuses: the user already holds (or no
    longer holds) the grant, or an item above already includes the access.
    """
    located = _locate(build_projects(await load_grant_tree(db, project_id)), node_id)
    if located is None:
        return AccessChange(False, GONE_NOTICE)
    _project, node = located
    if node.target is None:
        return AccessChange(False, NOT_GRANTABLE_NOTICE)
    user = await db.get(User, user_id)
    if user is None:
        return AccessChange(False, USER_GONE_NOTICE)
    if not user.is_active:
        return AccessChange(False, DISABLED_NOTICE, user.name)
    outcome = await apply_toggle(db, user, node, node.target, was=not grant, actor=actor)
    return AccessChange(outcome.changed, outcome.notice(STALE_NOTICE), user.name)


def removal_sources(
    project: GridProject,
    node: GridNode,
    names: dict[str, str],
    grants: frozenset[GrantTarget],
) -> tuple[AccessSource, ...]:
    """The grants a removal takes away so the user no longer reaches the item through them.

    The user's grant on the item itself when there is one; else the widest grant above it that
    passes the access down; else every grant on an item inside it. Empty when the user reaches the
    item through none of them.
    """
    if node.target is not None and node.target in grants:
        return (AccessSource(node.target, names[node.id]),)
    covering = covering_source(node, grants)
    if covering is not None:
        return (AccessSource.of(covering),)
    return tuple(
        AccessSource(item.target, names[item.id])
        for item in _grantable_below(project, node)
        if item.target is not None and item.target in grants
    )


async def load_removal(
    db: AsyncSession, project_id: int, node_id: str, user_id: int
) -> tuple[AccessSource, ...] | None:
    """What removing a user's access to an item would take away, None when the item is gone."""
    located = _locate(build_projects(await load_grant_tree(db, project_id)), node_id)
    if located is None:
        return None
    project, node = located
    grants = (await load_user_grants(db, project_id=project_id)).get(user_id, frozenset())
    return removal_sources(project, node, _item_names(project), grants)


async def remove_access(
    db: AsyncSession, project_id: int, node_id: str, user_id: int, *, actor: AuditActor
) -> AccessChange:
    """Remove the grants that give a user access to an item, each audited like a grid change.

    The grants are those of ``removal_sources``: the direct one, the one above that passes the
    access down, or those inside. Refused, with nothing written, when the item or the user is gone,
    the user is disabled, or none of those grants is held any more; a refusal of one grant stops
    the others and returns its reason.
    """
    located = _locate(build_projects(await load_grant_tree(db, project_id)), node_id)
    if located is None:
        return AccessChange(False, GONE_NOTICE)
    project, node = located
    user = await db.get(User, user_id)
    if user is None:
        return AccessChange(False, USER_GONE_NOTICE)
    if not user.is_active:
        return AccessChange(False, DISABLED_NOTICE, user.name)
    grants = (await load_user_grants(db, project_id=project_id)).get(user_id, frozenset())
    sources = removal_sources(project, node, _item_names(project), grants)
    if not sources:
        return AccessChange(False, STALE_NOTICE, user.name)
    nodes = {item.target: item for item in project.nodes if item.target is not None}
    for source in sources:
        outcome = await apply_toggle(
            db, user, nodes[source.target], source.target, was=True, actor=actor
        )
        notice = outcome.notice(STALE_NOTICE)
        if notice is not None:
            return AccessChange(False, notice, user.name)
    return AccessChange(True, None, user.name)
