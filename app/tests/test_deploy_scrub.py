"""Post-deploy output is scrubbed of configuration values before it is stored, audited or returned.

The deploy runs as a user who cannot see every file of the service, with a runner that prints
values of files in and out of that user's scope.
"""

import json
import re
from pathlib import Path

import pytest

from app.models import DeployStatus, HookType, PostDeployStatus, ServiceHook
from app.seeds import EDITOR_SECRET_VALUES
from app.services import deploy as deploy_module
from app.services.deploy import SCRUBBER_FAILED_REASON, DeployEngine
from app.services.deploy_types import DeployOutcome
from app.services.hook_runner import DockerHookRunner
from app.services.hooks import PostDeployResult
from app.tests.deploy_helpers import Harness, audit_of, runs
from app.tests.dist_helpers import link_dist
from app.tests.fake_docker import FakeDocker

PRINTED = (
    "password seed-secret port 3306 host db "
    f"editor {EDITOR_SECRET_VALUES[3]} {EDITOR_SECRET_VALUES[2]}"
)
SCRUBBED = "password **** port **** host db editor **** ****"


def leaks(value: str, text: str) -> bool:
    """Whether the value appears in the text as its own token, not inside a longer number or id."""
    return re.search(rf"(?<![\w.]){re.escape(value)}(?![\w.])", text) is not None


async def prepare_mysql(h: Harness, commands: str | None = "docker compose up -d") -> None:
    """Unlink the dist template the temporary folder lacks and store a command, if any."""
    await link_dist(h.db, h.seed.envs.db, None)
    if commands is not None:
        h.db.add(
            ServiceHook(
                service_id=h.seed.services.mysql.id, type=HookType.POST_DEPLOY, commands=commands
            )
        )
        await h.db.commit()


async def deploy_mysql_as_scoped_admin(h: Harness, printed: str = PRINTED) -> DeployOutcome:
    """Deploy the prod mysql service as the user who holds only the ``.env.db`` file."""
    await prepare_mysql(h)
    h.runner.result = PostDeployResult(PostDeployStatus.RAN, exit_code=0, output=printed)
    return await h.deploy(h.seed.services.mysql.id, user="env_scoped_admin")


async def test_values_of_files_the_user_cannot_see_never_reach_the_run_audit_or_outcome(
    harness: Harness,
) -> None:
    h = harness
    outcome = await deploy_mysql_as_scoped_admin(h)

    assert outcome.post_deploy.output == SCRUBBED
    await h.db.rollback()
    run = (await runs(h.db))[-1]
    assert run.post_deploy_output == SCRUBBED
    (entry,) = await audit_of(h.db, "service.deploy")
    assert entry.details["post_deploy_output"] == SCRUBBED
    everything = json.dumps(entry.details) + str(run.post_deploy_output) + repr(outcome)
    for value in ("seed-secret", "3306", EDITOR_SECRET_VALUES[2], EDITOR_SECRET_VALUES[3]):
        assert not leaks(value, everything), value


async def test_a_short_value_is_not_scrubbed(harness: Harness) -> None:
    """The documented limit: a value under four characters (here ``db``) stays in the output."""
    outcome = await deploy_mysql_as_scoped_admin(harness, "host db and a db pool")
    assert outcome.post_deploy.output == "host db and a db pool"


async def test_the_scrubber_holds_the_files_of_the_whole_deployment(harness: Harness) -> None:
    h = harness
    await deploy_mysql_as_scoped_admin(h)
    (request,) = h.runner.requests
    scrubber = request.scrubber
    assert scrubber.scrub_text("seed-secret") == "****"
    assert scrubber.scrub_text(EDITOR_SECRET_VALUES[3]) == "****"
    assert scrubber.scrub_text(EDITOR_SECRET_VALUES[2]) == "****"
    assert scrubber.scrub_text("seed-key") == "seed-key"
    # Only values are scrubbed: an old password that survives in a comment is not.
    assert scrubber.scrub_text(EDITOR_SECRET_VALUES[1]) == EDITOR_SECRET_VALUES[1]


async def test_a_runner_that_ignores_the_scrubber_is_still_scrubbed_on_every_field(
    harness: Harness,
) -> None:
    h = harness
    await prepare_mysql(h)
    h.runner.result = PostDeployResult(
        PostDeployStatus.FAILED,
        exit_code=1,
        output="out seed-secret",
        reason="Failed on seed-secret.",
    )
    outcome = await h.deploy(h.seed.services.mysql.id, user="env_scoped_admin")
    assert outcome.status is DeployStatus.POST_DEPLOY_FAILED
    assert outcome.post_deploy.output == "out ****"
    assert outcome.post_deploy.reason == "Failed on ****."
    run = (await runs(h.db))[-1]
    assert (run.post_deploy_output, run.post_deploy_reason) == ("out ****", "Failed on ****.")
    (entry,) = await audit_of(h.db, "service.deploy")
    assert "seed-secret" not in json.dumps(entry.details)


async def test_a_service_without_a_command_decrypts_nothing_for_scrubbing(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = harness
    await prepare_mysql(h, None)

    async def refuse(*arguments: object, **options: object) -> None:
        raise AssertionError("values were loaded for a service without a command")

    monkeypatch.setattr(deploy_module, "load_scrubber", refuse)
    outcome = await h.deploy(h.seed.services.mysql.id, user="env_scoped_admin")
    assert outcome.post_deploy.status is PostDeployStatus.RAN
    (request,) = h.runner.requests
    assert not request.scrubber


async def test_when_the_values_cannot_be_loaded_the_command_is_not_run(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = harness
    await prepare_mysql(h)

    async def broken(*arguments: object, **options: object) -> None:
        raise RuntimeError("database trouble")

    monkeypatch.setattr(deploy_module, "load_scrubber", broken)
    outcome = await h.deploy(h.seed.services.mysql.id, user="env_scoped_admin")
    assert outcome.status is DeployStatus.POST_DEPLOY_FAILED
    assert outcome.post_deploy.status is PostDeployStatus.FAILED
    assert outcome.post_deploy.reason == SCRUBBER_FAILED_REASON
    assert h.runner.requests == []
    await h.db.rollback()
    assert (await runs(h.db))[-1].post_deploy_reason == SCRUBBER_FAILED_REASON


async def test_the_real_runner_output_is_scrubbed_end_to_end(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    fake = FakeDocker(tmp_path / "fake")
    fake.behavior('echo "config: password=seed-secret port=3306"')
    h.engine = DeployEngine(
        h.crypto, h.lock_dir, hook_runner=DockerHookRunner(fake.cli()), notifier=h.notifier
    )
    await prepare_mysql(h, "docker compose config")
    outcome = await h.deploy(h.seed.services.mysql.id, user="env_scoped_admin")
    assert outcome.post_deploy.status is PostDeployStatus.RAN
    assert outcome.post_deploy.output == "config: password=**** port=****\n"
    (entry,) = await audit_of(h.db, "service.deploy")
    assert entry.details["post_deploy_output"] == "config: password=**** port=****\n"
    assert h.runner.requests == []


def test_a_short_value_inside_a_longer_number_is_not_a_leak() -> None:
    assert leaks("3306", "port 3306 host") and leaks("3306", "(3306,")
    assert not leaks("3306", "started_at=0.33061 id=133067 run-3306x")
