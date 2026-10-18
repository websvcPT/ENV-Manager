"""Post-deploy output that prints the resolved configuration of the demo stack, scrubbed for real.

A superadmin command such as ``docker compose config`` prints the values of every env file the
Compose project loads, for both demo services. The run, the audit entry and what the command line
prints must hold none of the managed demo values (``****`` stands in for them), whichever service
the command belongs to and whoever deployed it. The one demo value under four characters is the
documented exception: values that short are not scrubbed.
"""

from sqlalchemy import select

from app.models import DeployRun, DeployStatus, PostDeployStatus
from app.real_scenarios.conftest import DemoStack, Workloads, World
from app.real_scenarios.helpers import cli_runs, deploy_as, deploy_audit, output

# Managed values of the demo files that are long enough to be scrubbed. "demo" is both the API
# mode and the database user, so it disappears from the output wherever it appears, including
# the project name of the Compose file.
SCRUBBED_VALUES = (
    "8081",
    "demo",
    "demo-token-not-a-secret",
    "demo-sqlite",
    "5433",
    "demo-password-not-a-secret",
)
SHORT_VALUE_LINE = 'DEMO_DB_POOL: "4"'
CONFIG_COMMAND = "docker compose config"


def assert_scrubbed(text: str, where: str) -> None:
    """The text holds the resolved configuration with every long demo value replaced."""
    assert "services:" in text and "DEMO_API_TOKEN:" in text, f"{where}: not the configuration"
    for value in SCRUBBED_VALUES:
        assert value not in text, f"{where}: {value}"
    assert "DEMO_API_TOKEN: ****" in text and "DEMO_DB_PASSWORD: ****" in text, where
    assert SHORT_VALUE_LINE in text, f"{where}: a value under four characters stays visible"


async def test_a_command_that_prints_the_resolved_configuration_stores_no_demo_value(
    demo: DemoStack, world: World, workloads: Workloads
) -> None:
    # The baseline api file is drifted: its on-disk mode value is in no managed version. Deploy
    # it first, so every file Compose loads holds a managed version.
    (demo.targets / ".env.api").unlink()
    await world.change_hook(
        world.demo_seed.api.id, commands=CONFIG_COMMAND, run_only_if_changed=False
    )
    result = world.deploy("api", "--env", ".env.api")

    assert result.returncode == 0, output(result)
    assert "post-deploy: ran" in result.stdout and "post-deploy exit code: 0" in result.stdout
    printed = result.stdout.split("post-deploy output (truncated):", 1)[1]
    assert_scrubbed(printed, "cli output")
    assert "name: env-manager-****" in printed
    assert workloads.container_ids() == [], "the configuration was only printed, nothing started"

    async with world.session() as db:
        (run,) = await cli_runs(db)
        assert run.status is DeployStatus.SUCCESS
        assert run.post_deploy_status is PostDeployStatus.RAN and run.post_deploy_exit_code == 0
        assert run.post_deploy_output is not None
        assert_scrubbed(run.post_deploy_output, "stored run")
        (entry,) = await deploy_audit(db)
        stored = str(entry.details["post_deploy_output"])
        assert_scrubbed(stored, "audit entry")
        assert stored == run.post_deploy_output


async def test_a_user_who_cannot_see_the_secrets_file_gets_no_secret_in_the_output(
    demo: DemoStack, world: World, workloads: Workloads
) -> None:
    await world.change_hook(
        world.demo_seed.db.id, commands=CONFIG_COMMAND, run_only_if_changed=False
    )
    outcome = await deploy_as(world, "env_scoped_admin", world.demo_seed.db.id)

    assert outcome.status is DeployStatus.SUCCESS
    assert outcome.post_deploy.status is PostDeployStatus.RAN
    assert [environment.name for environment in outcome.environments] == [".env.db"]
    printed = outcome.post_deploy.output or ""
    for value in ("demo-password-not-a-secret", "demo-sqlite", "5433", "demo-token-not-a-secret"):
        assert value not in printed, value
    assert "DEMO_DB_PASSWORD: ****" in printed and SHORT_VALUE_LINE in printed

    async with world.session() as db:
        newest = (
            await db.execute(select(DeployRun).order_by(DeployRun.id.desc()).limit(1))
        ).scalar_one()
        assert newest.post_deploy_output == printed
        (entry,) = await deploy_audit(db)
        assert entry.details["post_deploy_output"] == printed

    # Only managed values are scrubbed: the drifted api file holds a value that exists on disk
    # alone, so Compose prints it and it stays visible. That limit is recorded in the notes.
    assert "DEMO_API_MODE: maintenance" in printed
