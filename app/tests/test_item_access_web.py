"""The item access dependencies of the web routes: sign-in page, then 404, then 403.

One matrix over the four kinds of item (project, deployment, service, environment): a page with
no capability requirement and a route that needs one a viewer lacks. A hidden item answers 404
even where the route needs a capability the caller lacks, so its existence is never revealed.
"""

from collections.abc import Callable
from dataclasses import dataclass

import httpx
import pytest

from app.seeds import SeedData
from app.tests.conftest import sign_in
from app.tests.template_databases import SeededTemplate

MISSING_ID = 999_999


@dataclass(frozen=True)
class Kind:
    """One kind of item: its routes and the seeded ids inside and outside the viewer's scope."""

    name: str
    page: str
    guarded: str
    inside: Callable[[SeedData], int]
    outside: Callable[[SeedData], int]


@pytest.fixture
def seed(seeded_template: SeededTemplate) -> SeedData:
    """The seeded structure; its ids are those of the web application's database."""
    return seeded_template.data


KINDS = [
    Kind(
        "project",
        "/projects/{id}",
        "/projects/{id}/deployments/new",
        lambda seed: seed.projects.fancy.id,
        lambda seed: seed.projects.billing.id,
    ),
    Kind(
        "deployment",
        "/deployments/{id}",
        "/deployments/{id}/edit",
        lambda seed: seed.deployments.fancy_prod.id,
        lambda seed: seed.deployments.billing_main.id,
    ),
    Kind(
        "service",
        "/services/{id}",
        "/services/{id}/environments/new",
        lambda seed: seed.services.mysql.id,
        lambda seed: seed.services.api.id,
    ),
    Kind(
        "environment",
        "/environments/{id}",
        "/environments/{id}/new-row",
        lambda seed: seed.envs.db.id,
        lambda seed: seed.envs.api.id,
    ),
]


@pytest.mark.parametrize("kind", KINDS, ids=lambda kind: kind.name)
async def test_a_signed_out_request_is_refused_before_anything_is_loaded(
    web_client: httpx.AsyncClient, seed: SeedData, kind: Kind
) -> None:
    for item in (kind.inside(seed), MISSING_ID):
        for url in (kind.page, kind.guarded):
            response = await web_client.get(url.format(id=item))
            assert (response.status_code, response.headers["location"]) == (303, "/login")


@pytest.mark.parametrize("kind", KINDS, ids=lambda kind: kind.name)
async def test_a_viewer_reads_the_item_in_scope_and_cannot_use_the_guarded_route(
    web_client: httpx.AsyncClient, seed: SeedData, kind: Kind
) -> None:
    await sign_in(web_client, "viewer")
    inside = kind.inside(seed)
    assert (await web_client.get(kind.page.format(id=inside))).status_code == 200
    assert (await web_client.get(kind.guarded.format(id=inside))).status_code == 403


@pytest.mark.parametrize("kind", KINDS, ids=lambda kind: kind.name)
async def test_an_item_outside_the_scope_or_missing_is_a_404_whatever_the_capability(
    web_client: httpx.AsyncClient, seed: SeedData, kind: Kind
) -> None:
    await sign_in(web_client, "viewer")
    for item in (kind.outside(seed), MISSING_ID):
        assert (await web_client.get(kind.page.format(id=item))).status_code == 404
        assert (await web_client.get(kind.guarded.format(id=item))).status_code == 404


@pytest.mark.parametrize("kind", KINDS, ids=lambda kind: kind.name)
async def test_a_user_without_grants_sees_nothing_and_the_superadmin_everything(
    web_client: httpx.AsyncClient, seed: SeedData, kind: Kind
) -> None:
    await sign_in(web_client, "ungranted_admin")
    for item in (kind.inside(seed), kind.outside(seed)):
        assert (await web_client.get(kind.page.format(id=item))).status_code == 404
        assert (await web_client.get(kind.guarded.format(id=item))).status_code == 404

    await sign_in(web_client, "root")
    for item in (kind.inside(seed), kind.outside(seed)):
        assert (await web_client.get(kind.page.format(id=item))).status_code == 200
        assert (await web_client.get(kind.guarded.format(id=item))).status_code == 200
    assert (await web_client.get(kind.page.format(id=MISSING_ID))).status_code == 404
    assert (await web_client.get(kind.guarded.format(id=MISSING_ID))).status_code == 404
