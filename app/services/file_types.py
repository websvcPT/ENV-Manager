"""What the type of a file (ENV file or certificate) decides, kept in one place.

An ENV file is KEY=VALUE text handled variable by variable. A certificate is PEM text (a
certificate, a key or a chain) handled as one value: it has no dist file, it is compared line by
line, and the post-deploy output scrubber hides its base64 lines. The rules here are shared by
the add and settings forms, the editor, history, drift, the dist check and the scrubber.
"""

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

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


def line_name(number: int) -> str:
    """The row name of a line, ``Line 3``: history and drift compare a certificate through these
    names in the place of variable names, so a differing line is reported by its number and never
    by its content."""
    return f"Line {number}"


@dataclass(frozen=True)
class AlignedLine:
    """One step of the alignment of two texts: the line of each side, None where a side has none.

    The numbers are 1-based line numbers of that side.
    """

    left: str | None
    right: str | None
    left_number: int | None
    right_number: int | None


@dataclass(frozen=True)
class LineAlignment:
    """How the lines of a text sit against a reference text.

    ``matched`` has one entry per reference line: the line of the other text aligned with it, or
    None. ``inserted`` holds the lines of the other text that sit before the reference line of
    that index (before the end of the reference for the index of its line count).
    """

    matched: list[str | None]
    inserted: dict[int, list[str]]


def align_text_lines(reference: str, other: str) -> LineAlignment:
    """Align the lines of ``other`` with those of ``reference`` the way ``difflib`` does.

    A line added in the middle shows as one inserted line and the lines after it stay matched,
    where a comparison by position would report every later line as changed. In a block of
    different lines the first lines of each side are paired as changed lines, and the rest of
    the longer side is removed or inserted.
    """
    reference_lines = reference.splitlines()
    other_lines = other.splitlines()
    matcher = SequenceMatcher(None, reference_lines, other_lines, autojunk=False)
    matched: list[str | None] = [None] * len(reference_lines)
    inserted: dict[int, list[str]] = {}
    for tag, start, end, other_start, other_end in matcher.get_opcodes():
        paired = min(end - start, other_end - other_start) if tag != "insert" else 0
        for offset in range(paired):
            matched[start + offset] = other_lines[other_start + offset]
        rest = other_lines[other_start + paired : other_end]
        if rest:
            inserted.setdefault(end if tag == "replace" else start, []).extend(rest)
    return LineAlignment(matched, inserted)


def aligned_lines(left: str, right: str) -> list[AlignedLine]:
    """Every line of two texts in order, paired where ``difflib`` aligns them."""
    reference_lines = left.splitlines()
    alignment = align_text_lines(left, right)
    steps: list[AlignedLine] = []
    right_number = 0
    for index in range(len(reference_lines) + 1):
        for line in alignment.inserted.get(index, []):
            right_number += 1
            steps.append(AlignedLine(None, line, None, right_number))
        if index == len(reference_lines):
            break
        left_number = index + 1
        other = alignment.matched[index]
        if other is not None:
            right_number += 1
        steps.append(
            AlignedLine(
                reference_lines[index],
                other,
                left_number,
                right_number if other is not None else None,
            )
        )
    return steps
