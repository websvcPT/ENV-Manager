"""The Compose runner: the real ``docker compose config`` on fixture projects, and the fake docker
for the way it is started (arguments, folder, environment, deadline, clean up)."""

import shutil
import socket
from pathlib import Path

import pytest

from app.services import compose_config
from app.services.compose_config import (
    MISSING_FILE_MESSAGE,
    REMOTE_MESSAGE,
    TIMED_OUT_MESSAGE,
    UNREADABLE_MESSAGE,
    YAML_MESSAGE,
    ComposeModel,
    ComposeScanError,
    EnvFileRef,
    extract_model,
    failure_message,
    read_compose_model,
)
from app.services.docker_cli import CLI_MISSING_REASON, DockerCli
from app.services.paths import AllowedPaths
from app.tests.fake_docker import FakeDocker


def project(root: Path, files: dict[str, str]) -> Path:
    """Write the files of a fixture project below ``root`` and return the real root."""
    root.mkdir(parents=True, exist_ok=True)
    for relative, text in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root.resolve()


@pytest.fixture
def cli(tmp_path: Path) -> DockerCli:
    """The real docker command on a socket path that does not exist, as in the development stack."""
    return DockerCli(socket_path=tmp_path / "no-docker.sock", scratch=tmp_path / "scratch")


async def read(cli: DockerCli, root: Path, *names: str) -> ComposeModel:
    """Run the real Compose on the named files of a project, the first being the base file."""
    files = [root / name for name in names]
    return await read_compose_model(cli, AllowedPaths([root]), root, files[0].parent, files)


def service(model: ComposeModel, name: str) -> compose_config.ComposeService:
    """The service of the model with this name."""
    return next(item for item in model.services if item.name == name)


async def test_env_file_forms_are_read_without_reading_the_files(
    cli: DockerCli, tmp_path: Path
) -> None:
    root = project(
        tmp_path / "stack",
        {
            "compose.yaml": (
                "services:\n"
                "  web:\n"
                "    image: nginx\n"
                "    env_file:\n"
                "      - .env.web\n"
                "      - path: ./.env.optional\n"
                "        required: false\n"
                "      - path: ./missing.env\n"
                "        required: true\n"
                "  api:\n"
                "    image: x\n"
                "    env_file: .env.api\n"
            ),
            ".env.web": "A=1\n",
        },
    )
    model = await read(cli, root, "compose.yaml")
    assert [item.name for item in model.services] == ["api", "web"]
    assert service(model, "api").env_files == (EnvFileRef(f"{root}/.env.api", True),)
    assert service(model, "web").env_files == (
        EnvFileRef(f"{root}/.env.web", True),
        EnvFileRef(f"{root}/.env.optional", False),
        EnvFileRef(f"{root}/missing.env", True),
    )


async def test_a_service_under_a_profile_is_kept_with_its_profiles(
    cli: DockerCli, tmp_path: Path
) -> None:
    root = project(
        tmp_path / "stack",
        {
            "compose.yaml": (
                "services:\n"
                "  web:\n    image: x\n"
                "  debug:\n    image: y\n    profiles: [debug, tools]\n"
            )
        },
    )
    model = await read(cli, root, "compose.yaml")
    assert service(model, "web").profiles == ()
    assert service(model, "debug").profiles == ("debug", "tools")


async def test_an_override_file_is_merged_over_the_base_file(
    cli: DockerCli, tmp_path: Path
) -> None:
    root = project(
        tmp_path / "stack",
        {
            "compose.yaml": "services:\n  web:\n    image: x\n    env_file: .env.web\n",
            "compose.override.yaml": (
                "services:\n"
                "  web:\n"
                "    env_file:\n      - .env.web\n      - .env.local\n"
                "    volumes:\n      - ./extra.conf:/extra.conf\n"
                "  worker:\n    image: w\n    env_file: .env.worker\n"
            ),
        },
    )
    model = await read(cli, root, "compose.yaml", "compose.override.yaml")
    web = service(model, "web")
    assert [item.path for item in web.env_files] == [f"{root}/.env.web", f"{root}/.env.local"]
    assert web.bind_sources == (f"{root}/extra.conf",)
    assert service(model, "worker").env_files == (EnvFileRef(f"{root}/.env.worker", True),)


async def test_extends_and_include_resolve_paths_against_the_file_that_names_them(
    cli: DockerCli, tmp_path: Path
) -> None:
    root = project(
        tmp_path / "stack",
        {
            "compose.yaml": (
                "include:\n"
                "  - path: parts/part.yaml\n"
                "services:\n"
                "  web:\n"
                "    extends:\n      file: shared/common.yaml\n      service: base\n"
                "    image: nginx\n"
                "    env_file: .env.web\n"
            ),
            "shared/common.yaml": (
                "services:\n  base:\n    image: busybox\n    env_file: base.env\n"
            ),
            "parts/part.yaml": "services:\n  part:\n    image: y\n    env_file: part.env\n",
        },
    )
    model = await read(cli, root, "compose.yaml")
    assert [item.path for item in service(model, "web").env_files] == [
        f"{root}/shared/base.env",
        f"{root}/.env.web",
    ]
    assert service(model, "part").env_files == (EnvFileRef(f"{root}/parts/part.env", True),)


