"""Finding stacks from Docker and from the allowed paths, merged, with the used ones marked.

Docker is the fake executable (a real script), the folders are real trees in ``tmp_path``.
"""

import json
import shutil
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.db import create_session_factory
from app.models import Deployment, Project
from app.services import stack_discovery
from app.services.docker_cli import (
    CLI_MISSING_REASON,
    DAEMON_UNREACHABLE_REASON,
    NOT_MOUNTED_REASON,
    DockerCli,
)
from app.services.docker_query import FAILED_MESSAGE
from app.services.paths import AllowedPaths
from app.services.stack_discovery import (
    NO_FILES_REASON,
    OUTSIDE_REASON,
    TOO_MANY_FILES_REASON,
    UNREADABLE_REASON,
    DetectedStack,
    ExistingDeployment,
    StackDiscovery,
    StackSource,
    discover_stacks,
    parse_projects,
    parse_working_dirs,
)
from app.tests.fake_docker import FakeDocker

BEHAVIOR = """case "$*" in
  "compose ls"*) cat "$dir/ls.out"; cat "$dir/ls.err" >&2; exit "$(cat "$dir/ls.code")" ;;
  "ps --all"*) cat "$dir/labels.out"; exit "$(cat "$dir/labels.code")" ;;
esac
"""


@pytest.fixture
def host(tmp_path: Path) -> Path:
    """The allowed root, as a real path."""
    root = tmp_path.resolve() / "host"
    root.mkdir()
    return root


@pytest.fixture
def fake(tmp_path: Path) -> FakeDocker:
    """A fake docker that knows no project and whose label lookup finds nothing."""
    docker = FakeDocker(tmp_path / "fake")
    answer(docker, [])
    return docker


def answer(
    docker: FakeDocker,
    projects: list[object] | str,
    labels: dict[str, str] | None = None,
    *,
    ls_code: int = 0,
    ls_error: str = "",
    labels_code: int = 0,
) -> None:
    """Make the fake print a ``compose ls`` document and the label lines, or fail."""
    text = projects if isinstance(projects, str) else json.dumps(projects)
    lines = "".join(
        json.dumps({"project": name, "folder": folder}) + "\n"
        for name, folder in (labels or {}).items()
    )
    files = {
        "ls.out": text,
        "ls.err": ls_error,
        "ls.code": str(ls_code),
        "labels.out": lines,
        "labels.code": str(labels_code),
    }
    for name, content in files.items():
        (docker.folder / name).write_text(content, encoding="utf-8")
    docker.behavior(BEHAVIOR)


def project(name: str, files: list[Path], status: str = "running(2)") -> dict[str, str]:
    """One entry of ``docker compose ls``."""
    return {"Name": name, "Status": status, "ConfigFiles": ",".join(str(path) for path in files)}


def make_stack(folder: Path, *names: str) -> Path:
    """A folder holding the named Compose files, the base one when none is named."""
    folder.mkdir(parents=True, exist_ok=True)
    for name in names or ("compose.yaml",):
        (folder / name).write_text("services: {}\n", encoding="utf-8")
    return folder


async def add_deployment(
    engine: AsyncEngine, name: str, root: Path, compose_project: str | None = None
) -> int:
    """A deployment in a new project, and its id."""
    async with create_session_factory(engine)() as db:
        owner = Project(name=f"project {name}")
        db.add(owner)
        await db.flush()
        deployment = Deployment(
            project_id=owner.id, name=name, root_path=str(root), compose_project=compose_project
        )
        db.add(deployment)
        await db.commit()
        return deployment.id


async def discover(engine: AsyncEngine, cli: DockerCli, *roots: Path) -> StackDiscovery:
    """Run the discovery with the given allowed roots."""
    async with create_session_factory(engine)() as db:
        return await discover_stacks(db, cli, AllowedPaths(roots))


def by_name(found: StackDiscovery) -> dict[str, DetectedStack]:
    """The stacks by their name."""
    return {stack.name: stack for stack in found.stacks}


