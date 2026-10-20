"""The file type: the rules of the module, the model and what the seed marks as a certificate."""

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.db import create_session_factory
from app.models import Environment, FileType
from app.seeds import SeedData
from app.services.file_types import (
    CERT_NAME_SUFFIXES,
    NOT_PEM_MESSAGE,
    content_problem_for,
    line_values,
    parse_file_type,
    pem_body_lines,
    pem_problem,
)

PEM = "-----BEGIN CERTIFICATE-----\nQUJD\n-----END CERTIFICATE-----\n"


def test_the_types_have_their_stored_values_and_labels() -> None:
    assert [(item.value, item.label) for item in FileType] == [
        ("env", "ENV file"),
        ("cert", "Certificate"),
    ]
    assert CERT_NAME_SUFFIXES == (".pem", ".crt", ".cer", ".key")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("", FileType.ENV), ("  ", FileType.ENV), ("env", FileType.ENV), (" cert ", FileType.CERT)],
)
def test_a_form_value_names_a_type_or_the_default(raw: str, expected: FileType) -> None:
    assert parse_file_type(raw, FileType.ENV) is expected


def test_an_empty_value_keeps_the_given_default_and_an_unknown_one_is_refused() -> None:
    assert parse_file_type("", FileType.CERT) is FileType.CERT
    for unknown in ("ENV", "certificate", "pem", "x"):
        assert parse_file_type(unknown, FileType.ENV) is None


@pytest.mark.parametrize(
    "text_value",
    [
        PEM,
        "Bag Attributes\n" + PEM + "trailing words\n",
        "-----BEGIN RSA PRIVATE KEY-----\nQUJD\n-----END RSA PRIVATE KEY-----",
        PEM + "-----BEGIN CERTIFICATE-----\nREVG\n-----END CERTIFICATE-----\n",
        "-----BEGIN CERTIFICATE-----\n-----END CERTIFICATE-----",
    ],
)
def test_a_text_with_a_pem_block_passes_the_light_check(text_value: str) -> None:
    assert pem_problem(text_value) is None
    assert content_problem_for(FileType.CERT, text_value) is None


@pytest.mark.parametrize(
    "text_value",
    [
        "",
        "A=1\n",
        "-----BEGIN CERTIFICATE-----\nQUJD\n",
        "QUJD\n-----END CERTIFICATE-----\n",
        "-----BEGIN CERTIFICATE-----\nQUJD\n-----END PRIVATE KEY-----\n",
        "----BEGIN CERTIFICATE----\nQUJD\n----END CERTIFICATE----\n",
    ],
)
def test_a_text_without_a_complete_pem_block_is_refused(text_value: str) -> None:
    assert pem_problem(text_value) == NOT_PEM_MESSAGE
    assert content_problem_for(FileType.CERT, text_value) == NOT_PEM_MESSAGE


def test_an_env_file_has_no_content_rule_of_its_own() -> None:
    assert content_problem_for(FileType.ENV, "anything\n") is None
    assert content_problem_for(FileType.ENV, "") is None


def test_the_body_lines_are_the_base64_lines_inside_each_block() -> None:
    assert pem_body_lines(PEM) == ["QUJD"]
    text_value = (
        "outside\n-----BEGIN A-----\nQUJD\nREVG\n-----END A-----\n"
        "-----BEGIN B-----\nProc-Type: 4,ENCRYPTED\n\nR0hJ\n-----END B-----\n"
    )
    assert pem_body_lines(text_value) == ["QUJD\nREVG", "R0hJ"]
    assert pem_body_lines("no block") == []
    assert pem_body_lines("-----BEGIN A-----\n-----END A-----") == []


def test_lines_are_named_by_position_and_a_final_line_break_adds_none() -> None:
    assert line_values("a\nb\n") == {"Line 1": "a", "Line 2": "b"}
    assert line_values("a\r\nb") == {"Line 1": "a", "Line 2": "b"}
    assert line_values("a\n\nb") == {"Line 1": "a", "Line 2": "", "Line 3": "b"}
    assert line_values("") == {}


async def test_the_seed_marks_its_certificates_and_keeps_every_other_file_an_env_file(
    seeded_database: AsyncEngine, seed_data: SeedData
) -> None:
    async with create_session_factory(seeded_database)() as db:
        rows = await db.execute(select(Environment.name, Environment.file_type))
        types = dict(rows.all())
    assert {name for name, file_type in types.items() if file_type is FileType.CERT} == {
        "web-ca.pem"
    }
    assert seed_data.figures.ca.file_type is FileType.CERT
    assert seed_data.envs.db.file_type is FileType.ENV


async def test_a_new_environment_is_an_env_file_unless_told_otherwise(
    db: AsyncSession, seed_data: SeedData
) -> None:
    environment = Environment(
        service_id=seed_data.services.mysql.id, name="plain", target_path="environments/plain"
    )
    db.add(environment)
    await db.commit()
    assert environment.file_type is FileType.ENV
    stored = (
        await db.execute(text("SELECT file_type FROM environments WHERE name = 'plain'"))
    ).scalar_one()
    assert stored == "env"


async def test_the_database_refuses_a_type_it_does_not_know(
    db: AsyncSession, seed_data: SeedData
) -> None:
    with pytest.raises(IntegrityError):
        await db.execute(
            text(
                "INSERT INTO environments (service_id, name, target_path, file_type,"
                " created_at, updated_at) VALUES (:service, 'odd', 'odd', 'binary',"
                " '2026-01-01', '2026-01-01')"
            ),
            {"service": seed_data.services.mysql.id},
        )
    await db.rollback()