async def test_bind_mounts_secrets_and_configs_come_with_their_files(
    cli: DockerCli, tmp_path: Path
) -> None:
    root = project(
        tmp_path / "stack",
        {
            "compose.yaml": (
                "services:\n"
                "  web:\n"
                "    image: nginx\n"
                "    volumes:\n"
                "      - ./conf/nginx.conf:/etc/nginx/nginx.conf:ro\n"
                "      - ./data:/data\n"
                "      - named:/n\n"
                "      - /var/run/docker.sock:/var/run/docker.sock\n"
                "    secrets: [token, from_env]\n"
                "    configs:\n      - source: app\n        target: /app.cfg\n"
                "secrets:\n"
                "  token:\n    file: ./secrets/token.txt\n"
                "  from_env:\n    environment: SOME_VARIABLE\n"
                "configs:\n  app:\n    file: ./conf/app.cfg\n"
                "volumes:\n  named: {}\n"
            )
        },
    )
    model = await read(cli, root, "compose.yaml")
    web = service(model, "web")
    assert web.bind_sources == (
        f"{root}/conf/nginx.conf",
        f"{root}/data",
        "/var/run/docker.sock",
    )
    assert web.secrets == ("token", "from_env") and web.configs == ("app",)
    assert model.secret_files == {"token": f"{root}/secrets/token.txt"}
    assert model.config_files == {"app": f"{root}/conf/app.cfg"}


async def test_interpolation_uses_the_project_env_file_and_no_value_is_kept(
    cli: DockerCli, tmp_path: Path
) -> None:
    root = project(
        tmp_path / "stack",
        {
            "compose.yaml": (
                "services:\n"
                "  web:\n"
                "    image: x\n"
                "    env_file: ${ENV_DIR:-.}/.env.web\n"
                "    environment:\n      TOKEN: ${SECRET_VALUE}\n"
            ),
            ".env": "ENV_DIR=./conf\nSECRET_VALUE=hunter2-secret\n",
        },
    )
    model = await read(cli, root, "compose.yaml")
    assert service(model, "web").env_files == (EnvFileRef(f"{root}/conf/.env.web", True),)
    assert "hunter2-secret" not in repr(model)


async def test_relative_paths_resolve_against_the_folder_of_the_base_file(
    cli: DockerCli, tmp_path: Path
) -> None:
    root = project(
        tmp_path / "stack",
        {
            "web/compose.yaml": "services:\n  web:\n    image: x\n    env_file: ./web.env\n",
            "web.env": "OUTSIDE=1\n",
        },
    )
    model = await read(cli, root, "web/compose.yaml")
    assert service(model, "web").env_files == (EnvFileRef(f"{root}/web/web.env", True),)


async def test_a_project_folder_that_is_not_a_valid_project_name_still_scans(
    cli: DockerCli, tmp_path: Path
) -> None:
    root = project(
        tmp_path / "My Stack_!",
        {"compose.yaml": "services:\n  web:\n    image: x\n"},
    )
    assert [item.name for item in (await read(cli, root, "compose.yaml")).services] == ["web"]


@pytest.mark.parametrize(
    "address",
    [
        "git@example.invalid:team/stack.git",
        "https://example.invalid/team/stack.git",
        "git://example.invalid/team/stack",
        "oci://example.invalid/team/stack:1",
    ],
)
async def test_a_remote_include_is_refused_before_the_command_runs(
    tmp_path: Path, address: str
) -> None:
    fake = FakeDocker(tmp_path / "fake")
    root = project(
        tmp_path / "stack",
        {"compose.yaml": f"include:\n  - {address}\nservices:\n  web:\n    image: x\n"},
    )
    with pytest.raises(ComposeScanError) as caught:
        await read(fake.cli(), root, "compose.yaml")
    assert address in str(caught.value) and "remote source" in str(caught.value)
    assert fake.calls() == []


