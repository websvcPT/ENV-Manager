"""Parsing, validation and placeholder substitution of post-deploy command lines."""

import pytest

from app.services.hook_commands import (
    MAX_COMMANDS,
    MAX_LINE_LENGTH,
    CommandError,
    PlaceholderValues,
    normalize_commands,
    parse_commands,
    substitute,
)

VALUES = PlaceholderValues.validated("/srv/deployments/stack", "stack", "api")


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


def test_the_number_of_commands_is_limited() -> None:
    assert len(parse_commands("\n".join(["docker ps"] * MAX_COMMANDS))) == MAX_COMMANDS
    with pytest.raises(CommandError, match="at most"):
        parse_commands("\n".join(["docker ps"] * (MAX_COMMANDS + 1)))


def test_the_stored_form_drops_blank_lines_and_padding() -> None:
    assert normalize_commands("  docker ps  \r\n\r\n\tdocker compose up -d\n\n") == (
        "docker ps\ndocker compose up -d"
    )
    assert normalize_commands(" \n ") == ""


def test_placeholders_are_replaced_inside_words_and_never_change_their_number() -> None:
    (command,) = parse_commands(
        "docker compose -f {deployment_path}/compose.yaml -p {deployment_name} up {service_name}"
    )
    assert substitute(command, VALUES) == (
        "docker",
        "compose",
        "-f",
        "/srv/deployments/stack/compose.yaml",
        "-p",
        "stack",
        "up",
        "api",
    )


def test_a_path_with_spaces_stays_one_argument() -> None:
    values = PlaceholderValues.validated("/srv/my deployments/stack one", "stack", "api")
    (command,) = parse_commands("docker compose --project-directory {deployment_path} up")
    assert substitute(command, values)[3] == "/srv/my deployments/stack one"
    assert len(substitute(command, values)) == len(command)


def test_values_are_not_scanned_for_placeholders_again() -> None:
    values = PlaceholderValues.validated("/srv/{service_name}/{deployment_name}", "stack", "api")
    (command,) = parse_commands("docker run {deployment_path} {service_name}")
    assert substitute(command, values) == (
        "docker",
        "run",
        "/srv/{service_name}/{deployment_name}",
        "api",
    )


def test_text_that_only_looks_like_a_placeholder_is_left_alone() -> None:
    (command,) = parse_commands("docker ps --format {{.Names}} --filter name={servce_name}")
    result = substitute(command, VALUES)
    assert result[3] == "{{.Names}}"
    assert result[5] == "name={servce_name}"


@pytest.mark.parametrize(
    ("path", "deployment", "service"),
    [
        ("relative/path", "stack", "api"),
        ("", "stack", "api"),
        ("/srv/stack\nx", "stack", "api"),
        ("/srv/stack\x00", "stack", "api"),
        ("/srv/stack", "-rf", "api"),
        ("/srv/stack", "stack", "--all"),
        ("/srv/stack", "st ack", "api"),
        ("/srv/stack", "stack", "api;rm"),
        ("/srv/stack", "stack", ""),
        ("/srv/stack", "stack", "a" * 5000),
    ],
)
def test_a_value_that_could_become_an_option_or_extra_argument_is_refused(
    path: str, deployment: str, service: str
) -> None:
    with pytest.raises(CommandError):
        PlaceholderValues.validated(path, deployment, service)
