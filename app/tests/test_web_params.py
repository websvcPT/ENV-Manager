"""Whole numbers read from query strings and form fields."""

import pytest

from app.web.params import MAX_NUMBER_DIGITS, plain_number, plain_number_text


@pytest.mark.parametrize(
    ("value", "number"),
    [
        ("1", 1),
        ("42", 42),
        ("007", 7),
        ("00005", 5),
        ("9" * MAX_NUMBER_DIGITS, int("9" * MAX_NUMBER_DIGITS)),
        ("٣", 3),
        ("0", None),
        ("000", None),
        ("", None),
        ("-1", None),
        ("+1", None),
        ("1.5", None),
        (" 1", None),
        ("1 ", None),
        ("1e3", None),
        ("<b>", None),
        ("9" * (MAX_NUMBER_DIGITS + 1), None),
        (None, None),
        (7, None),
        (b"7", None),
    ],
)
def test_a_plain_number_is_a_positive_whole_number_of_decimal_digits(
    value: object, number: int | None
) -> None:
    assert plain_number(value) == number


@pytest.mark.parametrize(
    ("value", "text"),
    [
        ("1", "1"),
        ("0", "0"),
        ("007", "007"),
        ("9" * MAX_NUMBER_DIGITS, "9" * MAX_NUMBER_DIGITS),
        ("", ""),
        ("-1", ""),
        ("1.5", ""),
        (" 1", ""),
        ("<b>", ""),
        ("9" * (MAX_NUMBER_DIGITS + 1), ""),
        (None, ""),
        (7, ""),
    ],
)
def test_the_number_text_is_returned_as_written_or_empty(value: object, text: str) -> None:
    assert plain_number_text(value) == text
