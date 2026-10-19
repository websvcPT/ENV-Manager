"""The written code style rules that a slip would break without anything noticing.

No em-dash, no emoji, and no reference to the specification, a phase, a milestone or a numbered
item in code, templates, scripts or the demo stack: such a reference means nothing once the plan
that numbered it changes. The scan reads the folders the test image holds (``app``, ``scripts``,
``demo``), this file included, so every pattern and fixture below is spelled with character names
or split text and never matches itself.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCANNED_FOLDERS = ("app", "scripts", "demo")
SCANNED_SUFFIXES = frozenset(
    {".py", ".html", ".js", ".css", ".sh", ".svg", ".yaml", ".txt", ".mako"}
)
SCANNED_ENDINGS = ("-dist",)
VENDORED_ENDINGS = (".min.js",)
GENERATED_FILES = frozenset({"app/static/style.css"})

EM_DASH = "\N{EM DASH}"
SECTION_SIGN = "\N{SECTION SIGN}"
# Pictographs, miscellaneous symbols and dingbats, miscellaneous symbols and arrows, and the
# selector that asks for the emoji form of a character. Letters of any script and the typographic
# marks of ordinary text (bullet, middle dot, ellipsis) are outside these ranges.
EMOJI = re.compile(
    f"[{chr(0x1F000)}-{chr(0x1FAFF)}{chr(0x2600)}-{chr(0x27BF)}{chr(0x2B00)}-{chr(0x2BFF)}"
    f"{chr(0xFE0F)}]"
)
REFERENCE_RULES = {
    "section sign": re.compile(SECTION_SIGN),
    "double S reference": re.compile(r"\bSS ?\d"),
    "phase number": re.compile(r"\b[Pp]hase[ -]\d"),
    "milestone number": re.compile(r"\b[Mm]ilestone[ -]\d"),
    "section number": re.compile(r"\b[Ss]ection \d"),
    "item number": re.compile(r"\b[Ii]tem \d+[a-c]?\b"),
    "planning document": re.compile(r"\b(?:SPEC|PROGRESS)\.md\b|Requirements\s+Specification"),
}


def line_findings(line: str) -> list[str]:
    """The rules one line of text breaks, by name."""
    found = []
    if EM_DASH in line:
        found.append("em-dash")
    if EMOJI.search(line):
        found.append("emoji")
    found.extend(name for name, pattern in REFERENCE_RULES.items() if pattern.search(line))
    return found


def is_scanned(path: Path, root: Path) -> bool:
    """Whether a file is code, a template, a script or a demo text file of ours."""
    name = path.name
    if "__pycache__" in path.parts or name.endswith(VENDORED_ENDINGS):
        return False
    if path.relative_to(root).as_posix() in GENERATED_FILES:
        return False
    return path.suffix in SCANNED_SUFFIXES or name.endswith(SCANNED_ENDINGS)


def scan(root: Path) -> list[str]:
    """``path:line: rule`` for every slip in the scanned folders below ``root``."""
    findings: list[str] = []
    for folder in SCANNED_FOLDERS:
        for path in sorted((root / folder).rglob("*")):
            if not path.is_file() or not is_scanned(path, root):
                continue
            text = path.read_text(encoding="utf-8")
            for number, line in enumerate(text.splitlines(), start=1):
                findings.extend(
                    f"{path.relative_to(root).as_posix()}:{number}: {rule}"
                    for rule in line_findings(line)
                )
    return findings


def test_the_code_holds_no_em_dash_emoji_or_spec_reference() -> None:
    """A slip in the application, its scripts or the demo stack fails the run."""
    assert scan(REPO_ROOT) == []


def test_every_rule_catches_its_slip_and_names_it() -> None:
    """One slip per rule, each found with its own name and no other."""
    slips = {
        "em-dash": f"wait {EM_DASH} then go",
        "emoji": "done \N{ROCKET}",
        "section sign": f"see {SECTION_SIGN} 14 of the plan",
        "double S reference": "rules in " + "SS" + "3.2",
        "phase number": "built in " + "phase" + " 5",
        "milestone number": "after " + "Milestone" + "-2",
        "section number": "as " + "section" + " 5 says",
        "item number": "item " + "32b",
        "planning document": "listed in " + "SPEC" + ".md",
    }
    for rule, line in slips.items():
        assert line_findings(line) == [rule], line
    assert line_findings("Requirements" + " Specification of the app") == ["planning document"]
    assert line_findings("check \N{WHITE HEAVY CHECK MARK} and \N{UPWARDS BLACK ARROW}") == [
        "emoji"
    ]


def test_ordinary_wording_is_not_a_slip() -> None:
    """Plain words that merely contain or resemble a reference stay clean."""
    clean = [
        "runs in the capture phase so the form's own handler reads the choice",
        "this section lists the items of the phase in use",
        "ACCESS 2 and LASS 3 are not references",
        "emphasis, phased rollout, sectional view, itemized list",
        "the Specification field and a progress bar",
        "an item of the list, and the items shown",
        "bullet \N{BULLET}, middle dot \N{MIDDLE DOT}, ellipsis \N{HORIZONTAL ELLIPSIS}",
        "caf\N{LATIN SMALL LETTER E WITH ACUTE}, \N{ARABIC-INDIC DIGIT THREE}, "
        "\N{CJK UNIFIED IDEOGRAPH-540D}\N{CJK UNIFIED IDEOGRAPH-524D}",
    ]
    for line in clean:
        assert line_findings(line) == [], line


def test_the_scan_reads_our_text_files_and_skips_vendored_generated_and_binary_ones(
    tmp_path: Path,
) -> None:
    """Only code, templates, scripts and demo text count; a folder outside the three is ignored."""
    slip = f"x {EM_DASH} y\n"
    (tmp_path / "app" / "static" / "htmx").mkdir(parents=True)
    (tmp_path / "app" / "__pycache__").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "demo" / "stack").mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    (tmp_path / "app" / "page.html").write_text(f"<p>ok</p>\n<p>{slip}</p>\n", encoding="utf-8")
    (tmp_path / "scripts" / "run.sh").write_text(slip, encoding="utf-8")
    (tmp_path / "demo" / "stack" / "app.env-dist").write_text(slip, encoding="utf-8")
    (tmp_path / "app" / "static" / "htmx" / "htmx.min.js").write_text(slip, encoding="utf-8")
    (tmp_path / "app" / "static" / "style.css").write_text(slip, encoding="utf-8")
    (tmp_path / "app" / "__pycache__" / "mod.py").write_text(slip, encoding="utf-8")
    (tmp_path / "app" / "logo.png").write_bytes(EM_DASH.encode())
    (tmp_path / "docs" / "notes.md").write_text(slip, encoding="utf-8")
    assert scan(tmp_path) == [
        "app/page.html:2: em-dash",
        "scripts/run.sh:1: em-dash",
        "demo/stack/app.env-dist:1: em-dash",
    ]
