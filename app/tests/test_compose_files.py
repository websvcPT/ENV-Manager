"""The Compose files linked to a deployment: linking, the removal of the managed copies the
deployment files held (on link and at startup) and the audited read only view."""

import pytest
from sqlalchemy import select

from app.models import (
    ApiToken,
    ApiTokenRestriction,
    AuditActorType,
    Deployment,
    Environment,
    RestrictionLevel,
    Service,
)
from app.models.types import utc_now
from app.services.compose_config import ComposeScanError
from app.services.compose_files import (
    NOT_LINKED_MESSAGE,
    NOT_TEXT_MESSAGE,
    drop_managed_compose_files_everywhere,
    link_compose_files,
    read_compose_file,
)
from app.services.services import ensure_deployment_files, find_deployment_files
from app.services.versions import create_version
from app.tests.compose_helpers import GEOTRAVEL, add_stack
from app.tests.deploy_helpers import Harness, audit_of

COMPOSE = "docker-compose.yml"


async def managed_files(h: Harness, deployment: Deployment, *targets: str) -> dict[str, int]:
    """Deployment files holding the given targets, each with one version; their ids by target."""
    files = await ensure_deployment_files(h.db, deployment, actor=h.seed.users.superadmin)
    ids: dict[str, int] = {}
    for target in targets:
        environment = Environment(service_id=files.id, name=target, target_path=target)
        h.db.add(environment)
        await h.db.flush()
        await create_version(
            h.db, h.crypto, environment.id, GEOTRAVEL[target], author=h.seed.users.superadmin
        )
        ids[target] = environment.id
    await h.db.commit()
    return ids


async def targets_of(h: Harness, deployment: Deployment) -> list[str]:
    """The target paths of the deployment's files, sorted."""
    rows = await h.db.scalars(
        select(Environment.target_path)
        .join(Service, Service.id == Environment.service_id)
        .where(Service.deployment_id == deployment.id)
    )
    return sorted(rows)


async def test_linking_stores_the_files_in_order_and_unlinking_clears_them(
    harness: Harness,
) -> None:
    h = harness
    deployment, _ = await add_stack(h, {**GEOTRAVEL, "docker-compose.override.yml": "x: 1\n"})
    dropped = await link_compose_files(
        h.db, deployment, [COMPOSE, "docker-compose.override.yml"], actor=h.seed.users.superadmin
    )
    assert dropped.removed == () and dropped.kept == ()
    await h.db.refresh(deployment)
    assert deployment.compose_files == [COMPOSE, "docker-compose.override.yml"]
    await link_compose_files(h.db, deployment, [], actor=h.seed.users.superadmin)
    await h.db.refresh(deployment)
    assert deployment.compose_files is None
    first, second = await audit_of(h.db, "deployment.compose_link")
    assert first.details == {
        "compose_files": [COMPOSE, "docker-compose.override.yml"],
        "previous": [],
        "removed_files": [],
        "kept_files": [],
    }
    assert second.details is not None
    assert second.details["previous"] == [COMPOSE, "docker-compose.override.yml"]
    assert second.details["compose_files"] == []


@pytest.mark.parametrize(
    ("paths", "message"),
    [
        (["../outside.yml"], "relative to the deployment folder"),
        (["missing.yml"], "does not exist"),
        (["environments"], "not a regular file"),
        ([COMPOSE, COMPOSE], "chosen twice"),
    ],
)
async def test_a_path_the_import_would_refuse_is_not_linked(
    harness: Harness, paths: list[str], message: str
) -> None:
    h = harness
    deployment, _ = await add_stack(h, GEOTRAVEL)
    with pytest.raises(ComposeScanError, match=message):
        await link_compose_files(h.db, deployment, paths, actor=h.seed.users.superadmin)
    await h.db.rollback()
    await h.db.refresh(deployment)
    assert deployment.compose_files is None
    assert await audit_of(h.db, "deployment.compose_link") == []


async def test_linking_removes_the_managed_copy_but_not_the_host_file(harness: Harness) -> None:
    h = harness
    deployment, root = await add_stack(h, GEOTRAVEL)
    await managed_files(h, deployment, ".env", COMPOSE)
    dropped = await link_compose_files(h.db, deployment, [COMPOSE], actor=h.seed.users.superadmin)
    assert dropped.removed == (COMPOSE,) and dropped.kept == ()
    assert await targets_of(h, deployment) == [".env"]
    assert (root / COMPOSE).read_text() == GEOTRAVEL[COMPOSE]
    (removed,) = await audit_of(h.db, "environment.delete")
    assert removed.actor_type is AuditActorType.USER and removed.target_name == COMPOSE
    assert removed.details == {
        "versions": 1,
        "target_path": COMPOSE,
        "reason": "compose_file_linked",
    }
    (link,) = await audit_of(h.db, "deployment.compose_link")
    assert link.details is not None and link.details["removed_files"] == [COMPOSE]


