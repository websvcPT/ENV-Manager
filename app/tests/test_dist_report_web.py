"""Dist report and deployment overview on the seeded application: states, roles and secrecy."""

import re
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, select

from app.models import (
    AllowedPath,
    AuditEntry,
    Deployment,
    DistMissingPolicy,
    Environment,
    EnvironmentVersion,
    Project,
)
from app.seeds import EDITOR_SECRET_VALUES
from app.services.notify_events import FileChange, FileChanged
from app.tests.conftest import sign_in
from app.tests.dist_helpers import link_dist, relocate_deployments, write_dist
from app.tests.notify_helpers import RecordingEvents, file_event_ids
from app.tests.web_helpers import (
    EnvironmentIds,
    assert_never_cached,
    main_of,
    post,
    without_csrf_token,
)

DIST_DEFAULT_VALUE = "dist-default-Qw83"
DB_DIST = "environments/.env.db-dist"
CHECKERS = ("root", "admin", "project_admin", "deployment_admin")
KEY_ROW = re.compile(r'<th scope="row"[^>]*><span class="font-mono text-\[13px\]">([^<]+)</span>')
OVERVIEW_ROW = re.compile(
    r'data-fragment="dist-row"><th[^>]*><a href="/environments/(\d+)/dist"[^>]*>([^<]+)</a>'
)
ROOT_OUTSIDE_TEXT = "The deployment is outside the allowed paths. Dist files are not read from it."
SECRET_TEXTS = (
    *EDITOR_SECRET_VALUES,
    DIST_DEFAULT_VALUE,
    "AQIEAgAAuTAN",
    "db.internal.example.com",
)
CERTIFICATE = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
CERTIFICATE_SKIPPED = "This file is not env format (certificate). Key comparison is skipped for it."


@pytest.fixture
async def roots(web_app: FastAPI, tmp_path: Path) -> dict[str, Path]:
    """The seeded deployments moved below a real folder that is the only allowed path."""
    async with web_app.state.session_factory() as db:
        return await relocate_deployments(db, tmp_path / "host")


@pytest.fixture
async def deployment_ids(web_app: FastAPI) -> dict[str, int]:
    """The seeded deployment ids by name."""
    async with web_app.state.session_factory() as db:
        return dict((await db.execute(select(Deployment.name, Deployment.id))).all())


async def link(
    web_app: FastAPI,
    environment_id: int,
    dist_path: str | None,
    policy: DistMissingPolicy | None = None,
) -> None:
    """Point an environment at a dist file, optionally with its own missing-keys policy."""
    async with web_app.state.session_factory() as db:
        environment = await db.get(Environment, environment_id)
        assert environment is not None
        await link_dist(db, environment, dist_path, policy=policy)


async def forget_allowed_paths(web_app: FastAPI) -> None:
    """Empty the allowed path list, as an administrator removing every entry would."""
    async with web_app.state.session_factory() as db:
        await db.execute(delete(AllowedPath))
        await db.commit()


async def entries(web_app: FastAPI, action: str) -> list[AuditEntry]:
    """The audit entries of an action, oldest first."""
    async with web_app.state.session_factory() as db:
        return list(
            (
                await db.execute(
                    select(AuditEntry).where(AuditEntry.action == action).order_by(AuditEntry.id)
                )
            ).scalars()
        )


async def version_numbers(web_app: FastAPI, environment_id: int) -> list[int]:
    """The saved version numbers of an environment."""
    async with web_app.state.session_factory() as db:
        return list(
            (
                await db.execute(
                    select(EnvironmentVersion.number)
                    .where(EnvironmentVersion.environment_id == environment_id)
                    .order_by(EnvironmentVersion.number)
                )
            ).scalars()
        )


def section_links(page: str) -> list[str]:
    """Visible text of the section links under the header."""
    nav = re.search(r'<nav aria-label="Sections".*?</nav>', page, re.DOTALL)
    assert nav is not None
    anchors = re.findall(r"<a [^>]*>(.*?)</a>", nav.group(0), re.DOTALL)
    return [re.sub(r"<[^>]+>", "", anchor).strip() for anchor in anchors]


def summary_counts(page: str) -> list[int]:
    """The three summary figures: missing, extra and empty."""
    return [int(count) for count in re.findall(r'leading-none">(\d+)</p>', page)]


