"""The JSON document the outbound webhook posts for a deploy result.

It carries every field of the API's run result, through the same model, so what the webhook may
say is exactly what the API may say: no value, file content, host path, command text or command
output. On top of that it names the event and the project and deployment, which the API leaves
to the URL.
"""

from typing import Literal

from pydantic import BaseModel

from app.schemas.api import RunResult

DEPLOY_EVENT = "deploy.finished"
TEST_EVENT = "webhook.test"


class ItemInfo(BaseModel):
    """A project or a deployment by id and name."""

    id: int
    name: str


class DeployWebhookBody(RunResult):
    """A finished deploy run as the webhook posts it.

    ``event`` is ``deploy.finished``, or ``webhook.test`` for the test event, which carries a
    made-up run so a receiver can check how it reads the document. ``event_id`` is unique per
    event and stays the same when a delivery is tried again, so a receiver can ignore repeats.
    """

    event: Literal["deploy.finished", "webhook.test"]
    event_id: str
    project: ItemInfo
    deployment: ItemInfo
