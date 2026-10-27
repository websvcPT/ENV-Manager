"""The Compose project name rule."""

import pytest

from app.services.compose_project import MAX_COMPOSE_PROJECT_LENGTH, is_compose_project_name


@pytest.mark.parametrize(
    "name",
    [
        "web",
        "env-manager-demo",
        "my_stack",
        "9lives",
        "a",
        "a-b_c9",
        "x" * MAX_COMPOSE_PROJECT_LENGTH,
    ],
)
def test_names_that_follow_the_compose_rule_are_accepted(name: str) -> None:
    assert is_compose_project_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "",
        "Web",
        "my stack",
        "-web",
        "_web",
        "web.prod",
        "web/prod",
        "wëb",
        "web\n",
        "x" * (MAX_COMPOSE_PROJECT_LENGTH + 1),
    ],
)
def test_names_that_break_the_compose_rule_are_refused(name: str) -> None:
    assert not is_compose_project_name(name)
