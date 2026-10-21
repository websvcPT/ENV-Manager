"""A deployment's own files deploy like a service: same engine, same results, own run."""

from app.models import DEPLOYMENT_FILES_NAME, DeployResultStatus, DeployStatus, Environment
from app.services.services import ensure_deployment_files, find_deployment_files
from app.services.versions import create_version
from app.tests.deploy_helpers import Harness, results_of


async def test_the_files_are_created_once_and_found_again(harness: Harness) -> None:
    h = harness
    deployment = h.seed.deployments.fancy_prod
    assert await find_deployment_files(h.db, deployment.id) is None
    first = await ensure_deployment_files(h.db, deployment, actor=h.seed.users.superadmin)
    await h.db.commit()
    again = await ensure_deployment_files(h.db, deployment, actor=h.seed.users.superadmin)
    assert first.id == again.id and first.name == DEPLOYMENT_FILES_NAME
    assert (await find_deployment_files(h.db, deployment.id)) is not None


async def test_deploying_the_files_writes_them_in_a_run_of_their_own(harness: Harness) -> None:
    h = harness
    deployment = h.seed.deployments.fancy_prod
    files = await ensure_deployment_files(h.db, deployment, actor=h.seed.users.superadmin)
    environment = Environment(service_id=files.id, name=".env", target_path=".env")
    h.db.add(environment)
    await h.db.flush()
    await create_version(
        h.db, h.crypto, environment.id, "STACK=prod\n", author=h.seed.users.superadmin
    )
    await h.db.commit()
    outcome = await h.deploy(files.id, user="superadmin")
    assert outcome.status is DeployStatus.SUCCESS and outcome.run_id is not None
    results = await results_of(h.db, outcome.run_id)
    assert results[".env"].status is DeployResultStatus.WRITTEN
    assert (h.roots["prod"] / ".env").read_text() == "STACK=prod\n"
