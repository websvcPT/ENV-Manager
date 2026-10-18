"""The ``/api/v1`` routes: deploy a service and read the last deploy result of a service.

A deploy request stays open until the deploy has finished, which includes the post-deploy
command of the service, so a client needs a timeout longer than the command's. Dry runs and
deploys answer the result of the run they made.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import require_api_token
from app.api.errors import NO_STORE
from app.schemas.api import MAX_BODY_BYTES, RunResult
from app.services.api_auth import ApiCaller
from app.services.api_deploy import deploy_service, read_last_deployment
from app.services.deploy import DeployEngine
from app.web.dependencies import get_db, get_deploy_engine

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_token)])

Caller = Annotated[ApiCaller, Depends(require_api_token)]
Database = Annotated[AsyncSession, Depends(get_db)]
Engine = Annotated[DeployEngine, Depends(get_deploy_engine)]


async def read_body(request: Request) -> bytes | None:
    """The request body, or None when it is larger than the API accepts.

    A body that announces more than the limit is not read at all.
    """
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return None
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            return None
    return bytes(body)


def result_response(result: RunResult) -> JSONResponse:
    """The run as JSON, kept out of every cache."""
    return JSONResponse(result.model_dump(mode="json"), headers=NO_STORE)


@router.post("/services/{service_id}/deploy")
async def deploy(
    service_id: str, request: Request, caller: Caller, db: Database, engine: Engine
) -> JSONResponse:
    """Deploy or dry run a service as the token, and answer the run when it has finished."""
    body = await read_body(request)
    return result_response(await deploy_service(db, engine, caller, service_id, body))


@router.get("/services/{service_id}/deployments/last")
async def last_deployment(service_id: str, caller: Caller, db: Database) -> JSONResponse:
    """The last real deploy of the service, limited to the files the token reaches."""
    return result_response(await read_last_deployment(db, caller, service_id))
