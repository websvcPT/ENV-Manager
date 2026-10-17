"""Permission engine tests: capability matrix, grants, inheritance, scope and token narrowing.

Every test runs against the seed dataset in a real temporary SQLite database. Item keys are
"level:name" labels for the seeded structure:

    rentals
      prod
        mysql: db, db_secrets
        web: web
      staging
        staging_web: staging_web
    billing
      main
        api: api
        worker: worker
"""

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.db import create_session_factory
from app.models import (
    AccessGrant,
    ApiToken,
    ApiTokenRestriction,
    Deployment,
    Environment,
    GrantLevel,
    RestrictionLevel,
    Service,
    User,
    UserGroup,
)
from app.models.types import utc_now
from app.seeds import SeedData
from app.services.permissions import (
    CAPABILITY_GROUPS,
    CAPABILITY_TARGETS,
    AccessScope,
    Capability,
    ItemLevel,
    ItemRef,
    TargetSet,
    compute_visibility,
    load_item,
    load_token_scope,
    load_user_scope,
    permitted_environment_ids,
    token_is_usable,
)
from app.tests.auth_helpers import make_user

LEVEL_PREFIX = {
    ItemLevel.PROJECT: "project",
    ItemLevel.DEPLOYMENT: "deployment",
    ItemLevel.SERVICE: "service",
    ItemLevel.ENVIRONMENT: "environment",
}
ALL_RENTALS = {
    "project:rentals",
    "deployment:prod",
    "deployment:staging",
    "service:mysql",
    "service:web",
    "service:staging_web",
    "environment:db",
    "environment:db_secrets",
    "environment:web",
    "environment:staging_web",
}
ALL_BILLING = {
    "project:billing",
    "deployment:main",
    "deployment:legacy",
    "service:api",
    "service:worker",
    "service:cron",
    "environment:api",
    "environment:worker",
}
ALL_ITEMS = ALL_RENTALS | ALL_BILLING
DEPLOYMENT_PROD = {
    "deployment:prod",
    "service:mysql",
    "service:web",
    "environment:db",
    "environment:db_secrets",
    "environment:web",
}


class Tree:
    """Ids of the seeded structure, addressed by "level:name", plus the seed handle."""

    def __init__(self, data: SeedData) -> None:
        self.data = data
        self.ids: dict[str, int] = {}

    def id_of(self, key: str) -> int:
        """The id of an item such as ``environment:db``."""
        return self.ids[key]

    def key_of(self, level: ItemLevel, item_id: int) -> str:
        """The "level:name" key of an id."""
        prefix = LEVEL_PREFIX[level]
        for key, value in self.ids.items():
            if key.startswith(f"{prefix}:") and value == item_id:
                return key
        raise KeyError((level, item_id))