async def test_the_folder_source_finds_stacks_two_levels_down_and_skips_the_rest(
    engine: AsyncEngine, host: Path, fake: FakeDocker, tmp_path: Path
) -> None:
    make_stack(host)
    make_stack(host / "api")
    make_stack(host / "group" / "web", "docker-compose.yml", "docker-compose.override.yml")
    make_stack(host / "group" / "deeper" / "too-deep")
    make_stack(host / ".hidden")
    make_stack(host / "only-override", "compose.override.yaml")
    outside = make_stack(tmp_path / "elsewhere")
    (host / "linked").symlink_to(outside)
    found = await discover(engine, fake.cli(), host)
    folders = {stack.folder: stack for stack in found.stacks}
    assert set(folders) == {str(host), str(host / "api"), str(host / "group" / "web")}
    assert folders[str(host / "group" / "web")].compose_files == (
        "docker-compose.yml",
        "docker-compose.override.yml",
    )
    assert folders[str(host / "api")].compose_files == ("compose.yaml",)
    for stack in found.stacks:
        assert stack.source is StackSource.FOLDER and stack.usable and stack.reason is None
        assert stack.project is None and stack.running is None and stack.total is None
        assert stack.used_by is None


async def test_one_base_file_and_its_own_override_are_chosen_when_a_folder_has_several(
    engine: AsyncEngine, host: Path, fake: FakeDocker
) -> None:
    make_stack(
        host / "mixed",
        "compose.yml",
        "compose.yaml",
        "docker-compose.yaml",
        "compose.override.yml",
        "docker-compose.override.yaml",
    )
    found = await discover(engine, fake.cli(), host)
    assert found.stacks[0].compose_files == ("compose.yaml", "compose.override.yml")


async def test_without_a_usable_docker_the_folder_source_stands_alone_with_the_reason(
    engine: AsyncEngine, host: Path, fake: FakeDocker
) -> None:
    make_stack(host / "api")
    found = await discover(engine, fake.cli(mounted=False), host)
    assert found.docker_reason == NOT_MOUNTED_REASON
    assert [stack.name for stack in found.stacks] == ["api"]
    assert not (fake.folder / "calls").exists()


async def test_a_daemon_that_does_not_answer_is_the_reason_and_asks_nothing_more(
    engine: AsyncEngine, host: Path, fake: FakeDocker
) -> None:
    make_stack(host / "api")
    fake.switch("no-daemon")
    found = await discover(engine, fake.cli(), host)
    assert found.docker_reason == DAEMON_UNREACHABLE_REASON
    assert [stack.name for stack in found.stacks] == ["api"]
    assert fake.calls() == []


