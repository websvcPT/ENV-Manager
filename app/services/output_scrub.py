"""Removing known configuration values from the output of a command.

A post-deploy command can print values it was never meant to show: ``docker compose config``
prints every value of the files its Compose project loads, and a container started by the command
may echo its own environment. Before that output is stored or shown, ``OutputScrubber`` replaces
each occurrence of a known value with ``****``. Matching is exact and runs on bytes, longest
value first, in one pass, so a short value inside a longer one never leaves the rest of the long
one behind and a replacement is never scanned again.

Besides the value itself these forms are recognized: the value as Compose prints it (``$``
doubled), a multi-line value with its line breaks written as ``\\n``, and each line of a
multi-line value (a certificate printed line by line). A piece of a value that was cut by the
end of the kept output (the first and last kilobytes of a long output are all that is stored) is
replaced as well, for pieces of at least ``MIN_PARTIAL_LENGTH`` bytes.

Known limits, on purpose: values shorter than ``MIN_VALUE_LENGTH`` characters are not scrubbed
(they would match inside unrelated words and numbers, so a one-character setting such as a pool
size stays visible), and a value that the command re-encodes (quotes or backslashes escaped,
base64, hashed, split over several lines by the command) is not recognized.
"""

import re
from collections.abc import Iterable, Iterator

REPLACEMENT = "****"
MIN_VALUE_LENGTH = 4
MIN_PARTIAL_LENGTH = 4

_REPLACEMENT_BYTES = REPLACEMENT.encode("ascii")


def _variants(value: str) -> Iterator[str]:
    """The forms of a value that can appear in output: itself, as Compose prints it, per line."""
    yield value
    if "$" in value:
        yield value.replace("$", "$$")
    if "\n" in value or "\r" in value:
        yield "\\n".join(value.splitlines())
        for line in value.splitlines():
            stripped = line.strip()
            if stripped:
                yield stripped


class OutputScrubber:
    """Replaces a fixed set of values in text. An empty scrubber changes nothing.

    The values are never part of the object's representation, so logging or printing a scrubber
    (or a request that holds one) does not leak them.
    """

    def __init__(self, values: Iterable[str] = ()) -> None:
        """Take the values to remove; those shorter than ``MIN_VALUE_LENGTH`` are ignored."""
        needles = {
            variant.encode("utf-8")
            for value in values
            for variant in _variants(value)
            if len(variant) >= MIN_VALUE_LENGTH
        }
        self._needles = tuple(sorted(needles, key=lambda needle: (-len(needle), needle)))
        self._pattern = (
            re.compile(b"|".join(re.escape(needle) for needle in self._needles))
            if self._needles
            else None
        )

    def __repr__(self) -> str:
        """The number of values only."""
        return f"OutputScrubber({len(self._needles)} values)"

    def __bool__(self) -> bool:
        """Whether there is anything to scrub."""
        return self._pattern is not None

    def scrub_bytes(self, data: bytes) -> bytes:
        """The data with every occurrence of a known value replaced."""
        if self._pattern is None:
            return data
        return self._pattern.sub(_REPLACEMENT_BYTES, data)

    def scrub_text(self, text: str) -> str:
        """The text with every occurrence of a known value replaced."""
        if self._pattern is None:
            return text
        data = text.encode("utf-8", errors="replace")
        return self.scrub_bytes(data).decode("utf-8", errors="replace")

    def scrub_head(self, data: bytes) -> bytes:
        """Scrub the kept start of a longer stream: a value cut off by its end is replaced too."""
        data = self.scrub_bytes(data)
        piece = self._longest_cut(data, at_end=True)
        return data[: len(data) - piece] + _REPLACEMENT_BYTES if piece else data

    def scrub_tail(self, data: bytes) -> bytes:
        """Scrub the kept end of a longer stream: a value cut off at its start is replaced too."""
        data = self.scrub_bytes(data)
        piece = self._longest_cut(data, at_end=False)
        return _REPLACEMENT_BYTES + data[piece:] if piece else data

    def _longest_cut(self, data: bytes, *, at_end: bool) -> int:
        """Length of the longest piece at the end (or start) of ``data`` that is part of a value.

        At the end the piece is a proper start of a value, at the start a proper end of one; it
        is at least ``MIN_PARTIAL_LENGTH`` bytes long, and 0 when there is none.
        """
        best = 0
        for needle in self._needles:
            longest = min(len(needle) - 1, len(data))
            for size in range(longest, max(best, MIN_PARTIAL_LENGTH - 1), -1):
                if at_end and data.endswith(needle[:size]):
                    best = size
                    break
                if not at_end and data.startswith(needle[len(needle) - size :]):
                    best = size
                    break
        return best
