"""Helpers of the Compose scan and import tests: a deployment that has no services, standing on a
real folder below the allowed path, and a project of Compose and host files written into it."""

from collections.abc import Mapping
from pathlib import Path

from app.models import Deployment
from app.services.docker_cli import DockerCli
from app.tests.deploy_helpers import Harness

PEM = (
    "-----BEGIN PRIVATE KEY-----\n"
    "MIIBVgIBADANBgkqhkiG9w0BAQEFAASCAUAwggE8AgEAAkEAu3cT\n"
    "-----END PRIVATE KEY-----\n"
)

COMPOSE = """\
services:
  web:
    image: nginx
    env_file:
      - .env.web
      - path: .env.optional
        required: false
    volumes:
      - ./conf/nginx.conf:/etc/nginx/nginx.conf:ro
      - ./data:/data
      - named:/named
      - /var/run/docker.sock:/var/run/docker.sock
      - ./missing.conf:/missing.conf
    secrets: [server_key]
    configs: [app]
  api:
    image: api
    env_file:
      - .env.shared
      - .env.api
      - ../outside.env
  Worker_1:
    image: worker
    env_file: .env.shared
    profiles: [jobs]
  worker-1:
    image: worker
secrets:
  server_key:
    file: ./certs/server.key
configs:
  app:
    file: ./conf/app.cfg
volumes:
  named: {}
"""

PROJECT: dict[str, str] = {
    "compose.yaml": COMPOSE,
    ".env": "PROJECT_VARIABLE=1\n",
    ".env.web": "WEB=1\n",
    ".env.web.example": "WEB=\n",
    ".env.api": "API=1\n",
    ".env.api-dist": "API=\n",
    ".env.shared": "SHARED=1\n",
    "conf/nginx.conf": "worker_processes 1;\n",
    "conf/app.cfg": "mode=prod\n",
    "certs/server.key": PEM,
    "data/keep.txt": "kept\n",
}


def write_project(root: Path, files: Mapping[str, str | bytes]) -> None:
    """Write the files of a project below ``root``, creating the folders; each gets mode 0644."""
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")
        target.chmod(0o644)


def real_cli(tmp_path: Path) -> DockerCli:
    """The real docker command on a socket path that does not exist, as in the development stack."""
    return DockerCli(socket_path=tmp_path / "no-docker.sock", scratch=tmp_path / "scratch")


async def add_stack(
    h: Harness, files: Mapping[str, str | bytes] | None = None, *, name: str = "imported"
) -> tuple[Deployment, Path]:
    """A deployment of the fancy project with no services, rooted in a new folder of the allowed
    path that holds ``files`` (the whole project by default). Returns it and the real root."""
    root = h.roots["prod"].parent / name
    root.mkdir()
    write_project(root, PROJECT if files is None else files)
    deployment = Deployment(project_id=h.seed.projects.fancy.id, name=name, root_path=str(root))
    h.db.add(deployment)
    await h.db.commit()
    return deployment, root
