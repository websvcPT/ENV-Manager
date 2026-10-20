"""What the type of a file (ENV file or certificate) decides, kept in one place.

An ENV file is KEY=VALUE text handled variable by variable. A certificate is PEM text (a
certificate, a key or a chain) handled as one value: it has no dist file, it is compared line by
line, and the post-deploy output scrubber hides its base64 lines. The rules here are shared by
the add and settings forms, the editor, history, drift, the dist check and the scrubber.
"""

import re

from app.models import FileType

# The add form selects Certificate by itself while a name with one of these endings is typed.
CERT_NAME_SUFFIXES = (".pem", ".crt", ".cer", ".key")
NOT_PEM_MESSAGE = (
    "A certificate must hold at least one PEM block, from -----BEGIN ...----- to -----END ...-----."
)
CERT_HAS_NO_DIST_MESSAGE = "A certificate has no dist file."
CERT_KEY_VALUE_MESSAGE = "A certificate is edited as raw text."
CHOOSE_TYPE_MESSAGE = "Choose the type of the file."
TYPE_FIXED_MESSAGE = (
    "The type of a file cannot be changed. Delete the file and create it again with the other type."
)

_PEM_BLOCK = re.compile(r"-----BEGIN ([A-Z0-9 ]+)-----(.*?)-----END \1-----", re.DOTALL)
_BASE64_LINE = re.compile(r"[A-Za-z0-9+/=]+")


def parse_file_type(raw: str, default: FileType) -> FileType | None:
    """The type a form value names, ``default`` for an empty one and None for an unknown one."""
    text = raw.strip()
    if not text:
        return default
    try:
        return FileType(text)
    except ValueError:
        return None


def pem_problem(text: str) -> str | None:
    """The reason a text cannot be a certificate file, None when it holds a PEM block.

    The check is light on purpose: a block needs matching BEGIN and END lines, and text around
    the blocks (a bag of attributes, a comment) is allowed.
    """
    return None if _PEM_BLOCK.search(text) else NOT_PEM_MESSAGE


def content_problem_for(file_type: FileType, text: str) -> str | None:
    """The reason a text cannot be stored as a new version of a file of this type."""
    return pem_problem(text) if file_type is FileType.CERT else None


def pem_body_lines(text: str) -> list[str]:
    """The base64 body of each PEM block of the text, one string per block, lines joined by LF.

    Header lines inside a block (``Proc-Type`` and ``DEK-Info`` of an encrypted key) are not part
    of the body. The BEGIN and END lines are not secret and are left out too.
    """
    bodies: list[str] = []
    for block in _PEM_BLOCK.finditer(text):
        lines = [line.strip() for line in block.group(2).splitlines()]
        body = [line for line in lines if line and _BASE64_LINE.fullmatch(line)]
        if body:
            bodies.append("\n".join(body))
    return bodies


def line_values(text: str) -> dict[str, str]:
    """The lines of a text under the names ``Line 1``, ``Line 2``, ... in file order.

    History and drift compare a certificate through these names in the place of variable names,
    so a differing line is reported by its position and never by its content.
    """
    return {f"Line {number}": line for number, line in enumerate(text.splitlines(), start=1)}
