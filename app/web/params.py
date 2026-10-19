"""Whole numbers read from query strings and form fields.

Text from a request is never trusted to be a number: these helpers accept only plain decimal
digits, short enough that the number can never be an absurdly large id.
"""

MAX_NUMBER_DIGITS = 9


def plain_number_text(value: object) -> str:
    """The value when it is text made of at most nine decimal digits, else an empty string.

    The digits are returned as written: a toast that echoes a version number from the address
    shows exactly what was sent, and anything that is not a plain number shows nothing.
    """
    if isinstance(value, str) and value.isdecimal() and len(value) <= MAX_NUMBER_DIGITS:
        return value
    return ""


def plain_number(value: object) -> int | None:
    """The value as a positive whole number, None for anything that is not one."""
    text = plain_number_text(value)
    if text and int(text) > 0:
        return int(text)
    return None
