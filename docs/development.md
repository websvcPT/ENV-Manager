# Development

Run everything through `make` (see `make help`); nothing runs on the host directly. Application commands, linting and tests all run in containers.

## The development stack

`make up-bg` starts the stack in the background and waits until it is ready: the `app` service
(port 8080, the working tree bind-mounted, running as your host user id) and `smtp4dev`, a mail
catcher whose web interface is at <http://localhost:5080>. `make seed` loads the development
dataset into the running app (`envmanager dev seed`); the seeded users have the address
`<name>@example.test`, so the superadmin signs in at <http://localhost:8080> as
`root@example.test`. The fixed development password is defined in `app/seeds/dataset.py` and is
for development and testing only.

| Target | What it does |
| --- | --- |
| `make up`, `make up-app`, `make up-bg`, `make upforce` | Start the environment (all services, only the app, in the background and wait, or force build and recreate). |
| `make stop`, `make destroy` | Stop the environment, or destroy it and clean up. |
| `make logs`, `make app-logs`, `make app` | Follow the logs, follow the app logs, or open a shell in the app container. |
| `make format`, `make lint` | Auto-format and fix, or lint and type-check. |
| `make tests ARGS="app/tests/test_x.py -k name"` | Run the tests inside the running app container, optionally narrowed. |
| `make tests-docker` | Run the whole test suite in an isolated container built from the `test` stage of the `Dockerfile`. |
| `make e2e ARGS="--project=desktop tests/users.spec.ts"` | Seed the development dataset and run the Playwright specs. |
| `make seed`, `make dev-reset`, `make dev-cleanup` | Load the dataset, replace the development database with a fresh seeded one (the old one stays as `*.before-reset`), or remove the scratch users and projects the browser specs left behind. |
| `make migrate`, `make revision` | Run migrations up, or print the command that creates a new revision. |
| `make tailwind-build`, `make tailwind-watch`, `make frontend-build` | Build the CSS once (minified, in a container), watch it, or build the whole bundle. |
| `make static-fetch`, `make check-pins` | Re-download the pinned vendor assets, or fail when a tool version written in more than one file differs. |
| `make demo-up`, `make demo-reset`, `make demo-down`, `make e2e-demo` | The demo deployment, described below. |

## Tests

`make tests` runs the pytest suite in the app container; narrow it with
`ARGS="<test files>"`. `make tests-docker` runs everything in a clean container with no running
stack, which is what a CI run does. Tests stand alone: they use temporary files and databases,
fake only the boundaries (filesystem, clock, Docker CLI, git, SMTP) and reuse the seed dataset
in `app/seeds`. Browser specs live under `playwright/tests` and run with `make e2e`. Flows that
write host files also have scenarios under `app/real_scenarios`, run by `make e2e-demo` against
the demo stack.

## Demo deployment

`demo/` holds a small deployment that behaves like a real one managed by the app, for trying
the deploy flow and for real-scenario tests. It is opt in: the default development stack never
mounts the Docker socket.

- `demo/stack/compose.yaml` runs two workloads (`api` and `db`) on a pinned `alpine` image.
  They load their files with `env_file` from `environments/`, only sleep, and print the names
  (never the values) of the `DEMO_` variables they received.
- `demo/stack/environments/` holds the committed dist templates (`.env.*-dist`; the certificate
  has none, as a certificate has no dist file). The real target files next to them are written by the app or restored by `make demo-reset`, and are ignored by
  git, as in a real deployment.
- `demo/baseline/environments/` is the known target state `make demo-reset` restores
  (files written with mode 0600): `.env.api` drifted from its managed version, `.env.db` and
  `.env.db.secrets` identical to theirs, `demo-ca.pem` missing. The dist template of
  `.env.db.secrets` has a key its managed version lacks, so a full deploy of `db` is blocked
  while a deploy limited to `.env.db` is not.
- The seed (`envmanager dev seed` and the test dataset) describes the same deployment in the
  database when the demo root is given: project `demo`, deployment `stack` rooted at
  `demo/stack` (added to the allowed paths), services `api` and `db` with the post-deploy
  commands `docker compose up -d --no-deps --force-recreate api` and `... db` (each names its own
  service), and grants for `project_admin` (the project) and `env_scoped_admin` (only
  `.env.db`). Nothing else changes for the other seeded users. `make e2e` seeds these rows too;
  without the demo override the app container cannot reach the folder, which only matters for
  deploys.

