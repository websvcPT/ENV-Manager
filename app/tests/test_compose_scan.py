"""Finding Compose files and mapping a scan to service and file candidates."""

import os
from pathlib import Path

import pytest

from app.models import DEPLOYMENT_FILES_NAME, FileType
from app.services import compose_scan
from app.services.compose_config import ComposeScanError
from app.services.compose_scan import (
    CERT_EMPTY_REASON,
    FILE_NAME_REASON,
    FOLDER_REASON,
    MISSING_BIND_REASON,
    NO_NAME_REASON,
    OUTSIDE_REASON,
    ComposeFileOffer,
    ComposeScan,
    FileCandidate,
    FileDefaults,
    FileSource,
    assign_names,
    clean_service_name,
    compose_command,
    default_file_type,
    discover_compose_files,
    file_attributes,
    file_defaults,
    find_compose_files,
    looks_like_file,
    resolve_compose_files,
    scan_compose,
)
from app.services.content import MAX_CONTENT_BYTES
from app.services.hook_commands import parse_commands
from app.services.paths import AllowedPaths
from app.tests.compose_helpers import GEOTRAVEL, PEM, add_stack, real_cli, write_project
from app.tests.deploy_helpers import Harness


def candidate(scan: ComposeScan, key: str) -> FileCandidate:
    """The file candidate with this key."""
    return next(item for item in scan.files if item.key == key)


def test_compose_files_are_offered_from_the_root_and_one_folder_below(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    write_project(
        root,
        {
            "compose.yaml": "",
            "docker-compose.yml": "",
            "compose.override.yaml": "",
            "web/compose.yaml": "",
            "web/docker-compose.override.yml": "",
            "web/readme.txt": "",
            "deep/inner/compose.yaml": "",
            ".hidden/compose.yaml": "",
            "other/notes.txt": "",
        },
    )
    (root / "linked.yaml").write_text("", encoding="utf-8")
    (root / "compose.yml").symlink_to(root / "linked.yaml")
    offers = discover_compose_files(AllowedPaths([root]), str(root))
    assert offers == (
        ComposeFileOffer("compose.yaml", "", False),
        ComposeFileOffer("docker-compose.yml", "", False),
        ComposeFileOffer("compose.override.yaml", "", True),
        ComposeFileOffer("web/compose.yaml", "web", False),
        ComposeFileOffer("web/docker-compose.override.yml", "web", True),
    )


def test_a_root_outside_the_allowed_paths_offers_nothing(tmp_path: Path) -> None:
    root = (tmp_path / "root").resolve()
    root.mkdir()
    other = (tmp_path / "other").resolve()
    other.mkdir()
    with pytest.raises(ComposeScanError):
        discover_compose_files(AllowedPaths([other]), str(root))


async def test_the_offers_of_a_deployment_are_read_with_the_allowed_paths_of_now(
    harness: Harness,
) -> None:
    deployment, _ = await add_stack(harness)
    offers = await find_compose_files(harness.db, deployment)
    assert [offer.path for offer in offers] == ["compose.yaml"]


def test_the_chosen_files_are_resolved_below_the_root_with_the_base_first(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    write_project(root, {"web/compose.yaml": "a: 1", "web/compose.override.yaml": "b: 2"})
    resolved = resolve_compose_files(
        AllowedPaths([root]), str(root), [" web/compose.yaml ", "web/compose.override.yaml"]
    )
    assert resolved.relative == ("web/compose.yaml", "web/compose.override.yaml")
    assert resolved.project_directory == root / "web"
    assert resolved.paths[0] == root / "web/compose.yaml"


@pytest.mark.parametrize(
    "paths",
    [
        [],
        ["../outside.yaml"],
        ["/etc/hostname"],
        ["folder"],
        ["missing.yaml"],
        ["compose.yaml", "./compose.yaml"],
        ["link.yaml"],
        ["big.yaml"],
        ["compose.yaml"] * 9,
    ],
)
def test_a_path_that_is_not_a_usable_compose_file_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, paths: list[str]
) -> None:
    root = (tmp_path / "root").resolve()
    outside = tmp_path / "outside.yaml"
    outside.write_text("services: {}", encoding="utf-8")
    write_project(root, {"compose.yaml": "services: {}", "big.yaml": "x" * 50})
    (root / "folder").mkdir()
    (root / "link.yaml").symlink_to(outside)
    monkeypatch.setattr(compose_scan, "MAX_COMPOSE_FILE_BYTES", 10)
    with pytest.raises(ComposeScanError) as caught:
        resolve_compose_files(AllowedPaths([tmp_path.resolve()]), str(root), paths)
    assert str(tmp_path) not in str(caught.value)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("web", "web"),
        ("Web_1", "web-1"),
        ("my.service", "my-service"),
        ("-edge--case_", "edge-case"),
        ("a" * 120, "a" * 100),
        ("___", None),
        ("", None),
    ],
)
def test_a_compose_name_becomes_a_slug_or_nothing(raw: str, expected: str | None) -> None:
    assert clean_service_name(raw) == expected


