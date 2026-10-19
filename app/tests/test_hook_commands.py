"""Parsing, validation and the safety net of post-deploy command lines."""

import pytest

from app.services.hook_commands import (
    MAX_COMMANDS,
    MAX_LINE_LENGTH,
    CommandError,
    normalize_commands,
    parse_commands,
)


def test_one_command_per_line_is_split_into_words_without_a_shell() -> None:
    text = "docker compose rm -sf api\n\ndocker compose up -d --no-deps api\n"
    assert parse_commands(text) == (
        ("docker", "compose", "rm", "-sf", "api"),
        ("docker", "compose", "up", "-d", "--no-deps", "api"),
    )


def test_quotes_group_words_and_operators_inside_quotes_are_plain_text() -> None:
    (command,) = parse_commands("docker run --name 'a b' alpine sh -c \"echo x | tr x y; ls\"")
    assert command[3:] == ("a b", "alpine", "sh", "-c", "echo x | tr x y; ls")


def test_a_hash_is_text_not_a_comment_and_a_docker_template_is_one_word() -> None:
    (command,) = parse_commands("docker ps --filter name=#1 --format {{.Names}}")
    assert command[-3:] == ("name=#1", "--format", "{{.Names}}")


def test_text_in_braces_is_passed_on_exactly_as_typed() -> None:
    """Nothing is substituted: a name that was once a placeholder is just text."""
    (command,) = parse_commands(
        "docker compose -f {deployment_path}/compose.yaml -p {deployment_name} up {service_name}"
    )
    assert command[3:] == (
        "{deployment_path}/compose.yaml",
        "-p",
        "{deployment_name}",
        "up",
        "{service_name}",
    )


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("podman ps", "Line 1: the first word must be docker."),
        ("sudo docker ps", "Line 1: the first word must be docker."),
        ("/usr/bin/docker ps", "Line 1: the first word must be docker."),
        ("DOCKER_HOST=x docker ps", "Line 1: the first word must be docker."),
        ("Docker ps", "Line 1: the first word must be docker."),
        ("# docker ps", "Line 1: the first word must be docker."),
        ("docker ps\nls -l", "Line 2: the first word must be docker."),
        ("docker", "Line 1: say what docker should do."),
        ("docker ps | grep x", "Line 1: shell operators are not supported."),
        ("docker ps && docker ps", "Line 1: shell operators are not supported."),
        ("docker ps || true", "Line 1: shell operators are not supported."),
        ("docker ps; docker ps", "Line 1: shell operators are not supported."),
        ("docker ps & ", "Line 1: shell operators are not supported."),
        ("docker ps > out", "Line 1: shell operators are not supported."),
        ("docker ps >> out", "Line 1: shell operators are not supported."),
        ("docker ps < in", "Line 1: shell operators are not supported."),
        ("docker ps 2>&1", "Line 1: shell operators are not supported."),
        ("docker ps $(id)", "Line 1: shell operators are not supported."),
        ("docker ps\n\ndocker ps 'x", "Line 3: a quote is not closed."),
        ("docker ps\x00x", "Line 1 contains a control character."),
        ("docker ps\x0bdocker ps", "Line 1 contains a control character."),
        ("docker ps " + "x" * MAX_LINE_LENGTH, f"Line 1 is longer than {MAX_LINE_LENGTH}"),
    ],
)
def test_a_line_that_is_not_one_plain_docker_command_is_refused(text: str, message: str) -> None:
    with pytest.raises(CommandError) as raised:
        parse_commands(text)
    assert str(raised.value).startswith(message)


def test_the_message_names_the_line_never_its_text() -> None:
    with pytest.raises(CommandError) as raised:
        parse_commands("docker ps\nsecret-tool --password hunter2")
    assert "hunter2" not in str(raised.value) and "secret-tool" not in str(raised.value)


def test_the_limits_are_ten_commands_of_five_hundred_characters() -> None:
    assert (MAX_COMMANDS, MAX_LINE_LENGTH) == (10, 500)


def test_the_number_of_commands_is_limited() -> None:
    assert len(parse_commands("\n".join(["docker ps"] * MAX_COMMANDS))) == MAX_COMMANDS
    with pytest.raises(CommandError, match="Use at most 10 commands"):
        parse_commands("\n".join(["docker ps"] * (MAX_COMMANDS + 1)))


