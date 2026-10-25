"""Testing a stored registry login: ``docker login`` with the credentials the app keeps.

The check opens that one login, writes it into a private Docker config folder of its own (the
same folder a post-deploy command run gets, removed when the check ends) and runs ``docker login``
for the registry. No username or password is given on the command line, so Docker takes the
stored credentials from the config folder and verifies them through the daemon; the child has no
standard input, so it can never fall back to asking for them. The token is never an argument, so
it is not visible in the process list either.

The answer is OK when Docker exits with 0, and otherwise a refusal with the output of Docker,
scrubbed of the token and of its base64 form and truncated, without the lines about prompting that
Docker adds when it has no input to ask for new credentials with. Every attempt on a stored
registry is audited as ``registry.test`` with the host and the result, never the token or the
output, also when Docker is not usable and nothing was run.
"""

import re
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import CryptoService
from app.models import AuditResult, DockerRegistry, User
from app.services.audit import AuditActor, record_actor_audit
from app.services.docker_cli import DockerCli
from app.services.hook_process import BoundedOutput, run_process
from app.services.output_scrub import OutputScrubber
from app.services.registries import (
    DOCKER_HUB_KEY,
    TARGET_TYPE,
    RegistryNotFoundError,
    RegistryTokenError,
    auth_key,
    login_secrets,
    open_registry_login,
)
from app.services.run_config import RunConfigError, private_docker_config

ACTION_TEST = "registry.test"
CHECK_TIMEOUT_SECONDS = 30
OUTPUT_PART_BYTES = 2048

OUTCOME_OK = "ok"
OUTCOME_REFUSED = "refused"
OUTCOME_TIMED_OUT = "timed_out"
OUTCOME_NOT_STARTED = "not_started"
OUTCOME_UNAVAILABLE = "unavailable"
OUTCOME_TOKEN_UNREADABLE = "token_unreadable"  # noqa: S105
OUTCOME_CONFIG_UNWRITABLE = "config_unwritable"

OK_MESSAGE = "Docker signed in to the registry with the stored login."
REFUSED_MESSAGE = "The registry did not accept the stored login."
TIMED_OUT_MESSAGE = "The check took too long and was stopped."
NOT_STARTED_MESSAGE = "The check could not be started."
UNAVAILABLE_SUFFIX = "The login cannot be tested."
DOCKER_UNAVAILABLE_REASON = "Docker is not available."

_PROMPT_NOISE = re.compile(r"interactive login|non[- ]?tty|docker logout", re.IGNORECASE)


@dataclass(frozen=True)
class LoginCheck:
    """The answer of a login test.

    ``output`` is the scrubbed and truncated output of a check that ran and did not succeed.
    ``outcome`` is the short name the audit entry keeps.
    """

    ok: bool
    message: str
    outcome: str
    output: str | None = None


def without_prompt_noise(text: str) -> str:
    """The output of a refused login without the lines that only talk about asking for input.

    When the registry refuses the stored login, Docker goes on to ask for another one, and with
    no standard input it ends with a complaint about a non-TTY device after a hint to run
    ``docker logout``. Neither says why the login failed, and the hint names a command the app
    refuses, so what is left is the registry's own reason. Blank lines go too, and the text is
    left as it is when nothing else would remain.
    """
    kept = [line for line in text.splitlines() if line.strip() and not _PROMPT_NOISE.search(line)]
    return "\n".join(kept) if kept else text


def login_arguments(host: str) -> tuple[str, ...]:
    """The arguments of ``docker login`` for a stored host.

    Docker Hub is logged in to without naming a server, which is the address Docker keeps its
    credentials under; any other registry is named. Never a username or a password.
    """
    return ("login",) if auth_key(host) == DOCKER_HUB_KEY else ("login", host)


async def _run_login(crypto: CryptoService, cli: DockerCli, registry: DockerRegistry) -> LoginCheck:
    """Run the login of one registry row, or say why it could not be run."""
    status = await cli.status(fresh=True)
    executable = cli.executable
    if not status.available or executable is None:
        reason = status.reason or DOCKER_UNAVAILABLE_REASON
        return LoginCheck(False, f"{reason} {UNAVAILABLE_SUFFIX}", OUTCOME_UNAVAILABLE)
    try:
        login = open_registry_login(crypto, registry)
    except RegistryTokenError as error:
        return LoginCheck(False, str(error), OUTCOME_TOKEN_UNREADABLE)
    scrubber = OutputScrubber(login_secrets((login,)))
    output = BoundedOutput(OUTPUT_PART_BYTES)
    try:
        with private_docker_config(cli.scratch, (login,)) as folder:
            try:
                result = await run_process(
                    [executable, *login_arguments(login.host)],
                    cwd=folder,
                    env={**cli.environment(), "DOCKER_CONFIG": str(folder)},
                    timeout=CHECK_TIMEOUT_SECONDS,
                    output=output,
                )
            except OSError:
                return LoginCheck(False, NOT_STARTED_MESSAGE, OUTCOME_NOT_STARTED)
    except RunConfigError as error:
        return LoginCheck(False, str(error), OUTCOME_CONFIG_UNWRITABLE)
    shown = output.text(scrubber).strip() or None
    if result.timed_out:
        return LoginCheck(False, TIMED_OUT_MESSAGE, OUTCOME_TIMED_OUT, shown)
    if result.exit_code != 0:
        return LoginCheck(
            False, REFUSED_MESSAGE, OUTCOME_REFUSED, without_prompt_noise(shown) if shown else None
        )
    return LoginCheck(True, OK_MESSAGE, OUTCOME_OK)


async def check_registry_login(
    db: AsyncSession,
    crypto: CryptoService,
    cli: DockerCli,
    registry_id: int,
    *,
    actor: User,
    ip_address: str | None = None,
) -> LoginCheck:
    """Test the stored login of a registry with ``docker login`` and audit the attempt.

    Needs a Docker socket, daemon and compose plugin that work right now (a fresh check, not the
    cached one); otherwise the answer is a refusal that gives the reason. Raises
    ``RegistryNotFoundError`` when the registry is gone.
    """
    registry = await db.get(DockerRegistry, registry_id)
    if registry is None:
        raise RegistryNotFoundError
    host, username = registry.host, registry.username
    check = await _run_login(crypto, cli, registry)
    await record_actor_audit(
        db,
        AuditActor.for_user(actor, ip_address),
        action=ACTION_TEST,
        result=AuditResult.SUCCESS if check.ok else AuditResult.FAILURE,
        target_type=TARGET_TYPE,
        target_id=registry_id,
        target_name=host,
        details={"host": host, "username": username, "ok": check.ok, "outcome": check.outcome},
    )
    await db.commit()
    return check