async def test_a_file_a_live_token_is_restricted_to_is_kept_and_a_dead_one_goes(
    harness: Harness,
) -> None:
    h = harness
    deployment, _ = await add_stack(h, {**GEOTRAVEL, "docker-compose.override.yml": "x: 1\n"})
    ids = await managed_files(h, deployment, COMPOSE)
    files = await find_deployment_files(h.db, deployment.id)
    assert files is not None
    other = Environment(
        service_id=files.id,
        name="override",
        target_path="docker-compose.override.yml",
    )
    h.db.add(other)
    await h.db.flush()
    owner = h.seed.users.superadmin
    live = ApiToken(owner_id=owner.id, name="live", token_hash="a" * 64, prefix="envm_live")
    dead = ApiToken(
        owner_id=owner.id,
        name="dead",
        token_hash="b" * 64,
        prefix="envm_dead",
        revoked_at=utc_now(),
    )
    h.db.add_all([live, dead])
    await h.db.flush()
    h.db.add_all(
        [
            ApiTokenRestriction(
                token_id=live.id, level=RestrictionLevel.ENVIRONMENT, environment_id=ids[COMPOSE]
            ),
            ApiTokenRestriction(
                token_id=dead.id, level=RestrictionLevel.ENVIRONMENT, environment_id=other.id
            ),
        ]
    )
    await h.db.commit()
    dropped = await link_compose_files(
        h.db, deployment, [COMPOSE, "docker-compose.override.yml"], actor=owner
    )
    assert dropped.removed == ("docker-compose.override.yml",) and dropped.kept == (COMPOSE,)
    assert await targets_of(h, deployment) == [COMPOSE]
    remaining = await h.db.scalars(
        select(ApiTokenRestriction.token_id).where(
            ApiTokenRestriction.token_id.in_([live.id, dead.id])
        )
    )
    assert list(remaining) == [live.id]


async def test_the_startup_step_removes_the_managed_copies_once_as_the_system(
    harness: Harness,
) -> None:
    h = harness
    first, first_root = await add_stack(h, GEOTRAVEL, name="first")
    second, _ = await add_stack(h, GEOTRAVEL, name="second")
    plain, _ = await add_stack(h, GEOTRAVEL, name="plain")
    for deployment in (first, second, plain):
        await managed_files(h, deployment, ".env", COMPOSE)
    for deployment in (first, second):
        deployment.compose_files = [COMPOSE]
    await h.db.commit()

    dropped = await drop_managed_compose_files_everywhere(h.db)

    assert dropped.removed == (COMPOSE, COMPOSE) and dropped.kept == ()
    assert await targets_of(h, first) == [".env"] and await targets_of(h, second) == [".env"]
    assert await targets_of(h, plain) == [".env", COMPOSE]
    assert (first_root / COMPOSE).read_text() == GEOTRAVEL[COMPOSE]
    entries = await audit_of(h.db, "environment.delete")
    assert [(entry.actor_type, entry.actor_name) for entry in entries] == [
        (AuditActorType.SYSTEM, "upgrade")
    ] * 2
    again = await drop_managed_compose_files_everywhere(h.db)
    assert again.removed == () and len(await audit_of(h.db, "environment.delete")) == 2


async def test_a_view_reads_the_current_text_and_is_audited(harness: Harness) -> None:
    h = harness
    deployment, root = await add_stack(h, GEOTRAVEL)
    await link_compose_files(h.db, deployment, [COMPOSE], actor=h.seed.users.superadmin)
    (root / COMPOSE).write_text("services: {}\n", encoding="utf-8")
    shown = await read_compose_file(h.db, deployment, 0, actor=h.seed.users.admin)
    assert (shown.path, shown.text) == (COMPOSE, "services: {}\n")
    (entry,) = await audit_of(h.db, "deployment.compose_view")
    assert entry.details == {"file": COMPOSE} and entry.deployment_id == deployment.id
    assert entry.actor_name == h.seed.users.admin.name


@pytest.mark.parametrize(
    ("index", "content", "message"),
    [
        (1, None, NOT_LINKED_MESSAGE),
        (-1, None, NOT_LINKED_MESSAGE),
        (0, b"\xff\xfe not text", NOT_TEXT_MESSAGE),
        (0, "gone", "does not exist"),
    ],
)
async def test_a_view_that_cannot_be_shown_says_why_and_is_not_audited(
    harness: Harness, index: int, content: bytes | str | None, message: str
) -> None:
    h = harness
    deployment, root = await add_stack(h, GEOTRAVEL)
    await link_compose_files(h.db, deployment, [COMPOSE], actor=h.seed.users.superadmin)
    if content == "gone":
        (root / COMPOSE).unlink()
    elif isinstance(content, bytes):
        (root / COMPOSE).write_bytes(content)
    with pytest.raises(ComposeScanError, match=message):
        await read_compose_file(h.db, deployment, index, actor=h.seed.users.admin)
    assert await audit_of(h.db, "deployment.compose_view") == []