@pytest.mark.parametrize("role", CHECKERS)
async def test_blocked_report_shows_key_names_lines_and_counts_never_a_value(
    web_client: httpx.AsyncClient, ids: EnvironmentIds, roots: dict[str, Path], role: str
) -> None:
    write_dist(
        roots["prod"],
        DB_DIST,
        f"# template\nDB_HOST=\nDB_PORT=\nDB_NAME={DIST_DEFAULT_VALUE}\nDB_USER=\n",
    )
    await sign_in(web_client, role)
    response = await web_client.get(f"/environments/{ids.db}/dist")
    assert response.status_code == 200
    assert_never_cached(response)
    page = response.text
    body = main_of(page)
    assert "Blocked: keys missing" in body and "Policy is block." in body
    assert 'data-status="blocked"' in body and f" by {role}" in body
    assert KEY_ROW.findall(body) == ["DB_NAME", "DB_USER"]
    assert "Line 4" in body and "Line 5" in body
    assert summary_counts(body) == [2, 0, 0]
    assert "Dist file <span" in body and DB_DIST in body
    assert "Add missing keys to the editor" in body
    assert DIST_DEFAULT_VALUE not in page and "3306" not in without_csrf_token(page)
    assert str(roots["prod"]) not in page
    access_tab = ["AccessSuperadmin"] if role == "root" else []
    assert section_links(body) == ["Edit", "History", "Compare to dist", *access_tab]
    assert f'<a href="/environments/{ids.db}/dist" aria-current="page"' in body


async def test_report_lists_extra_keys_with_the_note_and_never_a_value(
    web_client: httpx.AsyncClient, web_app: FastAPI, ids: EnvironmentIds, roots: dict[str, Path]
) -> None:
    write_dist(
        roots["prod"],
        "environments/.env.db.app-dist",
        f"MYSQL_HOST={DIST_DEFAULT_VALUE}\nMYSQL_PORT=\nBRAND_NEW=\n",
    )
    await link(web_app, ids.editor, "environments/.env.db.app-dist")
    await sign_in(web_client, "root")
    page = (await web_client.get(f"/environments/{ids.editor}/dist")).text
    body = main_of(page)
    assert KEY_ROW.findall(body) == [
        "BRAND_NEW",
        "MYSQL_PASSWORD",
        "MAX_CONNECTIONS",
        "TLS_MIN_VERSION",
        "TLS_CA_CERT",
    ]
    assert summary_counts(body) == [1, 4, 0]
    assert body.count("Not in dist. Allowed, no policy applies.") == 4
    assert not [text for text in SECRET_TEXTS if text in page]
    assert "BEGIN CERTIFICATE" not in page


async def test_warning_follows_the_policy_in_force(
    web_client: httpx.AsyncClient, web_app: FastAPI, ids: EnvironmentIds, roots: dict[str, Path]
) -> None:
    write_dist(roots["prod"], DB_DIST, "DB_HOST=\nDB_NAME=\n")
    await link(web_app, ids.db, DB_DIST, DistMissingPolicy.WARN)
    await sign_in(web_client, "root")
    body = main_of((await web_client.get(f"/environments/{ids.db}/dist")).text)
    assert "Warning: keys missing" in body
    assert "Policy is warn and continue. A deploy proceeds and reports the missing key." in body
    assert 'data-status="drift"' in body and ">Warning<" in body
    assert "Blocked: keys missing" not in body and "Add missing keys to the editor" in body


async def test_report_without_missing_keys_has_a_passed_banner_and_no_missing_table(
    web_client: httpx.AsyncClient, ids: EnvironmentIds, roots: dict[str, Path]
) -> None:
    write_dist(roots["prod"], DB_DIST, "DB_HOST=\n")
    await sign_in(web_client, "root")
    body = main_of((await web_client.get(f"/environments/{ids.db}/dist")).text)
    assert "No keys missing" in body and ">Passed<" in body and 'role="status"' in body
    assert "Add missing keys to the editor" not in body
    assert summary_counts(body) == [0, 1, 0]
    assert KEY_ROW.findall(body) == ["DB_PORT"]


async def test_skipped_states_name_their_reason_and_read_nothing(
    web_client: httpx.AsyncClient, web_app: FastAPI, ids: EnvironmentIds, roots: dict[str, Path]
) -> None:
    await sign_in(web_client, "root")
    certificate = main_of((await web_client.get(f"/environments/{ids.ca}/dist")).text)
    assert ">Skipped<" in certificate and "none linked" in certificate
    assert CERTIFICATE_SKIPPED in certificate
    assert 'role="note"' in certificate and 'aria-label="Summary"' not in certificate

    unlinked = main_of((await web_client.get(f"/environments/{ids.tuning}/dist")).text)
    assert "No dist file is linked to this file. Key comparison is skipped for it." in unlinked

    write_dist(roots["prod"], DB_DIST, CERTIFICATE)
    dist_is_text = main_of((await web_client.get(f"/environments/{ids.db}/dist")).text)
    assert "The dist file is not env format. Key comparison is skipped for it." in dist_is_text
    assert "Add missing keys to the editor" not in dist_is_text


