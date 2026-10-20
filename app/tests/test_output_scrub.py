"""Removing configuration values from command output: matching, limits and truncation edges."""

import pytest
from sqlalchemy import select

from app.models import EnvironmentVersion, FileType
from app.services.hook_process import OUTPUT_PART_BYTES, BoundedOutput
from app.services.output_scrub import MIN_VALUE_LENGTH, OutputScrubber
from app.services.scrub_values import load_scrubber, secret_texts
from app.services.versions import create_version
from app.tests.deploy_helpers import Harness


def test_every_occurrence_of_a_value_is_replaced() -> None:
    scrubber = OutputScrubber(["hunter2-secret"])
    assert scrubber.scrub_text("a hunter2-secret b hunter2-secret") == "a **** b ****"
    assert scrubber.scrub_bytes(b"x hunter2-secret") == b"x ****"


def test_the_longest_value_wins_when_one_value_contains_another() -> None:
    scrubber = OutputScrubber(["pass", "password-1234", "word-1234"])
    assert scrubber.scrub_text("password-1234") == "****"
    assert scrubber.scrub_text("pass and word-1234") == "**** and ****"


def test_a_replacement_is_not_scanned_again() -> None:
    scrubber = OutputScrubber(["abcd", "****"])
    assert scrubber.scrub_text("abcd") == "****"


def test_values_shorter_than_four_characters_are_not_scrubbed() -> None:
    """The documented limit: short values match inside unrelated text, so they stay visible."""
    assert MIN_VALUE_LENGTH == 4
    scrubber = OutputScrubber(["4", "db", "abc", "port"])
    printed = 'DEMO_DB_POOL: "4" host db abc port'
    assert scrubber.scrub_text(printed) == 'DEMO_DB_POOL: "4" host db abc ****'
    assert not OutputScrubber(["4", "db", "abc"])


def test_a_scrubber_without_values_changes_nothing() -> None:
    scrubber = OutputScrubber()
    assert not scrubber
    assert scrubber.scrub_text("anything") == "anything"
    assert scrubber.scrub_head(b"anything") == b"anything"
    assert scrubber.scrub_tail(b"anything") == b"anything"


def test_the_value_as_compose_prints_it_is_replaced() -> None:
    scrubber = OutputScrubber(["pa$$word-1"])
    assert scrubber.scrub_text("A: pa$$$$word-1 and pa$$word-1") == "A: **** and ****"


def test_a_multiline_value_is_replaced_whole_escaped_and_line_by_line() -> None:
    certificate = "-----BEGIN KEY-----\nQUJDREVGR0g=\nSUpLTE1OT1A=\n-----END KEY-----"
    scrubber = OutputScrubber([certificate])
    assert scrubber.scrub_text(f"k: {certificate}") == "k: ****"
    assert scrubber.scrub_text("k: " + certificate.replace("\n", "\\n")) == "k: ****"
    printed = "    QUJDREVGR0g=\n    SUpLTE1OT1A=\n"
    assert scrubber.scrub_text(printed) == "    ****\n    ****\n"


def test_non_ascii_values_are_replaced() -> None:
    scrubber = OutputScrubber(["pässwörd-ü"])
    assert scrubber.scrub_text("x pässwörd-ü y") == "x **** y"


def test_the_representation_never_shows_a_value() -> None:
    scrubber = OutputScrubber(["very-secret-value"])
    assert "very-secret-value" not in repr(scrubber)
    assert repr(scrubber) == "OutputScrubber(1 values)"


def test_a_value_cut_off_at_the_end_of_the_kept_start_is_replaced() -> None:
    scrubber = OutputScrubber(["seed-secret"])
    assert scrubber.scrub_head(b"line\nDB=seed-s") == b"line\nDB=****"
    assert scrubber.scrub_head(b"line\nDB=seed") == b"line\nDB=****"
    assert scrubber.scrub_head(b"line\nDB=see") == b"line\nDB=see"
    assert scrubber.scrub_head(b"complete seed-secret then seed-") == b"complete **** then ****"


def test_a_value_cut_off_at_the_start_of_the_kept_end_is_replaced() -> None:
    scrubber = OutputScrubber(["seed-secret"])
    assert scrubber.scrub_tail(b"-secret\nnext") == b"****\nnext"
    assert scrubber.scrub_tail(b"cret\nnext") == b"****\nnext"
    assert scrubber.scrub_tail(b"ret\nnext") == b"ret\nnext"


def test_output_that_fits_is_scrubbed_across_the_join_of_its_two_parts() -> None:
    output = BoundedOutput(limit=8)
    output.add(b"xx seed-secret")
    scrubber = OutputScrubber(["seed-secret"])
    assert output.text() == "xx seed-secret"
    assert output.text(scrubber) == "xx ****"


