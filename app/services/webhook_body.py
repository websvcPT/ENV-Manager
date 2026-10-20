"""The JSON bytes the webhook posts: for a finished deploy and for the test event.

A deploy is read from the stored run through the same function the API uses, for every file the
run touched, so the document holds exactly the fields the API answers and never a value, file
content, host path, command text or command output. The document is serialized once; the same
bytes are signed and sent on every attempt.
"""

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    DeployResultStatus,
    DeployStatus,
    DeployTrigger,
    DistOutcome,
    DriftState,
    FileType,
    PostDeployStatus,
    Project,
)
from app.schemas.api import EnvironmentResult, PostDeployInfo, RunResult, ServiceInfo
from app.schemas.webhook import DEPLOY_EVENT, TEST_EVENT, DeployWebhookBody, ItemInfo
from app.services.api_runs import load_whole_run_result
from app.services.deploy_events import DeployFinished

UNKNOWN_NAME = "(unknown)"


def new_event_id() -> str:
    """A fresh identifier for one event, kept across the attempts to deliver it."""
    return str(uuid.uuid4())


def _document(
    run: RunResult, *, event: str, event_id: str, project: ItemInfo, deployment: ItemInfo
) -> bytes:
    """The compact JSON bytes of a run with its event, event id, project and deployment."""
    body = DeployWebhookBody(
        **run.model_dump(),
        event=event,  # type: ignore[arg-type]
        event_id=event_id,
        project=project,
        deployment=deployment,
    )
    return body.model_dump_json().encode("utf-8")


async def deploy_document(db: AsyncSession, event: DeployFinished, event_id: str) -> bytes | None:
    """The document for a finished deploy, None when its run can no longer be read."""
    run = await load_whole_run_result(db, event.run_id, event.service_id)
    if run is None:
        return None
    project = await db.get(Project, event.project_id)
    return _document(
        run,
        event=DEPLOY_EVENT,
        event_id=event_id,
        project=ItemInfo(id=event.project_id, name=project.name if project else UNKNOWN_NAME),
        deployment=ItemInfo(id=event.deployment_id, name=event.deployment_name),
    )


def sample_document(event_id: str, now: datetime) -> bytes:
    """The document of the test event: a made-up successful run of a made-up service."""
    run = RunResult(
        run_id=0,
        service=ServiceInfo(id=0, name="test-service"),
        trigger=DeployTrigger.API,
        dry_run=False,
        status=DeployStatus.SUCCESS,
        started_at=now,
        finished_at=now,
        environments=[
            EnvironmentResult(
                id=0,
                name=".env.test",
                file_type=FileType.ENV,
                version=1,
                status=DeployResultStatus.WRITTEN,
                dist=DistOutcome.PASSED,
                drift=DriftState.IN_SYNC,
                message=None,
            )
        ],
        post_deploy=PostDeployInfo(status=PostDeployStatus.NONE, exit_code=None, reason=None),
    )
    return _document(
        run,
        event=TEST_EVENT,
        event_id=event_id,
        project=ItemInfo(id=0, name="Test project"),
        deployment=ItemInfo(id=0, name="test-deployment"),
    )