async def test_a_missing_dist_file_is_a_failed_check_with_fixed_copy(
    web_client: httpx.AsyncClient, ids: EnvironmentIds, roots: dict[str, Path]
) -> None:
    await sign_in(web_client, "root")
    page = (await web_client.get(f"/environments/{ids.db}/dist")).text
    body = main_of(page)
    assert "The check could not run" in body and ">Check failed<" in body
    assert "The dist file does not exist or is not a regular file." in body
    assert 'aria-label="Summary"' not in body and "Add missing keys" not in body
    assert str(roots["prod"]) not in page


async def test_a_scoped_admin_never_sees_the_deployment_root_even_when_it_is_outside(
    web_client: httpx.AsyncClient, web_app: FastAPI, ids: EnvironmentIds, roots: dict[str, Path]
) -> None:
    write_dist(roots["prod"], DB_DIST, "DB_HOST=\n")
    await forget_allowed_paths(web_app)
    await sign_in(web_client, "env_scoped_admin")
    page = (await web_client.get(f"/environments/{ids.db}/dist")).text
    assert ROOT_OUTSIDE_TEXT in main_of(page)
    for hidden in (str(roots["prod"]), "/host/prod", "/opt/rentals"):
        assert hidden not in page
    (entry,) = await entries(web_app, "environment.dist_check")
    assert entry.details["problem"] == "root_outside" and entry.result.value == "failure"


async def test_allowed_paths_are_read_again_for_every_request(
    web_client: httpx.AsyncClient, web_app: FastAPI, ids: EnvironmentIds, roots: dict[str, Path]
) -> None:
    write_dist(roots["prod"], DB_DIST, "DB_HOST=\nDB_PORT=\n")
    await sign_in(web_client, "root")
    assert "No keys missing" in (await web_client.get(f"/environments/{ids.db}/dist")).text
    await forget_allowed_paths(web_app)
    after = await post(web_client, f"/environments/{ids.db}/dist-check")
    assert ROOT_OUTSIDE_TEXT in after.text and "No keys missing" not in after.text


async def test_the_report_is_a_check_and_every_run_is_audited_without_values(
    web_client: httpx.AsyncClient, web_app: FastAPI, ids: EnvironmentIds, roots: dict[str, Path]
) -> None:
    write_dist(roots["prod"], DB_DIST, f"DB_HOST=\nDB_NAME={DIST_DEFAULT_VALUE}\n")
    await sign_in(web_client, "admin")
    assert await entries(web_app, "environment.dist_check") == []
    await web_client.get(f"/environments/{ids.db}/dist")
    assert (await post(web_client, f"/environments/{ids.db}/dist-check")).status_code == 200
    first, second = await entries(web_app, "environment.dist_check")
    for entry in (first, second):
        assert entry.environment_id == ids.db and entry.actor_name == "admin"
        assert entry.details["outcome"] == "blocked" and entry.result.value == "blocked"
        assert DIST_DEFAULT_VALUE not in str(entry.details)


async def test_running_again_answers_the_report_with_the_header_as_a_partial(
    web_client: httpx.AsyncClient, ids: EnvironmentIds, roots: dict[str, Path]
) -> None:
    write_dist(roots["prod"], DB_DIST, "DB_HOST=\nDB_PORT=\n")
    await sign_in(web_client, "admin")
    response = await post(web_client, f"/environments/{ids.db}/dist-check")
    assert response.status_code == 200
    assert_never_cached(response)
    report, partial = response.text.split("<template hx", 1)
    assert report.lstrip().startswith('<div id="dist-report" data-fragment="dist-report"')
    assert "No keys missing" in report
    assert 'type="partial" hx-target="[data-fragment=page-header]" hx-swap="outerHTML"' in partial
    assert 'data-fragment="page-header"' in partial and ">Passed<" in partial


