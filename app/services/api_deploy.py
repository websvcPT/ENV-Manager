"""The two operations of the API: deploy a service and read the last deploy result of a service.

Each call checks, in this order, that the token carries the operation (403), that the service is
inside what the token reaches (404, the same answer for a service that does not exist), the body
(422) and then runs the deploy engine as the token. The scope is the owner's grants narrowed by
the token's restrictions, loaded when the call arrives. The API never forces: a body carrying
``force`` is refused, so a blocked or halted file is resolved by a person.

Every call is recorded by one audit entry with the token and its owner (a deploy also leaves the
dist check entries of the engine). A deploy the engine ran or refused because of the lock is
audited by the engine; every other refusal and every read is audited here. An entry for a service
the token does not reach names only the id that was asked for.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditResult, Environment
from app.schemas.api import (
    MAX_ID,
    DeployBody,
    FieldIssue,
    InvalidBody,
    RunResult,
    parse_deploy_body,
)
from app.services.api_auth import ApiCaller
from app.services.api_errors import ApiRefusal, RefusalKind
from app.services.api_runs import load_last_run_result, load_run_result
from app.services.audit import record_committed_audit
from app.services.deploy import DeployEngine
from app.services.deploy_records import ACTION_DEPLOY
from app.services.deploy_types import DeployRefusal, DeployRequest, DeployRequestError
from app.services.permissions import Capability, permitted_environment_ids
from app.services.structure_lookup import ServiceChain, service_with_deployment
from app.services.tokens import OPERATION_DEPLOY, OPERATION_READ_LAST_DEPLOYMENT

ACTION_READ_LAST = "service.last_deploy_read"
SERVICE_NOT_FOUND = "Service not found."
NO_DEPLOYMENT_FOUND = "No deployment was found for this service."
DEPLOY_NOT_ALLOWED = "This token is not allowed to deploy."
READ_NOT_ALLOWED = "This token is not allowed to read the last deployment."
INVALID_REQUEST = "The request is not valid."
LOCKED_MESSAGE = "Another deploy is running for this deployment folder. Try again when it ends."
LOCK_UNAVAILABLE_MESSAGE = "The deploy lock could not be taken."
MAX_SERVICE_ID_DIGITS = len(str(MAX_ID)) - 1


@dataclass
class _Call:
    """What is known about one call while it is checked, for the entry that records it.

    ``service`` is filled in only after the service was found inside the token's scope.
    """

    requested: int | None
    service: ServiceChain | None = None


def _service_id(reference: str) -> int | None:
    """The id a path segment names, or None when it cannot name a service."""
    if not reference.isascii() or not reference.isdigit() or len(reference) > MAX_SERVICE_ID_DIGITS:
        return None
    return int(reference)


async def _reachable(
    db: AsyncSession, caller: ApiCaller, call: _Call, capability: Capability, message: str
) -> tuple[int, list[int]]:
    """The service id and the environments of it the token reaches, or a 404 refusal."""
    not_found = ApiRefusal(RefusalKind.NOT_FOUND, message)
    if call.requested is None:
        raise not_found
    found = await service_with_deployment(db, call.requested)
    permitted = (
        await permitted_environment_ids(db, caller.scope, capability, service_id=call.requested)
        if found is not None
        else []
    )
    if found is None or not permitted:
        raise not_found
    call.service = found
    return call.requested, permitted


async def _audit_call(
    db: AsyncSession,
    caller: ApiCaller,
    call: _Call,
    *,
    action: str,
    result: AuditResult,
    details: dict[str, object],
) -> None:
    """Add and commit the entry of a call: the service is named only when the token reaches it."""
    service = call.service.service if call.service else None
    deployment = call.service.deployment if call.service else None
    await record_committed_audit(
        db,
        caller.actor,
        action=action,
        result=result,
        target_type="service",
        target_id=call.requested,
        target_name=service.name if service else None,
        project_id=deployment.project_id if deployment else None,
        deployment_id=deployment.id if deployment else None,
        service_id=service.id if service else None,
        details=details,
    )


async def _audit_refusal(
    db: AsyncSession, caller: ApiCaller, call: _Call, action: str, refusal: ApiRefusal
) -> None:
    """Record a refusal that no other entry covers; field names are kept, never their values."""
    details: dict[str, object] = {"refused": refusal.kind.value}
    if refusal.fields:
        details["fields"] = [issue.field for issue in refusal.fields]
    await _audit_call(db, caller, call, action=action, result=AuditResult.FAILURE, details=details)


async def _deploy_request(
    db: AsyncSession, service_id: int, permitted: list[int], body: DeployBody
) -> DeployRequest:
    """The engine request a body asks for, with its environments resolved inside the scope.

    An environment is a file name or, as a number, an id; a name that is not one of the files the
    token may deploy reads the same as one that does not exist. A text that is not a file name is
    tried as an id, so the keys of ``versions`` can name a file by its id.
    """
    rows = (
        await db.execute(
            select(Environment.id, Environment.name).where(Environment.id.in_(permitted))
        )
    ).all()
    names = dict(rows)
    by_name = {name: environment_id for environment_id, name in rows}

    def resolve(reference: int | str) -> int | None:
        """The id of the permitted environment a name or id points at, or None."""
        if isinstance(reference, int):
            return reference if reference in names else None
        if reference in by_name:
            return by_name[reference]
        if reference.isascii() and reference.isdigit() and int(reference) in names:
            return int(reference)
        return None

    issues: list[FieldIssue] = []
    selected: tuple[int, ...] | None = None
    if body.environments is not None:
        chosen: list[int] = []
        for reference in body.environments:
            found = resolve(reference)
            if found is None:
                issues.append(
                    FieldIssue(
                        field="environments",
                        message=f"Environment {reference!r} was not found in this service.",
                    )
                )
            elif found not in chosen:
                chosen.append(found)
        if not body.environments:
            issues.append(
                FieldIssue(
                    field="environments",
                    message="Name at least one environment, or leave the field out to deploy "
                    "every environment of the service.",
                )
            )
        selected = tuple(chosen)
    versions: dict[int, int] = {}
    for reference, number in (body.versions or {}).items():
        found = resolve(reference)
        if found is None:
            issues.append(
                FieldIssue(
                    field="versions",
                    message=f"Environment {reference!r} was not found in this service.",
                )
            )
        elif selected is not None and found not in selected:
            issues.append(
                FieldIssue(
                    field="versions",
                    message=f"Environment {reference!r} is not one of the environments deployed.",
                )
            )
        elif versions.get(found, number) != number:
            issues.append(
                FieldIssue(
                    field="versions",
                    message=f"Environment {reference!r} is given two different versions.",
                )
            )
        else:
            versions[found] = number
    if issues:
        raise ApiRefusal(RefusalKind.INVALID, INVALID_REQUEST, fields=tuple(issues))
    return DeployRequest(
        service_id=service_id,
        environment_ids=selected,
        versions=versions,
        dry_run=bool(body.dry_run),
        force=False,
    )


async def _deploy(
    db: AsyncSession,
    engine: DeployEngine,
    caller: ApiCaller,
    call: _Call,
    raw_body: bytes | None,
) -> RunResult:
    """Check the call and run the deploy; a refusal is raised for ``deploy_service`` to audit."""
    if not caller.may(OPERATION_DEPLOY):
        raise ApiRefusal(RefusalKind.FORBIDDEN, DEPLOY_NOT_ALLOWED)
    service_id, permitted = await _reachable(db, caller, call, Capability.DEPLOY, SERVICE_NOT_FOUND)
    try:
        body = parse_deploy_body(raw_body)
    except InvalidBody as invalid:
        raise ApiRefusal(
            RefusalKind.INVALID, INVALID_REQUEST, fields=tuple(invalid.issues)
        ) from None
    request = await _deploy_request(db, service_id, permitted, body)
    try:
        outcome = await engine.run(db, caller.scope, caller.actor, request)
    except DeployRequestError as error:
        raise ApiRefusal(RefusalKind.INVALID, str(error)) from None
    if outcome.refusal is DeployRefusal.LOCKED:
        raise ApiRefusal(RefusalKind.DEPLOY_LOCKED, LOCKED_MESSAGE, audited=True)
    if outcome.refusal is not None or outcome.run_id is None:
        raise ApiRefusal(RefusalKind.LOCK_UNAVAILABLE, LOCK_UNAVAILABLE_MESSAGE, audited=True)
    result = await load_run_result(db, outcome.run_id, service_id, permitted)
    if result is None:
        raise ApiRefusal(RefusalKind.INVALID, INVALID_REQUEST)
    return result


async def deploy_service(
    db: AsyncSession,
    engine: DeployEngine,
    caller: ApiCaller,
    service_reference: str,
    raw_body: bytes | None,
) -> RunResult:
    """Deploy (or dry run) a service for the token and answer the run.

    ``raw_body`` is the request body, None when it was over the size limit. Raises ``ApiRefusal``.
    """
    call = _Call(_service_id(service_reference))
    try:
        return await _deploy(db, engine, caller, call, raw_body)
    except ApiRefusal as refusal:
        if not refusal.audited:
            await _audit_refusal(db, caller, call, ACTION_DEPLOY, refusal)
        raise


async def read_last_deployment(
    db: AsyncSession, caller: ApiCaller, service_reference: str
) -> RunResult:
    """The last real run of the service, limited to the files the token reaches.

    A service the token does not reach, one that does not exist and one that was never deployed
    give the same refusal. Raises ``ApiRefusal``.
    """
    call = _Call(_service_id(service_reference))
    try:
        if not caller.may(OPERATION_READ_LAST_DEPLOYMENT):
            raise ApiRefusal(RefusalKind.FORBIDDEN, READ_NOT_ALLOWED)
        service_id, permitted = await _reachable(
            db, caller, call, Capability.VIEW, NO_DEPLOYMENT_FOUND
        )
        result = await load_last_run_result(db, service_id, permitted)
        if result is None:
            raise ApiRefusal(RefusalKind.NOT_FOUND, NO_DEPLOYMENT_FOUND)
    except ApiRefusal as refusal:
        await _audit_refusal(db, caller, call, ACTION_READ_LAST, refusal)
        raise
    await _audit_call(
        db,
        caller,
        call,
        action=ACTION_READ_LAST,
        result=AuditResult.SUCCESS,
        details={"run_id": result.run_id},
    )
    return result
