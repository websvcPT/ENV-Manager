"""Helpers of the Compose scan and import tests: a deployment that has no services, standing on a
real folder below the allowed path, and a project of Compose and host files written into it."""

from collections.abc import Mapping
from pathlib import Path

from app.models import Deployment
from app.services.docker_cli import DockerCli, DockerStatus
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


GEOTRAVEL_COMPOSE = """\
name: ${ENVIRONMENT_NAME:-geotravel}

services:
  db:
    image: mysql:8.0.41-debian
    hostname: db
    restart: always
    env_file:
      - ./environments/.env.db
    ports:
      - "${DB_PORT:-127.0.0.1:13381}:3306"
    networks:
      - app_network
    volumes:
      - ./data/mysql/data:/var/lib/mysql
      - ./backups:/var/backups
      - ./scripts:/scripts
    environment:
      TZ: ${TZ:-Europe/Lisbon}
    healthcheck:
      test: "mysql --user=$$MYSQL_USER --password=$$MYSQL_PASSWORD --execute='SHOW DATABASES;'"
      timeout: 10s
      interval: 10s
      retries: 20

  api:
    image: registry.example.test/geotravel/api:${IMAGE_API_VERSION:-latest}
    hostname: api
    restart: always
    networks:
      - app_network
    ports:
      - "${API_PORT:-127.0.0.1:8101}:80"
    depends_on:
      db:
        condition: service_healthy
    volumes:
      - ./environments/.env.api:/var/www/html/.env
    environment:
      RUN_MIGRATIONS: true
      TZ: ${TZ:-Europe/Lisbon}

  frontend:
    image: registry.example.test/geotravel/frontend:${IMAGE_FRONTEND_VERSION:-latest}
    hostname: frontend
    restart: always
    networks:
      - app_network
    ports:
      - "${FRONTEND_PORT:-127.0.0.1:8102}:80"
    env_file:
      - ./environments/.env.frontend
    environment:
      TZ: ${TZ:-Europe/Lisbon}

networks:
  app_network:
    name: ${DOCKER_NETWORK_NAME:-geotravel_network}
    driver: bridge
"""

GEOTRAVEL: dict[str, str] = {
    "docker-compose.yml": GEOTRAVEL_COMPOSE,
    ".env": "ENVIRONMENT_NAME=tests\nTZ=Europe/Lisbon\n",
    "environments/.env.db": "MYSQL_USER=app\nMYSQL_PASSWORD=secret\n",
    "environments/.env.db-dist": "MYSQL_USER=\nMYSQL_PASSWORD=\n",
    "environments/.env.frontend-dist": "API_URL=http://localhost:8101\n",
    "environments/.env.api-dist": "APP_KEY=\nDB_HOST=db\n",
    "backups/.keep": "",
    "scripts/init.sql": "SELECT 1;\n",
}
"""A project shaped like a real one: an env file on disk with its dist file, a required env file
that is not on disk yet but has a dist file, a mounted file that is not on disk yet, a missing
data folder, two real folders, and the deployment environment file beside the Compose file."""


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


class MountedCli(DockerCli):
    """The real docker command for Compose, reporting the socket, the daemon and the compose
    plugin as usable, so post-deploy commands may be stored."""

    async def status(self, *, fresh: bool = False) -> DockerStatus:
        """A usable Docker, whatever the socket path."""
        return DockerStatus(True, True, "2.40.0")


def mounted_cli(tmp_path: Path) -> DockerCli:
    """The real docker command, reported as usable for post-deploy commands."""
    return MountedCli(socket_path=tmp_path / "no-docker.sock", scratch=tmp_path / "scratch")


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