async def test_nested_includes_and_extends_resolve_as_the_check_reads_them(
    cli: DockerCli, tmp_path: Path
) -> None:
    root = project(
        tmp_path / "stack",
        {
            "compose.yaml": (
                "include:\n"
                "  - path: parts/a.yaml\n"
                "  - path: folder/d.yaml\n    project_directory: folder/inner\n"
                "services:\n"
                "  web:\n    image: x\n"
                "    extends:\n      file: shared/base.yaml\n      service: base\n"
            ),
            "parts/a.yaml": "include:\n  - deeper/e.yaml\nservices:\n  a:\n    image: x\n",
            "parts/deeper/e.yaml": "services:\n  e:\n    image: x\n",
            "folder/d.yaml": "include:\n  - f.yaml\nservices:\n  d:\n    image: x\n",
            "folder/inner/f.yaml": "services:\n  f:\n    image: x\n",
            "shared/base.yaml": (
                "services:\n  base:\n    image: b\n"
                "  mid:\n    extends:\n      file: nested/more.yaml\n      service: m\n"
            ),
            "shared/nested/more.yaml": "services:\n  m:\n    image: x\n",
        },
    )
    model = await read(cli, root, "compose.yaml")
    assert [item.name for item in model.services] == ["a", "d", "e", "f", "web"]


async def test_an_include_that_leaves_the_folder_by_a_symlink_never_starts_the_command(
    tmp_path: Path,
) -> None:
    fake = FakeDocker(tmp_path / "fake")
    root = project(
        tmp_path / "site" / "stack",
        {"compose.yaml": "include:\n  - linked/part.yaml\nservices: {}\n"},
    )
    (tmp_path / "site" / "elsewhere").mkdir()
    (tmp_path / "site" / "elsewhere" / "part.yaml").write_text("services: {}\n", encoding="utf-8")
    (root / "linked").symlink_to(tmp_path / "site" / "elsewhere", target_is_directory=True)
    with pytest.raises(ComposeScanError) as caught:
        await read_compose_model(
            fake.cli(), AllowedPaths([root.parent]), root, root, [root / "compose.yaml"]
        )
    assert '"linked/part.yaml"' in str(caught.value)
    assert fake.calls() == []


