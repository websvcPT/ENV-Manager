"""A deploy made over the API reaches the webhook through the running application's notifier,
signed, as the same run the API answers, and never with a value, output, path or command."""

import json

from app.core.crypto import CryptoService
from app.seeds import EDITOR_ENVIRONMENT_NAME, EDITOR_SECRET_VALUES
from app.services.notifier import Notifications
from app.tests.api_helpers import ApiWorld, deploy, make_token
from app.tests.deploy_helpers import version_text
from app.tests.notify_helpers import RecordingTransport
from app.tests.webhook_helpers import (
    WEBHOOK_SECRET,
    RecordingWebhook,
    configure_webhook,
    verifies,
)

FILES = [".env.db", EDITOR_ENVIRONMENT_NAME, ".env.db.secrets"]


async def install_webhook(
    world: ApiWorld, crypto: CryptoService
) -> tuple[Notifications, RecordingWebhook]:
    """Switch the webhook on and give the application a notifier that posts to a recorder."""
    await configure_webhook(world.app.state.engine, crypto)
    recorder = RecordingWebhook()
    notifier = Notifications(
        world.app.state.session_factory,
        crypto,
        base_url="https://env.example.com",
        transport=RecordingTransport(),
        webhook_transport=recorder,
    )
    world.app.state.notifier = notifier
    return notifier, recorder


async def test_an_api_deploy_is_posted_as_the_run_the_api_answered(
    api_world: ApiWorld, crypto: CryptoService
) -> None:
    """Same fields as the answer, plus the event and the names; a command that printed the
    managed files leaves nothing of them in the request."""
    world = api_world
    notifier, recorder = await install_webhook(world, crypto)
    secret = await make_token(world, "admin")
    mysql = world.services["prod/mysql"]
    values = list(EDITOR_SECRET_VALUES)
    async with world.session() as db:
        for name in FILES:
            text = await version_text(db, crypto, world.environments[f"mysql/{name}"])
            for line in text.splitlines():
                if "=" in line and not line.startswith("#"):
                    values.append(line.partition("=")[2].strip().strip("'\""))
    world.docker.behavior(
        "echo OUTPUT-MARKER-5521; cat " + " ".join(f"environments/{name}" for name in FILES)
    )
    answered = await deploy(world, secret, mysql)
    assert answered.status_code == 200
    await notifier.idle()
    (request,) = recorder.sent
    assert verifies(WEBHOOK_SECRET, request)
    document = json.loads(request.body)
    answer = answered.json()
    assert {key: document[key] for key in answer} == answer
    assert document["event"] == "deploy.finished" and document["event_id"]
    assert document["project"]["name"] == "rentals" and document["deployment"]["name"] == "prod"
    text = request.body.decode() + json.dumps(dict(request.headers))
    assert "OUTPUT-MARKER" not in text and "output" not in text
    assert "compose" not in text and "docker" not in text
    assert str(world.roots["prod"]) not in text and ".bak" not in text
    assert not [value for value in values if len(value) >= 4 and value in text]
    assert WEBHOOK_SECRET not in text


async def test_a_dry_run_over_the_api_posts_nothing_and_a_real_run_is_posted(
    api_world: ApiWorld, crypto: CryptoService
) -> None:
    """Dry runs never leave the application; a real run does."""
    world = api_world
    notifier, recorder = await install_webhook(world, crypto)
    secret = await make_token(world, "admin")
    mysql = world.services["prod/mysql"]
    dry = await deploy(world, secret, mysql, {"dry_run": True})
    assert dry.status_code == 200 and dry.json()["dry_run"] is True
    await notifier.idle()
    assert recorder.sent == []
    real = await deploy(world, secret, mysql)
    assert real.status_code == 200
    await notifier.idle()
    (request,) = recorder.sent
    assert json.loads(request.body)["run_id"] == real.json()["run_id"]
    assert json.loads(request.body)["status"] == real.json()["status"]