def test_a_file_name_is_unique_for_its_owner_by_a_folder_prefix() -> None:
    names = assign_names(
        [
            ("app.env", "app.env", "web"),
            ("conf/app.env", "app.env", "web"),
            ("other/app.env", "app.env", "api"),
            ("a/b/app.env", "app.env", "web"),
            ("bad name.env", "bad name.env", "web"),
        ]
    )
    assert names == {
        "app.env": "app.env",
        "conf/app.env": "conf-app.env",
        "other/app.env": "app.env",
        "a/b/app.env": "b-app.env",
        "bad name.env": None,
    }
    taken_twice = assign_names(
        [
            ("x/app.env", "app.env", "web"),
            ("y/x/app.env", "app.env", "web"),
            ("x/app.env2", "app.env", "web"),
        ]
    )
    assert taken_twice["x/app.env"] == "app.env"
    assert taken_twice["y/x/app.env"] == "x-app.env"
    assert taken_twice["x/app.env2"] is None


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("server.pem", FileType.CERT),
        ("CA.CRT", FileType.CERT),
        ("tls.cer", FileType.CERT),
        ("server.key", FileType.CERT),
        (".env", FileType.ENV),
        ("app.cfg", FileType.ENV),
        ("key.txt", FileType.ENV),
    ],
)
def test_the_type_follows_the_ending_of_the_name(name: str, expected: FileType) -> None:
    assert default_file_type(name) is expected


def test_attributes_equal_to_the_deployment_defaults_are_not_stored() -> None:
    defaults = FileDefaults(0o600, 1000, 1000)
    assert file_attributes(0o600, 1000, 1000, defaults) == (None, None, None)
    assert file_attributes(0o640, 0, 1000, defaults) == ("0640", "root", None)
    assert file_attributes(0o644, 1000, 4_000_000, defaults) == ("0644", None, "4000000")
    assert file_attributes(None, None, None, defaults) == (None, None, None)


async def test_the_defaults_follow_the_deployment_and_else_the_app_user(harness: Harness) -> None:
    deployment, _ = await add_stack(harness)
    plain = file_defaults(deployment)
    assert plain == FileDefaults(0o600, os.geteuid(), os.getegid())
    deployment.file_mode = "0640"
    deployment.file_owner = "root"
    deployment.file_group = "12345"
    assert file_defaults(deployment) == FileDefaults(0o640, 0, 12345)
    deployment.file_owner = "no-such-account"
    assert file_defaults(deployment).owner_id == -1


async def scan_project(harness: Harness, tmp_path: Path) -> tuple[ComposeScan, Path]:
    """Scan the whole fixture project with the real Compose."""
    deployment, root = await add_stack(harness)
    (root / ".env.web").chmod(0o640)
    return (
        await scan_compose(harness.db, real_cli(tmp_path), deployment, ["compose.yaml"]),
        root,
    )


async def test_the_services_are_candidates_with_cleaned_names_and_profiles(
    harness: Harness, tmp_path: Path
) -> None:
    scan, root = await scan_project(harness, tmp_path)
    assert scan.compose_files == ("compose.yaml",)
    by_compose = {item.compose_name: item for item in scan.services}
    assert list(by_compose) == ["Worker_1", "api", "web", "worker-1"]
    assert by_compose["Worker_1"].name == "worker-1"
    assert by_compose["Worker_1"].profiles == ("jobs",)
    assert by_compose["Worker_1"].disabled_reason is None
    assert by_compose["worker-1"].disabled_reason == "Another service is already named worker-1."
    assert by_compose["web"].files == (
        ".env.web",
        ".env.optional",
        "conf/nginx.conf",
        "missing.conf",
        "certs/server.key",
        "conf/app.cfg",
    )
    assert by_compose["api"].files == (".env.shared", ".env.api", str(root.parent / "outside.env"))