async def test_a_remote_include_the_check_missed_is_stopped_by_compose_without_connecting(
    cli: DockerCli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(compose_config, "check_compose_references", lambda *args, **kwargs: None)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.setblocking(False)
    port = listener.getsockname()[1]
    root = project(
        tmp_path / "stack",
        {
            "compose.yaml": "include:\n  - parts/part.yaml\nservices:\n  web:\n    image: x\n",
            "parts/part.yaml": f"include:\n  - git://127.0.0.1:{port}/team/stack.git\n",
        },
    )
    try:
        with pytest.raises(ComposeScanError) as caught:
            await read(cli, root, "compose.yaml")
        with pytest.raises(BlockingIOError):
            listener.accept()
    finally:
        listener.close()
    assert str(caught.value) == REMOTE_MESSAGE


async def test_a_missing_required_variable_is_named_and_a_broken_file_is_not_quoted(
    cli: DockerCli, tmp_path: Path
) -> None:
    root = project(
        tmp_path / "stack",
        {
            "compose.yaml": "services:\n  web:\n    image: ${IMAGE_NAME:?set the image}\n",
            "broken.yaml": "services:\n  web:\n    image: [unclosed secret-value-123\n",
            "include.yaml": "include:\n  - ./gone.yaml\nservices: {}\n",
        },
    )
    with pytest.raises(ComposeScanError) as variable:
        await read(cli, root, "compose.yaml")
    assert str(variable.value) == (
        "The Compose files need the variable IMAGE_NAME, which is not set in the project .env file."
    )
    with pytest.raises(ComposeScanError) as broken:
        await read(cli, root, "broken.yaml")
    assert str(broken.value) == YAML_MESSAGE and "secret-value-123" not in str(broken.value)
    with pytest.raises(ComposeScanError) as gone:
        await read(cli, root, "include.yaml")
    assert str(gone.value) == MISSING_FILE_MESSAGE


def test_failures_are_reduced_to_fixed_sentences() -> None:
    assert failure_message('git remote resource is disabled by "X"') == REMOTE_MESSAGE
    assert failure_message("failed to pull OCI resource") == REMOTE_MESSAGE
    assert failure_message("yaml: line 3: did not find expected key") == YAML_MESSAGE
    assert failure_message("go-yaml load error in parser at L3.C12") == YAML_MESSAGE
    assert failure_message("open /srv/x/y.yaml: no such file or directory") == MISSING_FILE_MESSAGE
    assert failure_message("something else with password=hunter2") == UNREADABLE_MESSAGE


def test_only_the_structure_is_kept_from_a_document() -> None:
    model = extract_model(
        {
            "services": {
                "web": {
                    "image": "x",
                    "environment": {"TOKEN": "hunter2-secret"},
                    "env_file": ["/srv/a.env", {"path": "/srv/b.env", "required": False}, 3],
                    "volumes": [
                        {"type": "bind", "source": "/srv/c.conf"},
                        {"type": "volume", "source": "named"},
                        "bad",
                    ],
                    "secrets": ["s", {"source": "t"}],
                    "profiles": ["p", 1],
                }
            },
            "secrets": {"s": {"file": "/srv/s.txt"}, "t": {"environment": "X"}},
        }
    )
    web = model.services[0]
    assert web.env_files == (EnvFileRef("/srv/a.env", True), EnvFileRef("/srv/b.env", False))
    assert web.bind_sources == ("/srv/c.conf",) and web.profiles == ("p",)
    assert web.secrets == ("s", "t") and model.secret_files == {"s": "/srv/s.txt"}
    assert "hunter2-secret" not in repr(model)
    assert extract_model(["not", "a", "mapping"]) == ComposeModel((), {}, {})


async def test_the_command_runs_without_a_shell_in_the_project_folder_with_a_minimal_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENVMANAGER_KEY", "key-material-0123456789")
    fake = FakeDocker(tmp_path / "fake")
    fake.behavior('for last; do :; done; printf \'{"services": {}}\' > "$last"')
    root = project(tmp_path / "stack", {"stack/compose.yaml": "services: {}\n"})
    cli = fake.cli(mounted=False)
    await read_compose_model(
        cli, AllowedPaths([root]), root, root / "stack", [root / "stack/compose.yaml"]
    )
    (call,) = fake.calls()
    arguments = call.arguments
    assert arguments[:3] == ("compose", "--project-directory", str(root / "stack"))
    assert arguments[3:7] == ("-p", "envmanager-scan", "--profile", "*")
    assert arguments[7:9] == ("-f", str(root / "stack/compose.yaml"))
    assert arguments[9:13] == ("config", "--format", "json", "--no-env-resolution")
    assert arguments[13] == "-o" and arguments[14].startswith(str(cli.scratch))
    assert call.working_directory == str(root / "stack")
    environment = call.environment
    assert environment["COMPOSE_EXPERIMENTAL_GIT_REMOTE"] == "false"
    assert environment["COMPOSE_EXPERIMENTAL_OCI_REMOTE"] == "false"
    assert "ENVMANAGER_KEY" not in environment
    assert set(environment) <= {
        "PATH",
        "HOME",
        "LANG",
        "DOCKER_CONFIG",
        "DOCKER_HOST",
        "COMPOSE_EXPERIMENTAL_GIT_REMOTE",
        "COMPOSE_EXPERIMENTAL_OCI_REMOTE",
        "PWD",
        "SHLVL",
        "_",
    }
    assert list(cli.scratch.glob("scan-*")) == []


async def test_a_command_that_does_not_finish_is_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(compose_config, "SCAN_TIMEOUT_SECONDS", 0.5)
    fake = FakeDocker(tmp_path / "fake")
    fake.behavior("exec sleep 300")
    root = project(tmp_path / "stack", {"compose.yaml": "services: {}\n"})
    cli = fake.cli(mounted=False)
    with pytest.raises(ComposeScanError) as caught:
        await read(cli, root, "compose.yaml")
    assert str(caught.value) == TIMED_OUT_MESSAGE
    assert list(cli.scratch.glob("scan-*")) == []


async def test_a_failing_command_shows_a_fixed_sentence_and_cleans_up(tmp_path: Path) -> None:
    fake = FakeDocker(tmp_path / "fake")
    fake.behavior('echo "password=hunter2 in /srv/secret" >&2; exit 1')
    root = project(tmp_path / "stack", {"compose.yaml": "services: {}\n"})
    cli = fake.cli(mounted=False)
    with pytest.raises(ComposeScanError) as caught:
        await read(cli, root, "compose.yaml")
    assert str(caught.value) == UNREADABLE_MESSAGE
    assert list(cli.scratch.glob("scan-*")) == []


async def test_an_answer_that_is_too_large_or_not_json_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(compose_config, "MAX_MODEL_BYTES", 100)
    fake = FakeDocker(tmp_path / "fake")
    root = project(tmp_path / "stack", {"compose.yaml": "services: {}\n"})
    cli = fake.cli(mounted=False)
    fake.behavior('for last; do :; done; head -c 500 /dev/zero | tr "\\0" " " > "$last"')
    with pytest.raises(ComposeScanError) as large:
        await read(cli, root, "compose.yaml")
    fake.behavior('for last; do :; done; echo "not json" > "$last"')
    monkeypatch.setattr(compose_config, "MAX_MODEL_BYTES", 10000)
    with pytest.raises(ComposeScanError) as invalid:
        await read(cli, root, "compose.yaml")
    assert str(large.value) == str(invalid.value) == compose_config.OUTPUT_MESSAGE


async def test_a_missing_docker_command_is_reported(
    cli: DockerCli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = project(tmp_path / "stack", {"compose.yaml": "services: {}\n"})
    monkeypatch.setattr(shutil, "which", lambda *args, **kwargs: None)
    with pytest.raises(ComposeScanError) as caught:
        await read(cli, root, "compose.yaml")
    assert str(caught.value) == CLI_MISSING_REASON
