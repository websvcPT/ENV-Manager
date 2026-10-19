"""The env parser against the real ``docker compose config``.

Every case of the committed Compose fixtures is written as an ``env_file`` and run through the
real Compose, on an environment without the variables the files name. What Compose prints must be
the table the unit tests pin, and the parser must read the same keys and values (a template that
interpolates and a bare key are flagged instead of valued).
"""

import json
import os
import subprocess
from pathlib import Path

from app.services.env_parser import parse_env
from app.tests.compose_env_cases import CASES, HOST_VARIABLES, REFUSED, ComposeCase


def clean_environment() -> dict[str, str]:
    """The process environment without any variable the fixture files name."""
    return {name: value for name, value in os.environ.items() if name not in HOST_VARIABLES}


def run_compose(
    folder: Path, environment: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """``docker compose config`` as JSON for the compose file in ``folder``."""
    return subprocess.run(  # noqa: S603
        [  # noqa: S607
            "docker",
            "compose",
            "--project-directory",
            str(folder),
            "config",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        env=environment or clean_environment(),
        timeout=60,
        check=False,
    )


def write_cases(folder: Path, cases: tuple[ComposeCase, ...]) -> None:
    """One service per case, each with its own env file."""
    services = []
    for case in cases:
        # Bytes, so a CRLF fixture reaches Compose as written.
        (folder / f"{case.name}.env").write_bytes(case.text.encode())
        services.append(f"  {case.name}:\n    image: scratch\n    env_file: {case.name}.env\n")
    (folder / "compose.yaml").write_text("services:\n" + "".join(services))


def printed_environments(folder: Path) -> dict[str, dict[str, str]]:
    """The environment of every service as Compose printed it, dollar signs unescaped."""
    result = run_compose(folder)
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    return {
        name: {
            key: value.replace("$$", "$")
            for key, value in (service.get("environment") or {}).items()
        }
        for name, service in services.items()
    }


def test_compose_prints_what_the_committed_table_says(tmp_path: Path) -> None:
    write_cases(tmp_path, CASES)
    printed = printed_environments(tmp_path)
    for case in CASES:
        assert printed[case.name] == case.compose, case.name


def test_the_parser_reads_every_file_as_compose_prints_it(tmp_path: Path) -> None:
    write_cases(tmp_path, CASES)
    printed = printed_environments(tmp_path)
    for case in CASES:
        parsed = parse_env(case.text)
        environment = printed[case.name]
        assert parsed.is_env_format and not parsed.problem_lines, case.name
        assert set(parsed.keys) == set(environment) | set(case.bare), case.name
        for key, value in environment.items():
            variable = parsed.variables[key]
            if key in case.interpolated:
                assert variable.interpolated and not variable.empty, (case.name, key)
            else:
                assert variable.value == value, (case.name, key)
        for key in case.bare:
            assert not parsed.variables[key].assigned, (case.name, key)


def test_every_file_compose_refuses_has_a_problem_line_in_the_parser(tmp_path: Path) -> None:
    for case in REFUSED:
        folder = tmp_path / case.name
        folder.mkdir()
        write_cases(folder, (case,))
        assert run_compose(folder).returncode != 0, case.name
        assert parse_env(case.text).problem_lines, case.name


def test_a_bare_key_takes_the_host_value_in_compose(tmp_path: Path) -> None:
    folder = tmp_path / "bare"
    folder.mkdir()
    bare_case = next(case for case in CASES if case.name == "bare-keys")
    write_cases(folder, (bare_case,))
    result = run_compose(folder, {**clean_environment(), "BARE_KEY": "from-host"})
    assert result.returncode == 0, result.stderr
    printed = json.loads(result.stdout)["services"]["bare-keys"]["environment"]
    assert printed["BARE_KEY"] == "from-host"
    assert parse_env(bare_case.text).variables["BARE_KEY"].assigned is False