async def test_every_source_of_a_file_is_found_and_shared_files_are_shown_once(
    harness: Harness, tmp_path: Path
) -> None:
    scan, root = await scan_project(harness, tmp_path)
    keys = [item.key for item in scan.files]
    assert keys.count(".env.shared") == 1
    web = candidate(scan, ".env.web")
    assert (web.sources, web.users, web.owner) == ((FileSource.ENV_FILE,), ("web",), "web")
    assert (web.exists, web.optional, web.file_type) == (True, False, FileType.ENV)
    assert web.file_mode == "0640" and web.file_owner is None and web.file_group is None
    assert web.dist_suggestion == ".env.web.example" and web.preselected is True
    shared = candidate(scan, ".env.shared")
    assert shared.users == ("worker-1", "api") and shared.owner == "worker-1"
    assert candidate(scan, ".env.api").dist_suggestion == ".env.api-dist"
    assert candidate(scan, "conf/nginx.conf").sources == (FileSource.BIND_MOUNT,)
    assert candidate(scan, "conf/app.cfg").sources == (FileSource.CONFIG_FILE,)
    key = candidate(scan, "certs/server.key")
    assert (key.sources, key.file_type, key.type_problems) == (
        (FileSource.SECRET_FILE,),
        FileType.CERT,
        {},
    )
    assert key.dist_suggestion is None
    assert ".env" not in keys
    assert (root / ".env").is_file()
    assert ".env" not in {item.label for item in scan.ignored}
    own = {item.key: item for item in scan.deployment_files}
    assert list(own) == [".env"]


async def test_an_optional_file_that_is_absent_is_listed_but_not_ticked(
    harness: Harness, tmp_path: Path
) -> None:
    scan, root = await scan_project(harness, tmp_path)
    optional = candidate(scan, ".env.optional")
    assert (optional.exists, optional.optional, optional.size) == (False, True, None)
    assert optional.disabled_reason is None and optional.preselected is False
    assert not (root / ".env.optional").exists()


async def test_what_is_not_a_plain_file_inside_the_root_is_ignored_or_disabled(
    harness: Harness, tmp_path: Path
) -> None:
    scan, root = await scan_project(harness, tmp_path)
    ignored = {item.label: item.reason for item in scan.ignored}
    assert ignored == {
        "data": FOLDER_REASON,
        "/var/run/docker.sock": OUTSIDE_REASON,
    }
    missing = candidate(scan, "missing.conf")
    assert (missing.exists, missing.guessed, missing.sources) == (
        False,
        True,
        (FileSource.BIND_MOUNT,),
    )
    assert missing.dist_suggestion is None and missing.preselected is False
    assert missing.disabled_reason is None
    outside = next(item for item in scan.files if item.key.endswith("outside.env"))
    assert outside.key == str(root.parent / "outside.env")
    assert outside.disabled_reason == OUTSIDE_REASON and outside.preselected is False
    assert outside.exists is False


async def test_nothing_outside_the_root_is_probed(harness: Harness, tmp_path: Path) -> None:
    deployment, root = await add_stack(
        harness,
        {"compose.yaml": "services:\n  web:\n    image: x\n    env_file: ../secret-neighbor.env\n"},
    )
    (root.parent / "secret-neighbor.env").write_text("TOKEN=hunter2\n", encoding="utf-8")
    scan = await scan_compose(harness.db, real_cli(tmp_path), deployment, ["compose.yaml"])
    (neighbor,) = scan.files
    assert neighbor.exists is False and neighbor.size is None and neighbor.file_mode is None
    assert "hunter2" not in repr(scan)