@pytest.fixture
async def db(seeded_database: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session on the migrated temporary database."""
    async with create_session_factory(seeded_database)() as session:
        yield session


async def _add_environment(db: AsyncSession, tree: Tree, service: str, name: str) -> int:
    """Add an environment to a service and return its id."""
    environment = Environment(
        service_id=tree.id_of(f"service:{service}"), name=name, target_path=f"env/{name}"
    )
    db.add(environment)
    await db.flush()
    tree.ids[f"environment:{name}"] = environment.id
    return environment.id


async def _add_service(db: AsyncSession, tree: Tree, deployment: str, name: str) -> int:
    """Add a service to a deployment and return its id."""
    service = Service(deployment_id=tree.id_of(f"deployment:{deployment}"), name=name)
    db.add(service)
    await db.flush()
    tree.ids[f"service:{name}"] = service.id
    return service.id


async def _add_deployment(db: AsyncSession, tree: Tree, project: str, name: str) -> int:
    """Add a deployment to a project and return its id."""
    deployment = Deployment(
        project_id=tree.id_of(f"project:{project}"), name=name, root_path=f"/opt/{name}"
    )
    db.add(deployment)
    await db.flush()
    tree.ids[f"deployment:{name}"] = deployment.id
    return deployment.id


@pytest.fixture
def tree(seed_data: SeedData) -> Tree:
    """Labels for the seeded structure."""
    built = Tree(seed_data)
    seeded = {
        "project": vars(seed_data.projects),
        "deployment": vars(seed_data.deployments),
        "service": vars(seed_data.services),
        "environment": vars(seed_data.envs),
    }
    labels = {
        "project": {"rentals": "rentals", "billing": "billing"},
        "deployment": {
            "rentals_prod": "prod",
            "rentals_staging": "staging",
            "billing_main": "main",
            "billing_legacy": "legacy",
        },
        "service": {
            "mysql": "mysql",
            "web": "web",
            "staging_web": "staging_web",
            "api": "api",
            "worker": "worker",
            "cron": "cron",
        },
        "environment": {
            "db": "db",
            "db_secrets": "db_secrets",
            "web": "web",
            "staging_web": "staging_web",
            "api": "api",
            "worker": "worker",
        },
    }
    for level, names in labels.items():
        for attribute, label in names.items():
            built.ids[f"{level}:{label}"] = seeded[level][attribute].id
    return built


async def _make_user(db: AsyncSession, name: str, group: UserGroup, *, active: bool = True) -> User:
    """Persist a user."""
    user = make_user(name, group, active=active)
    db.add(user)
    await db.commit()
    return user


async def _grant(db: AsyncSession, tree: Tree, user: User, *keys: str) -> None:
    """Grant the user access to items given as "level:name" keys."""
    for key in keys:
        level_name, _, _ = key.partition(":")
        level = GrantLevel(level_name)
        target = {f"{level.value}_id": tree.id_of(key)}
        db.add(AccessGrant(user_id=user.id, level=level, **target))
    await db.commit()


async def _item(db: AsyncSession, tree: Tree, key: str) -> ItemRef:
    """Load the reference for an item key."""
    level = ItemLevel(key.partition(":")[0])
    item = await load_item(db, level, tree.id_of(key))
    assert item is not None
    return item


async def _visible(db: AsyncSession, tree: Tree, scope: AccessScope) -> tuple[set[str], set[str]]:
    """Names of the items in scope and of the context-only items."""
    visibility = await compute_visibility(db, scope)
    full = {
        tree.key_of(level, item_id) for level, ids in visibility.full.items() for item_id in ids
    }
    context = {
        tree.key_of(level, item_id) for level, ids in visibility.context.items() for item_id in ids
    }
    return full, context


async def _new_token(
    db: AsyncSession, owner: User, *restrictions: tuple[RestrictionLevel, int], name: str = "ci"
) -> ApiToken:
    """Persist a token with restrictions given as (level, target id)."""
    token = ApiToken(
        owner_id=owner.id,
        name=name,
        token_hash=f"hash-{owner.id}-{name}",
        prefix="emt_test",
        operations=["deploy"],
    )
    db.add(token)
    await db.flush()
    for level, target_id in restrictions:
        target = {f"{level.value}_id": target_id}
        db.add(ApiTokenRestriction(token_id=token.id, level=level, **target))
    await db.commit()
    return token


def _group_scope(group: UserGroup, grants: TargetSet | None = None) -> AccessScope:
    """A scope for a group with the given grants."""
    return AccessScope(1, group, grants or TargetSet())


# Capability matrix as the requirements state it: (capability, superadmin, admin, viewer).
CAPABILITY_MATRIX = [
    (Capability.MANAGE_USERS, True, False, False),
    (Capability.MANAGE_GRANTS, True, False, False),
    (Capability.MANAGE_SETTINGS, True, False, False),
    (Capability.MANAGE_STRUCTURE, True, False, False),
    (Capability.EDIT_VALUES, True, True, False),
    (Capability.DEPLOY, True, True, False),
    (Capability.VIEW, True, True, True),
    (Capability.REVEAL_SECRETS, True, True, False),
    (Capability.VIEW_POST_DEPLOY_STATUS, True, True, True),
    (Capability.VIEW_POST_DEPLOY_OUTPUT, True, True, False),
    (Capability.VIEW_POST_DEPLOY_COMMAND, True, False, False),
    (Capability.VIEW_AUDIT, True, True, False),
    (Capability.MANAGE_OWN_TOKENS, True, True, False),
]


def test_matrix_table_covers_every_capability() -> None:
    assert {row[0] for row in CAPABILITY_MATRIX} == set(Capability)
    assert set(CAPABILITY_GROUPS) == set(Capability)
    assert set(CAPABILITY_TARGETS) == set(Capability)


@pytest.mark.parametrize(("capability", "superadmin", "admin", "viewer"), CAPABILITY_MATRIX)
async def test_capability_matrix_with_a_covering_grant(
    db: AsyncSession,
    tree: Tree,
    capability: Capability,
    superadmin: bool,
    admin: bool,
    viewer: bool,
) -> None:
    """With a grant that covers the item, the group alone decides."""
    item = await _item(db, tree, "environment:db")
    covering = TargetSet(project_ids=frozenset({tree.id_of("project:rentals")}))
    target_item = None if CAPABILITY_TARGETS[capability].value == "none" else item
    expected = {
        UserGroup.SUPERADMIN: superadmin,
        UserGroup.ADMIN: admin,
        UserGroup.VIEWER: viewer,
    }
    for group, allowed in expected.items():
        grants = TargetSet(unrestricted=True) if group is UserGroup.SUPERADMIN else covering
        assert _group_scope(group, grants).can(capability, target_item) is allowed, group


@pytest.mark.parametrize(
    "capability",
    [row[0] for row in CAPABILITY_MATRIX if CAPABILITY_TARGETS[row[0]].value != "none"],
)
@pytest.mark.parametrize("group", [UserGroup.ADMIN, UserGroup.VIEWER])
async def test_group_never_grants_scope_without_a_grant(
    db: AsyncSession, tree: Tree, capability: Capability, group: UserGroup
) -> None:
    item = await _item(db, tree, "environment:db")
    assert _group_scope(group).can(capability, item) is False


async def test_superadmin_needs_no_grants(db: AsyncSession, tree: Tree) -> None:
    superadmin = tree.data.users.superadmin
    scope = await load_user_scope(db, superadmin)
    full, context = await _visible(db, tree, scope)
    assert full == ALL_ITEMS
    assert context == set()
    visibility = await compute_visibility(db, scope)
    assert visibility.unrestricted


# Minimum grant: environment-level actions need a grant at or above the environment.
# Columns: environment, then whether a grant on project rentals, deployment prod or
# environment db allows.
MINIMUM_GRANT_TABLE = [
    ("environment:db", True, True, True),
    ("environment:db_secrets", True, True, False),
    ("environment:web", True, True, False),
    ("environment:staging_web", True, False, False),
    ("environment:api", False, False, False),
]
ENVIRONMENT_CAPABILITIES = [
    Capability.EDIT_VALUES,
    Capability.DEPLOY,
    Capability.REVEAL_SECRETS,
]


@pytest.mark.parametrize(
    ("key", "by_project", "by_deployment", "by_environment"), MINIMUM_GRANT_TABLE
)
@pytest.mark.parametrize("capability", ENVIRONMENT_CAPABILITIES)
async def test_minimum_grant_for_environment_actions(
    db: AsyncSession,
    tree: Tree,
    capability: Capability,
    key: str,
    by_project: bool,
    by_deployment: bool,
    by_environment: bool,
) -> None:
    item = await _item(db, tree, key)
    for grant_key, expected in (
        ("project:rentals", by_project),
        ("deployment:prod", by_deployment),
        ("environment:db", by_environment),
    ):
        name = f"admin-{grant_key.replace(':', '-')}"
        admin = await _make_user(db, name, UserGroup.ADMIN)
        await _grant(db, tree, admin, grant_key)
        scope = await load_user_scope(db, admin)
        assert scope.can(capability, item) is expected, grant_key
    viewer = await _make_user(db, "viewer-all", UserGroup.VIEWER)
    await _grant(db, tree, viewer, "project:rentals", "project:billing")
    assert (await load_user_scope(db, viewer)).can(capability, item) is False


async def test_environment_capabilities_refuse_other_levels(db: AsyncSession, tree: Tree) -> None:
    scope = _group_scope(UserGroup.SUPERADMIN, TargetSet(unrestricted=True))
    for key in ("project:rentals", "deployment:prod", "service:mysql"):
        with pytest.raises(ValueError, match="environments"):
            scope.can(Capability.DEPLOY, await _item(db, tree, key))


async def test_item_capabilities_need_an_item(db: AsyncSession, tree: Tree) -> None:
    with pytest.raises(ValueError, match="needs an item"):
        _group_scope(UserGroup.ADMIN).can(Capability.VIEW)


# What a single set of grants lets a user see: items in scope and context-only parents.
VISIBILITY_TABLE = [
    pytest.param((), set(), set(), id="no-grants"),
    pytest.param(("project:rentals",), ALL_RENTALS, set(), id="project"),
    pytest.param(
        ("deployment:prod",),
        {
            "deployment:prod",
            "service:mysql",
            "service:web",
            "environment:db",
            "environment:db_secrets",
            "environment:web",
        },
        {"project:rentals"},
        id="deployment",
    ),
    pytest.param(
        ("environment:db",),
        {"environment:db"},
        {"project:rentals", "deployment:prod", "service:mysql"},
        id="environment",
    ),
    pytest.param(
        ("environment:db", "environment:web"),
        {"environment:db", "environment:web"},
        {"project:rentals", "deployment:prod", "service:mysql", "service:web"},
        id="two-environments-of-one-deployment",
    ),
    pytest.param(
        ("environment:db", "environment:staging_web"),
        {"environment:db", "environment:staging_web"},
        {
            "project:rentals",
            "deployment:prod",
            "deployment:staging",
            "service:mysql",
            "service:staging_web",
        },
        id="two-environments-of-two-deployments",
    ),
    pytest.param(
        ("environment:db", "deployment:staging"),
        {"environment:db", "deployment:staging", "service:staging_web", "environment:staging_web"},
        {"project:rentals", "deployment:prod", "service:mysql"},
        id="mixed-levels",
    ),
    pytest.param(
        ("deployment:prod", "project:billing"),
        DEPLOYMENT_PROD | ALL_BILLING,
        {"project:rentals"},
        id="deployment-and-other-project",
    ),
    pytest.param(
        ("project:rentals", "environment:db"),
        ALL_RENTALS,
        set(),
        id="overlapping-grants-are-a-union",
    ),
    pytest.param(("project:rentals", "project:billing"), ALL_ITEMS, set(), id="every-project"),
]


@pytest.mark.parametrize("group", [UserGroup.ADMIN, UserGroup.VIEWER])
@pytest.mark.parametrize(("grants", "expected_full", "expected_context"), VISIBILITY_TABLE)
async def test_visibility_follows_grants(
    db: AsyncSession,
    tree: Tree,
    group: UserGroup,
    grants: tuple[str, ...],
    expected_full: set[str],
    expected_context: set[str],
) -> None:
    user = await _make_user(db, "scoped", group)
    await _grant(db, tree, user, *grants)
    scope = await load_user_scope(db, user)
    full, context = await _visible(db, tree, scope)
    assert full == expected_full
    assert context == expected_context
    for key in ALL_ITEMS:
        item = await _item(db, tree, key)
        assert scope.can(Capability.VIEW, item) is (key in expected_full), key
        assert scope.covers(item) is (key in expected_full), key


# Seeded users and what each sees: (user attribute, items in scope, context-only items).
SEEDED_USER_TABLE = [
    ("superadmin", ALL_ITEMS, set()),
    ("admin", ALL_ITEMS, set()),
    ("viewer", ALL_RENTALS, set()),
    ("project_admin", ALL_RENTALS, set()),
    ("deployment_admin", DEPLOYMENT_PROD, {"project:rentals"}),
    (
        "env_scoped_admin",
        {"environment:db"},
        {"project:rentals", "deployment:prod", "service:mysql"},
    ),
    (
        "env_scoped_viewer",
        {"environment:db", "environment:api"},
        {
            "project:rentals",
            "project:billing",
            "deployment:prod",
            "deployment:main",
            "service:mysql",
            "service:api",
        },
    ),
    ("ungranted_admin", set(), set()),
]


@pytest.mark.parametrize(("attribute", "expected_full", "expected_context"), SEEDED_USER_TABLE)
async def test_seeded_users_see_their_scope(
    db: AsyncSession,
    tree: Tree,
    attribute: str,
    expected_full: set[str],
    expected_context: set[str],
) -> None:
    user = getattr(tree.data.users, attribute)
    scope = await load_user_scope(db, user)
    full, context = await _visible(db, tree, scope)
    assert full == expected_full
    assert context == expected_context


async def test_seeded_env_scoped_admin_works_on_one_file_of_a_service(
    db: AsyncSession, tree: Tree
) -> None:
    scope = await load_user_scope(db, tree.data.users.env_scoped_admin)
    mysql_id = tree.id_of("service:mysql")
    assert await permitted_environment_ids(db, scope, Capability.DEPLOY, service_id=mysql_id) == [
        tree.data.envs.db.id
    ]
    secrets = await _item(db, tree, "environment:db_secrets")
    for capability in (Capability.EDIT_VALUES, Capability.DEPLOY, Capability.REVEAL_SECRETS):
        assert scope.can(capability, secrets) is False
    viewer_scope = await load_user_scope(db, tree.data.users.env_scoped_viewer)
    db_item = await _item(db, tree, "environment:db")
    assert viewer_scope.can(Capability.VIEW, db_item) is True
    assert viewer_scope.can(Capability.EDIT_VALUES, db_item) is False


async def test_environment_grant_user_sees_parents_only_as_context(
    db: AsyncSession, tree: Tree
) -> None:
    admin = await _make_user(db, "env-admin", UserGroup.ADMIN)
    await _grant(db, tree, admin, "environment:db")
    scope = await load_user_scope(db, admin)
    visibility = await compute_visibility(db, scope)
    for key in ("project:rentals", "deployment:prod", "service:mysql"):
        level = ItemLevel(key.partition(":")[0])
        item_id = tree.id_of(key)
        assert visibility.is_context_only(level, item_id)
        assert not visibility.can_see(level, item_id)
        assert scope.can(Capability.VIEW, await _item(db, tree, key)) is False
    for key in ("deployment:staging", "service:web", "environment:db_secrets", "project:billing"):
        level = ItemLevel(key.partition(":")[0])
        assert not visibility.is_context_only(level, tree.id_of(key))
        assert not visibility.can_see(level, tree.id_of(key))


async def test_project_grant_covers_items_added_later(db: AsyncSession, tree: Tree) -> None:
    admin = await _make_user(db, "project-admin", UserGroup.ADMIN)
    await _grant(db, tree, admin, "project:rentals")
    await _add_deployment(db, tree, "rentals", "d-new")
    await _add_service(db, tree, "d-new", "s-new")
    await _add_environment(db, tree, "s-new", "e-new")
    await _add_environment(db, tree, "mysql", "e-extra")
    await db.commit()
    scope = await load_user_scope(db, admin)
    full, _ = await _visible(db, tree, scope)
    assert {
        "deployment:d-new",
        "service:s-new",
        "environment:e-new",
        "environment:e-extra",
    } <= full
    assert scope.can(Capability.DEPLOY, await _item(db, tree, "environment:e-new"))


async def test_deployment_grant_covers_services_and_environments_added_later(
    db: AsyncSession, tree: Tree
) -> None:
    admin = await _make_user(db, "deployment-admin", UserGroup.ADMIN)
    await _grant(db, tree, admin, "deployment:prod")
    await _add_service(db, tree, "prod", "s-new")
    await _add_environment(db, tree, "s-new", "e-new")
    await _add_environment(db, tree, "mysql", "e-extra")
    await _add_deployment(db, tree, "rentals", "d-sibling")
    await _add_service(db, tree, "d-sibling", "s-sibling")
    await _add_environment(db, tree, "s-sibling", "e-sibling")
    await db.commit()
    scope = await load_user_scope(db, admin)
    full, context = await _visible(db, tree, scope)
    assert {"service:s-new", "environment:e-new", "environment:e-extra"} <= full
    assert not {"deployment:d-sibling", "service:s-sibling", "environment:e-sibling"} & full
    assert not {"deployment:d-sibling", "service:s-sibling"} & context


async def test_environment_grant_leaves_new_environments_invisible(
    db: AsyncSession, tree: Tree
) -> None:
    admin = await _make_user(db, "env-admin", UserGroup.ADMIN)
    await _grant(db, tree, admin, "environment:db")
    await _add_environment(db, tree, "mysql", "x")
    await db.commit()
    scope = await load_user_scope(db, admin)
    full, context = await _visible(db, tree, scope)
    assert "environment:x" not in full
    assert "environment:x" not in context
    assert scope.can(Capability.EDIT_VALUES, await _item(db, tree, "environment:x")) is False
    await _grant(db, tree, admin, "environment:x")
    full, _ = await _visible(db, tree, await load_user_scope(db, admin))
    assert "environment:x" in full


async def test_revoked_grant_applies_immediately(db: AsyncSession, tree: Tree) -> None:
    admin = await _make_user(db, "revoked", UserGroup.ADMIN)
    await _grant(db, tree, admin, "project:rentals", "environment:api")
    item = await _item(db, tree, "environment:db")
    assert (await load_user_scope(db, admin)).can(Capability.DEPLOY, item)
    await db.execute(
        delete(AccessGrant).where(
            AccessGrant.user_id == admin.id, AccessGrant.project_id == tree.id_of("project:rentals")
        )
    )
    await db.commit()
    scope = await load_user_scope(db, admin)
    assert scope.can(Capability.DEPLOY, item) is False
    full, _ = await _visible(db, tree, scope)
    assert full == {"environment:api"}


async def test_grants_of_other_users_do_not_leak(db: AsyncSession, tree: Tree) -> None:
    first = await _make_user(db, "first", UserGroup.ADMIN)
    second = await _make_user(db, "second", UserGroup.ADMIN)
    await _grant(db, tree, first, "project:rentals")
    await _grant(db, tree, second, "project:billing")
    assert (await _visible(db, tree, await load_user_scope(db, first)))[0] == ALL_RENTALS
    full, _ = await _visible(db, tree, await load_user_scope(db, second))
    assert full == ALL_BILLING


async def test_disabled_user_scope_allows_nothing(db: AsyncSession, tree: Tree) -> None:
    for group in UserGroup:
        user = await _make_user(db, f"off-{group.value}", group, active=False)
        await _grant(db, tree, user, "project:rentals")
        scope = await load_user_scope(db, user)
        assert not scope.active
        for capability in Capability:
            target = CAPABILITY_TARGETS[capability]
            item = None if target.value == "none" else await _item(db, tree, "environment:db")
            assert scope.can(capability, item) is False, (group, capability)
        full, context = await _visible(db, tree, scope)
        assert full == set()
        assert context == set()
        assert not (await compute_visibility(db, scope)).unrestricted


async def test_user_moved_to_viewer_loses_edit_capabilities_at_once(
    db: AsyncSession, tree: Tree
) -> None:
    user = await _make_user(db, "demoted", UserGroup.ADMIN)
    await _grant(db, tree, user, "project:rentals")
    item = await _item(db, tree, "environment:db")
    assert (await load_user_scope(db, user)).can(Capability.EDIT_VALUES, item)
    user.group = UserGroup.VIEWER
    await db.commit()
    scope = await load_user_scope(db, user)
    assert scope.can(Capability.EDIT_VALUES, item) is False
    assert scope.can(Capability.DEPLOY, item) is False
    assert scope.can(Capability.VIEW, item) is True


async def test_permitted_environments_for_a_service(db: AsyncSession, tree: Tree) -> None:
    admin = await _make_user(db, "db-user", UserGroup.ADMIN)
    await _grant(db, tree, admin, "environment:db")
    scope = await load_user_scope(db, admin)
    mysql = tree.id_of("service:mysql")
    assert await permitted_environment_ids(db, scope, Capability.DEPLOY, service_id=mysql) == [
        tree.id_of("environment:db")
    ]
    assert (
        await permitted_environment_ids(
            db, scope, Capability.DEPLOY, service_id=tree.id_of("service:web")
        )
        == []
    )
    viewer = await _make_user(db, "db-viewer", UserGroup.VIEWER)
    await _grant(db, tree, viewer, "project:rentals")
    viewer_scope = await load_user_scope(db, viewer)
    assert (
        await permitted_environment_ids(db, viewer_scope, Capability.DEPLOY, service_id=mysql) == []
    )
    assert len(await permitted_environment_ids(db, viewer_scope, Capability.VIEW)) == 4


# Audit entries are in scope when their most specific id is in scope.
AUDIT_TABLE = [
    pytest.param(("project:rentals",), (None, None, None, None), False, id="no-ids-hidden"),
    pytest.param(("project:rentals",), ("rentals", None, None, None), True, id="project-in-scope"),
    pytest.param(
        ("project:rentals",), ("billing", None, None, None), False, id="project-out-of-scope"
    ),
    pytest.param(
        ("project:rentals",),
        ("rentals", "staging", "staging_web", "staging_web"),
        True,
        id="environment-in-scope",
    ),
    pytest.param(
        ("environment:db",), ("rentals", "prod", "mysql", "db"), True, id="granted-environment"
    ),
    pytest.param(
        ("environment:db",),
        ("rentals", "prod", "mysql", "db_secrets"),
        False,
        id="sibling-environment",
    ),
    pytest.param(
        ("environment:db",), ("rentals", "prod", "mysql", None), False, id="context-service"
    ),
    pytest.param(
        ("environment:db",), ("rentals", "prod", None, None), False, id="context-deployment"
    ),
    pytest.param(("environment:db",), ("rentals", None, None, None), False, id="context-project"),
    pytest.param(
        ("deployment:prod",), ("rentals", "prod", None, None), True, id="deployment-in-scope"
    ),
    pytest.param(
        ("deployment:prod",), ("rentals", "staging", None, None), False, id="other-deployment"
    ),
    pytest.param(("deployment:prod",), (None, None, None, 99999), False, id="deleted-item"),
]


@pytest.mark.parametrize(("grants", "ids", "expected"), AUDIT_TABLE)
async def test_audit_entry_scope(
    db: AsyncSession,
    tree: Tree,
    grants: tuple[str, ...],
    ids: tuple[str | None, str | None, str | None, int | str | None],
    expected: bool,
) -> None:
    admin = await _make_user(db, "auditor", UserGroup.ADMIN)
    await _grant(db, tree, admin, *grants)
    visibility = await compute_visibility(db, await load_user_scope(db, admin))

    def resolve(level: str, value: int | str | None) -> int | None:
        if value is None or isinstance(value, int):
            return value
        return tree.id_of(f"{level}:{value}")

    entry = {
        "project_id": resolve("project", ids[0]),
        "deployment_id": resolve("deployment", ids[1]),
        "service_id": resolve("service", ids[2]),
        "environment_id": resolve("environment", ids[3]),
    }
    assert visibility.includes_audit_entry(**entry) is expected


async def test_superadmin_sees_every_audit_entry_including_unscoped_and_deleted(
    db: AsyncSession, tree: Tree
) -> None:
    superadmin = tree.data.users.superadmin
    visibility = await compute_visibility(db, await load_user_scope(db, superadmin))
    for entry in (
        {"project_id": None, "deployment_id": None, "service_id": None, "environment_id": None},
        {"project_id": 9999, "deployment_id": 9999, "service_id": None, "environment_id": None},
    ):
        assert visibility.includes_audit_entry(**entry)


# Token narrowing: owner grants, token restrictions, and the items the token may reach.
NARROWING_TABLE = [
    pytest.param(
        ("project:rentals",),
        (("project", "rentals"),),
        ALL_RENTALS,
        id="same-project",
    ),
    pytest.param(
        ("project:rentals",),
        (("deployment", "prod"),),
        {
            "deployment:prod",
            "service:mysql",
            "service:web",
            "environment:db",
            "environment:db_secrets",
            "environment:web",
        },
        id="project-owner-deployment-token",
    ),
    pytest.param(
        ("project:rentals",),
        (("service", "mysql"),),
        {"service:mysql", "environment:db", "environment:db_secrets"},
        id="project-owner-service-token",
    ),
    pytest.param(
        ("project:rentals",),
        (("environment", "db"), ("environment", "staging_web")),
        {"environment:db", "environment:staging_web"},
        id="project-owner-environment-set-token",
    ),
    pytest.param(
        ("project:rentals",), (("project", "billing"),), set(), id="other-project-gives-nothing"
    ),
    pytest.param(
        ("project:rentals",),
        (("project", "rentals"), ("project", "billing")),
        ALL_RENTALS,
        id="extra-restriction-outside-owner-scope-adds-nothing",
    ),
    pytest.param(
        ("environment:db",),
        (("project", "rentals"),),
        {"environment:db"},
        id="environment-owner-wide-token",
    ),
    pytest.param(
        ("environment:db",),
        (("environment", "db_secrets"),),
        set(),
        id="environment-owner-other-environment-token",
    ),
    pytest.param(
        ("environment:db",),
        (("service", "mysql"),),
        {"environment:db"},
        id="environment-owner-service-token",
    ),
    pytest.param(
        ("deployment:prod", "environment:staging_web"),
        (("environment", "db_secrets"), ("environment", "staging_web"), ("environment", "api")),
        {"environment:db_secrets", "environment:staging_web"},
        id="mixed-owner-grants",
    ),
    pytest.param(("project:rentals",), (), ALL_RENTALS, id="no-restriction-is-owner-scope"),
]


def _restriction(tree: Tree, spec: tuple[str, str]) -> tuple[RestrictionLevel, int]:
    """A (level, id) pair from a (level name, item name) pair."""
    level_name, name = spec
    return RestrictionLevel(level_name), tree.id_of(f"{level_name}:{name}")


@pytest.mark.parametrize(("grants", "restrictions", "expected_full"), NARROWING_TABLE)
@pytest.mark.parametrize("group", [UserGroup.ADMIN])
async def test_token_scope_is_owner_scope_narrowed(
    db: AsyncSession,
    tree: Tree,
    group: UserGroup,
    grants: tuple[str, ...],
    restrictions: tuple[tuple[str, str], ...],
    expected_full: set[str],
) -> None:
    owner = await _make_user(db, "owner", group)
    await _grant(db, tree, owner, *grants)
    token = await _new_token(db, owner, *(_restriction(tree, spec) for spec in restrictions))
    token_scope = await load_token_scope(db, token)
    assert token_scope is not None and token_scope.is_token
    owner_full, _ = await _visible(db, tree, await load_user_scope(db, owner))
    full, context = await _visible(db, tree, token_scope)
    assert full == expected_full
    assert full <= owner_full
    assert not context & full
    for key in ALL_ITEMS:
        item = await _item(db, tree, key)
        assert token_scope.can(Capability.VIEW, item) is (key in expected_full), key
        if item.level is ItemLevel.ENVIRONMENT:
            assert token_scope.can(Capability.DEPLOY, item) is (key in expected_full), key


async def test_token_context_lists_only_ancestors_of_reachable_items(
    db: AsyncSession, tree: Tree
) -> None:
    owner = await _make_user(db, "owner", UserGroup.ADMIN)
    await _grant(db, tree, owner, "project:rentals")
    token = await _new_token(db, owner, _restriction(tree, ("environment", "web")))
    scope = await load_token_scope(db, token)
    assert scope is not None
    full, context = await _visible(db, tree, scope)
    assert full == {"environment:web"}
    assert context == {"project:rentals", "deployment:prod", "service:web"}

    other_owner = await _make_user(db, "other", UserGroup.ADMIN)
    await _grant(db, tree, other_owner, "environment:db")
    disjoint = await _new_token(
        db, other_owner, _restriction(tree, ("environment", "db_secrets")), name="disjoint"
    )
    disjoint_scope = await load_token_scope(db, disjoint)
    assert disjoint_scope is not None
    assert await _visible(db, tree, disjoint_scope) == (set(), set())


async def test_token_never_widens_when_owner_grants_change(db: AsyncSession, tree: Tree) -> None:
    owner = await _make_user(db, "owner", UserGroup.ADMIN)
    await _grant(db, tree, owner, "project:rentals")
    token = await _new_token(db, owner, _restriction(tree, ("deployment", "prod")))
    await db.execute(delete(AccessGrant).where(AccessGrant.user_id == owner.id))
    await db.commit()
    scope = await load_token_scope(db, token)
    assert scope is not None
    assert await _visible(db, tree, scope) == (set(), set())
    assert scope.can(Capability.DEPLOY, await _item(db, tree, "environment:db")) is False


async def test_token_sees_items_added_later_inside_its_restriction_only(
    db: AsyncSession, tree: Tree
) -> None:
    owner = await _make_user(db, "owner", UserGroup.ADMIN)
    await _grant(db, tree, owner, "project:rentals")
    token = await _new_token(db, owner, _restriction(tree, ("service", "mysql")))
    await _add_environment(db, tree, "mysql", "e-new")
    await _add_environment(db, tree, "web", "e-other")
    await db.commit()
    scope = await load_token_scope(db, token)
    assert scope is not None
    full, _ = await _visible(db, tree, scope)
    assert "environment:e-new" in full
    assert "environment:e-other" not in full


async def test_token_cannot_use_capabilities_without_an_item(db: AsyncSession, tree: Tree) -> None:
    superadmin = tree.data.users.superadmin
    token = await _new_token(db, superadmin)
    scope = await load_token_scope(db, token)
    assert scope is not None
    for capability, target in CAPABILITY_TARGETS.items():
        if target.value == "none":
            assert scope.can(capability) is False, capability
    assert scope.can(Capability.DEPLOY, await _item(db, tree, "environment:api")) is True


async def test_superadmin_token_can_be_restricted(db: AsyncSession, tree: Tree) -> None:
    superadmin = tree.data.users.superadmin
    token = await _new_token(db, superadmin, _restriction(tree, ("project", "billing")))
    scope = await load_token_scope(db, token)
    assert scope is not None
    full, _ = await _visible(db, tree, scope)
    assert full == ALL_BILLING
    assert not (await compute_visibility(db, scope)).unrestricted


async def test_token_stops_when_owner_is_disabled_or_made_viewer(
    db: AsyncSession, tree: Tree
) -> None:
    owner = await _make_user(db, "owner", UserGroup.ADMIN)
    await _grant(db, tree, owner, "project:rentals")
    token = await _new_token(db, owner)
    assert await load_token_scope(db, token) is not None

    owner.is_active = False
    await db.commit()
    assert await load_token_scope(db, token) is None

    owner.is_active = True
    owner.group = UserGroup.VIEWER
    await db.commit()
    assert await load_token_scope(db, token) is None

    owner.group = UserGroup.ADMIN
    await db.commit()
    assert await load_token_scope(db, token) is not None


async def test_revoked_grant_reaches_tokens_immediately(db: AsyncSession, tree: Tree) -> None:
    owner = await _make_user(db, "owner", UserGroup.ADMIN)
    await _grant(db, tree, owner, "project:rentals", "project:billing")
    token = await _new_token(db, owner)
    item = await _item(db, tree, "environment:db")
    scope = await load_token_scope(db, token)
    assert scope is not None and scope.can(Capability.DEPLOY, item)
    await db.execute(
        delete(AccessGrant).where(
            AccessGrant.user_id == owner.id, AccessGrant.project_id == tree.id_of("project:rentals")
        )
    )
    await db.commit()
    scope = await load_token_scope(db, token)
    assert scope is not None
    assert scope.can(Capability.DEPLOY, item) is False
    assert scope.can(Capability.DEPLOY, await _item(db, tree, "environment:api")) is True


async def test_losing_all_access_leaves_a_usable_token_with_empty_scope(
    db: AsyncSession, tree: Tree
) -> None:
    owner = await _make_user(db, "owner", UserGroup.ADMIN)
    token = await _new_token(db, owner)
    scope = await load_token_scope(db, token)
    assert scope is not None
    assert await _visible(db, tree, scope) == (set(), set())


# Token usability: (revoked, expires in seconds from now or None, owner active, group, usable).
USABILITY_TABLE = [
    (False, None, True, UserGroup.ADMIN, True),
    (False, None, True, UserGroup.SUPERADMIN, True),
    (False, 3600, True, UserGroup.ADMIN, True),
    (False, -1, True, UserGroup.ADMIN, False),
    (True, None, True, UserGroup.ADMIN, False),
    (True, 3600, True, UserGroup.SUPERADMIN, False),
    (False, None, False, UserGroup.ADMIN, False),
    (False, None, False, UserGroup.SUPERADMIN, False),
    (False, None, True, UserGroup.VIEWER, False),
]


@pytest.mark.parametrize(
    ("revoked", "expires_in", "owner_active", "group", "usable"), USABILITY_TABLE
)
async def test_token_usability(
    db: AsyncSession,
    revoked: bool,
    expires_in: int | None,
    owner_active: bool,
    group: UserGroup,
    usable: bool,
) -> None:
    now = utc_now()
    owner = await _make_user(db, "owner", group, active=owner_active)
    token = await _new_token(db, owner)
    token.revoked_at = now if revoked else None
    token.expires_at = None if expires_in is None else now + timedelta(seconds=expires_in)
    await db.commit()
    assert token_is_usable(token, owner, now=now) is usable
    assert (await load_token_scope(db, token, now=now) is not None) is usable
