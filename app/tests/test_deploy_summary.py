"""What the deployment page shows of recorded deploys: check outcomes, drift state and actor."""

import re
from pathlib import Path

import httpx
from fastapi import FastAPI
from sqlalchemy import select

from app.models import (
    Deployment,
    DistMissingPolicy,
    DistOutcome,
    DriftPolicy,
    DriftState,
    Service,
)
from app.services.audit import AuditActor
from app.services.deploy import DeployEngine
from app.services.deploy_types import DeployRequest
from app.services.deployment_detail import DeploymentDetail, load_deployment_detail
from app.services.paths import load_allowed_paths
from app.services.permissions import cli_scope, load_user_scope
from app.tests.conftest import sign_in
from app.tests.deploy_helpers import Harness, docker_less_runner, require_extra_key, set_deployment
from app.tests.dist_helpers import link_dist, relocate_deployments, write_dist


async def staging_detail(h: Harness, user: str = "superadmin") -> DeploymentDetail:
    """The staging deployment page data as a seeded user sees it."""
    scope = await load_user_scope(h.db, getattr(h.seed.users, user))
    detail = await load_deployment_detail(
        h.db, scope, h.seed.deployments.rentals_staging.id, await load_allowed_paths(h.db)
    )
    assert detail is not None
    return detail


async def test_a_deployment_without_recorded_checks_has_no_outcome(harness: Harness) -> None:
    detail = await staging_detail(harness)
    assert detail.dist_outcome is None and detail.drift_state is None


async def test_the_page_shows_the_worst_dist_outcome_and_a_missing_target(
    harness: Harness,
) -> None:
    h = harness
    await require_extra_key(h)
    await h.deploy(h.seed.services.staging_web.id)
    detail = await staging_detail(h)
    assert detail.dist_outcome is DistOutcome.BLOCKED
    assert detail.drift_state is DriftState.MISSING


async def test_a_clean_deploy_leaves_the_deployment_in_sync(harness: Harness) -> None:
    h = harness
    await h.deploy(h.seed.services.staging_web.id)
    detail = await staging_detail(h)
    assert detail.dist_outcome is DistOutcome.SKIPPED
    assert detail.drift_state is DriftState.IN_SYNC


async def test_a_dry_run_that_finds_drift_counts_until_a_write_settles_it(
    harness: Harness,
) -> None:
    h = harness
    web = h.seed.envs.staging_web
    target = h.roots["staging"] / "environments" / web.name
    target.write_text("WORKERS=old\n", encoding="utf-8")
    await set_deployment(
        h.db, h.seed.deployments.rentals_staging.id, drift_policy=DriftPolicy.OVERWRITE
    )
    await h.deploy(h.seed.services.staging_web.id, environments=[web], dry_run=True)
    assert (await staging_detail(h)).drift_state is DriftState.DRIFT
    await h.deploy(h.seed.services.staging_web.id, environments=[web])
    assert (await staging_detail(h)).drift_state is DriftState.IN_SYNC


async def test_a_halted_drift_stays_visible(harness: Harness) -> None:
    h = harness
    web = h.seed.envs.staging_web
    (h.roots["staging"] / "environments" / web.name).write_text("EDITED=1\n", encoding="utf-8")
    await h.deploy(h.seed.services.staging_web.id, environments=[web])
    assert (await staging_detail(h)).drift_state is DriftState.DRIFT


async def test_a_scoped_caller_sees_only_the_outcomes_of_the_files_in_scope(
    harness: Harness,
) -> None:
    h = harness
    await link_dist(h.db, h.seed.envs.db, None)
    write_dist(h.roots["prod"], "environments/.env.db.secrets-dist", "DB_USER=\nDB_EXTRA=\n")
    await link_dist(
        h.db,
        h.seed.envs.db_secrets,
        "environments/.env.db.secrets-dist",
        policy=DistMissingPolicy.BLOCK,
    )
    await h.deploy(h.seed.services.mysql.id, environments=[h.seed.envs.db, h.seed.envs.db_secrets])

    async def prod_detail(user: str) -> DeploymentDetail:
        scope = await load_user_scope(h.db, getattr(h.seed.users, user))
        detail = await load_deployment_detail(
            h.db, scope, h.seed.deployments.rentals_prod.id, await load_allowed_paths(h.db)
        )
        assert detail is not None
        return detail

    assert (await prod_detail("superadmin")).dist_outcome is DistOutcome.BLOCKED
    assert (await prod_detail("env_scoped_admin")).dist_outcome is DistOutcome.SKIPPED


async def test_a_cli_run_shows_the_cli_as_who(harness: Harness) -> None:
    h = harness
    await h.engine.run(
        h.db,
        cli_scope(),
        AuditActor.cli(),
        DeployRequest(service_id=h.seed.services.staging_web.id),
    )
    detail = await staging_detail(h)
    assert detail.history[0].who == "cli"
    (web,) = [row for row in detail.services if row.name == "web"]
    assert web.last_deploy is not None and web.last_deploy.actor == "cli"


def cell(page: str, label: str) -> str:
    """The markup of one status strip cell of the deployment page."""
    found = re.search(rf">{label}</p><div[^>]*>(.*?)</div>", page, re.DOTALL)
    assert found is not None
    return found.group(1)


async def test_the_status_strip_shows_recorded_outcomes_instead_of_not_checked_yet(
    web_app: FastAPI, web_client: httpx.AsyncClient, tmp_path: Path
) -> None:
    async with web_app.state.session_factory() as db:
        roots = await relocate_deployments(db, tmp_path / "host")
        deployment = await db.scalar(select(Deployment).where(Deployment.name == "staging"))
        assert deployment is not None
        service = await db.scalar(
            select(Service).where(Service.name == "web", Service.deployment_id == deployment.id)
        )
        assert service is not None
        await sign_in(web_client, "root")
        before = (await web_client.get(f"/deployments/{deployment.id}")).text
        assert cell(before, "Drift state").count("Not checked yet") == 1
        assert cell(before, "Dist check").count("Not checked yet") == 1

        engine = DeployEngine(
            web_app.state.crypto, tmp_path / "locks", hook_runner=docker_less_runner(tmp_path)
        )
        await engine.run(
            db, cli_scope(), AuditActor.cli(), DeployRequest(service_id=service.id, force=False)
        )
        roots["staging"].joinpath("environments", ".env.web").write_text("x=1\n", encoding="utf-8")
        await engine.run(
            db, cli_scope(), AuditActor.cli(), DeployRequest(service_id=service.id, dry_run=True)
        )
        after = (await web_client.get(f"/deployments/{deployment.id}")).text
    assert "Not checked yet" not in cell(after, "Drift state")
    assert 'data-status="drift"' in cell(after, "Drift state")
    assert "Skipped" in cell(after, "Dist check")
    assert "cli" in cell(after, "Last deploy")