def test_a_value_split_by_the_cut_of_long_output_leaves_no_piece_of_it() -> None:
    scrubber = OutputScrubber(["seed-secret"])
    head = b"x" * (OUTPUT_PART_BYTES - 6) + b"seed-s"
    tail = b"ecret" + b"y" * (OUTPUT_PART_BYTES - 5)
    output = BoundedOutput()
    output.add(head + b"z" * 100 + tail)
    plain = output.text()
    assert plain.count("seed-s") == 1 and "ecret" in plain
    scrubbed = output.text(scrubber)
    assert "seed" not in scrubbed and "ecret" not in scrubbed
    assert scrubbed.startswith("x" * (OUTPUT_PART_BYTES - 6) + "****\n[... 100 bytes omitted")
    assert "bytes omitted" in scrubbed and scrubbed.endswith("y" * (OUTPUT_PART_BYTES - 5))


def test_a_multibyte_character_cut_by_the_end_does_not_hide_a_piece() -> None:
    scrubber = OutputScrubber(["pässwörd-ü"])
    data = "xx pässwörd-ü".encode()[:-1]
    assert scrubber.scrub_head(data) == b"xx ****"


def test_a_value_is_scrubbed_in_the_middle_of_long_output_parts() -> None:
    output = BoundedOutput()
    output.add(b"start seed-secret " + b"a" * 20000 + b" seed-secret end")
    scrubbed = output.text(OutputScrubber(["seed-secret"]))
    assert scrubbed.startswith("start **** aaa") and scrubbed.endswith("aaa **** end")
    assert "seed-secret" not in scrubbed


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("A=one-value\nB=two-value\n", ["one-value", "two-value"]),
        ("A=\nB=x y\n", ["", "x y"]),
        ("# only a comment with seed-secret\n", []),
        (
            "-----BEGIN CERTIFICATE-----\nQUJD\n-----END CERTIFICATE-----\n",
            ["-----BEGIN CERTIFICATE-----\nQUJD\n-----END CERTIFICATE-----"],
        ),
    ],
)
def test_a_file_contributes_its_variable_values_or_its_whole_text(
    text: str, expected: list[str]
) -> None:
    assert secret_texts(text) == expected


async def newest_version_id(h: Harness, environment_id: int) -> int:
    """The id of the newest version of an environment."""
    return (
        await h.db.execute(
            select(EnvironmentVersion.id)
            .where(EnvironmentVersion.environment_id == environment_id)
            .order_by(EnvironmentVersion.number.desc())
            .limit(1)
        )
    ).scalar_one()


async def test_the_deployment_scrubber_holds_the_newest_value_of_every_file(
    harness: Harness,
) -> None:
    """All files of the deployment count, also those a caller has no grant for."""
    h = harness
    scrubber = await load_scrubber(h.db, h.crypto, h.seed.deployments.fancy_prod.id)
    assert scrubber.scrub_text("p=3306 s=seed-secret") == "p=**** s=****"
    assert scrubber.scrub_text("host db") == "host db"
    other = await load_scrubber(h.db, h.crypto, h.seed.deployments.billing_main.id)
    assert other.scrub_text("s=seed-secret k=seed-key") == "s=seed-secret k=****"


async def test_a_written_older_version_adds_its_values(harness: Harness) -> None:
    h = harness
    deployment_id = h.seed.deployments.fancy_prod.id
    author = h.seed.users.superadmin
    older = await create_version(h.db, h.crypto, h.seed.envs.db.id, "P=4406\n", author=author)
    await create_version(h.db, h.crypto, h.seed.envs.db.id, "P=5507\n", author=author)
    await h.db.commit()
    current = await load_scrubber(h.db, h.crypto, deployment_id)
    with_written = await load_scrubber(h.db, h.crypto, deployment_id, written=[older, None])
    assert current.scrub_text("P=4406 P=5507") == "P=4406 P=****"
    assert with_written.scrub_text("P=4406 P=5507") == "P=**** P=****"


async def test_a_version_that_cannot_be_read_is_skipped(harness: Harness) -> None:
    h = harness
    unreadable = await h.db.get(
        EnvironmentVersion, await newest_version_id(h, h.seed.envs.db_secrets.id)
    )
    assert unreadable is not None
    unreadable.content_encrypted = "not a ciphertext"
    await h.db.commit()
    scrubber = await load_scrubber(h.db, h.crypto, h.seed.deployments.fancy_prod.id)
    assert scrubber.scrub_text("s=seed-secret p=3306") == "s=seed-secret p=****"