async def test_a_link_that_leaves_the_root_is_disabled(harness: Harness, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "target.env").write_text("A=1\n", encoding="utf-8")
    deployment, root = await add_stack(
        harness, {"compose.yaml": "services:\n  web:\n    image: x\n    env_file: leak.env\n"}
    )
    (root / "leak.env").symlink_to(elsewhere / "target.env")
    scan = await scan_compose(harness.db, real_cli(tmp_path), deployment, ["compose.yaml"])
    (leak,) = scan.files
    assert leak.disabled_reason == OUTSIDE_REASON and leak.exists is False


async def test_files_that_cannot_be_imported_say_why(harness: Harness, tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "compose.yaml": (
            "services:\n  web:\n    image: x\n    env_file:\n"
            "      - binary.env\n      - big.env\n      - latin.env\n      - folder.env\n"
            "      - ca.pem\n      - absent.pem\n      - bad name.env\n      - plain.env\n"
        ),
        "binary.env": b"A=1\x00B=2\n",
        "big.env": "A=" + "x" * MAX_CONTENT_BYTES + "\n",
        "latin.env": b"CAF\xe9=1\n",
        "ca.pem": "not a certificate\n",
        "plain.env": "A=1\n",
    }
    deployment, root = await add_stack(harness, files)
    (root / "folder.env").mkdir()
    scan = await scan_compose(harness.db, real_cli(tmp_path), deployment, ["compose.yaml"])
    reasons = {item.key: item.disabled_reason for item in scan.files}
    assert reasons["binary.env"] == "Binary content is not supported."
    assert reasons["big.env"] is not None and "too large" in reasons["big.env"]
    assert reasons["latin.env"] is not None and "UTF-8" in reasons["latin.env"]
    assert reasons["folder.env"] == FOLDER_REASON
    assert reasons["bad name.env"] == FILE_NAME_REASON
    assert reasons["plain.env"] is None and candidate(scan, "plain.env").preselected is True
    bad_cert = candidate(scan, "ca.pem")
    assert bad_cert.file_type is FileType.CERT and "PEM" in bad_cert.type_problems["cert"]
    assert bad_cert.preselected is False
    absent = candidate(scan, "absent.pem")
    assert absent.type_problems == {"cert": CERT_EMPTY_REASON} and absent.exists is False


async def test_a_file_name_the_environment_rule_refuses_is_disabled(
    harness: Harness, tmp_path: Path
) -> None:
    deployment, _ = await add_stack(
        harness,
        {
            "compose.yaml": "services:\n  web:\n    image: x\n    env_file: 'bad name.env'\n",
            "bad name.env": "A=1\n",
        },
    )
    scan = await scan_compose(harness.db, real_cli(tmp_path), deployment, ["compose.yaml"])
    (bad,) = scan.files
    assert bad.disabled_reason == FILE_NAME_REASON and bad.preselected is False


async def test_a_service_whose_name_cannot_be_used_is_disabled_and_owns_nothing(
    harness: Harness, tmp_path: Path
) -> None:
    deployment, _ = await add_stack(
        harness,
        {
            "compose.yaml": (
                "services:\n  '__':\n    image: x\n    env_file: a.env\n  web:\n    image: y\n"
            ),
            "a.env": "A=1\n",
        },
    )
    scan = await scan_compose(harness.db, real_cli(tmp_path), deployment, ["compose.yaml"])
    odd = next(item for item in scan.services if item.compose_name == "__")
    assert odd.disabled_reason == NO_NAME_REASON
    (only,) = scan.files
    assert only.owner is None and only.preselected is False


async def test_overrides_in_a_folder_below_the_root_are_read_against_that_folder(
    harness: Harness, tmp_path: Path
) -> None:
    deployment, _ = await add_stack(
        harness,
        {
            "app/compose.yaml": "services:\n  web:\n    image: x\n    env_file: web.env\n",
            "app/compose.override.yaml": "services:\n  web:\n    env_file: [web.env, more.env]\n",
            "app/web.env": "A=1\n",
            "app/.env": "B=1\n",
            "web.env": "ROOT=1\n",
        },
    )
    scan = await scan_compose(
        harness.db,
        real_cli(tmp_path),
        deployment,
        ["app/compose.yaml", "app/compose.override.yaml"],
    )
    assert scan.compose_files == ("app/compose.yaml", "app/compose.override.yaml")
    assert [item.key for item in scan.files] == ["app/web.env", "app/more.env"]
    assert candidate(scan, "app/more.env").exists is False
    assert scan.ignored == ()
    assert [item.key for item in scan.deployment_files] == ["app/.env"]
    assert scan.deployment_command == (
        "docker compose -f app/compose.yaml -f app/compose.override.yaml up -d"
    )


