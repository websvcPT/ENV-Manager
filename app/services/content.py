"""Text rules shared by every path that stores environment content.

Pasted text, raw edits and key/value edits are normalized to LF line breaks; every source
(including uploads and imports, which keep their bytes otherwise) passes the same size and
binary checks. A key/value edit normalizes the stored text before patching it, so the same
resulting text stores the same content whichever editor produced it.
"""

# Well below the CSRF form body bound and the multipart text part bound, even when a browser
# sends every line break as two bytes.
MAX_CONTENT_BYTES = 256 * 1024


def normalize_newlines(text: str) -> str:
    """The text with every line break as a single LF, as browsers send CRLF."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def content_problem(content: str) -> str | None:
    """The reason a text cannot be stored as content, None when it can."""
    if "\x00" in content:
        return "Binary content is not supported."
    if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
        return f"Content is larger than {MAX_CONTENT_BYTES // 1024} KiB."
    return None
