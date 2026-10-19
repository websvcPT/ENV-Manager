"""The API against the running application and the demo stack: real HTTP, files and containers.

A token restricted to the demo project is created in the database the running application uses
(the app container's own data folder), and the demo ``api`` service is deployed with plain HTTP
requests to the application's own port, the way a CI job would. Every check is on the answers,
on the real files (mode, content, what was not touched), on the demo containers and on the audit
log. The token is revoked at the end, whatever happens.
"""

import asyncio
import json
import secrets
import stat
import time
from collections import Counter
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

import httpx
import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.crypto import CryptoService, build_crypto_service
from app.core.db import create_engine, create_session_factory
from app.models import (
    ApiToken,
    AuditEntry,
    Deployment,
    Environment,
    EnvironmentVersion,
    LoginCounter,
    LoginCounterKind,
    Project,
    Service,
    User,
)
from app.real_scenarios.conftest import DemoStack, Workloads
from app.real_scenarios.helpers import snapshot
from app.seeds.demo import DEMO_DEPLOYMENT_NAME, DEMO_PROJECT_NAME
from app.services.audit import AuditActor
from app.services.permissions import load_user_scope
from app.services.tokens import (
    OPERATION_DEPLOY,
    OPERATION_READ_LAST_DEPLOYMENT,
    create_token,
    revoke_token,
)
from app.tests.deploy_helpers import version_text

OWNER = "project_admin"
CA = "demo-ca.pem"
API_ENVIRONMENT = ".env.api"
LOCAL_ADDRESS = "127.0.0.1"
RUN_TIMEOUT_SECONDS = 240.0
WAIT_SECONDS = 90.0


@dataclass(frozen=True)
class Live:
    """A client of the running application with a token for the demo project."""

    http: httpx.AsyncClient
    secret: str
    token_name: str
    owner_id: int
    api: int
    outside: int
    session: Callable[[], AsyncSession]
    crypto: CryptoService

    @property
    def headers(self) -> dict[str, str]:
        """The header that carries the token."""
        return {"Authorization": f"Bearer {self.secret}"}

    async def managed(self, name: str) -> str:
        """The newest managed text of an environment of the demo api service."""
        async with self.session() as db:
            environment_id = await db.scalar(
                select(Environment.id).where(
                    Environment.service_id == self.api, Environment.name == name
                )
            )
            assert environment_id is not None
            return await version_text(db, self.crypto, environment_id)

    async def newest_version(self, name: str) -> int:
        """The newest version number of an environment of the demo api service."""
        async with self.session() as db:
            number = await db.scalar(
                select(func.max(EnvironmentVersion.number))
                .join(Environment, Environment.id == EnvironmentVersion.environment_id)
                .where(Environment.service_id == self.api, Environment.name == name)
            )
        assert number is not None
        return number

    def deploy_url(self) -> str:
        """The deploy endpoint of the demo api service."""
        return f"/api/v1/services/{self.api}/deploy"

    def last_url(self, service: int | None = None) -> str:
        """The last deployment endpoint of the demo api service, or of another one."""
        return f"/api/v1/services/{service or self.api}/deployments/last"