async def test_a_remote_include_stops_the_scan(harness: Harness, tmp_path: Path) -> None:
    deployment, _ = await add_stack(
        harness,
        {"compose.yaml": "include:\n  - git@example.invalid:team/stack.git\nservices: {}\n"},
    )
    with pytest.raises(ComposeScanError) as caught:
        await scan_compose(harness.db, real_cli(tmp_path), deployment, ["compose.yaml"])
    assert "git@example.invalid:team/stack.git" in str(caught.value)
    assert "remote source" in str(caught.value)


async def test_an_include_outside_the_deployment_folder_stops_the_scan(
    harness: Harness, tmp_path: Path
) -> None:
    deployment, root = await add_stack(
        harness,
        {"compose.yaml": "include:\n  - ../elsewhere.yaml\nservices: {}\n"},
    )
    (root.parent / "elsewhere.yaml").write_text("services:\n  other:\n    image: x\n")
    with pytest.raises(ComposeScanError) as caught:
        await scan_compose(harness.db, real_cli(tmp_path), deployment, ["compose.yaml"])
    assert '"../elsewhere.yaml"' in str(caught.value)
    assert "outside the deployment folder" in str(caught.value)


async def test_a_scan_holds_names_and_attributes_but_no_content(
    harness: Harness, tmp_path: Path
) -> None:
    scan, _ = await scan_project(harness, tmp_path)
    text = repr(scan)
    for content in ("WEB=1", "SHARED=1", "worker_processes", "mode=prod", "PROJECT_VARIABLE"):
        assert content not in text
    assert PEM.splitlines()[1] not in text


async def scan_geotravel(harness: Harness, tmp_path: Path, *, api_dist: bool = True) -> ComposeScan:
    """Scan the GeoTravel shaped project, with or without the dist file of the mounted file."""
    files = dict(GEOTRAVEL)
    if not api_dist:
        del files["environments/.env.api-dist"]
    deployment, _ = await add_stack(harness, files, name="geotravel")
    return await scan_compose(harness.db, real_cli(tmp_path), deployment, ["docker-compose.yml"])


async def test_a_real_layout_offers_every_service_file_and_its_dist_file(
    harness: Harness, tmp_path: Path
) -> None:
    scan = await scan_geotravel(harness, tmp_path)
    assert [service.name for service in scan.services] == ["api", "db", "frontend"]
    assert all(service.disabled_reason is None for service in scan.services)
    assert [item.key for item in scan.files] == [
        "environments/.env.api",
        "environments/.env.db",
        "environments/.env.frontend",
    ]
    db = candidate(scan, "environments/.env.db")
    assert (db.exists, db.dist_suggestion, db.starts_from_dist, db.preselected) == (
        True,
        "environments/.env.db-dist",
        False,
        True,
    )
    frontend = candidate(scan, "environments/.env.frontend")
    assert (frontend.exists, frontend.optional, frontend.guessed) == (False, False, False)
    assert frontend.dist_suggestion == "environments/.env.frontend-dist"
    assert frontend.starts_from_dist is True and frontend.preselected is True
    api = candidate(scan, "environments/.env.api")
    assert (api.sources, api.users, api.owner) == ((FileSource.BIND_MOUNT,), ("api",), "api")
    assert (api.exists, api.guessed, api.dist_suggestion) == (
        False,
        True,
        "environments/.env.api-dist",
    )
    assert api.starts_from_dist is True and api.preselected is True
    assert {item.label: item.reason for item in scan.ignored} == {
        "data/mysql/data": MISSING_BIND_REASON,
        "backups": FOLDER_REASON,
        "scripts": FOLDER_REASON,
    }