async def test_adding_the_missing_keys_saves_a_version_and_reloads_the_report(
    web_client: httpx.AsyncClient, web_app: FastAPI, ids: EnvironmentIds, roots: dict[str, Path]
) -> None:
    dist = f"DB_HOST=\nDB_PORT=\nDB_NAME={DIST_DEFAULT_VALUE}\nDB_USER=\n"
    write_dist(roots["prod"], DB_DIST, dist)
    await sign_in(web_client, "admin")
    response = await post(web_client, f"/environments/{ids.db}/add-missing?base=2")
    assert response.status_code == 200
    assert_never_cached(response)
    redirect = response.headers["hx-redirect"]
    assert redirect == f"/environments/{ids.db}/dist?added=2&version=3"
    assert await version_numbers(web_app, ids.db) == [1, 2, 3]
    (save,) = await entries(web_app, "environment.save")
    assert save.details["keys"] == ["DB_NAME", "DB_USER"] and save.actor_name == "admin"
    assert DIST_DEFAULT_VALUE not in str(save.details)

    page = (await web_client.get(redirect)).text
    body = main_of(page)
    assert "Added 2 missing keys with empty values as version 3. Pending deploy." in body
    assert "No keys missing" in body and summary_counts(body) == [0, 0, 2]
    assert body.count("Value is empty in the environment.") == 2
    assert DIST_DEFAULT_VALUE not in page


@pytest.mark.parametrize("query", ["?base=1", "?base=3", "", "?base=abc"])
async def test_adding_refuses_a_stale_report_with_a_fresh_one(
    web_client: httpx.AsyncClient,
    web_app: FastAPI,
    ids: EnvironmentIds,
    roots: dict[str, Path],
    query: str,
) -> None:
    write_dist(roots["prod"], DB_DIST, "DB_HOST=\nDB_NAME=\n")
    await sign_in(web_client, "admin")
    response = await post(web_client, f"/environments/{ids.db}/add-missing{query}")
    assert response.status_code == 422
    assert "hx-redirect" not in response.headers
    assert "This file changed since the check. The report below is up to date." in response.text
    assert 'id="dist-report"' in response.text and "<template hx" in response.text
    assert "DB_NAME" in response.text
    assert await version_numbers(web_app, ids.db) == [1, 2]
    assert await entries(web_app, "environment.save") == []


async def test_adding_refuses_when_nothing_is_missing_or_the_check_cannot_run(
    web_client: httpx.AsyncClient, web_app: FastAPI, ids: EnvironmentIds, roots: dict[str, Path]
) -> None:
    await sign_in(web_client, "admin")
    cannot_run = await post(web_client, f"/environments/{ids.db}/add-missing?base=2")
    assert cannot_run.status_code == 422
    assert "The dist file does not exist or is not a regular file." in cannot_run.text
    write_dist(roots["prod"], DB_DIST, "DB_HOST=\n")
    nothing = await post(web_client, f"/environments/{ids.db}/add-missing?base=2")
    assert nothing.status_code == 422 and "No keys are missing." in nothing.text
    assert await version_numbers(web_app, ids.db) == [1, 2]


@pytest.mark.parametrize("role", ["viewer", "env_scoped_viewer"])
async def test_viewers_get_no_report_tab_no_check_and_no_add(
    web_client: httpx.AsyncClient,
    web_app: FastAPI,
    ids: EnvironmentIds,
    roots: dict[str, Path],
    role: str,
) -> None:
    write_dist(roots["prod"], DB_DIST, "DB_HOST=\nDB_NAME=\n")
    await sign_in(web_client, role)
    editor_page = (await web_client.get(f"/environments/{ids.db}")).text
    assert "Compare to dist" not in editor_page
    assert (await web_client.get(f"/environments/{ids.db}/dist")).status_code == 403
    assert (await post(web_client, f"/environments/{ids.db}/dist-check")).status_code == 403
    assert (await post(web_client, f"/environments/{ids.db}/add-missing?base=2")).status_code == 403
    assert await entries(web_app, "environment.dist_check") == []
    assert await version_numbers(web_app, ids.db) == [1, 2]


async def test_environments_outside_the_scope_answer_404_without_a_check(
    web_client: httpx.AsyncClient, web_app: FastAPI, ids: EnvironmentIds, roots: dict[str, Path]
) -> None:
    write_dist(roots["prod"], DB_DIST, "DB_HOST=\nDB_NAME=\n")
    for role, environment in (("ungranted_admin", ids.db), ("env_scoped_admin", ids.editor)):
        await sign_in(web_client, role)
        assert (await web_client.get(f"/environments/{environment}/dist")).status_code == 404
        check = await post(web_client, f"/environments/{environment}/dist-check")
        assert check.status_code == 404
        add = await post(web_client, f"/environments/{environment}/add-missing?base=2")
        assert add.status_code == 404
    assert (await web_client.get("/environments/999999/dist")).status_code == 404
    assert await entries(web_app, "environment.dist_check") == []


