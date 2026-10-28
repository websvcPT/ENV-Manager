"""The values a post-deploy command must not print: those of every file of the deployment.

A post-deploy command runs in a Compose project, usually the deployment root, and that project
can load the files of other services of the same deployment (``docker compose config`` prints
all of them). So the scrubber of a deploy holds the values of the newest saved version of every
environment of the deployment, including environments the deploying user cannot see, plus the
versions the deploy wrote (which can be older than the newest). Container output can hold a value
a container started with before it changed, so it asks for the previous version as well.
Decrypted text stays in memory for this call only; the scrubber keeps the values, never their
names or the file text.

A file in env format contributes the value of each variable; any other text file contributes its
whole text. A certificate contributes the base64 lines of each of its PEM blocks (the BEGIN and
END lines are not secret); a certificate without a PEM block, which a file whose type changed can
hold, falls back to its whole text. A version that cannot be decrypted is skipped and logged
without any detail.
"""

from collections.abc import Iterable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import CryptoError, CryptoService
from app.core.log import get_logger
from app.models import Environment, EnvironmentVersion, FileType, Service
from app.services.env_parser import parse_env
from app.services.file_types import pem_body_lines
from app.services.output_scrub import OutputScrubber

log = get_logger("scrub_values")

CONTAINER_SCRUB_DEPTH = 2


def secret_texts(text: str, file_type: FileType = FileType.ENV) -> list[str]:
    """The strings of one file that count as values: each variable value, or the whole text.

    A certificate gives the base64 body of each PEM block, whose lines the scrubber also removes
    one by one.
    """
    if file_type is FileType.CERT:
        bodies = pem_body_lines(text)
        return bodies or [text.strip()]
    parsed = parse_env(text)
    if parsed.is_env_format:
        return [
            variable.value
            for variable in parsed.variables.values()
            if variable.assigned and not variable.interpolated
        ]
    return [text.strip()]


async def _newest_versions(
    db: AsyncSession, deployment_id: int, depth: int = 1
) -> list[tuple[EnvironmentVersion, FileType]]:
    """The ``depth`` newest saved versions of every environment of the deployment, each with the
    file type of its environment."""
    environment_ids = (
        select(Environment.id)
        .join(Service, Service.id == Environment.service_id)
        .where(Service.deployment_id == deployment_id)
    )
    ranked = (
        select(
            EnvironmentVersion.id.label("id"),
            func.row_number()
            .over(
                partition_by=EnvironmentVersion.environment_id,
                order_by=EnvironmentVersion.number.desc(),
            )
            .label("age"),
        )
        .where(EnvironmentVersion.environment_id.in_(environment_ids))
        .subquery()
    )
    rows = await db.execute(
        select(EnvironmentVersion, Environment.file_type)
        .join(Environment, Environment.id == EnvironmentVersion.environment_id)
        .join(ranked, ranked.c.id == EnvironmentVersion.id)
        .where(ranked.c.age <= depth)
    )
    return [(version, file_type) for version, file_type in rows.all()]


async def load_scrubber(
    db: AsyncSession,
    crypto: CryptoService,
    deployment_id: int,
    written: Iterable[EnvironmentVersion | None] = (),
    *,
    opened: dict[int, str] | None = None,
    extra: Iterable[str] = (),
    depth: int = 1,
) -> OutputScrubber:
    """The scrubber for a deployment: current versions of all its files plus ``written``.

    ``depth`` is how many of the newest versions of each environment count as current. A deploy
    and its commands use 1. Output that a container produced (its logs, the answer of an action)
    uses 2, so a container that was not recreated after a value changed does not show the old
    value in clear; a value from three versions back or older is not masked.

    ``opened`` maps version ids to text the caller's operation already decrypted: those versions
    are not decrypted again, and each one decrypted here is added to it. ``extra`` are further
    values to remove, such as the registry logins of the run.
    """
    newest = await _newest_versions(db, deployment_id, depth)
    versions = {version.id: version for version, _file_type in newest}
    types = {version.environment_id: file_type for version, file_type in newest}
    for version in written:
        if version is not None:
            versions.setdefault(version.id, version)
    values: list[str] = list(extra)
    for version in versions.values():
        text = opened.get(version.id) if opened is not None else None
        if text is None:
            try:
                text = crypto.decrypt_text(version.content_encrypted)
            except CryptoError:
                log.warning("a saved version could not be read for output scrubbing")
                continue
            if opened is not None:
                opened[version.id] = text
        values.extend(secret_texts(text, types.get(version.environment_id, FileType.ENV)))
    return OutputScrubber(values)
