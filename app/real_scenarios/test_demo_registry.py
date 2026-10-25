"""Registry logins against a real private registry and the host Docker daemon.

A local registry with a login runs on 127.0.0.1 (see ``registry.py``) and holds a private image.
A post-deploy command pulls it through the real daemon: it works with the login the app stores
and fails with the registry's own refusal without it or with a wrong token. Test login runs the
real ``docker login`` against the same registry. In every case neither the token nor its base64
form appears in what the run, the audit log or the command line kept.
"""

import base64
import subprocess

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditEntry, DeployStatus, PostDeployStatus, User
from app.real_scenarios.conftest import World
from app.real_scenarios.helpers import cli_runs, output
from app.real_scenarios.registry import LocalRegistry, local_image_present
from app.services.docker_cli import DockerCli
from app.services.registries import add_registry, update_registry
from app.services.registry_check import (
    ACTION_TEST,
    OK_MESSAGE,
    REFUSED_MESSAGE,
    check_registry_login,
)
from app.services.scratch_sweep import RUN_FOLDER_PREFIX

WRONG_TOKEN = "dev-only-wrong-registry-token"
REGISTRY_REASONS = ("no basic auth credentials", "unauthorized")


def encoded(registry: LocalRegistry, token: str) -> str:
    """The base64 of ``username:token``, the form a Docker config file keeps."""
    return base64.b64encode(f"{registry.username}:{token}".encode()).decode("ascii")


async def superadmin(db: AsyncSession) -> User:
    """The superadmin of the seeded database."""
    user = await db.scalar(select(User).where(User.name == "root"))
    assert user is not None
    return user


async def store_login(world: World, registry: LocalRegistry, token: str) -> int:
    """Store a login for the registry through the service, as the superadmin, and give its id."""
    async with world.session() as db:
        stored = await add_registry(
            db, world.crypto, registry.host, registry.username, token, actor=await superadmin(db)
        )
        return stored.id


async def pull_with_deploy(
    world: World, reference: str
) -> tuple[subprocess.CompletedProcess[str], str]:
    """Deploy ``db`` with a stored command that pulls the image; the finished command line and
    the post-deploy output the run stored."""
    await world.change_hook(
        world.demo_seed.db.id, commands=f"docker pull {reference}", run_only_if_changed=False
    )
    result = world.deploy("db", "--env", ".env.db")
    async with world.session() as db:
        (run,) = await cli_runs(db)
        assert run.post_deploy_output is not None, output(result)
    return result, run.post_deploy_output


async def audit_text(world: World) -> str:
    """The details of every audit entry, as one text."""
    async with world.session() as db:
        entries = (await db.execute(select(AuditEntry))).scalars()
        return "\n".join(str(entry.details) for entry in entries)


async def test_a_post_deploy_pull_of_a_private_image_works_with_the_stored_login_and_shows_no_token(
    world: World, local_registry: LocalRegistry, private_image: str
) -> None:
    await store_login(world, local_registry, local_registry.password)
    assert not local_image_present(private_image)

    result, stored = await pull_with_deploy(world, private_image)
    printed = output(result)

    assert result.returncode == 0, printed
    assert "post-deploy: ran" in printed and "post-deploy exit code: 0" in printed
    assert "Pulling from private/alpine" in stored
    assert local_image_present(private_image), "the image was pulled from the registry"
    async with world.session() as db:
        (run,) = await cli_runs(db)
        assert run.status is DeployStatus.SUCCESS
        assert (run.post_deploy_status, run.post_deploy_exit_code) == (PostDeployStatus.RAN, 0)
    shown = printed + stored + await audit_text(world)
    for secret in (local_registry.password, encoded(local_registry, local_registry.password)):
        assert secret not in shown


async def test_without_a_login_the_pull_is_refused_by_the_registry(
    world: World, private_image: str
) -> None:
    result, stored = await pull_with_deploy(world, private_image)
    printed = output(result)

    assert result.returncode == 1, printed
    assert "post-deploy: failed (The command exited with code 1.)" in printed
    assert any(reason in stored.lower() for reason in REGISTRY_REASONS), stored
    assert not local_image_present(private_image)
    async with world.session() as db:
        (run,) = await cli_runs(db)
        assert run.status is DeployStatus.POST_DEPLOY_FAILED
        assert (run.post_deploy_status, run.post_deploy_exit_code) == (PostDeployStatus.FAILED, 1)


async def test_a_stored_login_with_a_wrong_token_is_refused_and_the_token_is_not_shown(
    world: World, local_registry: LocalRegistry, private_image: str
) -> None:
    await store_login(world, local_registry, WRONG_TOKEN)

    result, stored = await pull_with_deploy(world, private_image)
    printed = output(result)

    assert result.returncode == 1, printed
    assert "post-deploy: failed (The command exited with code 1.)" in printed
    assert "401 unauthorized" in stored.lower(), stored
    assert not local_image_present(private_image)
    shown = printed + stored + await audit_text(world)
    for secret in (WRONG_TOKEN, encoded(local_registry, WRONG_TOKEN)):
        assert secret not in shown


async def test_test_login_says_ok_for_the_right_token_and_gives_the_registrys_reason_for_a_wrong(
    world: World, local_registry: LocalRegistry
) -> None:
    cli = DockerCli()
    registry_id = await store_login(world, local_registry, local_registry.password)

    async with world.session() as db:
        actor = await superadmin(db)
        right = await check_registry_login(db, world.crypto, cli, registry_id, actor=actor)
        assert right.ok and right.message == OK_MESSAGE and right.output is None

        await update_registry(
            db,
            world.crypto,
            registry_id,
            local_registry.host,
            local_registry.username,
            WRONG_TOKEN,
            actor=actor,
        )
        wrong = await check_registry_login(db, world.crypto, cli, registry_id, actor=actor)
        assert not wrong.ok and wrong.message == REFUSED_MESSAGE
        assert wrong.output is not None
        assert wrong.output.splitlines()[-1].endswith("401 Unauthorized"), wrong.output
        for secret in (WRONG_TOKEN, encoded(local_registry, WRONG_TOKEN)):
            assert secret not in wrong.output

        entries = (
            await db.execute(
                select(AuditEntry).where(AuditEntry.action == ACTION_TEST).order_by(AuditEntry.id)
            )
        ).scalars()
        assert [(entry.details["ok"], entry.details["outcome"]) for entry in entries] == [
            (True, "ok"),
            (False, "refused"),
        ]
    shown = await audit_text(world)
    for secret in (
        local_registry.password,
        WRONG_TOKEN,
        encoded(local_registry, local_registry.password),
        encoded(local_registry, WRONG_TOKEN),
    ):
        assert secret not in shown
    assert not [p for p in cli.scratch.iterdir() if p.name.startswith(RUN_FOLDER_PREFIX)]