async def test_a_scoped_admin_checks_and_adds_to_the_granted_file(
    web_client: httpx.AsyncClient, web_app: FastAPI, ids: EnvironmentIds, roots: dict[str, Path]
) -> None:
    write_dist(roots["prod"], DB_DIST, "DB_HOST=\nDB_NAME=\n")
    await sign_in(web_client, "env_scoped_admin")
    page = (await web_client.get(f"/environments/{ids.db}/dist")).text
    assert "Blocked: keys missing" in page and "Add missing keys to the editor" in page
    added = await post(web_client, f"/environments/{ids.db}/add-missing?base=2")
    assert added.status_code == 200 and "hx-redirect" in added.headers
    assert await version_numbers(web_app, ids.db) == [1, 2, 3]


async def test_no_response_of_the_dist_screens_holds_a_value(
    web_client: httpx.AsyncClient, web_app: FastAPI, ids: EnvironmentIds, roots: dict[str, Path]
) -> None:
    write_dist(
        roots["prod"],
        "environments/.env.db.app-dist",
        f"MYSQL_HOST={DIST_DEFAULT_VALUE}\nMYSQL_PASSWORD={DIST_DEFAULT_VALUE}\nBRAND_NEW=\n",
    )
    await link(web_app, ids.editor, "environments/.env.db.app-dist")
    await sign_in(web_client, "root")
    base = f"/environments/{ids.editor}"
    responses = [
        await web_client.get(f"{base}/dist"),
        await web_client.get(f"{base}/dist", headers={"HX-Request": "true"}),
        await post(web_client, f"{base}/dist-check"),
        await post(web_client, f"{base}/add-missing?base=1"),
        await post(web_client, f"{base}/add-missing?base=3"),
        await web_client.get(f"{base}/dist?added=1&version=4"),
        await web_client.get(f"{base}/history"),
    ]
    for response in responses:
        assert not [text for text in SECRET_TEXTS if text in response.text], response.url
        assert "BEGIN CERTIFICATE" not in response.text, response.url
    for entry in await entries(web_app, "environment.dist_check"):
        assert not [text for text in SECRET_TEXTS if text in str(entry.details)]


async def test_deployment_overview_checks_every_file_in_scope_and_audits_each(
    web_client: httpx.AsyncClient,
    web_app: FastAPI,
    ids: EnvironmentIds,
    deployment_ids: dict[str, int],
    roots: dict[str, Path],
) -> None:
    write_dist(roots["prod"], DB_DIST, f"DB_HOST=\nDB_NAME={DIST_DEFAULT_VALUE}\n")
    await sign_in(web_client, "admin")
    response = await web_client.get(f"/deployments/{deployment_ids['prod']}/dist")
    assert response.status_code == 200
    assert_never_cached(response)
    body = main_of(response.text)
    assert [name for _id, name in OVERVIEW_ROW.findall(body)] == [
        ".env.db",
        ".env.db.app",
        ".env.db.secrets",
        ".env.web",
    ]
    rows = body.split('data-fragment="dist-row"')[1:]
    assert 'data-status="blocked"' in rows[0] and ">Blocked<" in rows[0]
    assert ">Skipped<" in rows[1] and "None linked" in rows[1]
    assert ">Check failed<" in rows[3]
    assert body.count('data-fragment="dist-item"') == 4
    assert f'href="/environments/{ids.db}/dist"' in rows[0] and "Open report" in rows[0]
    assert "Dist check" in body and "Run check again" in body
    assert DIST_DEFAULT_VALUE not in response.text and str(roots["prod"]) not in response.text
    assert len(await entries(web_app, "environment.dist_check")) == 4


async def test_deployment_overview_names_only_the_files_in_a_scoped_callers_scope(
    web_client: httpx.AsyncClient,
    web_app: FastAPI,
    ids: EnvironmentIds,
    deployment_ids: dict[str, int],
    roots: dict[str, Path],
) -> None:
    write_dist(roots["prod"], DB_DIST, "DB_HOST=\nDB_NAME=\n")
    await sign_in(web_client, "env_scoped_admin")
    response = await web_client.get(f"/deployments/{deployment_ids['prod']}/dist")
    assert response.status_code == 200
    body = main_of(response.text)
    assert [name for _id, name in OVERVIEW_ROW.findall(body)] == [".env.db"]
    for hidden in (".env.db.secrets", ".env.db.app", ".env.web", str(roots["prod"])):
        assert hidden not in response.text
    assert len(await entries(web_app, "environment.dist_check")) == 1


