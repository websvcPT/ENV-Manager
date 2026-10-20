"""The check of the files Compose files include and extend: what is accepted inside the deployment
root, and every way a target can leave it, name a remote source or hide behind a variable."""

from collections.abc import Mapping
from pathlib import Path

import pytest

from app.services.compose_includes import (
    MAX_DEPTH,
    MAX_REFERENCED_FILES,
    MAX_SHOWN_LENGTH,
    NOT_YAML_MESSAGE,
    TOO_LARGE_MESSAGE,
    TOO_MANY_MESSAGE,
    ComposeReferenceError,
    check_compose_references,
)
from app.services.paths import AllowedPaths

SERVICE = "services:\n  web:\n    image: x\n"


def stack(tmp_path: Path, files: Mapping[str, str]) -> Path:
    """Write the files below ``site/stack`` of the temporary folder and return the real root; the
    folder ``site`` is the allowed path, so ``..`` reaches a place that is allowed but not the
    deployment."""
    root = (tmp_path / "site" / "stack").resolve()
    root.mkdir(parents=True)
    for relative, text in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root


def check(root: Path, *names: str, max_bytes: int = 1024 * 1024) -> None:
    """Check the named files of a stack the way a scan does: the first is the base file."""
    files = [root / name for name in names]
    check_compose_references(
        AllowedPaths([root.parent]), root, files[0].parent, files, max_bytes=max_bytes
    )


def refused(root: Path, *names: str) -> ComposeReferenceError:
    """The error the check raises for the named files."""
    with pytest.raises(ComposeReferenceError) as caught:
        check(root, *names)
    return caught.value


def test_nested_includes_and_extends_inside_the_root_are_accepted(tmp_path: Path) -> None:
    root = stack(
        tmp_path,
        {
            "compose.yaml": (
                "include:\n"
                "  - parts/a.yaml\n"
                "  - path:\n      - parts/b.yaml\n      - parts/c.yaml\n"
                "    env_file: parts/include.env\n"
                "  - path: folder/d.yaml\n    project_directory: folder/inner\n"
                "services:\n"
                "  web:\n    extends:\n      file: shared/base.yaml\n      service: base\n"
                "  same:\n    extends: web\n"
            ),
            "parts/a.yaml": "include:\n  - deeper/e.yaml\n",
            "parts/deeper/e.yaml": SERVICE,
            "parts/b.yaml": SERVICE,
            "parts/c.yaml": SERVICE,
            "folder/d.yaml": "include:\n  - f.yaml\n",
            "folder/inner/f.yaml": SERVICE,
            "shared/base.yaml": (
                "services:\n  base:\n    extends:\n      file: nested/more.yaml\n      service: m\n"
            ),
            "shared/nested/more.yaml": "services:\n  m:\n    image: x\n",
        },
    )
    check(root, "compose.yaml")


def test_an_absolute_path_inside_the_root_is_accepted(tmp_path: Path) -> None:
    root = stack(tmp_path, {"other.yaml": SERVICE})
    (root / "compose.yaml").write_text(f"include:\n  - {root}/other.yaml\n", encoding="utf-8")
    check(root, "compose.yaml")


def test_a_missing_target_and_a_file_without_a_mapping_are_left_to_compose(
    tmp_path: Path,
) -> None:
    root = stack(
        tmp_path,
        {
            "compose.yaml": "include:\n  - gone.yaml\n  - list.yaml\n  - empty.yaml\n  - 7\n",
            "list.yaml": "- a\n- b\n",
            "empty.yaml": "",
        },
    )
    check(root, "compose.yaml")


def test_a_cycle_of_includes_ends(tmp_path: Path) -> None:
    root = stack(
        tmp_path,
        {
            "compose.yaml": "include:\n  - a.yaml\n",
            "a.yaml": "include:\n  - b.yaml\n",
            "b.yaml": "include:\n  - a.yaml\n  - compose.yaml\n",
        },
    )
    check(root, "compose.yaml")


def test_the_files_of_an_override_resolve_against_the_folder_of_the_base_file(
    tmp_path: Path,
) -> None:
    root = stack(
        tmp_path,
        {
            "compose.yaml": SERVICE,
            "sub/override.yaml": "include:\n  - ../elsewhere.yaml\n",
            "sub/fine.yaml": "include:\n  - parts/p.yaml\n",
            "parts/p.yaml": SERVICE,
        },
    )
    check(root, "compose.yaml", "sub/fine.yaml")
    error = refused(root, "compose.yaml", "sub/override.yaml")
    assert "outside the deployment folder" in str(error)