async def test_a_missing_mount_without_a_dist_file_is_offered_unticked_when_it_looks_like_a_file(
    harness: Harness, tmp_path: Path
) -> None:
    scan = await scan_geotravel(harness, tmp_path, api_dist=False)
    api = candidate(scan, "environments/.env.api")
    assert (api.guessed, api.dist_suggestion, api.starts_from_dist) == (True, None, False)
    assert api.preselected is False and api.disabled_reason is None
    assert "data/mysql/data" in {item.label for item in scan.ignored}


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (".env", True),
        (".env.api", True),
        ("nginx.conf", True),
        ("tls.pem", True),
        ("data", False),
        ("mysql", False),
        (".git", False),
    ],
)
def test_a_name_looks_like_a_file_when_it_is_an_env_name_or_has_an_extension(
    name: str, expected: bool
) -> None:
    assert looks_like_file(name) is expected


def own_file(scan: ComposeScan, key: str) -> FileCandidate:
    """The deployment file candidate with this key."""
    return next(item for item in scan.deployment_files if item.key == key)


async def test_the_deployment_env_is_offered_as_a_deployment_file_and_the_compose_file_is_not(
    harness: Harness, tmp_path: Path
) -> None:
    scan = await scan_geotravel(harness, tmp_path)
    assert scan.compose_files == ("docker-compose.yml",)
    assert [item.key for item in scan.deployment_files] == [".env"]
    env = own_file(scan, ".env")
    assert (env.sources, env.users, env.owner) == (
        (FileSource.DEPLOYMENT_ENV,),
        (DEPLOYMENT_FILES_NAME,),
        DEPLOYMENT_FILES_NAME,
    )
    assert (env.name, env.exists, env.optional, env.file_type) == (".env", True, True, FileType.ENV)
    assert env.preselected is True and env.disabled_reason is None
    assert all(item.key not in {".env", "docker-compose.yml"} for item in scan.files)


async def test_the_scan_offers_a_command_for_the_deployment_and_for_each_service(
    harness: Harness, tmp_path: Path
) -> None:
    scan = await scan_geotravel(harness, tmp_path)
    assert scan.deployment_command == "docker compose -f docker-compose.yml up -d"
    assert {service.name: service.command for service in scan.services} == {
        name: f"docker compose -f docker-compose.yml up -d --no-deps {name}"
        for name in ("api", "db", "frontend")
    }
    for command in [scan.deployment_command, *(service.command for service in scan.services)]:
        assert parse_commands(command)[0][:4] == ("docker", "compose", "-f", "docker-compose.yml")


def test_a_command_quotes_a_path_with_spaces_so_the_parser_reads_one_word() -> None:
    command = compose_command(["my app/compose.yaml", "my app/compose.override.yaml"])
    assert command == (
        "docker compose -f 'my app/compose.yaml' -f 'my app/compose.override.yaml' up -d"
    )
    assert parse_commands(command) == (
        (
            "docker",
            "compose",
            "-f",
            "my app/compose.yaml",
            "-f",
            "my app/compose.override.yaml",
            "up",
            "-d",
        ),
    )


async def test_a_missing_deployment_env_is_offered_only_with_a_dist_file(
    harness: Harness, tmp_path: Path
) -> None:
    compose = "services:\n  web:\n    image: x\n"
    deployment, _ = await add_stack(
        harness, {"compose.yaml": compose, ".env.example": "A=\n"}, name="with-dist"
    )
    scan = await scan_compose(harness.db, real_cli(tmp_path), deployment, ["compose.yaml"])
    env = own_file(scan, ".env")
    assert (env.exists, env.dist_suggestion, env.starts_from_dist) == (
        False,
        ".env.example",
        True,
    )
    assert env.preselected is True
    bare, _ = await add_stack(harness, {"compose.yaml": compose}, name="without-dist")
    scan = await scan_compose(harness.db, real_cli(tmp_path), bare, ["compose.yaml"])
    assert scan.deployment_files == ()


async def test_a_deployment_env_a_service_names_itself_stays_a_service_file(
    harness: Harness, tmp_path: Path
) -> None:
    deployment, _ = await add_stack(
        harness,
        {"compose.yaml": "services:\n  web:\n    image: x\n    env_file: .env\n", ".env": "A=1\n"},
    )
    scan = await scan_compose(harness.db, real_cli(tmp_path), deployment, ["compose.yaml"])
    assert candidate(scan, ".env").owner == "web"
    assert scan.deployment_files == ()