@pytest.fixture
async def live(demo: DemoStack) -> AsyncIterator[Live]:
    """A token restricted to the demo project, made in the running application's own database."""
    settings = get_settings()
    engine = create_engine(settings.database_path)
    factory = create_session_factory(engine)
    crypto = build_crypto_service(settings.key, settings.key_file)
    name = f"real-scenario-{secrets.token_hex(4)}"
    token_id: int | None = None
    try:
        async with factory() as db:
            owner = await db.scalar(select(User).where(User.name == OWNER))
            project = await db.scalar(select(Project).where(Project.name == DEMO_PROJECT_NAME))
            services = select(Service.id).join(Deployment, Deployment.id == Service.deployment_id)
            services = services.join(Project, Project.id == Deployment.project_id)
            api = await db.scalar(
                services.where(
                    Project.name == DEMO_PROJECT_NAME,
                    Deployment.name == DEMO_DEPLOYMENT_NAME,
                    Service.name == "api",
                )
            )
            outside = await db.scalar(services.where(Project.name != DEMO_PROJECT_NAME).limit(1))
            if owner is None or project is None or api is None or outside is None:
                pytest.fail("the demo project is not seeded in the running app: use make e2e-demo")
            created = await create_token(
                db,
                owner,
                await load_user_scope(db, owner),
                name=name,
                operations=(OPERATION_DEPLOY, OPERATION_READ_LAST_DEPLOYMENT),
                restriction=f"project:{project.id}",
                expires="",
                actor=AuditActor.for_user(owner),
            )
            token_id = created.token.id
            owner_id = owner.id
        base_url = f"http://{LOCAL_ADDRESS}:{settings.port}"
        async with httpx.AsyncClient(
            base_url=base_url, timeout=httpx.Timeout(RUN_TIMEOUT_SECONDS)
        ) as http:
            assert (await http.get("/healthz")).status_code == 200
            yield Live(
                http,
                created.secret,
                name,
                owner_id,
                api,
                outside,
                factory,
                crypto,
            )
    finally:
        async with factory() as db:
            if token_id is not None:
                token = await db.get(ApiToken, token_id)
                if token is not None:
                    await revoke_token(db, token, AuditActor.cli())
            await db.execute(
                delete(LoginCounter).where(
                    LoginCounter.kind == LoginCounterKind.API_IP,
                    LoginCounter.subject == LOCAL_ADDRESS,
                )
            )
            await db.commit()
        await engine.dispose()


def environments_of(answer: dict[str, object]) -> dict[str, dict[str, object]]:
    """The environment results of a run answer by file name."""
    items = answer["environments"]
    assert isinstance(items, list)
    return {item["name"]: item for item in items}


async def last_run_id(live: Live) -> int:
    """The id of the run the last deployment endpoint reports now, 0 while there is none."""
    read = await live.http.get(live.last_url(), headers=live.headers)
    return int(read.json()["run_id"]) if read.status_code == 200 else 0