@pytest.mark.parametrize(
    ("text", "entry"),
    [
        ("include:\n  - ../outside.yaml\n", "../outside.yaml"),
        ("include:\n  - parts/../../outside.yaml\n", "parts/../../outside.yaml"),
        ("include:\n  - path: ../outside.yaml\n", "../outside.yaml"),
        ("include:\n  - path:\n      - ok.yaml\n      - ../outside.yaml\n", "../outside.yaml"),
        ("include:\n  - /etc/hosts\n", "/etc/hosts"),
        ("include:\n  - /etc/../etc/hosts\n", "/etc/../etc/hosts"),
        ("include:\n  - link.yaml\n", "link.yaml"),
        ("include:\n  - linked-folder/outside.yaml\n", "linked-folder/outside.yaml"),
        ("include:\n  - path: ok.yaml\n    env_file: ../outside.env\n", "../outside.env"),
        ("include:\n  - path: ok.yaml\n    env_file: [ok.env, /etc/passwd]\n", "/etc/passwd"),
        ("include:\n  - path: ok.yaml\n    project_directory: ..\n", ".."),
        ("include:\n  - path: ok.yaml\n    project_directory: /etc\n", "/etc"),
        (
            "services:\n  web:\n    extends:\n      file: ../outside.yaml\n      service: s\n",
            "../outside.yaml",
        ),
        (
            "services:\n  web:\n    extends:\n      file: /etc/hosts\n      service: s\n",
            "/etc/hosts",
        ),
        (
            "services:\n  web:\n    extends:\n      file: link.yaml\n      service: s\n",
            "link.yaml",
        ),
    ],
)
def test_a_target_outside_the_deployment_folder_is_refused_by_name(
    tmp_path: Path, text: str, entry: str
) -> None:
    root = stack(tmp_path, {"compose.yaml": text, "ok.yaml": SERVICE, "ok.env": "A=1\n"})
    outside = root.parent / "outside"
    outside.mkdir()
    (outside / "outside.yaml").write_text(SERVICE, encoding="utf-8")
    (root.parent / "outside.yaml").write_text(SERVICE, encoding="utf-8")
    (root / "link.yaml").symlink_to(outside / "outside.yaml")
    (root / "linked-folder").symlink_to(outside, target_is_directory=True)
    error = refused(root, "compose.yaml")
    assert f'"{entry}"' in str(error) and "outside the deployment folder" in str(error)
    assert not error.not_yaml


def test_a_path_outside_the_allowed_paths_is_refused_even_when_it_exists(tmp_path: Path) -> None:
    root = stack(tmp_path, {"compose.yaml": f"include:\n  - {tmp_path}/beside.yaml\n"})
    (tmp_path / "beside.yaml").write_text(SERVICE, encoding="utf-8")
    error = refused(root, "compose.yaml")
    assert "outside the deployment folder or the allowed paths" in str(error)


def test_a_target_outside_is_refused_in_a_file_that_is_included(tmp_path: Path) -> None:
    root = stack(
        tmp_path,
        {
            "compose.yaml": "include:\n  - parts/a.yaml\n",
            "parts/a.yaml": "include:\n  - b.yaml\n",
            "parts/b.yaml": "services:\n  s:\n    extends:\n      file: ../../escape.yaml\n",
        },
    )
    assert '"../../escape.yaml"' in str(refused(root, "compose.yaml"))


@pytest.mark.parametrize(
    "text",
    [
        "include:\n  - git@example.invalid:team/stack.git\n",
        "include:\n  - https://example.invalid/team/stack.git\n",
        "include:\n  - git://example.invalid/team/stack\n",
        "include:\n  - oci://example.invalid/team/stack:1\n",
        "include:\n  - path: git@example.invalid:a/b.git\n    env_file: x.env\n",
        "include: [https://example.invalid/a.git]\n",
        "'include':\n  - path:\n      - ./local.yaml\n      - git://example.invalid/a\n",
        '{"include": ["oci://example.invalid/a:1"], "services": {}}',
        "include:\n  # a note\n  - 'git@example.invalid:a/b.git'  # remote\n",
        "include:\n  - &remote oci://example.invalid/a:1\n",
        "services:\n  web:\n    extends:\n      file: https://example.invalid/a.git\n",
    ],
)
def test_every_written_form_of_a_remote_source_is_refused(tmp_path: Path, text: str) -> None:
    root = stack(tmp_path, {"compose.yaml": text, "local.yaml": SERVICE})
    error = refused(root, "compose.yaml")
    assert "remote source" in str(error) and "example.invalid" in str(error)


