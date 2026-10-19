"""Env files with what the real ``docker compose config`` printed for them.

Each case is one ``env_file`` and the environment Compose gave the service, captured from
``docker compose config --format json`` on a host where none of the variables named in the files
is set. ``compose`` is None when Compose refused the file. ``interpolated`` lists the keys whose
Compose value came from a reference (the parser keeps the template and flags it) and ``bare`` the
keys written without ``=`` (Compose leaves them out when the host has no such variable). The
real scenario of the demo suite runs every case through the real Compose again and compares.
"""

from dataclasses import dataclass


def lines(*items: str) -> str:
    """The items as the lines of a file, each with a line break."""
    return "\n".join(items) + "\n"


@dataclass(frozen=True)
class ComposeCase:
    """One env file and the environment Compose printed for it."""

    name: str
    text: str
    compose: dict[str, str] | None
    interpolated: tuple[str, ...] = ()
    bare: tuple[str, ...] = ()


CASES: tuple[ComposeCase, ...] = (
    ComposeCase(
        "single-quoted-literal",
        lines(
            "A='x # y'",
            "B='a b'",
            r"C='x\ny'",
            "D='$HOME'",
            "E='${UNSET_VARIABLE}'",
            "F=''",
        ),
        {
            "A": "x # y",
            "B": "a b",
            "C": r"x\ny",
            "D": "$HOME",
            "E": "${UNSET_VARIABLE}",
            "F": "",
        },
    ),
    ComposeCase(
        "single-quoted-escapes",
        lines(r"A='it\'s'", r"B='a\\b'", r"C='a\\'"),
        {"A": "it's", "B": r"a\\b", "C": r"a\\"},
    ),
    ComposeCase(
        "single-quoted-tail",
        lines("A='x'y", "B='x' # comment", "C='x'#c"),
        {"A": "x", "B": "x", "C": "x"},
    ),
    ComposeCase(
        "single-quoted-multiline",
        "A='first\nsecond'\nB=1\n",
        {"A": "first\nsecond", "B": "1"},
    ),
    ComposeCase(
        "double-quoted-escapes",
        lines(
            r'A="a\nb"',
            r'B="t\tab"',
            r'C="q\"x"',
            r'D="b\\c"',
            r'E="\r"',
            r'F="é"',
            r'G="\$x"',
            r'H="\a\b\f\v"',
        ),
        {
            "A": "a\nb",
            "B": "t\tab",
            "C": 'q"x',
            "D": "b\\c",
            "E": "\r",
            "F": "é",
            "G": "$x",
            "H": "\a\b\f\v",
        },
    ),
    ComposeCase(
        "double-quoted-kept-backslashes",
        lines(
            r'A="\x41"',
            r'B="\q"',
            r'C="\'"',
            r'D="\/"',
            r'E="\e"',
            r'F="\U00000041"',
            r'G="\ "',
            r'H="\u00zz"',
        ),
        {
            "A": r"\x41",
            "B": r"\q",
            "C": r"\'",
            "D": r"\/",
            "E": r"\e",
            "F": r"\U00000041",
            "G": "\\ ",
            "H": r"\u00zz",
        },
    ),
    ComposeCase(
        "double-quoted-zero-escape",
        lines(r'A="\0"', r'B="\012"'),
        {"A": "\\", "B": r"\12"},
    ),
    ComposeCase(
        "double-quoted-tail",
        lines('A="x"y', 'B="x"#c', 'C="  padded  "', 'D=""', r'E="a\\"'),
        {"A": "x", "B": "x", "C": "  padded  ", "D": "", "E": "a\\"},
    ),
    ComposeCase(
        "double-quoted-multiline",
        'A="first\nsecond"\nB=1\n',
        {"A": "first\nsecond", "B": "1"},
    ),
    ComposeCase(
        "unquoted-comments",
        lines("A=x # c", "B=y#z", "C=#c", "D= #c", "E=x\t#c", "F=x  # c  "),
        {"A": "x", "B": "y#z", "C": "#c", "D": "#c", "E": "x\t#c", "F": "x"},
    ),
    ComposeCase(
        "unquoted-trim-and-literals",
        lines("A=  lead", "B=trail   ", 'C=ab"c d"e', r"D=a\nb", r"E=a\\b", "F==x", "G=1=2", "H="),
        {
            "A": "lead",
            "B": "trail",
            "C": 'ab"c d"e',
            "D": r"a\nb",
            "E": r"a\\b",
            "F": "=x",
            "G": "1=2",
            "H": "",
        },
    ),
    ComposeCase(
        "unquoted-duplicate-key",
        lines("A=1", "B=2", "A=3"),
        {"A": "3", "B": "2"},
    ),
    ComposeCase(
        "interpolation-of-earlier-keys",
        lines(
            "FIRST=zed",
            "A=${FIRST}x",
            "B=$FIRST",
            "C=${FIRST:-no}",
            "D=${UNSET_VARIABLE:-${FIRST}}",
            'E="$FIRST"',
        ),
        {"FIRST": "zed", "A": "zedx", "B": "zed", "C": "zed", "D": "zed", "E": "zed"},
        interpolated=("A", "B", "C", "D", "E"),
    ),
    ComposeCase(
        "interpolation-of-unset-variables",
        lines(
            "A=${UNSET_VARIABLE}",
            "B=${UNSET_VARIABLE:-fallback}",
            "C=${UNSET_VARIABLE-other}",
            "D=${UNSET_VARIABLE:+alt}",
            "E=$UNSET_VARIABLE",
            "F=${UNSET_VARIABLE:-}",
        ),
        {"A": "", "B": "fallback", "C": "other", "D": "", "E": "", "F": ""},
        interpolated=("A", "B", "C", "D", "E", "F"),
    ),
    ComposeCase(
        "dollar-signs-that-do-not-interpolate",
        lines("A=$$lit", "B=a$$b", "C=$", "D=$1", "E=price$5", r'F="\${UNSET_VARIABLE}"'),
        {
            "A": "$lit",
            "B": "a$b",
            "C": "$",
            "D": "$1",
            "E": "price$5",
            "F": "${UNSET_VARIABLE}",
        },
    ),
    ComposeCase(
        "bare-keys",
        lines("A=1", "BARE_KEY", "export EXPORTED=2", "export BARE_EXPORTED"),
        {"A": "1", "EXPORTED": "2"},
        bare=("BARE_KEY", "BARE_EXPORTED"),
    ),
    ComposeCase(
        "wide-key-names",
        lines("a.b=1", "a-b=2", "1ABC=3", "A[1]=4", "ÜBER=5", "lower=6", "_x=7", ".dot=8"),
        {
            "a.b": "1",
            "a-b": "2",
            "1ABC": "3",
            "A[1]": "4",
            "ÜBER": "5",
            "lower": "6",
            "_x": "7",
            ".dot": "8",
        },
    ),
    ComposeCase(
        "spaces-around-equals",
        lines("S1 = 1", "S2= 2", "S3 =3", "S4=  x y  ", 'S5 = "q v"', "\tS6\t=\t6"),
        {"S1": "1", "S2": "2", "S3": "3", "S4": "x y", "S5": "q v", "S6": "6"},
    ),
    ComposeCase(
        "export-prefix-and-layout",
        "# comment\n  # indented comment\n\nexport A=1\nexport  B=2\n  C=3\n\n\nD=4 # tail\n",
        {"A": "1", "B": "2", "C": "3", "D": "4"},
    ),
    ComposeCase(
        "crlf-line-endings",
        "A=x\r\nB=\"y\"\r\nC='z'\r\n# c\r\nD=1 # t\r\n",
        {"A": "x", "B": "y", "C": "z", "D": "1"},
    ),
)