async def test_a_token_restricted_to_the_demo_project_deploys_api_over_http(
    demo: DemoStack, live: Live, workloads: Workloads
) -> None:
    http, headers = live.http, live.headers
    baseline = snapshot(demo.targets)
    drifted = (demo.targets / API_ENVIRONMENT).read_bytes()
    texts: list[str] = []

    # What the token may not do or reach is refused before anything happens.
    no_token = await http.post(live.deploy_url(), json={})
    assert no_token.status_code == 401 and no_token.headers["www-authenticate"] == "Bearer"
    outside = await http.get(live.last_url(live.outside), headers=headers)
    assert outside.status_code == 404
    forced = await http.post(live.deploy_url(), json={"force": True}, headers=headers)
    assert forced.status_code == 422 and "force is not accepted" in forced.text
    assert snapshot(demo.targets) == baseline

    # A dry run reports what would be written and writes nothing.
    dry = await http.post(
        live.deploy_url(), json={"environments": [CA], "dry_run": True}, headers=headers
    )
    assert dry.status_code == 200, dry.text
    texts.append(dry.text)
    assert dry.json()["dry_run"] is True
    assert environments_of(dry.json())[CA]["status"] == "would_write"
    assert snapshot(demo.targets) == baseline and workloads.container_ids() == []

    # The certificate is created and the post-deploy command recreates the workload once.
    before = time.time()
    deployed = await http.post(live.deploy_url(), json={"environments": [CA]}, headers=headers)
    assert deployed.status_code == 200, deployed.text
    texts.append(deployed.text)
    answer = deployed.json()
    assert answer["status"] == "success" and answer["dry_run"] is False
    assert answer["post_deploy"] == {"status": "ran", "exit_code": 0, "reason": None}
    created = environments_of(answer)[CA]
    assert (created["status"], created["version"]) == ("written", await live.newest_version(CA))
    assert list(environments_of(answer)) == [CA]
    target = demo.targets / CA
    assert target.read_text(encoding="utf-8") == await live.managed(CA)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert (demo.targets / API_ENVIRONMENT).read_bytes() == drifted
    assert workloads.created_since("api", before) == 1
    workloads.wait_for_names("api", {"DEMO_API_MODE", "DEMO_API_PORT", "DEMO_API_TOKEN"})

    # The whole service: the drifted file halts and stays as it is, the command does not run.
    again_before = time.time()
    whole = await http.post(live.deploy_url(), json={}, headers=headers)
    assert whole.status_code == 200, whole.text
    texts.append(whole.text)
    halted = whole.json()
    assert halted["status"] == "halted"
    files = environments_of(halted)
    assert (files[API_ENVIRONMENT]["status"], files[API_ENVIRONMENT]["drift"]) == (
        "halted",
        "drift",
    )
    assert files[CA]["status"] == "unchanged"
    assert halted["post_deploy"]["status"] == "skipped"
    assert (demo.targets / API_ENVIRONMENT).read_bytes() == drifted
    assert workloads.created_since("api", again_before) == 0

    # The last result is that run, in the same shape.
    read = await http.get(live.last_url(), headers=headers)
    assert read.status_code == 200
    texts.append(read.text)
    assert read.json() == halted

    # No managed value, output or command text reached any answer.
    settings_text = await live.managed(API_ENVIRONMENT)
    certificate_body = [
        line
        for line in (await live.managed(CA)).splitlines()
        if line and " " not in line and not line.startswith("-----")
    ]
    values = [
        line.partition("=")[2].strip()
        for line in settings_text.splitlines()
        if "=" in line and not line.startswith("#")
    ]
    distinctive = [value for value in values if len(value) >= 8] + certificate_body
    assert distinctive
    for text in texts:
        assert not [value for value in distinctive if value in text]
        assert "output" not in text and "compose" not in text and str(demo.root) not in text

    # Everything was audited with the token and its owner, and the token recorded its use.
    async with live.session() as db:
        token = await db.scalar(select(ApiToken).where(ApiToken.name == live.token_name))
        assert token is not None and token.last_used_ip == LOCAL_ADDRESS
        entries = (
            (await db.execute(select(AuditEntry).where(AuditEntry.actor_token_id == token.id)))
            .scalars()
            .all()
        )
    assert {entry.actor_user_id for entry in entries} == {live.owner_id}
    assert {entry.ip_address for entry in entries} == {LOCAL_ADDRESS}
    actions = Counter(entry.action for entry in entries if entry.action != "environment.dist_check")
    assert actions == {
        "service.deploy_dry_run": 1,
        "service.deploy": 3,
        "service.last_deploy_read": 2,
    }
    assert live.secret not in json.dumps([entry.details for entry in entries])


async def test_a_client_that_gives_up_does_not_stop_the_deploy(
    demo: DemoStack, live: Live, workloads: Workloads
) -> None:
    """The request stays open until the post-deploy command ends; leaving early stops nothing."""
    http, headers = live.http, live.headers
    earlier_run = await last_run_id(live)
    before = time.time()
    with pytest.raises(httpx.ReadTimeout):
        await http.post(
            live.deploy_url(),
            json={"environments": [CA]},
            headers=headers,
            timeout=httpx.Timeout(connect=10.0, read=0.05, write=10.0, pool=10.0),
        )
    deadline = time.monotonic() + WAIT_SECONDS
    answer: dict[str, object] = {}
    while time.monotonic() < deadline:
        read = await http.get(live.last_url(), headers=headers)
        if read.status_code == 200:
            candidate = read.json()
            run_id, post_deploy = candidate["run_id"], candidate["post_deploy"]
            assert isinstance(run_id, int) and isinstance(post_deploy, dict)
            finished = post_deploy["status"] != "running" and environments_of(candidate).get(CA)
            if run_id > earlier_run and finished:
                answer = candidate
                break
        await asyncio.sleep(0.5)
    assert answer, "the deploy never finished"
    assert answer["status"] == "success"
    assert answer["post_deploy"] == {"status": "ran", "exit_code": 0, "reason": None}
    assert answer["finished_at"] is not None
    assert (demo.targets / CA).read_text(encoding="utf-8") == await live.managed(CA)
    assert workloads.created_since("api", before) == 1