def test_a_remote_source_inside_an_included_file_is_refused(tmp_path: Path) -> None:
    root = stack(
        tmp_path,
        {
            "compose.yaml": "include:\n  - parts/part.yaml\n",
            "parts/part.yaml": "include:\n  - git://example.invalid/team/stack.git\n",
        },
    )
    assert "remote source" in str(refused(root, "compose.yaml"))


@pytest.mark.parametrize(
    "text",
    [
        "include:\n  - ${PARTS}/a.yaml\n",
        "include:\n  - $PARTS/a.yaml\n",
        "include:\n  - path: ${PARTS:-parts}/a.yaml\n",
        "include:\n  - path: a.yaml\n    env_file: ${ENV_FILE}\n",
        "include:\n  - path: a.yaml\n    project_directory: $DIR\n",
        "services:\n  web:\n    extends:\n      file: ${BASE}.yaml\n      service: s\n",
        "include:\n  - parts/$$a.yaml\n",
    ],
)
def test_a_path_with_a_variable_is_refused_because_compose_expands_it(
    tmp_path: Path, text: str
) -> None:
    root = stack(tmp_path, {"compose.yaml": text, "a.yaml": SERVICE})
    error = refused(root, "compose.yaml")
    assert "uses a variable" in str(error) and "Write the path out" in str(error)


def test_a_tilde_is_a_plain_name_that_compose_does_not_expand(tmp_path: Path) -> None:
    root = stack(tmp_path, {"compose.yaml": "include:\n  - ~/a.yaml\n"})
    check(root, "compose.yaml")


def test_a_file_that_is_not_yaml_or_is_too_large_is_refused(tmp_path: Path) -> None:
    root = stack(
        tmp_path,
        {
            "compose.yaml": "include:\n  - broken.yaml\n",
            "broken.yaml": "services:\n  web:\n    image: [unclosed secret-value-123\n",
            "big.yaml": "# " + "x" * 200 + "\n",
        },
    )
    error = refused(root, "compose.yaml")
    assert error.not_yaml and str(error) == NOT_YAML_MESSAGE
    assert "secret-value-123" not in str(error)
    root.joinpath("compose.yaml").write_text("include:\n  - big.yaml\n", encoding="utf-8")
    with pytest.raises(ComposeReferenceError) as large:
        check(root, "compose.yaml", max_bytes=100)
    assert str(large.value) == TOO_LARGE_MESSAGE and not large.value.not_yaml


def test_a_chain_of_includes_deeper_than_the_limit_is_refused(tmp_path: Path) -> None:
    chain = {
        f"level{number}.yaml": f"include:\n  - level{number + 1}.yaml\n"
        for number in range(MAX_DEPTH + 10)
    }
    root = stack(tmp_path, {"compose.yaml": "include:\n  - level0.yaml\n", **chain})
    assert str(refused(root, "compose.yaml")) == TOO_MANY_MESSAGE
    short = {f"s{number}.yaml": f"include:\n  - s{number + 1}.yaml\n" for number in range(3)}
    shallow = stack(tmp_path / "short", {"compose.yaml": "include:\n  - s0.yaml\n", **short})
    check(shallow, "compose.yaml")


def test_more_files_than_the_limit_are_refused(tmp_path: Path) -> None:
    names = [f"part{number}.yaml" for number in range(MAX_REFERENCED_FILES + 5)]
    root = stack(
        tmp_path,
        {
            "compose.yaml": "include:\n" + "".join(f"  - {name}\n" for name in names),
            **dict.fromkeys(names, SERVICE),
        },
    )
    assert str(refused(root, "compose.yaml")) == TOO_MANY_MESSAGE


def test_the_entry_is_shown_shortened_and_without_control_characters(tmp_path: Path) -> None:
    long_entry = "../" + "a" * 300 + ".yaml"
    root = stack(tmp_path, {"compose.yaml": f'include:\n  - "{long_entry}"\n'})
    message = str(refused(root, "compose.yaml"))
    assert "a" * (MAX_SHOWN_LENGTH + 1) not in message
    assert "..." in message and len(message) < 300
    odd = stack(tmp_path / "odd", {"compose.yaml": 'include:\n  - "../x\\ny\\u202e.yaml"\n'})
    shown = str(refused(odd, "compose.yaml"))
    assert "\n" not in shown and "\u202e" not in shown