REFUSED: tuple[ComposeCase, ...] = (
    ComposeCase("key-with-space", lines("A=1", "A B=2"), None),
    ComposeCase("key-with-slash", lines("A/B=1"), None),
    ComposeCase("key-with-at-sign", lines("A@B=1"), None),
    ComposeCase("key-with-plus", lines("A+B=1"), None),
    ComposeCase("key-with-dollar", lines("A$B=1"), None),
    ComposeCase("key-with-hash", lines("A#B=1"), None),
    ComposeCase("key-with-quotes", lines("'A'=1"), None),
    ComposeCase("uppercase-export-word", lines("EXPORT A=1"), None),
    ComposeCase("unterminated-double-quote", 'A="open\nB=2\n', None),
    ComposeCase("unterminated-single-quote", "A='open\nB=2\n", None),
    ComposeCase("reference-with-a-digit-first", lines("A=${1X}"), None),
    ComposeCase("reference-with-a-space", lines("A=${bad name}"), None),
    ComposeCase("reference-not-closed", lines("A=${UNSET_VARIABLE"), None),
    ComposeCase("reference-with-assignment", lines("A=${UNSET_VARIABLE:=x}"), None),
)

HOST_VARIABLES: tuple[str, ...] = ("UNSET_VARIABLE", "FIRST", "BARE_KEY", "BARE_EXPORTED")