CERTIFICATE_TEXT = (
    "Bag Attributes\n"
    "-----BEGIN CERTIFICATE-----\n"
    "MIIBkTCB+wIJAKHBfpegPjMCMA0GCSqGSIb3DQEBCwUAMBExDzANBgNVBAMMBnNl\n"
    "cnZlcjAeFw0yNDAxMDEwMDAwMDBaFw0zNDAxMDEwMDAwMDBaMBExDzANBgNVBAMM\n"
    "AQ==\n"
    "-----END CERTIFICATE-----\n"
)


def test_a_certificate_contributes_the_base64_body_of_each_pem_block() -> None:
    assert secret_texts(CERTIFICATE_TEXT, FileType.CERT) == [
        "MIIBkTCB+wIJAKHBfpegPjMCMA0GCSqGSIb3DQEBCwUAMBExDzANBgNVBAMMBnNl\n"
        "cnZlcjAeFw0yNDAxMDEwMDAwMDBaFw0zNDAxMDEwMDAwMDBaMBExDzANBgNVBAMM\n"
        "AQ=="
    ]
    two_blocks = (
        "-----BEGIN KEY-----\nQUJDRA==\n-----END KEY-----\n-----BEGIN X-----\nRUZH\n-----END X-----"
    )
    assert secret_texts(two_blocks, FileType.CERT) == ["QUJDRA==", "RUZH"]


def test_an_encrypted_key_header_is_not_part_of_the_body() -> None:
    text = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "Proc-Type: 4,ENCRYPTED\n"
        "DEK-Info: AES-128-CBC,0123456789ABCDEF\n"
        "\n"
        "c2VjcmV0S2V5Qm9keQ==\n"
        "-----END RSA PRIVATE KEY-----\n"
    )
    assert secret_texts(text, FileType.CERT) == ["c2VjcmV0S2V5Qm9keQ=="]


def test_a_certificate_without_a_pem_block_falls_back_to_its_whole_text() -> None:
    assert secret_texts("A=1\nB=two-value\n", FileType.CERT) == ["A=1\nB=two-value"]
    assert secret_texts("A=1\nB=two-value\n") == ["1", "two-value"]


def test_an_env_file_keeps_its_rules_whatever_it_holds() -> None:
    assert secret_texts(CERTIFICATE_TEXT) == [CERTIFICATE_TEXT.strip()]
    assert secret_texts(CERTIFICATE_TEXT, FileType.ENV) == [CERTIFICATE_TEXT.strip()]


async def test_the_scrubber_hides_each_base64_line_of_a_certificate_but_not_its_markers(
    harness: Harness,
) -> None:
    h = harness
    deployment_id = h.seed.deployments.fancy_staging.id
    author = h.seed.users.superadmin
    await create_version(h.db, h.crypto, h.seed.figures.ca.id, CERTIFICATE_TEXT, author=author)
    await h.db.commit()
    scrubber = await load_scrubber(h.db, h.crypto, deployment_id)
    printed = (
        "-----BEGIN CERTIFICATE-----\n"
        "cnZlcjAeFw0yNDAxMDEwMDAwMDBaFw0zNDAxMDEwMDAwMDBaMBExDzANBgNVBAMM\n"
        "-----END CERTIFICATE-----\n"
        "as one value: MIIBkTCB+wIJAKHBfpegPjMCMA0GCSqGSIb3DQEBCwUAMBExDzANBgNVBAMMBnNl\\n"
        "cnZlcjAeFw0yNDAxMDEwMDAwMDBaFw0zNDAxMDEwMDAwMDBaMBExDzANBgNVBAMM\\nAQ=="
    )
    scrubbed = scrubber.scrub_text(printed)
    assert "cnZlcjAeFw0y" not in scrubbed and "MIIBkTCB" not in scrubbed and "AQ==" not in scrubbed
    assert scrubbed.startswith("-----BEGIN CERTIFICATE-----\n****\n-----END CERTIFICATE-----\n")


async def test_the_type_of_the_file_decides_how_an_older_written_version_is_read(
    harness: Harness,
) -> None:
    h = harness
    deployment_id = h.seed.deployments.fancy_staging.id
    author = h.seed.users.superadmin
    older = await create_version(
        h.db, h.crypto, h.seed.figures.ca.id, CERTIFICATE_TEXT, author=author
    )
    await create_version(
        h.db,
        h.crypto,
        h.seed.figures.ca.id,
        "-----BEGIN CERTIFICATE-----\nbmV3Ym9keQ==\n-----END CERTIFICATE-----\n",
        author=author,
    )
    await h.db.commit()
    scrubber = await load_scrubber(h.db, h.crypto, deployment_id, written=[older])
    second_line = "cnZlcjAeFw0yNDAxMDEwMDAwMDBaFw0zNDAxMDEwMDAwMDBaMBExDzANBgNVBAMM"
    assert scrubber.scrub_text(second_line) == "****"
    assert scrubber.scrub_text("bmV3Ym9keQ==") == "****"