async def test_deployment_overview_refuses_viewers_and_hides_other_deployments(
    web_client: httpx.AsyncClient,
    web_app: FastAPI,
    deployment_ids: dict[str, int],
    roots: dict[str, Path],
) -> None:
    await sign_in(web_client, "viewer")
    assert (await web_client.get(f"/deployments/{deployment_ids['prod']}/dist")).status_code == 403
    await sign_in(web_client, "ungranted_admin")
    assert (await web_client.get(f"/deployments/{deployment_ids['prod']}/dist")).status_code == 404
    assert (await web_client.get("/deployments/999999/dist")).status_code == 404
    assert await entries(web_app, "environment.dist_check") == []


async def test_deployment_overview_runs_nothing_outside_the_allowed_paths(
    web_client: httpx.AsyncClient,
    web_app: FastAPI,
    deployment_ids: dict[str, int],
    roots: dict[str, Path],
) -> None:
    await forget_allowed_paths(web_app)
    await sign_in(web_client, "root")
    response = await web_client.get(f"/deployments/{deployment_ids['prod']}/dist")
    assert response.status_code == 200
    body = main_of(response.text)
    assert "This deployment is outside the allowed paths." in body
    assert 'data-fragment="dist-row"' not in body and "Run check again" not in body
    assert await entries(web_app, "environment.dist_check") == []


async def test_project_rows_link_the_check_for_checkers_with_files_inside_the_allowed_paths(
    web_client: httpx.AsyncClient,
    web_app: FastAPI,
    deployment_ids: dict[str, int],
    roots: dict[str, Path],
) -> None:
    async with web_app.state.session_factory() as db:
        query = select(Project.id).where(Project.name == "Fancy Proj. Name")
        fancy = (await db.execute(query)).scalar_one()
    link_prod = f'href="/deployments/{deployment_ids["prod"]}/dist"'
    link_staging = f'href="/deployments/{deployment_ids["staging"]}/dist"'
    await sign_in(web_client, "admin")
    page = (await web_client.get(f"/projects/{fancy}")).text
    assert link_prod in page and link_staging in page
    await sign_in(web_client, "viewer")
    assert link_prod not in (await web_client.get(f"/projects/{fancy}")).text
    await forget_allowed_paths(web_app)
    await sign_in(web_client, "admin")
    outside = (await web_client.get(f"/projects/{fancy}")).text
    assert link_prod not in outside and link_staging not in outside


async def test_deployment_overview_says_so_when_there_is_no_file_to_check(
    web_client: httpx.AsyncClient,
    web_app: FastAPI,
    deployment_ids: dict[str, int],
    roots: dict[str, Path],
) -> None:
    async with web_app.state.session_factory() as db:
        legacy = await db.get(Deployment, deployment_ids["legacy"])
        assert legacy is not None
        legacy.root_path = str(roots["main"])
        await db.commit()
    await sign_in(web_client, "root")
    response = await web_client.get(f"/deployments/{deployment_ids['legacy']}/dist")
    assert response.status_code == 200
    body = main_of(response.text)
    assert "No files to check" in body and 'data-fragment="dist-row"' not in body
    assert "outside the allowed paths" not in body
    assert await entries(web_app, "environment.dist_check") == []


async def test_adding_the_missing_keys_announces_the_file_change_to_the_notifier(
    web_client: httpx.AsyncClient, web_app: FastAPI, ids: EnvironmentIds, roots: dict[str, Path]
) -> None:
    """The keys go through the editor save, which tells the notifier after its commit."""
    write_dist(roots["prod"], DB_DIST, "DB_HOST=\nDB_PORT=\nDB_NAME=x\nDB_USER=\n")
    recorder = RecordingEvents()
    web_app.state.notifier = recorder
    await sign_in(web_client, "admin")
    response = await post(web_client, f"/environments/{ids.db}/add-missing?base=2")
    assert response.status_code == 200
    project, deployment, service, name = await file_event_ids(web_app, ids.db)
    assert recorder.events == [
        FileChanged(project, deployment, service, ids.db, name, 3, FileChange.SAVED, "admin")
    ]