async def test_a_missing_docker_command_is_the_reason(
    engine: AsyncEngine, host: Path, fake: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_stack(host / "api")
    monkeypatch.setattr(shutil, "which", lambda *args, **kwargs: None)
    cli = DockerCli(socket_path=fake.cli().socket_path, scratch=fake.folder / "scratch")
    found = await discover(engine, cli, host)
    assert found.docker_reason == CLI_MISSING_REASON
    assert [stack.name for stack in found.stacks] == ["api"]


async def test_the_status_is_probed_fresh_and_both_docker_reads_are_made(
    engine: AsyncEngine, host: Path, fake: FakeDocker
) -> None:
    cli = fake.cli()
    await cli.status()
    await discover(engine, cli, host)
    assert fake.probes() == 2
    assert [call.arguments[:2] for call in fake.calls()] == [("compose", "ls"), ("ps", "--all")]
    assert fake.calls()[0].arguments == ("compose", "ls", "--all", "--format", "json")


async def test_a_project_found_by_docker_and_in_a_folder_is_one_stack_with_dockers_files(
    engine: AsyncEngine, host: Path, fake: FakeDocker
) -> None:
    folder = make_stack(host / "shop", "compose.yaml", "compose.override.yaml", "prod.yaml")
    answer(
        fake,
        [
            project(
                "shop",
                [folder / "compose.yaml", folder / "prod.yaml"],
                "running(2), exited(1)",
            )
        ],
        {"shop": str(folder)},
    )
    found = await discover(engine, fake.cli(), host)
    assert found.docker_reason is None
    (stack,) = found.stacks
    assert stack == DetectedStack(
        project="shop",
        folder=str(folder),
        compose_files=("compose.yaml", "prod.yaml"),
        running=2,
        total=3,
        source=StackSource.BOTH,
        usable=True,
    )


async def test_a_project_below_the_walk_depth_is_found_by_docker_alone(
    engine: AsyncEngine, host: Path, fake: FakeDocker
) -> None:
    folder = make_stack(host / "a" / "b" / "c" / "deep")
    answer(fake, [project("deep", [folder / "compose.yaml"], "exited(1)")], {"deep": str(folder)})
    found = await discover(engine, fake.cli(), host)
    (stack,) = found.stacks
    assert stack.source is StackSource.DOCKER and stack.usable
    assert (stack.running, stack.total) == (0, 1)
    assert stack.compose_files == ("compose.yaml",)


async def test_without_the_label_the_folder_of_the_first_compose_file_is_the_project_folder(
    engine: AsyncEngine, host: Path, fake: FakeDocker
) -> None:
    folder = make_stack(host / "a" / "b" / "c" / "deep", "base.yml", "extra.yml")
    answer(fake, [project("deep", [folder / "base.yml", folder / "extra.yml"])])
    found = await discover(engine, fake.cli(), host)
    (stack,) = found.stacks
    assert stack.folder == str(folder) and stack.compose_files == ("base.yml", "extra.yml")


async def test_a_failing_label_lookup_falls_back_to_the_compose_file_folder(
    engine: AsyncEngine, host: Path, fake: FakeDocker
) -> None:
    folder = make_stack(host / "shop")
    answer(fake, [project("shop", [folder / "compose.yaml"])], labels_code=1)
    found = await discover(engine, fake.cli(), host)
    assert found.docker_reason is None
    (stack,) = found.stacks
    assert stack.folder == str(folder) and stack.source is StackSource.BOTH


async def test_the_label_lookup_selects_the_two_labels_and_never_the_environment(
    engine: AsyncEngine, host: Path, fake: FakeDocker
) -> None:
    folder = make_stack(host / "shop")
    answer(fake, [project("shop", [folder / "compose.yaml"])])
    await discover(engine, fake.cli(), host)
    arguments = fake.calls()[1].arguments
    template = arguments[arguments.index("--format") + 1]
    assert 'Label "com.docker.compose.project"' in template
    assert 'Label "com.docker.compose.project.working_dir"' in template
    assert "Env" not in template and "Config" not in template and ".Labels" not in template
    assert "label=com.docker.compose.project" in arguments


async def test_a_project_outside_the_allowed_paths_is_kept_but_not_usable(
    engine: AsyncEngine, host: Path, fake: FakeDocker, tmp_path: Path
) -> None:
    elsewhere = make_stack(tmp_path.resolve() / "elsewhere")
    answer(
        fake,
        [project("away", [elsewhere / "compose.yaml"], "running(1)")],
        {"away": str(elsewhere)},
    )
    found = await discover(engine, fake.cli(), host)
    (stack,) = found.stacks
    assert (stack.usable, stack.reason) == (False, OUTSIDE_REASON)
    assert stack.folder == str(elsewhere) and stack.compose_files == ()
    assert stack.source is StackSource.DOCKER and (stack.running, stack.total) == (1, 1)


async def test_a_label_that_is_a_symbolic_link_out_of_the_allowed_paths_is_not_usable(
    engine: AsyncEngine, host: Path, fake: FakeDocker, tmp_path: Path
) -> None:
    elsewhere = make_stack(tmp_path.resolve() / "elsewhere")
    (host / "shortcut").symlink_to(elsewhere)
    answer(
        fake,
        [project("away", [host / "shortcut" / "compose.yaml"])],
        {"away": str(host / "shortcut")},
    )
    found = await discover(engine, fake.cli(), host)
    (stack,) = found.stacks
    assert (stack.usable, stack.reason) == (False, OUTSIDE_REASON)
    assert stack.folder == str(host / "shortcut") and stack.compose_files == ()


async def test_compose_files_that_are_not_found_in_the_folder_make_it_not_usable(
    engine: AsyncEngine, host: Path, fake: FakeDocker
) -> None:
    folder = host / "gone"
    folder.mkdir()
    other = make_stack(host / "other")
    answer(
        fake,
        [
            project("gone", [folder / "compose.yaml"]),
            project("stray", [other / "compose.yaml", host / "elsewhere.yaml"], "running(1)"),
        ],
        {"gone": str(folder), "stray": str(other)},
    )
    found = await discover(engine, fake.cli(), host)
    stacks = by_name(found)
    assert (stacks["gone"].usable, stacks["gone"].reason) == (False, NO_FILES_REASON)
    assert stacks["gone"].compose_files == ()
    assert stacks["stray"].source is StackSource.BOTH
    assert stacks["stray"].compose_files == ("compose.yaml",) and stacks["stray"].usable


async def test_more_compose_files_than_can_be_linked_make_the_stack_not_usable(
    engine: AsyncEngine, host: Path, fake: FakeDocker
) -> None:
    names = [f"part{number}.yaml" for number in range(9)]
    folder = make_stack(host / "big", *names)
    answer(fake, [project("big", [folder / name for name in names])], {"big": str(folder)})
    found = await discover(engine, fake.cli(), host)
    (stack,) = found.stacks
    assert (stack.usable, stack.reason) == (False, TOO_MANY_FILES_REASON)
    assert stack.compose_files == tuple(names[:8])


async def test_a_stack_is_marked_used_by_its_root_or_by_its_project_name(
    engine: AsyncEngine, host: Path, fake: FakeDocker, tmp_path: Path
) -> None:
    by_root = make_stack(host / "by-root")
    by_project = make_stack(host / "by-project")
    free = make_stack(host / "free")
    first = await add_deployment(engine, "First", by_root)
    await add_deployment(engine, "Second", by_root)
    named = await add_deployment(engine, "Named", tmp_path / "somewhere-else", "by-project-stack")
    link = host / "via-link"
    link.symlink_to(free)
    answer(
        fake,
        [project("by-project-stack", [by_project / "compose.yaml"])],
        {"by-project-stack": str(by_project)},
    )
    found = await discover(engine, fake.cli(), host)
    stacks = {Path(stack.folder).name: stack for stack in found.stacks}
    assert stacks["by-root"].used_by == ExistingDeployment(first, "First")
    assert stacks["by-project"].used_by == ExistingDeployment(named, "Named")
    assert stacks["free"].used_by is None
    await add_deployment(engine, "Linked", link)
    again = await discover(engine, fake.cli(), host)
    assert {Path(s.folder).name: s for s in again.stacks}["free"].used_by is not None


async def test_usable_stacks_come_first_and_then_by_name(
    engine: AsyncEngine, host: Path, fake: FakeDocker, tmp_path: Path
) -> None:
    make_stack(host / "zeta")
    make_stack(host / "Alpha")
    away = make_stack(tmp_path.resolve() / "elsewhere")
    answer(fake, [project("aaa-away", [away / "compose.yaml"])], {"aaa-away": str(away)})
    found = await discover(engine, fake.cli(), host)
    assert [stack.name for stack in found.stacks] == ["Alpha", "zeta", "aaa-away"]


async def test_the_walk_stops_at_the_folder_cap_of_each_root(
    engine: AsyncEngine, host: Path, fake: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("a", "b", "c", "d", "e"):
        make_stack(host / name)
    monkeypatch.setattr(stack_discovery, "MAX_FOLDERS", 3)
    found = await discover(engine, fake.cli(), host)
    assert sorted(stack.name for stack in found.stacks) == ["a", "b"]


async def test_a_failing_compose_ls_gives_the_fixed_sentence_and_keeps_the_folder_source(
    engine: AsyncEngine, host: Path, fake: FakeDocker
) -> None:
    make_stack(host / "api")
    answer(fake, "", ls_code=2, ls_error="secret detail the user must not see")
    found = await discover(engine, fake.cli(), host)
    assert found.docker_reason == FAILED_MESSAGE
    assert [stack.name for stack in found.stacks] == ["api"]


@pytest.mark.parametrize("text", ["not json", "{}", '"text"', "", "[1, 2"])
async def test_an_answer_that_is_not_a_list_of_projects_is_reported_and_costs_nothing_else(
    engine: AsyncEngine, host: Path, fake: FakeDocker, text: str
) -> None:
    make_stack(host / "api")
    answer(fake, text)
    found = await discover(engine, fake.cli(), host)
    assert found.docker_reason == UNREADABLE_REASON
    assert [stack.name for stack in found.stacks] == ["api"]


async def test_malformed_entries_are_skipped_and_the_good_ones_kept(
    engine: AsyncEngine, host: Path, fake: FakeDocker
) -> None:
    folder = make_stack(host / "good")
    config = str(folder / "compose.yaml")
    answer(
        fake,
        [
            "text",
            None,
            {"Name": 5, "ConfigFiles": config},
            {"Name": "Bad Name", "ConfigFiles": config},
            {"Name": "nofiles", "ConfigFiles": ""},
            {"Name": "nofiles2"},
            {"Name": "relative", "ConfigFiles": "compose.yaml"},
            {"Name": "good", "ConfigFiles": config, "Status": "weird text"},
        ],
        {"good": str(folder)},
    )
    found = await discover(engine, fake.cli(), host)
    assert found.docker_reason is None
    assert [stack.name for stack in found.stacks] == ["good"]
    assert (found.stacks[0].running, found.stacks[0].total) == (None, None)


def test_the_status_text_gives_running_and_total_counts() -> None:
    text = json.dumps(
        [
            {"Name": "a", "ConfigFiles": "/x/c.yaml", "Status": "running(2), exited(1)"},
            {"Name": "b", "ConfigFiles": "/x/c.yaml", "Status": "exited(3)"},
            {"Name": "c", "ConfigFiles": "/x/c.yaml", "Status": "created(1), paused(1)"},
            {"Name": "d", "ConfigFiles": "/x/c.yaml", "Status": 7},
        ]
    )
    projects = parse_projects(text)
    assert projects is not None
    assert [(item.name, item.running, item.total) for item in projects] == [
        ("a", 2, 3),
        ("b", 0, 3),
        ("c", 0, 2),
        ("d", None, None),
    ]


def test_the_comma_separated_config_files_become_a_list() -> None:
    projects = parse_projects(
        json.dumps([{"Name": "a", "ConfigFiles": "/x/a.yaml, /x/b.yaml,,", "Status": ""}])
    )
    assert projects is not None and projects[0].config_files == ("/x/a.yaml", "/x/b.yaml")


def test_the_label_lines_keep_the_first_folder_of_each_project_and_skip_bad_lines() -> None:
    text = "\n".join(
        [
            json.dumps({"project": "a", "folder": "/x/one"}),
            json.dumps({"project": "a", "folder": "/x/two"}),
            json.dumps({"project": "b", "folder": ""}),
            "not json",
            json.dumps(["list"]),
            json.dumps({"project": 3, "folder": "/x"}),
            json.dumps({"project": "c", "folder": "/x/three"}),
        ]
    )
    assert parse_working_dirs(text) == {"a": "/x/one", "c": "/x/three"}
