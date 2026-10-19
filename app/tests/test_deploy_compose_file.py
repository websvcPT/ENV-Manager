"""The production compose example keeps the keys the deployment guide names."""

import re
from pathlib import Path

import pytest

from app.config import Settings

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "deploy" / "docker-compose.yaml"
ENV_EXAMPLE = ROOT / "deploy" / ".env.example"
GUIDE = ROOT / "docs" / "deployment.md"

ACTIVE_LINES = [
    "  envmanager:",
    "    restart: unless-stopped",
    "    env_file: .env",
    "      ENVMANAGER_KEY_FILE: /run/keys/master.key",
    "      - ./data/runtime:/data",
    "      - ./data/master.key:/run/keys/master.key:ro",
    "      - /var/www/vhosts:/var/www/vhosts",
    "      - /opt:/opt",
    "      - 127.0.0.1:8080:8080",
]
COMMENTED_LINES = [
    "      # - /var/run/docker.sock:/var/run/docker.sock",
    '    # group_add: ["<docker group id>"]',
]
GUIDE_NAMES = [
    "deploy/docker-compose.yaml",
    "deploy/.env.example",
    "ENVMANAGER_KEY_FILE",
    "ENVMANAGER_BASE_URL",
    "ENVMANAGER_CSRF_SECRET",
    "ENVMANAGER_TRUSTED_PROXIES",
    "/run/keys/master.key",
    "/var/www/vhosts",
    "/opt",
    "/var/run/docker.sock",
    "group_add",
    "data/runtime",
    "127.0.0.1",
]
ENV_NAME = re.compile(r"^(?:# )?(ENVMANAGER_[A-Z_]+)=", re.MULTILINE)


def compose_lines() -> list[str]:
    """The lines of the compose example."""
    return COMPOSE.read_text(encoding="utf-8").splitlines()


def guide() -> str:
    """The Deployment section of the deployment guide."""
    text = GUIDE.read_text(encoding="utf-8")
    return text.split("\n## Deployment\n", 1)[1].split("\n## ", 1)[0]


@pytest.mark.parametrize("line", ACTIVE_LINES)
def test_the_example_sets_what_the_guide_describes(line: str) -> None:
    assert line in compose_lines()


@pytest.mark.parametrize("line", COMMENTED_LINES)
def test_the_optional_settings_are_present_and_switched_off(line: str) -> None:
    assert line in compose_lines()
    uncommented = line.replace("# ", "", 1)
    assert uncommented not in compose_lines()


def test_the_example_has_one_service_and_no_other_top_level_section() -> None:
    top_level = [line for line in compose_lines() if line and line[0] not in " #"]
    assert top_level == ["services:"]
    services = [
        line for line in compose_lines() if line.startswith("  ") and not line.startswith("   ")
    ]
    assert [line for line in services if not line.strip().startswith("#")] == ["  envmanager:"]


def test_the_example_does_not_run_as_another_user() -> None:
    assert not any(line.strip().startswith("user:") for line in compose_lines())


@pytest.mark.parametrize("name", GUIDE_NAMES)
def test_the_deployment_guide_names_every_part_of_the_example(name: str) -> None:
    assert name in guide()


def test_the_example_uses_no_secret_value() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    assert "ENVMANAGER_KEY:" not in text
    assert "CSRF" not in text


def test_the_example_reads_its_settings_from_the_env_file_next_to_it() -> None:
    environment = [line for line in compose_lines() if line.startswith("      ENVMANAGER_")]
    assert environment == ["      ENVMANAGER_KEY_FILE: /run/keys/master.key"]


def test_the_env_example_names_only_settings_the_application_has() -> None:
    names = ENV_NAME.findall(ENV_EXAMPLE.read_text(encoding="utf-8"))
    known = {f"ENVMANAGER_{field.upper()}" for field in Settings.model_fields}
    assert names
    assert set(names) <= known


def test_the_env_example_sets_the_required_values_and_leaves_the_rest_off() -> None:
    active = [
        line.split("=", 1)[0]
        for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
        if line.startswith("ENVMANAGER_")
    ]
    assert active == ["ENVMANAGER_BASE_URL", "ENVMANAGER_CSRF_SECRET"]


def test_the_env_example_leaves_out_what_the_layout_or_development_owns() -> None:
    names = ENV_NAME.findall(ENV_EXAMPLE.read_text(encoding="utf-8"))
    for name in (
        "ENVMANAGER_KEY",
        "ENVMANAGER_KEY_FILE",
        "ENVMANAGER_DATA_PATH",
        "ENVMANAGER_TITLE",
        "ENVMANAGER_DESCRIPTION",
        "ENVMANAGER_SERVE_DOCS",
        "ENVMANAGER_DEMO_ROOT",
        "ENVMANAGER_LOGIN_IP_MAX_FAILURES",
    ):
        assert name not in names
