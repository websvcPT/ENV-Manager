"""A throwaway local Docker registry for the real scenarios and the Docker browser specs.

The pinned ``registry`` image runs with basic authentication on a port of 127.0.0.1 that Docker
picks, so the host daemon, which treats 127.0.0.0/8 as insecure, talks plain http to it and
nothing else on the network can reach it. The only login is the fixed development and testing
one below; its bcrypt entry is committed in ``demo/registry/htpasswd``. The image, the container
and its volume exist only while a run uses them: ``python -m app.real_scenarios.registry start``
starts a registry for the browser specs and prints ``<host:port> <username> <password>``, and the
Makefile removes every container with ``LABEL`` when the run is over.

Every docker command here uses a private, empty ``DOCKER_CONFIG`` (or one that holds only the
test login while an image is pushed and is removed right after), never the config folder of the
user that runs it.
"""

import os
import secrets
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

IMAGE = "registry:3.1.2"
LABEL = "env-manager.test-registry"
USERNAME = "demo-puller"
# Development and testing only: the password whose bcrypt entry is in demo/registry/htpasswd.
PASSWORD = "dev-only-registry-password"
HTPASSWD = Path(__file__).resolve().parents[2] / "demo" / "registry" / "htpasswd"

CONTAINER_HTPASSWD = "/htpasswd"
CONTAINER_PORT = "5000"
INTERNAL_PORT = f"{CONTAINER_PORT}/tcp"
LISTENING_TEXT = "listening on"
START_TIMEOUT_SECONDS = 30.0
IMAGE_PULL_TIMEOUT_SECONDS = 300.0
COMMAND_TIMEOUT_SECONDS = 120.0
NO_SUCH_CONTAINER = "No such container"
NO_SUCH_IMAGE = "No such image"


class RegistryError(Exception):
    """A docker command of the test registry failed. The message holds its output."""


def docker(
    *arguments: str,
    config: Path | None = None,
    stdin: str | None = None,
    timeout: float = COMMAND_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Run the docker command with a private config folder: ``config``, or an empty one."""
    with tempfile.TemporaryDirectory(prefix="registry-docker-") as empty:
        return subprocess.run(  # noqa: S603
            ["docker", *arguments],  # noqa: S607
            env={**os.environ, "DOCKER_CONFIG": str(config or empty)},
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )


def checked(result: subprocess.CompletedProcess[str]) -> str:
    """The standard output of a command that succeeded, else a ``RegistryError`` with its output."""
    if result.returncode != 0:
        raise RegistryError(f"{' '.join(result.args[1:3])}: {result.stdout}{result.stderr}")
    return result.stdout


def local_image_present(reference: str) -> bool:
    """Whether the host daemon holds an image under this name."""
    return docker("image", "inspect", reference).returncode == 0


def remove_local_image(reference: str) -> None:
    """Remove a name (and the image when it was the last one) from the host daemon, if present."""
    result = docker("rmi", reference)
    if result.returncode != 0 and NO_SUCH_IMAGE not in result.stderr:
        raise RegistryError(f"rmi: {result.stdout}{result.stderr}")


@dataclass(frozen=True)
class LocalRegistry:
    """A running test registry: its container and the host and port the daemon reaches it at."""

    name: str
    host: str
    username: str = USERNAME
    password: str = PASSWORD

    def remove(self) -> None:
        """Remove the container and its anonymous volume, running or not."""
        result = docker("rm", "--force", "--volumes", self.name)
        if result.returncode != 0 and NO_SUCH_CONTAINER not in result.stderr:
            raise RegistryError(f"rm: {result.stdout}{result.stderr}")

    def push(self, source: str, repository: str, tag: str) -> str:
        """Push the image the host holds as ``source`` to this registry and give its reference.

        The image is retagged ``<host>/<repository>:<tag>``, pushed with the test login (a
        private config folder that exists only for the push) and the local tag removed again, so
        the next pull of the reference has to go to the registry.
        """
        reference = f"{self.host}/{repository}:{tag}"
        checked(docker("pull", "--quiet", source, timeout=IMAGE_PULL_TIMEOUT_SECONDS))
        with tempfile.TemporaryDirectory(prefix="registry-login-") as folder:
            config = Path(folder)
            checked(
                docker(
                    "login",
                    "--username",
                    self.username,
                    "--password-stdin",
                    self.host,
                    config=config,
                    stdin=self.password,
                )
            )
            checked(docker("tag", source, reference))
            try:
                checked(
                    docker("push", reference, config=config, timeout=IMAGE_PULL_TIMEOUT_SECONDS)
                )
            finally:
                remove_local_image(reference)
        return reference


def _wait_until_listening(name: str) -> None:
    """Wait for the registry to say it listens, else raise with what it logged."""
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    logs = ""
    while time.monotonic() < deadline:
        result = docker("logs", name)
        logs = result.stdout + result.stderr
        if LISTENING_TEXT in logs:
            return
        time.sleep(0.2)
    raise RegistryError(f"the registry never listened: {logs[-2000:]}")


def start_registry() -> LocalRegistry:
    """Start a registry with the committed login on a free port of 127.0.0.1 and wait for it.

    The container is labelled with ``LABEL``. The password file is copied into the created
    container instead of mounted, so this works from inside the app container, where a mount
    path would be read on the host.
    """
    name = f"env-manager-test-registry-{secrets.token_hex(4)}"
    checked(
        docker(
            "create",
            "--name",
            name,
            "--label",
            f"{LABEL}=1",
            "--publish",
            f"127.0.0.1::{CONTAINER_PORT}",
            "--env",
            "REGISTRY_AUTH=htpasswd",
            "--env",
            "REGISTRY_AUTH_HTPASSWD_REALM=ENV Manager test registry",
            "--env",
            f"REGISTRY_AUTH_HTPASSWD_PATH={CONTAINER_HTPASSWD}",
            IMAGE,
            timeout=IMAGE_PULL_TIMEOUT_SECONDS,
        )
    )
    try:
        checked(docker("cp", str(HTPASSWD), f"{name}:{CONTAINER_HTPASSWD}"))
        checked(docker("start", name))
        published = checked(docker("port", name, INTERNAL_PORT)).splitlines()
        host = next(line for line in published if line.startswith("127.0.0.1:"))
        _wait_until_listening(name)
    except BaseException:
        LocalRegistry(name, "").remove()
        raise
    return LocalRegistry(name, host)


def main(arguments: list[str]) -> int:
    """``start``: start a registry and print its host, username and password for the Makefile."""
    if arguments != ["start"]:
        sys.stderr.write("usage: python -m app.real_scenarios.registry start\n")
        return 2
    registry = start_registry()
    sys.stdout.write(f"{registry.host} {registry.username} {registry.password}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
