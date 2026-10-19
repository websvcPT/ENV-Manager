"""In-place edits of env text: untouched source, quoting that reads back, duplicates."""

import pytest

from app.services.env_parser import parse_env
from app.services.env_text import (
    EnvEditError,
    VariableChanges,
    apply_changes,
    format_assignment,
)

SOURCE = (
    "# Database\n"
    "\n"
    "export HOST=db  # inline\n"
    "PORT=3306\n"
    'CERT="-----BEGIN-----\nline two\n-----END-----"\n'
    "\n"
    "# trailing comment\n"
)

AWKWARD_VALUES = [
    "plain",
    "",
    "two words",
    "has # hash",
    'has "double" quotes',
    "has 'single' quotes",
    "back\\slash and \\n literal",
    "line one\nline two\n",
    "price is $5 and ${HOME}",
    "mix ' and \\ and \" and $",
    "it's ${HOME} and $HOME and $$",
    "ends with a backslash\\",
    "\\u00e9 and \\x41 and \\0 as text",
    "crlf\r\ninside",
    "  padded  ",
    "a=b=c",
    "unicode café",
]


def changes(
    values: dict[str, str] | None = None, remove: set[str] | None = None
) -> VariableChanges:
    """Edits with optional values and removals."""
    return VariableChanges(values=values or {}, remove=remove or set())


@pytest.mark.parametrize("value", AWKWARD_VALUES)
def test_every_awkward_value_reads_back_exactly(value: str) -> None:
    result = apply_changes(SOURCE, changes({"PORT": value, "NEW_KEY": value}))
    parsed = parse_env(result).variables
    assert parsed["PORT"].value == value
    assert parsed["NEW_KEY"].value == value


@pytest.mark.parametrize("value", AWKWARD_VALUES)
def test_format_assignment_reads_back_exactly(value: str) -> None:
    text = format_assignment("KEY", value)
    assert text.endswith("\n")
    assert parse_env(text).variables["KEY"].value == value


def test_only_the_changed_statement_is_rewritten() -> None:
    result = apply_changes(SOURCE, changes({"PORT": "5432"}))
    assert result == SOURCE.replace("PORT=3306", "PORT=5432")


def test_an_unchanged_value_leaves_the_text_untouched() -> None:
    assert apply_changes(SOURCE, changes({"PORT": "3306", "HOST": "db"})) == SOURCE
    multiline = "-----BEGIN-----\nline two\n-----END-----"
    assert apply_changes(SOURCE, changes({"CERT": multiline})) == SOURCE


def test_export_prefix_comments_and_blank_lines_are_kept() -> None:
    result = apply_changes(SOURCE, changes({"HOST": "other host"}))
    assert result.startswith("# Database\n\nexport HOST='other host'\nPORT=3306\n")
    assert result.endswith("\n\n# trailing comment\n")


def test_a_statement_after_blank_lines_keeps_them() -> None:
    result = apply_changes("A=1\n\n\nB=2\n", changes({"B": "3"}))
    assert result == "A=1\n\n\nB=3\n"


def test_new_variables_are_appended_after_a_missing_final_newline() -> None:
    assert apply_changes("A=1", changes({"B": "2"})) == "A=1\nB=2\n"
    assert apply_changes("", changes({"B": "2"})) == "B=2\n"


def test_removal_drops_every_assignment_and_keeps_the_rest() -> None:
    result = apply_changes("A=1\n# note\nB=2\nA=3\n", changes(remove={"A"}))
    assert result == "# note\nB=2\n"


def test_the_last_assignment_of_a_repeated_key_is_the_one_changed() -> None:
    result = apply_changes("A=1\nB=2\nA=3\n", changes({"A": "9"}))
    assert result == "A=1\nB=2\nA=9\n"
    assert parse_env(result).variables["A"].value == "9"


def test_a_bare_key_gets_an_assignment() -> None:
    assert apply_changes("A\nB=2\n", changes({"A": "1"})) == "A=1\nB=2\n"


def test_unreadable_lines_are_preserved() -> None:
    source = "A=1\nthis is not valid\nB=2\n"
    assert apply_changes(source, changes({"B": "3"})) == "A=1\nthis is not valid\nB=3\n"


def test_a_value_that_cannot_read_back_is_refused() -> None:
    with pytest.raises(EnvEditError):
        apply_changes("A=1\n", changes({"A": "x"}, {"A"}))


def test_a_variable_with_a_compose_key_name_is_edited_in_place() -> None:
    source = "a.b=1\nA[1]=2\n\u00dcBER=3\n"
    result = apply_changes(source, changes({"a.b": "x y", "\u00dcBER": "9"}))
    assert result == "a.b='x y'\nA[1]=2\n\u00dcBER=9\n"


def test_a_variable_with_a_reference_is_untouched_unless_its_text_changes() -> None:
    source = "A=${HOST}:80\nB=2\n"
    assert apply_changes(source, changes({"A": "${HOST}:80", "B": "3"})) == ("A=${HOST}:80\nB=3\n")
    result = apply_changes(source, changes({"A": "${HOST}:81"}))
    assert result == "A='${HOST}:81'\nB=2\n"
    assert parse_env(result).variables["A"].value == "${HOST}:81"
    assert not parse_env(result).variables["A"].interpolated


def test_a_value_with_a_dollar_and_a_quote_is_written_without_interpolation() -> None:
    text = format_assignment("KEY", "it's ${HOME}")
    assert text == 'KEY="it\'s \\${HOME}"\n'
    parsed = parse_env(text).variables["KEY"]
    assert parsed.value == "it's ${HOME}"
    assert not parsed.interpolated


def test_a_file_that_uses_crlf_keeps_its_blank_lines_when_a_variable_is_removed() -> None:
    result = apply_changes("A=1\r\n\r\nB=2\r\n", changes(remove={"B"}))
    assert result == "A=1\r\n\r\n"