| Target | What it does |
| --- | --- |
| `make demo-up` | Restores the baseline when no target file exists yet, restarts the app container with `docker-compose-demo.yml` (the Docker socket, the host docker group id in `group_add`, and `demo/` mounted at the same absolute path as on the host), waits for it and seeds the demo project. |
| `make demo-reset` | Restores the demo target files to the baseline. Database state (versions, deploy results) is not touched. |
| `make demo-down` | Stops the demo workloads (Compose project `env-manager-demo`) and recreates the app container from the default stack, without the socket. |
| `make e2e-demo` | Runs `make demo-up` and `make demo-reset`, then the real-scenario tests in `app/real_scenarios` inside the app container, then the browser specs in `playwright/docker-tests` (the post-deploy settings against the real daemon), then `make demo-reset` and `make demo-down`, whether or not they pass. |

The real-scenario tests are outside the default test run. Each one seeds a throwaway database,
runs the real `envmanager deploy` command against the demo stack on the mounted host folder and
checks the files it left (mode, owner, content, what was not touched).

Because `demo/` sits at the same absolute path in the container and on the host, a
`docker compose` command run by the app (a post-deploy command works in the deployment root)
resolves the project directory, env files and labels exactly as it does on the host:

```sh
make demo-up
docker compose exec app docker compose -f "$PWD/demo/stack/compose.yaml" config --quiet
docker compose exec app docker compose -f "$PWD/demo/stack/compose.yaml" up -d
docker ps --filter label=com.docker.compose.project=env-manager-demo
make demo-down
```

The Docker socket is root-equivalent on the host; keep the demo override for local runs only.

## Continuous integration

`.github/workflows/checks.yml` runs on every pull request and as the first job of the release
workflow: `make check-pins`, the lint and type checks in the test image
(`scripts/lint.sh` through `docker-compose-testing.yml`) and the full pytest suite
(`make tests-docker`). Browser specs and the demo scenarios are not run in CI.

## Releases

Versions are computed by [python-semantic-release](https://python-semantic-release.readthedocs.io/)
from the commit messages, which follow [Conventional Commits](https://www.conventionalcommits.org/):
`<type>(<optional scope>): <summary>`.

| Commit | Release |
| --- | --- |
| `feat` | minor (1.2.0 to 1.3.0) |
| `fix`, `perf` | patch (1.2.0 to 1.2.1) |
| any type with `!` after it (`feat!: ...`) or a `BREAKING CHANGE:` footer | major (1.2.0 to 2.0.0) |
| `refactor`, `docs`, `test`, `build`, `ci`, `chore` | no release |

A push to `main` or `master` (the local branch is `master`, either name works) runs
`.github/workflows/release.yml`: the checks, then the release job, then the image job when a
version was released. The release job updates the version in `pyproject.toml` and in
`app/__init__.py` (the number the sidebar shows), refreshes the root package version in
`uv.lock` (`uv sync --locked` refuses a lock file whose root package version differs), writes `CHANGELOG.md`, pushes the release commit and the `v<version>` tag and creates the
GitHub release. The image job builds the Dockerfile's `runtime` target for `linux/amd64` from that
tag and pushes `websvcpt/env-manager` with the tags `<version>`, `<major>.<minor>`, `<major>` and
`latest`. The release commit is pushed with the workflow token, and a push made with that token
does not start another workflow run.

The first release is 1.0.0: zero versions are disabled, so while the repository has no release
tag, python-semantic-release makes 1.0.0 whatever the commit types are. Either the first push to
the release branch (after the checks pass) or a manual run of the release workflow (Actions,
Release, Run workflow) with `force` set to `major` cuts it; whichever comes first creates `v1.0.0`,
and a second `major` run after that would make 2.0.0. Later, the same `force` input
(`patch`, `minor` or `major`) makes an exceptional bump; leave it at `none` otherwise.

Setup on GitHub and Docker Hub:

- Repository secrets `DOCKERHUB_USERNAME` (the Docker Hub account) and `DOCKERHUB_TOKEN` (an
  access token of that account with read and write permission on `websvcpt/env-manager`).
- Settings, Actions, General, Workflow permissions: allow the workflows to run. The release job
  asks for `contents: write` itself.
- If the release branch is protected, the workflow token must be allowed to push the release
  commit and the tag directly (a ruleset bypass for GitHub Actions), or the workflow must use a
  token that can: a personal access token or an app token stored as a secret, passed to
  `actions/checkout` as `token` and to the release step as `github_token`. A required pull
  request or status check rule without a bypass makes the release job fail at the push.
- The Docker Hub repository `websvcpt/env-manager` must exist or the account must be allowed to
  create it on push.
