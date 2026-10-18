"""Tests for env parsing, on fixtures shaped like real Compose env files."""

import pytest

from app.services.env_parser import parse_env

PEM = (
    "-----BEGIN PRIVATE KEY-----\n"
    "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7\n"
    "abc+/def==\n"
    "-----END PRIVATE KEY-----"
)

COMPOSE_STYLE_FILE = (
    "# Database settings\n"
    "\n"
    "DB_HOST=db\n"
    'DB_PASSWORD="p@ss # not a comment"\n'
    "DB_PORT=3306 # inline comment\n"
    "export APP_ENV=prod\n"
    "GREETING='hello world'\n"
    "EMPTY=\n"
    'EMPTY_QUOTED=""\n'
    f'TLS_KEY="{PEM}"\n'
    "LAST=1\n"
)


def values(text: str) -> dict[str, str]:
    """Parsed values by key."""
    return {key: variable.value for key, variable in parse_env(text).variables.items()}


def test_compose_style_file_parses() -> None:
    parsed = parse_env(COMPOSE_STYLE_FILE)
    assert parsed.is_env_format
    assert parsed.problem_lines == ()
    assert parsed.keys == (
        "DB_HOST",
        "DB_PASSWORD",
        "DB_PORT",
        "APP_ENV",
        "GREETING",
        "EMPTY",
        "EMPTY_QUOTED",
        "TLS_KEY",
        "LAST",
    )
    result = {key: variable.value for key, variable in parsed.variables.items()}
    assert result["DB_PASSWORD"] == "p@ss # not a comment"
    assert result["DB_PORT"] == "3306"
    assert result["APP_ENV"] == "prod"
    assert result["GREETING"] == "hello world"
    assert result["TLS_KEY"] == PEM
    assert result["LAST"] == "1"
    assert parsed.empty_keys == ("EMPTY", "EMPTY_QUOTED")


def test_unquoted_hash_without_preceding_space_is_part_of_the_value() -> None:
    assert values("A=1 # comment\nB=2#kept\n") == {"A": "1", "B": "2#kept"}


def test_quoted_hash_is_part_of_the_value() -> None:
    assert values("A=\"x # y\"\nB='x # y'\n") == {"A": "x # y", "B": "x # y"}


def test_export_prefix_is_ignored() -> None:
    parsed = parse_env('export A=1\nexport  B="two"\n')
    assert values('export A=1\nexport  B="two"\n') == {"A": "1", "B": "two"}
    assert parsed.keys == ("A", "B")


def test_escaped_double_quote_inside_double_quotes() -> None:
    assert values('A="say \\"hi\\""\n') == {"A": 'say "hi"'}


def test_double_quoted_escape_sequences_are_decoded() -> None:
    assert values('A="line1\\nline2\\ttab"\n') == {"A": "line1\nline2\ttab"}


def test_multiline_quoted_value_keeps_its_lines() -> None:
    text = f'KEY="{PEM}"\nAFTER=1\n'
    assert values(text) == {"KEY": PEM, "AFTER": "1"}


def test_empty_values_are_reported() -> None:
    parsed = parse_env("A=\nB=\"\"\nC='x'\nD=\n")
    assert parsed.empty_keys == ("A", "B", "D")
    assert all(parsed.variables[key].assigned for key in ("A", "B", "D"))


def test_bare_key_is_unassigned_and_empty() -> None:
    parsed = parse_env("A=1\nBARE\n")
    assert parsed.is_env_format
    assert parsed.variables["BARE"].assigned is False
    assert parsed.variables["BARE"].empty
    assert parsed.empty_keys == ("BARE",)


def test_duplicate_keys_keep_first_position_and_last_value() -> None:
    parsed = parse_env("A=1\nB=2\nA=3\nA=4\n")
    assert parsed.keys == ("A", "B")
    assert parsed.variables["A"].value == "4"
    assert parsed.variables["A"].line == 4
    assert parsed.duplicate_keys == ("A",)


def test_crlf_line_endings_leave_no_carriage_returns() -> None:
    text = 'A=1\r\nB="two"\r\n# comment\r\nC=3 # inline\r\nD=\r\n'
    assert values(text) == {"A": "1", "B": "two", "C": "3", "D": ""}


def test_crlf_inside_a_quoted_multiline_value_is_kept() -> None:
    parsed = parse_env('KEY="a\r\nb"\r\nNEXT=1\r\n')
    assert parsed.variables["KEY"].value == "a\r\nb"
    assert parsed.variables["NEXT"].value == "1"


def test_empty_and_comment_only_files_are_env_format_without_variables() -> None:
    for text in ("", "\n\n", "# only a comment\n"):
        parsed = parse_env(text)
        assert parsed.is_env_format
        assert parsed.variables == {}


@pytest.mark.parametrize(
    "text",
    [
        "-----BEGIN CERTIFICATE-----\nMIIBabc+/==\n-----END CERTIFICATE-----\n",
        PEM + "\n",
        "just some words\n",
        "hello\n",
        "<html>\n<body>x</body>\n</html>\n",
    ],
)
def test_text_that_is_not_env_format_is_detected(text: str) -> None:
    assert parse_env(text).is_env_format is False


def test_unclosed_quote_is_a_problem_on_its_line() -> None:
    parsed = parse_env('A=1\nB="unclosed\nC=3\n')
    assert parsed.is_env_format is False
    assert parsed.problem_lines == (2,)
    assert "C" in parsed.variables


def test_invalid_key_names_are_problems() -> None:
    parsed = parse_env("OK=1\n1BAD=2\nA B=3\n")
    assert parsed.is_env_format is False
    assert 2 in parsed.problem_lines


def test_problem_report_holds_line_numbers_only() -> None:
    parsed = parse_env('SECRET="never shown\nOTHER=1\n')
    assert "never shown" not in repr(parsed.problem_lines)


def test_interpolation_is_not_performed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FROM_PROCESS", "leaked")
    assert values("A=${FROM_PROCESS}\nB=$FROM_PROCESS\nC=${UNSET:-fallback}\n") == {
        "A": "${FROM_PROCESS}",
        "B": "$FROM_PROCESS",
        "C": "${UNSET:-fallback}",
    }


def test_spaces_around_equals_are_accepted() -> None:
    assert values("A = 1\nB= 2\nC =3\n") == {"A": "1", "B": "2", "C": "3"}


def test_single_quoted_escapes_differ_from_compose() -> None:
    # python-dotenv treats \' and \\ as escapes inside single quotes and keeps \n literal.
    # Compose takes single quoted text literally, so a backslash is never an escape there.
    assert values("A='it\\'s'\nB='a\\\\b'\nC='x\\ny'\n") == {
        "A": "it's",
        "B": "a\\b",
        "C": "x\\ny",
    }


def test_line_numbers_skip_the_blank_lines_before_a_variable() -> None:
    parsed = parse_env("A=1\n\n\nB=2\n# comment\n\nC=3\n")
    assert [parsed.variables[key].line for key in ("A", "B", "C")] == [1, 4, 7]


def test_problem_lines_skip_the_blank_lines_before_the_problem() -> None:
    parsed = parse_env("A=1\n\n\nBAD KEY=2\n\nB=3\nC='open\n")
    assert parsed.problem_lines == (4, 7)