def test_a_line_of_exactly_the_limit_is_accepted_and_one_more_character_is_not() -> None:
    padding = "x" * (MAX_LINE_LENGTH - len("docker ps "))
    assert len(parse_commands(f"docker ps {padding}")[0]) == 3
    with pytest.raises(CommandError, match="longer than 500 characters"):
        parse_commands(f"docker ps {padding}x")


def test_the_stored_form_drops_blank_lines_and_padding() -> None:
    assert normalize_commands("  docker ps  \r\n\r\n\tdocker compose up -d\n\n") == (
        "docker ps\ndocker compose up -d"
    )
    assert normalize_commands(" \n ") == ""


@pytest.mark.parametrize(
    "line",
    [
        "docker compose down",
        "docker compose down -v",
        "docker compose down --volumes --remove-orphans",
        "docker compose -f a.yml down",
        "docker compose -f a.yml -p stack --profile x down",
        "docker compose --project-name=stack down",
        "docker compose --project-directory /srv/stack down",
        "docker --context remote compose down",
        "docker -H tcp://host:2375 compose down",
        "docker 'compose' \"do\"wn",
    ],
)
def test_compose_down_is_refused_however_it_is_spelled(line: str) -> None:
    with pytest.raises(CommandError) as raised:
        parse_commands(line)
    message = str(raised.value)
    assert message.startswith("Line 1:") and "down" in message
    assert "rm -sf" in message and "up -d" in message


@pytest.mark.parametrize(
    "line",
    [
        "docker system prune",
        "docker system prune -af --volumes",
        "docker image prune",
        "docker image prune -a",
        "docker volume prune",
        "docker container prune -f",
        "docker network prune",
        "docker builder prune",
        "docker buildx prune",
        "docker --context remote buildx --builder b prune",
        "docker 'sys'tem 'pr'une",
    ],
)
def test_every_prune_is_refused(line: str) -> None:
    with pytest.raises(CommandError, match=r"Line 1: prune commands are not allowed\."):
        parse_commands(line)


@pytest.mark.parametrize(
    "line",
    [
        "docker compose rm -v api",
        "docker compose rm -sfv api",
        "docker compose rm -vf api",
        "docker compose rm -s -f -v api",
        "docker compose rm --volumes api",
        "docker compose rm --volumes=true api",
        "docker compose rm -v=true api",
        "docker rm -v web",
        "docker rm -fv web",
        "docker rm --force --volumes web",
        "docker container rm -v web",
        "docker container rm --volumes web",
        "docker -c remote rm -v web",
        "docker run --rm alpine rm -v x",
    ],
)
def test_the_volume_options_of_rm_are_refused(line: str) -> None:
    with pytest.raises(CommandError) as raised:
        parse_commands(line)
    assert str(raised.value).startswith("Line 1: the -v and --volumes options of rm")


@pytest.mark.parametrize(
    "line",
    [
        "docker compose rm -sf api",
        "docker compose rm -s -f api",
        "docker compose up -d --no-deps --force-recreate api",
        "docker compose up -V -d api",
        "docker compose run --rm api migrate",
        "docker compose run --rm -v ./data:/data api migrate",
        "docker run --rm -v data:/data alpine ls /data",
        "docker run --rm alpine rm -f /tmp/x",
        "docker rm web",
        "docker rm -f web",
        "docker volume rm old-data",
        "docker volume ls",
        "docker compose logs --tail 20 api",
        "docker compose restart api",
        "docker compose ps --format {{.Name}}",
        "docker ps --filter name=prunes",
        "docker ps --format {{.Names}}",
    ],
)
def test_other_commands_are_not_touched_by_the_safety_net(line: str) -> None:
    assert parse_commands(line)


def test_a_refusal_names_the_line_of_the_typed_text_and_never_the_command() -> None:
    with pytest.raises(CommandError) as raised:
        parse_commands("docker ps\n\ndocker compose down --secret-flag hunter2")
    message = str(raised.value)
    assert message.startswith("Line 3:")
    assert "hunter2" not in message and "secret-flag" not in message
